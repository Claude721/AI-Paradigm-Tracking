"""Large durable queues, paid-stage timing, input supersession and restart."""

from __future__ import annotations

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
from main import _deliver_paradigm_job
from agents.paradigm_orchestrator import ParadigmOrchestrator
from database.paradigm_store import ParadigmStore
from paradigms.enrichment import EvidenceEnricher
from paradigms.discovery import DiscoveryBatch
from paradigms.models import EvidenceType, ParadigmCandidate, ParadigmExtraction, TechnicalEvidence
from paradigms.scheduler import ResearchLaneScheduler
from run_audit import run_audit
from runtime_clock import research_window
from reports.paradigm_generator import ParadigmReportGenerator
from tests.test_paradigm_pipeline import candidate as researched_candidate, verified_researcher


class Clock:
    def __init__(self):
        self.value = time.monotonic()

    def __call__(self):
        return self.value

    def advance(self, seconds):
        self.value += seconds


def origin(index, *, current=False):
    return TechnicalEvidence(
        source="arxiv", evidence_type=EvidenceType.PRIMARY_PAPER,
        title=f"Current {index}" if current else f"Old {index}",
        url=f"https://arxiv.org/abs/2609.{index:05d}",
        published_at="2026-10-07T00:00:00Z" if current else "2026-01-01T00:00:00Z",
    )


class ResearchServiceTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.store = ParadigmStore(Path(self.directory.name) / "state.db")
        self.clock = Clock()
        self.events = []
        self.orchestrator = ParadigmOrchestrator.__new__(ParadigmOrchestrator)
        self.orchestrator.store = self.store
        self.orchestrator.ordinary_discovery_lookback_days = 7
        self.orchestrator.enricher = SimpleNamespace(
            hydrate_priority_origins=AsyncMock(return_value={}),
            run=AsyncMock(side_effect=self.enrich),
            finalize=EvidenceEnricher.finalize,
        )
        self.orchestrator.analyzer = SimpleNamespace(run=self.analyze)
        self.orchestrator.synthesizer = SimpleNamespace(run=self.synthesize)
        self.orchestrator.trajectory = SimpleNamespace(run=self.trajectory)
        run_audit.reset()

    async def analyze(self, items):
        self.clock.advance(5)
        self.events.extend(("origin", item.title) for item in items)
        return [ParadigmExtraction(
            evidence=item, is_candidate=item.title.startswith("Current"),
            canonical_name=item.title, route_family=item.title,
            thesis="机制种子", problem_shift=item.title, mechanism=item.title,
            rubric_assessment={
                "decision": "deep_dive" if item.title.startswith("Current") else "reject",
                "answer_coverage": 1.0, "score": 80,
            },
        ) for item in items]

    async def enrich(self, items, _support):
        self.clock.advance(3)
        return items

    async def synthesize(self, items):
        self.clock.advance(5)
        self.events.extend(("synthesis", item.key) for item in items)
        for item in items:
            item.rubric_assessment = {"decision": "observe"}
        return items

    async def trajectory(self, items):
        self.clock.advance(3)
        self.events.extend(("trajectory", item.key) for item in items)
        return items

    async def service(self, items, *, seconds=90):
        with (
            research_window(datetime(2026, 10, 8, tzinfo=timezone.utc)),
            patch.object(config, "PARADIGM_ANALYSIS_BATCH_SIZE", 1),
            patch.object(config, "PARADIGM_DEEP_BATCH_SIZE", 1),
            patch.object(config, "PARADIGM_DEEP_SAFETY_LIMIT", 0),
            patch.object(config, "PARADIGM_REFRESH_SAFETY_LIMIT", 0),
            patch("agents.paradigm_orchestrator._remaining_seconds",
                  side_effect=lambda deadline: max(deadline - self.clock(), 0)),
        ):
            return await self.orchestrator._service_research_lanes(
                items, [], self.clock() + seconds, 20,
                candidate_keys=set(), stats={"origin_count": len(items)}, clock=self.clock,
            )

    async def test_ten_thousand_backfill_tasks_leave_current_end_to_end_and_updates(self):
        inputs = [origin(index) for index in range(10000)]
        inputs += [origin(10000 + index, current=True) for index in range(6)]
        items = self.store.observe_origins(inputs)[0]
        historical = ParadigmCandidate(
            key="old-route", name="Old route", thesis="", problem_shift="",
            mechanism="", status="observe", evidence=[copy.deepcopy(items[0])],
        )
        self.store.save_candidates([historical])

        async def refresh(values, _support):
            self.clock.advance(3)
            self.events.append(("refresh", values[0].key))
            return []

        self.orchestrator.enricher.refresh = refresh
        result = await self.service(items)
        first_deep = next(index for index, item in enumerate(self.events) if item[0] == "trajectory")
        self.assertLess(first_deep, 8)
        self.assertTrue(any(item[0] == "refresh" for item in self.events))
        self.assertTrue(any(item[0] == "origin" and item[1].startswith("Old") for item in self.events))
        self.assertTrue(result["deep_candidates"])
        self.assertTrue(all(
            item.deep_checkpoint_stage == "research_complete"
            for item in result["deep_candidates"]
        ))
        self.assertGreater(len(self.store.load_pending_origins()), 9900)
        self.assertGreater(result["research_service"]["operations"]["updates"], 0)

    async def test_later_same_route_input_supersedes_early_result_and_retains_support(self):
        items = self.store.observe_origins([origin(1, current=True), origin(2, current=True)])[0]

        async def same_route(group):
            values = await self.analyze(group)
            for item in values:
                item.canonical_name = "Same route"
            return values

        async def enriched(group, _support):
            group[0].evidence.append(TechnicalEvidence(
                source="independent", evidence_type=EvidenceType.INDEPENDENT_REPLICATION,
                title="Scoped support", url="https://research.example.org/replication",
            ))
            return group

        self.orchestrator.analyzer.run = same_route
        self.orchestrator.enricher.run = enriched
        result = await self.service(items)
        self.assertEqual(len(result["deep_candidates"]), 1)
        final = result["deep_candidates"][0]
        self.assertEqual(len({item.fingerprint for item in final.evidence}), 3)
        self.assertEqual([event[0] for event in self.events].count("synthesis"), 2)
        self.assertEqual(len(result["deep_pool"]), 1)

    async def test_completed_route_survives_failure_in_next_origin_visit(self):
        items = self.store.observe_origins([origin(1, current=True), origin(2, current=True)])[0]

        async def break_second(group):
            if group[0].title == "Current 2":
                raise sqlite3.OperationalError("injected storage boundary failure")
            return await self.analyze(group)

        self.orchestrator.analyzer.run = break_second
        with self.assertRaises(sqlite3.OperationalError):
            await self.service(items)
        recovered = ParadigmStore(self.store.db_path).load_pending_deep_candidates()
        self.assertEqual(len(recovered), 1)
        self.assertEqual(recovered[0].deep_checkpoint_stage, "research_complete")
        self.assertEqual({item.title for item in self.store.load_pending_origins()}, {"Current 2"})
        self.assertEqual(run_audit.last_stats["origin_count"], 2)

    async def test_later_failed_version_cannot_leave_old_success_in_output(self):
        items = self.store.observe_origins([origin(1, current=True), origin(2, current=True)])[0]

        async def same_route(group):
            values = await self.analyze(group)
            for item in values:
                item.canonical_name = "Same route"
            return values

        calls = 0

        async def fail_new_version(group):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise RuntimeError("new input synthesis failed")
            return await self.synthesize(group)

        self.orchestrator.analyzer.run = same_route
        self.orchestrator.synthesizer.run = fail_new_version
        result = await self.service(items)
        self.assertEqual(result["deep_candidates"], [])
        self.assertEqual(len(result["execution_deferred_candidates"]), 1)
        pending = self.store.load_pending_deep_candidates()[0]
        self.assertEqual(len(pending.evidence), 2)
        self.assertEqual(pending.deep_checkpoint_stage, "")

    async def test_changed_upstream_origin_is_committed_before_downstream_calls(self):
        prior = self.store.observe_origins([origin(1, current=True)])[0][0]
        pending = ParadigmCandidate(
            key="current-1", name="Current 1", thesis="机制种子",
            problem_shift="Current 1", mechanism="Current 1", status="pending_deep",
            evidence=[prior],
        )
        self.store.save_candidates([pending])
        changed = origin(1, current=True)
        changed.summary = "A genuinely revised upstream document"
        updated = self.store.observe_origins([changed])[0]
        result = await self.service(updated)
        self.assertEqual(self.events[0], ("origin", "Current 1"))
        self.assertEqual([event[0] for event in self.events].count("synthesis"), 1)
        self.assertEqual(result["input_deferred_candidates"], [])
        self.assertEqual(result["execution_deferred_candidates"], [])
        self.assertEqual(len(result["deep_candidates"]), 1)

    async def test_missing_upstream_service_is_dependency_deferred_without_paid_call(self):
        prior = self.store.observe_origins([origin(1, current=True)])[0][0]
        pending = ParadigmCandidate(
            key="current-1", name="Current 1", thesis="机制种子",
            problem_shift="Current 1", mechanism="Current 1", status="pending_deep",
            evidence=[prior],
        )
        self.store.save_candidates([pending])
        changed = origin(1, current=True)
        changed.summary = "New upstream version still outside this safety visit"
        self.store.observe_origins([changed])
        result = await self.service([])
        self.assertEqual(self.events, [])
        self.assertEqual(len(result["input_deferred_candidates"]), 1)
        self.assertEqual(result["execution_deferred_candidates"], [])
        self.assertEqual(result["budget_deferred_candidates"], [])
        self.assertEqual(len(self.store.load_pending_deep_candidates()), 1)

    async def test_revised_origin_without_new_route_does_not_strand_old_dependency(self):
        prior = self.store.observe_origins([origin(1, current=True)])[0][0]
        self.store.save_candidates([ParadigmCandidate(
            key="prior-route", name="Prior hypothesis", thesis="旧机制假说",
            problem_shift="原有边界", mechanism="待复核机制", status="pending_deep",
            evidence=[prior], rubric_assessment={"decision": "report"}, total_score=99,
            deep_checkpoint_stage="research_complete",
        )])
        changed = origin(1, current=True)
        changed.summary = "Revised text no longer produces a new mechanism"
        updated = self.store.observe_origins([changed])[0]

        async def reject_new_origin(group):
            values = await self.analyze(group)
            for value in values:
                value.is_candidate = False
                value.rubric_assessment = {"decision": "reject"}
            return values

        async def verify_rebased(group):
            self.assertEqual(group[0].evidence[0].summary, changed.summary)
            self.assertEqual(group[0].evidence[0].source_revision, updated[0].source_revision)
            self.assertEqual(group[0].rubric_assessment, {})
            self.assertEqual(group[0].deep_checkpoint_stage, "")
            self.assertEqual(group[0].total_score, 0)
            return await self.synthesize(group)

        self.orchestrator.analyzer.run = reject_new_origin
        self.orchestrator.synthesizer.run = verify_rebased
        result = await self.service(updated)
        self.assertEqual(self.events[0][0], "origin")
        self.assertEqual(result["origin_result"][0][0].rubric_assessment["decision"], "reject")
        self.assertEqual(result["input_deferred_candidates"], [])
        self.assertEqual(len(result["deep_candidates"]), 1)
        self.assertEqual(result["deep_candidates"][0].key, "prior-route")
        self.assertEqual(result["deep_candidates"][0].rubric_assessment["decision"], "observe")

    async def test_restart_after_origin_completion_rebases_pending_route(self):
        prior = self.store.observe_origins([origin(1, current=True)])[0][0]
        self.store.save_candidates([ParadigmCandidate(
            key="prior-route", name="Prior hypothesis", thesis="旧机制假说",
            problem_shift="原有边界", mechanism="待复核机制", status="pending_deep",
            evidence=[prior],
        )])
        changed = origin(1, current=True)
        changed.summary = "Upstream completed just before process exit"
        updated = self.store.observe_origins([changed])[0]
        self.store.mark_evidence(updated, analyzed=True)
        self.orchestrator.store = ParadigmStore(self.store.db_path)
        result = await self.service([])
        self.assertEqual(result["input_deferred_candidates"], [])
        self.assertEqual(len(result["deep_candidates"]), 1)
        self.assertEqual(result["deep_candidates"][0].evidence[0].summary, changed.summary)
        self.assertEqual([item[0] for item in self.events], ["synthesis", "trajectory"])

    async def test_rebase_waits_for_all_changed_origins_and_preserves_snapshot(self):
        prior = self.store.observe_origins([origin(1, current=True), origin(2, current=True)])[0]
        pending = ParadigmCandidate(
            key="two-inputs", name="Prior hypothesis", thesis="旧机制假说",
            problem_shift="原有边界", mechanism="待复核机制", status="pending_deep",
            evidence=prior,
        )
        self.store.save_candidates([pending])
        changed = [origin(1, current=True), origin(2, current=True)]
        for item in changed:
            item.summary = "New source version"
        updated = self.store.observe_origins(changed)[0]
        self.store.mark_evidence([updated[0]], analyzed=True)
        self.assertIsNone(self.store.rebase_analyzed_candidate_inputs(pending.key))
        self.assertEqual(self.store.load_candidate_snapshots({pending.key})[0].to_dict(), pending.to_dict())
        self.store.mark_evidence([updated[1]], analyzed=True)
        rebased = self.store.rebase_analyzed_candidate_inputs(pending.key)
        self.assertTrue(self.store.candidate_inputs_current(rebased))
        self.assertEqual(len(rebased.evidence), 2)
        self.assertEqual(self.store.load_pending_origins(), [])

    async def test_deep_safety_limit_keeps_every_unvisited_route_pending(self):
        items = self.store.observe_origins([origin(index, current=True) for index in (1, 2, 3)])[0]
        with patch.object(config, "PARADIGM_DEEP_SAFETY_LIMIT", 1):
            # Set the limit inside the helper's fixture override.
            with (
                research_window(datetime(2026, 10, 8, tzinfo=timezone.utc)),
                patch.object(config, "PARADIGM_ANALYSIS_BATCH_SIZE", 1),
                patch.object(config, "PARADIGM_DEEP_BATCH_SIZE", 1),
            ):
                result = await self.orchestrator._service_research_lanes(
                    items, [], time.monotonic() + 10, 0,
                    candidate_keys=set(), stats={},
                )
        self.assertEqual(len(result["deep_candidates"]), 1)
        self.assertEqual(len(result["safety_deferred_candidates"]), 2)
        self.assertEqual(len(self.store.load_pending_deep_candidates()), 3)

    async def test_current_route_crosses_real_rubric_report_outbox_and_delivery_gate(self):
        current = origin(100, current=True)
        current.authors = ["A. Researcher"]
        current.organization = "Example Research Lab"
        current.raw = {
            "publisher_tier": "established", "origin_kind": "technical_report",
            "origin_classification_reason": "explicit_document_metadata:Technical Report",
        }
        old = [origin(index) for index in range(100)]
        self.orchestrator.bootstrap_mode = False
        self.orchestrator.high_signal_discovery_lookback_days = 30
        self.orchestrator.discovery_lookback_days = 30
        self.orchestrator.discovery = SimpleNamespace(run=AsyncMock(return_value=DiscoveryBatch(
            origins=[*old, current], supporting=[], source_counts={"fixture": 101},
            coverage={"covered_domains": 0, "total_domains": 0},
        )))

        async def closed_synthesis(values):
            result = []
            for value in values:
                complete = researched_candidate(value.evidence)
                complete.key = value.key
                complete.status = value.status
                complete.screening_rubric = value.screening_rubric
                complete.researchers = [verified_researcher()]
                result.append(complete)
            return result

        self.orchestrator.synthesizer.run = closed_synthesis
        with patch.object(config, "PARADIGM_ANALYSIS_SAFETY_LIMIT", 1):
            stats = await self.orchestrator.run(
                reference_time=datetime(2026, 10, 8, tzinfo=timezone.utc)
            )
        self.assertEqual(stats["high_value_count"], 1)
        self.assertEqual(stats["current_window_deep_completed_count"], 1)
        self.assertTrue(stats["research_incomplete"])
        job = self.store.enqueue_report(
            self.orchestrator.pending_delivery, stats, report_date="2026-10-08"
        )
        body = (
            "旧系统把所有变化混在同一个表示里，新方法改写决定状态转移的接口。"
            "训练时预测误差沿此接口更新参数，运行时当前状态先形成中间表示，"
            "再决定下一状态；拿掉这一接口后，系统会退回平均预测。"
        ) * 8
        route = (
            f"### 世界模型开始改变状态接口\n\n{body} A. Researcher 推动这项研究，"
            f"[查看原文]({current.url})。\n\n"
            "**讨论势能判断：** 当前仍是发布团队的单点提出，独立复现尚未出现；"
            "本轮社区覆盖有限，暂不能判断为扩散。"
        )
        memo = (
            "本期已完成的研究把状态转移接口从像素生成中拆出，让训练信号和运行信息流"
            "围绕可行动变化组织。证据主要来自发布团队，外部复现仍不足，后续应观察"
            "接口能否在不同任务中保持稳定，以及现实控制系统能否沿同一机制承接。"
        ) * 5
        frame = (
            f"# AI 技术范式雷达\n\n## 本期研究 Memo\n\n{memo}\n\n"
            "## 接下来真正值得盯的信号\n\n观察独立复现和控制接口对齐。"
        )
        client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(
            create=AsyncMock(side_effect=[SimpleNamespace(choices=[SimpleNamespace(
                message=SimpleNamespace(content=value)
            )]) for value in (route, frame)])
        )))
        generator = ParadigmReportGenerator(self.directory.name, client=client, model="offline-editor")
        with (
            patch.object(config, "EMAIL_PUSH_ENABLED", True),
            patch("notifications.email_notifier.send_report_email", new=AsyncMock(return_value=True)) as send,
        ):
            delivered = await _deliver_paradigm_job(self.store, generator, job, recovered=False)
        self.assertTrue(delivered["email_sent"])
        send.assert_awaited_once()
        self.assertIsNone(self.store.load_pending_report_job())
        self.assertEqual(len(self.store.latest_reported_candidates()), 1)
        self.assertEqual(self.store.prepare_report(self.orchestrator.pending_delivery), [])
        content = Path(delivered["report_path"]).read_text(encoding="utf-8")
        self.assertIn(current.url, content)
        self.assertIn("## 关键人物与公开联系入口", content)
        self.assertNotIn("评分", content)


class LaneBudgetTests(unittest.TestCase):
    def test_first_long_origin_leaves_time_for_its_downstream_candidate(self):
        clock = Clock()
        scheduler = ResearchLaneScheduler(clock() + 90, 30, clock=clock)
        visit_end = scheduler.visit_deadline(
            "current", {"current", "updates", "backfill"}, stage="origin"
        )
        self.assertAlmostEqual(visit_end - clock(), 30)

    def test_finished_lane_lends_unspent_time_without_deadlock(self):
        clock = Clock()
        scheduler = ResearchLaneScheduler(clock() + 90, clock=clock)
        start = clock()
        clock.advance(60)
        scheduler.account("current", start, stage="deep")
        end = scheduler.visit_deadline("updates", {"updates", "backfill"}, stage="refresh")
        self.assertAlmostEqual(end - clock(), 20)


if __name__ == "__main__":
    unittest.main()
