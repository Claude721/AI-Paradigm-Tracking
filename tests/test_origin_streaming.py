"""Real SQLite + production streaming analyzer, with offline model substitutes."""
from __future__ import annotations

import asyncio
import copy
import sqlite3
import tempfile
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import config
from agents.paradigm_orchestrator import (
    ParadigmOrchestrator,
    _deep_execution_order,
    _refresh_reserve_seconds,
)
from database.paradigm_store import ParadigmStore, RouteHistoryIndex, _route_similarity
from database.state_migration import migrate_state
from paradigms.analyzer import ParadigmAnalyzer
from paradigms.clustering import cluster_extractions
from paradigms.models import (
    EvidenceType, ParadigmCandidate, ParadigmExtraction, TechnicalEvidence,
)


def origin(name):
    return TechnicalEvidence(source="arxiv", evidence_type=EvidenceType.PRIMARY_PAPER,
                             title=name, url=f"https://arxiv.org/abs/{name}")


def extraction(item, name=None, decision="deep_dive"):
    return ParadigmExtraction(
        evidence=item, is_candidate=decision == "deep_dive", canonical_name=name or item.title,
        thesis="改变信息更新方式", problem_shift="静态记忆变成可更新记忆",
        mechanism="按反馈更新记忆", route_family="adaptive episodic memory",
        keywords=["episodic", "memory", "feedback"],
        rubric_assessment={"decision": decision, "answer_coverage": 1.0, "score": 80},
    )


class OriginStreamingTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "radar.db"
        self.store = ParadigmStore(self.path)
        self.orchestrator = ParadigmOrchestrator.__new__(ParadigmOrchestrator)
        self.orchestrator.store = self.store
        self.orchestrator.enricher = SimpleNamespace(hydrate_priority_origins=AsyncMock(return_value={}))
        self.analyzer = ParadigmAnalyzer(concurrency=2, client=object())
        self.analyzer.extract = AsyncMock(side_effect=lambda item: [extraction(item)])
        self.orchestrator.analyzer = self.analyzer
        self.keys = set()

    def register(self, *names):
        return self.store.observe_origins([origin(name) for name in names])[0]

    async def run_origins(self, items):
        return await self.orchestrator._analyze_origins_in_batches(
            items, time.monotonic() + 10, candidate_keys=self.keys,
        )

    def pending_names(self):
        return {item.title for item in self.store.load_pending_origins()}

    async def test_queue_snapshot_counts_durable_unfinished_work(self):
        reference = datetime.now(timezone.utc)
        self.assertEqual(
            self.store.work_queue_snapshot(reference_time=reference)[
                "pending_total_count"
            ],
            0,
        )
        items = self.register("one", "two")
        before = self.store.work_queue_snapshot(reference_time=reference)
        self.assertEqual(before["pending_origin_count"], 2)
        await self.run_origins(items)
        after = self.store.work_queue_snapshot(reference_time=reference)
        self.assertEqual(after["pending_origin_count"], 0)
        self.assertEqual(after["pending_deep_count"], 1)
        self.assertEqual(after["pending_total_count"] - before["pending_total_count"], -1)

    async def test_current_pending_deep_routes_get_service_amid_old_queue(self):
        reference = datetime(2026, 9, 23, tzinfo=timezone.utc)
        old = []
        for index in range(40):
            candidate = ParadigmCandidate(
                key=f"old-{index}", name=f"Old route {index}",
                thesis="", problem_shift="", mechanism="",
                evidence=[origin(f"old-paper-{index}")],
            )
            candidate.evidence[0].published_at = "2026-08-01T00:00:00Z"
            old.append(candidate)
        current = []
        for index in range(12):
            candidate = ParadigmCandidate(
                key=f"current-{index}", name=f"Current route {index}",
                thesis="", problem_shift="", mechanism="",
                evidence=[origin(f"current-paper-{index}")],
            )
            candidate.evidence[0].published_at = "2026-09-22T00:00:00Z"
            current.append(candidate)
        ordered = _deep_execution_order(
            old, current, reference_time=reference, window_days=7
        )
        self.assertIn(ordered[0], current)
        self.assertEqual(sum(item in current for item in ordered[:16]), 12)
        self.assertEqual(sum(item in old for item in ordered[:16]), 4)
        self.assertEqual(len(ordered), 52)
        self.assertCountEqual(ordered, [*old, *current])

    async def test_completed_peer_is_durable_before_slow_peer_and_survives_cancel(self):
        items = self.register("fast", "slow")
        committed, slow_started, cancelled = asyncio.Event(), asyncio.Event(), asyncio.Event()
        original = self.store.mark_evidence

        def mark(values, *args, **kwargs):
            result = original(values, *args, **kwargs)
            if kwargs.get("analyzed") and values:
                committed.set()
            return result

        async def extract(item):
            if item.title == "slow":
                slow_started.set()
                try:
                    await asyncio.Event().wait()
                finally:
                    cancelled.set()
            await slow_started.wait()
            return [extraction(item)]

        self.analyzer.extract.side_effect = extract
        with patch.object(self.store, "mark_evidence", side_effect=mark):
            task = asyncio.create_task(self.run_origins(items))
            try:
                await asyncio.wait_for(committed.wait(), 2)
                self.assertFalse(task.done())
                self.assertEqual(self.pending_names(), {"slow"})
                self.assertEqual(len(self.store.load_pending_deep_candidates()), 1)
            finally:
                task.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await task
        self.assertTrue(cancelled.is_set())
        restored = ParadigmStore(self.path)
        self.assertEqual({item.title for item in restored.load_pending_origins()}, {"slow"})
        self.assertEqual(len(restored.load_pending_deep_candidates()), 1)

    async def test_timeout_defers_only_uncommitted_and_cancels_requests(self):
        items = self.register("fast", "slow")
        cancelled = asyncio.Event()

        async def extract(item):
            if item.title == "slow":
                try:
                    await asyncio.Event().wait()
                finally:
                    cancelled.set()
            return [extraction(item)]

        self.analyzer.extract.side_effect = extract
        with patch("agents.paradigm_orchestrator._remaining_seconds", return_value=0.15):
            result = await self.run_origins(items)
        self.assertEqual(result[1:4], (1, 0, 0))
        self.assertEqual([item.title for item in result[4]], ["slow"])
        self.assertTrue(cancelled.is_set())
        selected, stats, _ = self.store.observe_origins([origin("fast"), origin("slow")])
        self.assertEqual([item.title for item in selected], ["slow"])
        self.assertEqual(stats["unchanged_skip"], 1)
        self.assertNotIn("analysis_failure_count", selected[0].raw)

    async def test_bad_candidate_commit_does_not_reexecute_or_drop_healthy_peer(self):
        items = self.register("bad", "good")
        original = self.store.save_candidates

        def save(values):
            if any(item.name == "bad" for item in values):
                raise ValueError("candidate contract broken")
            original(values)

        with patch.object(self.store, "save_candidates", side_effect=save):
            result = await self.run_origins(items)
        self.assertEqual(result[1:4], (2, 1, 0))
        self.assertEqual(self.analyzer.extract.await_count, 2)
        self.assertEqual(self.pending_names(), {"bad"})
        self.assertEqual([item.name for item in self.store.load_pending_deep_candidates()], ["good"])
        self.assertEqual(migrate_state(self.path, config.PARADIGM_STATE_SCHEMA_VERSION), config.PARADIGM_STATE_SCHEMA_VERSION)

    async def test_database_failure_does_not_replay_models_or_rollback_prior_commit(self):
        items = self.register("good", "disk-failure")
        first_saved = asyncio.Event()
        original = self.store.save_candidates

        def save(values):
            if any(item.name == "disk-failure" for item in values):
                raise sqlite3.OperationalError("disk full")
            original(values)
            first_saved.set()

        async def extract(item):
            if item.title == "disk-failure":
                await first_saved.wait()
            return [extraction(item)]

        self.analyzer.extract.side_effect = extract
        with patch.object(self.store, "save_candidates", side_effect=save):
            with self.assertRaises(sqlite3.OperationalError):
                await self.run_origins(items)
        self.assertEqual(self.analyzer.extract.await_count, 2)
        self.assertEqual(self.pending_names(), {"disk-failure"})
        self.assertEqual(len(self.store.load_pending_deep_candidates()), 1)

    async def test_report_candidate_failure_never_advances_parent_progress(self):
        items = self.register("report")

        async def extract(item):
            item.raw.update({"origin_kind": "technical_report", "technical_report_slice_pending": True,
                             "technical_report_mechanism_seeds": [{"canonical_name": "a"}, {"canonical_name": "b"}],
                             "technical_report_completed_mechanisms": {"a": {"mechanism": "new"}}})
            return [extraction(item)]

        self.analyzer.extract.side_effect = extract
        with patch.object(self.store, "save_candidates", side_effect=ValueError("bad output")):
            result = await self.run_origins(items)
        self.assertEqual(result[1:4], (1, 1, 0))
        pending = self.store.load_pending_origins()[0]
        self.assertNotIn("technical_report_completed_mechanisms", pending.raw)
        self.assertEqual(pending.raw["analysis_failure_count"], 1)
        self.assertEqual(self.store.load_pending_deep_candidates(), [])

    async def test_report_slice_saves_candidate_and_parent_together(self):
        items = self.register("report")

        async def extract(item):
            item.raw.update({"origin_kind": "technical_report", "technical_report_slice_pending": True,
                             "technical_report_mechanism_seeds": [{"canonical_name": "a"}, {"canonical_name": "b"}],
                             "technical_report_completed_mechanisms": {"a": {"mechanism": "new"}}})
            return [extraction(item, "mechanism-a")]

        self.analyzer.extract.side_effect = extract
        result = await self.run_origins(items)
        self.assertEqual(result[1:4], (1, 0, 1))
        self.assertEqual(set(self.store.load_pending_origins()[0].raw["technical_report_completed_mechanisms"]), {"a"})
        self.assertEqual(len(self.store.load_pending_deep_candidates()), 1)

    async def test_foreign_duplicate_and_missing_stream_outputs_are_accounted(self):
        items = self.register("one", "two")

        async def stream(group):
            yield origin("foreign"), [extraction(origin("foreign"))]
            yield group[0], [extraction(group[0])]
            yield group[0], [extraction(group[0])]

        self.orchestrator.analyzer = SimpleNamespace(iter_results=stream)
        result = await self.run_origins(items)
        self.assertEqual(result[1:4], (2, 1, 0))
        self.assertEqual(self.pending_names(), {"two"})
        self.assertEqual(len(self.store.load_pending_deep_candidates()), 1)

    async def test_late_source_revision_cannot_commit_candidate(self):
        items = self.register("one")

        async def extract(item):
            fresh = origin("one")
            fresh.summary = "new source revision"
            self.store.observe_origins([fresh])
            return [extraction(item)]

        self.analyzer.extract.side_effect = extract
        result = await self.run_origins(items)
        self.assertEqual(result[1:4], (1, 1, 0))
        self.assertEqual(self.store.load_pending_deep_candidates(), [])
        self.assertEqual(self.store.load_pending_origins()[0].summary, "new source revision")

    async def test_result_bound_to_wrong_revision_is_pending(self):
        items = self.register("one")
        other = copy.deepcopy(items[0])
        other.source_revision = "wrong"
        self.analyzer.extract.side_effect = lambda item: [extraction(other)]
        result = await self.run_origins(items)
        self.assertEqual(result[1:4], (1, 1, 0))
        self.assertEqual(self.pending_names(), {"one"})

    async def test_pending_route_alignment_keeps_evidence_across_origin_commits(self):
        items = self.register("one", "two")
        self.analyzer.extract.side_effect = lambda item: [extraction(item, "same-mechanism")]
        with patch.object(
            self.store, "build_route_history_index",
            wraps=self.store.build_route_history_index,
        ) as index_builder:
            await self.run_origins(items)
        self.assertEqual(index_builder.call_count, 1)
        candidates = self.store.load_pending_deep_candidates()
        self.assertEqual(len(candidates), 1)
        self.assertEqual({item.title for item in candidates[0].evidence}, {"one", "two"})
        self.assertEqual(self.pending_names(), set())

    async def test_pending_route_rename_aligns_identity_without_inheriting_verdict(self):
        first = self.register("one")
        self.analyzer.extract.side_effect = lambda item: [extraction(item, "episodic feedback")]
        await self.run_origins(first)
        key = self.store.load_pending_deep_candidates()[0].key
        second = self.register("two")
        self.analyzer.extract.side_effect = lambda item: [extraction(item, "episodic memory feedback")]
        await self.run_origins(second)
        candidates = self.store.load_pending_deep_candidates()
        self.assertEqual([item.key for item in candidates], [key])
        self.assertEqual(candidates[0].status, "pending_deep")
        self.assertFalse(candidates[0].admission_reason)

    async def test_history_index_only_compares_routes_with_two_shared_tokens(self):
        history = [
            (
                f"historic-{index}",
                ParadigmCandidate(
                    key=f"historic-{index}", name=f"concept{index:04d}",
                    route_family=f"family{index:04d}", thesis="",
                    problem_shift="", mechanism="", evidence=[],
                ),
            )
            for index in range(500)
        ]
        indexed = RouteHistoryIndex(history)
        current = ParadigmCandidate(
            key="renamed", name="concept0477", route_family="family0477",
            thesis="", problem_shift="", mechanism="", evidence=[],
        )
        full = max(
            (_route_similarity(current, item), key)
            for key, item in history
        )
        relevant = indexed.relevant(current)
        self.assertEqual([key for key, _ in relevant], ["historic-477"])
        self.assertEqual(
            max((_route_similarity(current, item), key) for key, item in relevant),
            full,
        )
        indexed.upsert(ParadigmCandidate(
            key="historic-477", name="other0477", route_family="different0477",
            thesis="", problem_shift="", mechanism="", evidence=[],
        ))
        self.assertEqual(indexed.relevant(current), [])

        rejected = copy.deepcopy(history[477][1])
        rejected.status = "rejected"
        indexed.upsert(rejected)
        self.assertEqual(indexed.relevant(current), [])
        self.assertIsNone(indexed.get(rejected.key))

    async def test_same_report_different_mechanisms_do_not_merge_on_background(self):
        item = self.register("report")[0]
        item.raw["origin_kind"] = "technical_report"
        first, second = extraction(item, "mechanism-a"), extraction(item, "mechanism-b")
        self.assertEqual(len(cluster_extractions([first, second])), 2)
        candidate = cluster_extractions([first])[0]
        candidate.status = "pending_deep"
        self.store.save_candidates([candidate])
        aligned = self.store.attach_history(cluster_extractions([second]))
        self.assertNotEqual(aligned[0].key, candidate.key)

    async def test_closed_negative_is_complete_but_incomplete_rubric_is_pending(self):
        items = self.register("negative", "incomplete")
        self.analyzer.extract.side_effect = lambda item: [extraction(
            item, decision="reject" if item.title == "negative" else "incomplete")]
        result = await self.run_origins(items)
        self.assertEqual(result[1:4], (2, 1, 0))
        self.assertEqual(self.pending_names(), {"incomplete"})
        self.assertEqual(self.store.load_pending_deep_candidates(), [])

    async def test_current_deep_work_and_old_backlog_both_get_execution_turns(self):
        def candidate(name, priority):
            item = origin(name)
            item.raw["origin_priority"] = priority
            return ParadigmCandidate(
                key=name, name=name, thesis="hypothesis",
                problem_shift="changed boundary", mechanism="memory update",
                evidence=[item],
            )

        old = [candidate(f"old-{index}", 3) for index in range(3)]
        fresh = [candidate(f"fresh-{index}", 1) for index in range(5)]
        ordered = _deep_execution_order(old, fresh)
        self.assertEqual(
            [item.key for item in ordered[:4]],
            ["fresh-0", "fresh-1", "fresh-2", "old-0"],
        )
        self.assertEqual({item.key for item in ordered},
                         {item.key for item in [*old, *fresh]})
        self.assertEqual(len(ordered), 8)

    async def test_refresh_reserve_requires_real_work_and_remaining_time(self):
        with patch("agents.paradigm_orchestrator._remaining_seconds", return_value=1200):
            self.assertEqual(_refresh_reserve_seconds(1234, has_refresh_work=True), 300)
            self.assertEqual(_refresh_reserve_seconds(1234, has_refresh_work=False), 0)
        with patch("agents.paradigm_orchestrator._remaining_seconds", return_value=400):
            self.assertEqual(_refresh_reserve_seconds(1234, has_refresh_work=True), 100)
        with patch("agents.paradigm_orchestrator._remaining_seconds", return_value=179):
            self.assertEqual(_refresh_reserve_seconds(1234, has_refresh_work=True), 0)


if __name__ == "__main__":
    unittest.main()
