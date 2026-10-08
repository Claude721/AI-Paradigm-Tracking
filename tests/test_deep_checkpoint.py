"""Offline regression for the durable synthesis/trajectory boundary."""

from __future__ import annotations

import asyncio
import copy
import sqlite3
import tempfile
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock
from unittest.mock import patch

from agents.paradigm_orchestrator import (
    ParadigmOrchestrator,
    _deferred_deep_snapshot,
)
from database.paradigm_store import ParadigmStore
from tests.completion_fixtures import COMPLETED_RESEARCH
from paradigms.analyzer import ResearcherTrajectoryAnalyzer
from paradigms.enrichment import EvidenceEnricher
from paradigms.models import (
    EvidenceType, ParadigmCandidate, ResearcherProfile, TechnicalEvidence,
)


def origin(summary: str = "source") -> TechnicalEvidence:
    return TechnicalEvidence(
        source="arxiv", evidence_type=EvidenceType.PRIMARY_PAPER,
        title="Test paper", url="https://arxiv.org/abs/2609.00001",
        summary=summary, identifiers={"arxiv": "2609.00001"},
    )


def social() -> TechnicalEvidence:
    return TechnicalEvidence(
        source="reddit", evidence_type=EvidenceType.COMMUNITY_DISCUSSION,
        title="Private user headline", url="https://www.reddit.com/r/Local/comments/abc/",
        summary="Private user body", authors=["private_user"],
        identifiers={"reddit": "abc"},
        raw={"ephemeral_content": True, "social_author_name": "private_user"},
    )


def supporting(*, score: int = 1, temporary: bool = False) -> TechnicalEvidence:
    return TechnicalEvidence(
        source="independent-lab",
        evidence_type=EvidenceType.INDEPENDENT_REPLICATION,
        title="Test paper replication",
        url="https://research.example.org/test-paper-replication",
        summary="New independent result" if not temporary else "Temporary user body",
        authors=["Other team"],
        identifiers={"arxiv": "2609.00001"},
        metrics={"score": score},
        raw={
            "ephemeral_content": True,
            "body": "Hidden raw user body",
            "social_author_name": "private_user",
            "metric_delta": {
                "stars": 25,
                "private_note": "Hidden raw user body",
                "comments": "not a number",
            },
        } if temporary else {},
    )


class DeepCheckpointTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.path = Path(temporary.name) / "radar.db"
        self.store = ParadigmStore(self.path)
        source = self.store.observe_origins([origin()])[0][0]
        self.candidate = ParadigmCandidate(
            key="route-1", name="Route 1", thesis="test",
            problem_shift="test", mechanism="test", status="pending_deep",
            evidence=[source], screening_rubric={"decision": "deep_dive"},
        )
        self.store.save_candidates([self.candidate])
        self.orchestrator = ParadigmOrchestrator.__new__(ParadigmOrchestrator)
        self.orchestrator.store = self.store

    async def run_deep(self, candidates=None, *, support=None, seconds=10):
        return await self.orchestrator._deep_analyze_in_batches(
            candidates or [self.candidate], support or [], time.monotonic() + seconds,
        )

    def stages(self, *, decision="report", trajectory=None):
        async def enrich(values, _supporting):
            EvidenceEnricher._attach_support(values[0], _supporting)
            values[0].evidence.append(social())
            return values

        async def synthesize(values):
            values[0].technical_explanation = "已完成综合"
            values[0].rubric_assessment = {"decision": decision}
            return values

        self.orchestrator.enricher = SimpleNamespace(run=AsyncMock(side_effect=enrich))
        self.orchestrator.synthesizer = SimpleNamespace(run=AsyncMock(side_effect=synthesize))
        self.orchestrator.trajectory = SimpleNamespace(
            run=trajectory or AsyncMock(side_effect=lambda values: values)
        )

    async def test_trajectory_failure_resumes_without_enrichment_or_synthesis(self):
        self.stages(trajectory=AsyncMock(side_effect=RuntimeError("profile service")))
        completed, budget, failed = await self.run_deep()
        self.assertEqual((completed, budget), ([], []))
        self.assertEqual(len(failed), 1)
        self.assertEqual(failed[0].deep_checkpoint_stage, "synthesized")
        self.store.save_candidates(failed)  # production's final deferral save

        restored = ParadigmStore(self.path).load_pending_deep_candidates()[0]
        self.assertEqual(restored.execution_failure_count, 1)
        self.assertEqual(restored.technical_explanation, "已完成综合")
        discussion = next(item for item in restored.evidence if item.source == "reddit")
        self.assertEqual((discussion.summary, discussion.authors), ("", []))
        self.assertNotIn("social_author_name", discussion.raw)

        self.orchestrator.enricher.run.reset_mock()
        self.orchestrator.synthesizer.run.reset_mock()
        self.orchestrator.trajectory.run = AsyncMock(side_effect=lambda values: values)
        completed, budget, failed = await self.run_deep([restored])
        self.assertEqual((budget, failed), ([], []))
        self.assertEqual(len(completed), 1)
        self.assertEqual(completed[0].execution_failure_count, 0)
        self.orchestrator.enricher.run.assert_not_awaited()
        self.orchestrator.synthesizer.run.assert_not_awaited()
        self.orchestrator.trajectory.run.assert_awaited_once()

    async def test_completed_person_research_survives_later_run_failure(self):
        self.stages()
        completed, budget, failed = await self.run_deep()
        self.assertEqual((budget, failed), ([], []))
        self.assertEqual(len(completed), 1)
        persisted = ParadigmStore(self.path).load_pending_deep_candidates()[0]
        self.assertEqual(persisted.deep_checkpoint_stage, "research_complete")
        self.assertTrue(persisted.deep_checkpoint_trajectory_signature)
        self.assertEqual(persisted.status, "pending_deep")
        self.assertEqual(persisted.technical_explanation, "已完成综合")
        self.assertEqual(
            next(item for item in persisted.evidence if item.source == "reddit").summary,
            "",
        )

        # Simulate a crash after deep work, before report/outbox assembly.
        self.orchestrator.enricher.run.reset_mock()
        self.orchestrator.synthesizer.run.reset_mock()
        self.orchestrator.trajectory.run.reset_mock()
        resumed, budget, failed = await self.run_deep([persisted])
        self.assertEqual((budget, failed), ([], []))
        self.assertEqual(len(resumed), 1)
        self.orchestrator.enricher.run.assert_not_awaited()
        self.orchestrator.synthesizer.run.assert_not_awaited()
        self.orchestrator.trajectory.run.assert_not_awaited()

    async def test_completed_checkpoint_invalidates_on_relevant_support(self):
        self.stages()
        completed, _, _ = await self.run_deep()
        self.assertEqual(len(completed), 1)
        restored = self.store.load_pending_deep_candidates()[0]
        self.orchestrator.enricher.run.reset_mock()
        self.orchestrator.synthesizer.run.reset_mock()
        self.orchestrator.trajectory.run.reset_mock()
        next_completed, _, _ = await self.run_deep(
            [restored], support=[supporting()]
        )
        self.assertEqual(len(next_completed), 1)
        self.orchestrator.enricher.run.assert_awaited_once()
        self.orchestrator.synthesizer.run.assert_awaited_once()
        self.orchestrator.trajectory.run.assert_awaited_once()

    async def test_trajectory_policy_change_reuses_synthesis_only(self):
        self.stages()
        completed, _, _ = await self.run_deep()
        self.assertEqual(len(completed), 1)
        restored = self.store.load_pending_deep_candidates()[0]
        original_read = Path.read_bytes

        def changed_person_skill(path):
            result = original_read(path)
            if path.parts[-2:] == ("researcher_trajectory", "SKILL.md"):
                return result + b"\npolicy changed"
            return result

        self.orchestrator.enricher.run.reset_mock()
        self.orchestrator.synthesizer.run.reset_mock()
        self.orchestrator.trajectory.run.reset_mock()
        with patch.object(Path, "read_bytes", changed_person_skill):
            # No complete checkpoint may be reused under a new person policy.
            self.assertIsNone(self.store.load_synthesized_checkpoint(restored))
            resumed, _, _ = await self.run_deep([restored])
        self.assertEqual(len(resumed), 1)
        self.orchestrator.enricher.run.assert_awaited_once()
        self.orchestrator.synthesizer.run.assert_awaited_once()
        self.orchestrator.trajectory.run.assert_awaited_once()

    async def test_complete_checkpoint_compare_and_swap_rejects_newer_route(self):
        self.stages(trajectory=AsyncMock(side_effect=RuntimeError("profile service")))
        _, _, failed = await self.run_deep()
        self.store.save_candidates(failed)
        synthesized = self.store.load_synthesized_checkpoint(
            self.store.load_pending_deep_candidates()[0]
        )
        self.assertIsNotNone(synthesized)
        completed = copy.deepcopy(synthesized)
        newer = copy.deepcopy(synthesized)
        newer.name = "Newer route revision"
        self.store.save_candidates([newer])
        with self.assertRaisesRegex(ValueError, "冲突"):
            self.store.save_completed_deep_checkpoint(synthesized, completed)
        self.assertEqual(
            self.store.load_pending_deep_candidates()[0].name,
            "Newer route revision",
        )

    async def test_later_incomplete_objective_verdict_does_not_lose_person_work(self):
        self.stages()
        completed, _, _ = await self.run_deep()
        self.assertEqual(len(completed), 1)
        restored = self.store.load_pending_deep_candidates()[0]
        restored.rubric_assessment = {
            "decision": "incomplete", "failure_reason": "objective evidence missing"
        }
        self.store.save_candidates([restored])
        cached = self.store.load_synthesized_checkpoint(restored)
        self.assertEqual(cached.deep_checkpoint_stage, "research_complete")
        self.assertEqual(cached.rubric_assessment["decision"], "report")

    async def test_real_trajectory_api_failure_is_not_recorded_as_success(self):
        failed_call = AsyncMock(side_effect=RuntimeError("profile provider down"))
        client = SimpleNamespace(chat=SimpleNamespace(
            completions=SimpleNamespace(create=failed_call)
        ))
        analyzer = ResearcherTrajectoryAnalyzer(client=client, model="offline")
        profile = ResearcherProfile(
            name="Example Researcher",
            representative_works=[{"title": "Test paper"}],
        )
        candidate = copy.deepcopy(self.candidate)
        candidate.researchers = [profile]
        with self.assertRaisesRegex(RuntimeError, "研究轨迹自动分析失败"):
            await analyzer.run([candidate])
        failed_call.assert_awaited_once()
        self.assertEqual(profile.research_trajectory, "")

    async def test_timeout_after_synthesis_preserves_checkpoint(self):
        async def stalled(_values):
            await asyncio.Event().wait()

        self.stages(trajectory=AsyncMock(side_effect=stalled))
        completed, budget, failed = await self.run_deep(seconds=0.05)
        self.assertEqual((completed, failed), ([], []))
        self.assertEqual(len(budget), 1)
        self.assertEqual(budget[0].deep_checkpoint_stage, "synthesized")
        self.store.save_candidates(budget)
        restored = ParadigmStore(self.path).load_pending_deep_candidates()[0]
        self.assertIsNotNone(self.store.load_synthesized_checkpoint(restored))

    async def test_new_relevant_support_restarts_synthesis_with_new_evidence(self):
        self.stages(trajectory=AsyncMock(side_effect=RuntimeError("profile service")))
        _, _, failed = await self.run_deep()
        self.store.save_candidates(failed)
        restored = self.store.load_pending_deep_candidates()[0]
        self.orchestrator.trajectory.run = AsyncMock(side_effect=lambda values: values)
        completed, budget, failed = await self.run_deep(
            [restored], support=[supporting()]
        )
        self.assertEqual((budget, failed), ([], []))
        self.assertEqual(self.orchestrator.enricher.run.await_count, 2)
        self.assertEqual(self.orchestrator.synthesizer.run.await_count, 2)
        self.assertTrue(any(
            item.source == "independent-lab" for item in completed[0].evidence
        ))

    async def test_unrelated_support_does_not_invalidate_route(self):
        self.stages(trajectory=AsyncMock(side_effect=RuntimeError("profile service")))
        _, _, failed = await self.run_deep()
        self.store.save_candidates(failed)
        restored = self.store.load_pending_deep_candidates()[0]
        other = TechnicalEvidence(
            source="independent-lab",
            evidence_type=EvidenceType.INDEPENDENT_REPLICATION,
            title="Unrelated chemistry procedure",
            url="https://research.example.org/unrelated",
            summary="A different topic with no route overlap",
        )
        self.orchestrator.enricher.run.reset_mock()
        self.orchestrator.synthesizer.run.reset_mock()
        self.orchestrator.trajectory.run = AsyncMock(side_effect=lambda values: values)
        completed, _, _ = await self.run_deep([restored], support=[other])
        self.assertEqual(len(completed), 1)
        self.orchestrator.enricher.run.assert_not_awaited()
        self.orchestrator.synthesizer.run.assert_not_awaited()

    async def test_budget_deferral_preserves_new_support_and_clears_stale_stage(self):
        self.stages(trajectory=AsyncMock(side_effect=RuntimeError("profile service")))
        _, _, failed = await self.run_deep()
        self.store.save_candidates(failed)
        restored = self.store.load_pending_deep_candidates()[0]
        completed, budget, execution = await self.run_deep(
            [restored], support=[supporting(temporary=True)], seconds=-1
        )
        self.assertEqual((completed, execution), ([], []))
        self.assertEqual(len(budget), 1)
        self.assertEqual(budget[0].deep_checkpoint_stage, "")
        retained = next(item for item in budget[0].evidence if item.source == "independent-lab")
        self.assertEqual((retained.summary, retained.authors), ("", []))
        self.assertNotIn("body", retained.raw)
        self.assertEqual(retained.raw["metric_delta"], {"stars": 25.0})
        self.store.save_candidates(budget)
        next_run = self.store.load_pending_deep_candidates()[0]
        self.assertIsNone(self.store.load_synthesized_checkpoint(next_run))
        self.assertTrue(any(item.source == "independent-lab" for item in next_run.evidence))

    async def test_unstarted_budget_deferral_also_preserves_support(self):
        self.stages()
        completed, budget, failed = await self.run_deep(
            support=[supporting()], seconds=-1
        )
        self.assertEqual((completed, failed), ([], []))
        self.assertEqual(len(budget), 1)
        self.assertTrue(any(item.source == "independent-lab" for item in budget[0].evidence))
        self.orchestrator.enricher.run.assert_not_awaited()

    async def test_unstarted_queue_tail_and_safety_deferral_keep_support(self):
        self.stages()
        second = copy.deepcopy(self.candidate)
        second.key = "route-2"
        completed, budget, failed = await self.run_deep(
            [self.candidate, second], support=[supporting()], seconds=-1
        )
        self.assertEqual((completed, failed), ([], []))
        self.assertEqual(len(budget), 2)
        self.assertTrue(all(
            any(item.source == "independent-lab" for item in candidate.evidence)
            for candidate in budget
        ))
        safety = _deferred_deep_snapshot(
            self.store, copy.deepcopy(self.candidate), [supporting()]
        )
        self.assertTrue(any(
            item.source == "independent-lab" for item in safety.evidence
        ))

    async def test_material_metric_change_restarts_synthesis(self):
        self.stages(trajectory=AsyncMock(side_effect=RuntimeError("profile service")))
        _, _, failed = await self.run_deep(support=[supporting(score=1)])
        self.store.save_candidates(failed)
        restored = self.store.load_pending_deep_candidates()[0]
        self.orchestrator.trajectory.run = AsyncMock(side_effect=lambda values: values)
        completed, _, _ = await self.run_deep(
            [restored], support=[supporting(score=100)]
        )
        self.assertEqual(len(completed), 1)
        self.assertEqual(self.orchestrator.synthesizer.run.await_count, 2)

    async def test_support_signature_ignores_order_and_identical_duplicates(self):
        first = supporting()
        second = supporting(score=25)
        second.url = "https://research.example.org/second-replication"
        left = EvidenceEnricher.supporting_signature(
            self.candidate, [first, second]
        )
        right = EvidenceEnricher.supporting_signature(
            self.candidate, [second, first, copy.deepcopy(first)]
        )
        self.assertEqual(left, right)

    async def test_all_sqlite_candidate_and_support_writes_scrub_raw_body(self):
        temporary = supporting(temporary=True)
        self.store.mark_evidence([temporary], analyzed=False)
        pending = copy.deepcopy(self.candidate)
        pending.evidence.append(temporary)
        pending.status = "observe"
        self.store.mark_evidence([pending.evidence[0]], analyzed=True)
        self.store.save_candidates([pending])
        self.store.enqueue_report([pending], {**COMPLETED_RESEARCH, "origin_count": 1}, report_date="2026-09-23")
        with sqlite3.connect(self.path) as conn:
            values = [row[0] for row in conn.execute(
                "SELECT payload_json FROM evidence_state "
                "UNION ALL SELECT payload_json FROM paradigms "
                "UNION ALL SELECT candidate_payload_json FROM report_outbox"
            )]
        self.assertFalse(any("Hidden raw user body" in value for value in values))
        self.assertFalse(any("Temporary user body" in value for value in values))
        self.assertEqual(temporary.raw["body"], "Hidden raw user body")
        restored = self.store.load_candidate_snapshots({pending.key})[0]
        persisted = next(item for item in restored.evidence if item.source == "independent-lab")
        self.assertEqual(persisted.raw["metric_delta"], {"stars": 25.0})

    async def test_changed_source_revision_rejects_stale_synthesis(self):
        async def synthesize(values):
            changed = origin("new source revision")
            self.store.observe_origins([changed])
            values[0].rubric_assessment = {"decision": "report"}
            return values

        self.stages()
        self.orchestrator.synthesizer.run = AsyncMock(side_effect=synthesize)
        completed, budget, failed = await self.run_deep()
        self.assertEqual((completed, budget), ([], []))
        self.assertEqual(len(failed), 1)
        self.assertEqual(failed[0].deep_checkpoint_stage, "")
        self.assertIsNone(self.store.load_synthesized_checkpoint(self.candidate))

    async def test_incomplete_rubric_is_not_cached(self):
        self.stages(decision="incomplete")
        completed, budget, failed = await self.run_deep()
        self.assertEqual((budget, failed), ([], []))
        self.assertEqual(len(completed), 1)
        self.assertEqual(completed[0].deep_checkpoint_stage, "")
        self.assertIsNone(self.store.load_synthesized_checkpoint(self.candidate))

    async def test_new_origin_clears_old_checkpoint(self):
        self.stages(trajectory=AsyncMock(side_effect=RuntimeError("profile service")))
        _, _, failed = await self.run_deep()
        self.store.save_candidates(failed)
        old = self.store.load_pending_deep_candidates()[0]
        self.assertIsNotNone(self.store.load_synthesized_checkpoint(old))
        changed = copy.deepcopy(old)
        changed.evidence.append(TechnicalEvidence(
            source="official", evidence_type=EvidenceType.TECHNICAL_BLOG,
            title="New input", url="https://example.com/new-input",
        ))
        changed.deep_checkpoint_stage = ""
        changed.deep_checkpoint_input_signature = ""
        self.store.save_candidates([changed])
        self.assertIsNone(self.store.load_synthesized_checkpoint(old))

    async def test_policy_revision_invalidates_prior_synthesis(self):
        self.stages(trajectory=AsyncMock(side_effect=RuntimeError("profile service")))
        _, _, failed = await self.run_deep()
        self.store.save_candidates(failed)
        restored = self.store.load_pending_deep_candidates()[0]
        self.assertIsNotNone(self.store.load_synthesized_checkpoint(restored))
        with patch("database.paradigm_store.DEEP_SYNTHESIS_CHECKPOINT_VERSION", 2):
            self.assertIsNone(self.store.load_synthesized_checkpoint(restored))

        original_read = Path.read_bytes

        def updated_prompt(path):
            result = original_read(path)
            if path.parts[-2:] == ("paradigm_synthesis", "SKILL.md"):
                return result + b"\nnew rule"
            return result

        with patch.object(Path, "read_bytes", updated_prompt):
            self.assertIsNone(self.store.load_synthesized_checkpoint(restored))

    async def test_week_old_checkpoint_refreshes_external_evidence(self):
        self.stages(trajectory=AsyncMock(side_effect=RuntimeError("profile service")))
        _, _, failed = await self.run_deep()
        expired = failed[0]
        expired.deep_checkpoint_created_at = (
            datetime.now(timezone.utc) - timedelta(days=7)
        ).isoformat()
        self.store.save_candidates([expired])
        self.assertIsNone(self.store.load_synthesized_checkpoint(expired))

    async def test_downstream_incomplete_verdict_keeps_technical_synthesis(self):
        self.stages(trajectory=AsyncMock(side_effect=RuntimeError("profile service")))
        _, _, failed = await self.run_deep()
        failed[0].rubric_assessment = {
            "decision": "incomplete", "failure_reason": "objective evidence missing"
        }
        self.store.save_candidates(failed)
        restored = self.store.load_pending_deep_candidates()[0]
        cached = self.store.load_synthesized_checkpoint(restored)
        self.assertIsNotNone(cached)
        self.assertEqual(cached.rubric_assessment["decision"], "report")

    async def test_compare_and_swap_does_not_overwrite_newer_candidate(self):
        current = self.store.load_pending_deep_candidates()[0]
        newer = copy.deepcopy(current)
        newer.name = "Newer name from another origin"
        self.store.save_candidates([newer])
        synthesized = copy.deepcopy(current)
        synthesized.rubric_assessment = {"decision": "report"}
        synthesized.deep_checkpoint_support_signature = (
            EvidenceEnricher.supporting_signature(current, [])
        )
        with self.assertRaisesRegex(ValueError, "冲突"):
            self.store.save_synthesized_checkpoint(current, synthesized)
        self.assertEqual(
            self.store.load_pending_deep_candidates()[0].name,
            "Newer name from another origin",
        )

    async def test_batch_trajectory_failure_reuses_peer_synthesis_when_bisected(self):
        second = ParadigmCandidate(
            key="route-2", name="Route 2", thesis="test",
            problem_shift="test", mechanism="test", status="pending_deep",
            evidence=[TechnicalEvidence(
                source="official", evidence_type=EvidenceType.TECHNICAL_BLOG,
                title="Second input", url="https://example.com/second",
            )],
        )
        self.store.save_candidates([second])

        async def enrich(values, _supporting):
            return values

        async def synthesize(values):
            for value in values:
                value.rubric_assessment = {"decision": "report"}
            return values

        async def trajectory(values):
            if any(value.key == "route-1" for value in values):
                raise RuntimeError("one profile failed")
            return values

        self.orchestrator.enricher = SimpleNamespace(run=AsyncMock(side_effect=enrich))
        self.orchestrator.synthesizer = SimpleNamespace(run=AsyncMock(side_effect=synthesize))
        self.orchestrator.trajectory = SimpleNamespace(run=AsyncMock(side_effect=trajectory))
        with patch("config.PARADIGM_DEEP_BATCH_SIZE", 2):
            completed, budget, failed = await self.run_deep([self.candidate, second])
        self.assertEqual((len(completed), len(budget), len(failed)), (1, 0, 1))
        self.assertEqual(completed[0].key, "route-2")
        self.assertEqual(self.orchestrator.enricher.run.await_count, 1)
        self.assertEqual(self.orchestrator.synthesizer.run.await_count, 1)


if __name__ == "__main__":
    unittest.main()
