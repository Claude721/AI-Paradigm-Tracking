"""Execution completeness is not universal success. All I/O is offline."""
import copy
import asyncio
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

import config
from database.paradigm_store import ParadigmStore, _deep_checkpoint_input_signature
from paradigms.analyzer import ParadigmSynthesizer
from paradigms.completion import ResearchNotCompleteError, require_completed_research
from paradigms.delivery_policy import delivery_proof, execution_result_kind
from paradigms.enrichment import EvidenceEnricher
from paradigms.models import candidate_from_dict, ParadigmExtraction, EvidenceType, TechnicalEvidence
from paradigms.rubric import load_rubric
from reports.paradigm_generator import ParadigmReportGenerator
from tests import test_v0_delivery as fixture
from tests.test_paradigm_pipeline import candidate, rubric_answers, rubric_assessment


class ExecutionDeliveryTests(unittest.IsolatedAsyncioTestCase):
    setUp = fixture.V0DeliveryTests.setUp
    process = fixture.V0DeliveryTests.process

    def distinct_process(self, total=3, *, fail=1, fail_origin=1, reject=False):
        process = self.process(fixture.batch([fixture.origin(i) for i in range(1, 4)]))
        async def extract(items):
            return [ParadigmExtraction(evidence=item, is_candidate=True,
                canonical_name=f"Distinct mechanism {item.title}-{index}",
                thesis="可核验机制", problem_shift=f"Distinct interface {item.title}-{index}",
                mechanism=f"Distinct intervention {item.title}-{index}",
                keywords=[f"unique-{item.fingerprint}-{index}"], innovation_types=["architecture"],
                rubric_assessment=rubric_assessment(stage="screening"))
                for item in items for index in range(total // 3 + (item.title == fixture.origin(1).title) * (total % 3))]
        # Multiple mechanisms only come from a formal report.
        for item in process.discovery.run.return_value.origins:
            item.raw["origin_kind"] = "technical_report"
        base = process.synthesizer.run
        visits = set()
        async def synthesize(items):
            visits.update(item.key for item in items)
            if any(item.name == f"Distinct mechanism {fixture.origin(fail_origin).title}-{fail}" for item in items):
                raise RuntimeError("offline single-candidate failure")
            values = await base(items)
            if reject:
                weak = {q["id"] for q in load_rubric()["common_criteria"] + load_rubric()["type_criteria"]["architecture"]}
                for item in values:
                    item.rubric_assessment = rubric_assessment(stage="final", weak_ids=weak)
            return values
        process.analyzer.run = extract
        process.synthesizer.run = synthesize
        return process, visits

    async def test_failed_peer_still_allows_real_report_outbox_and_confirmed_mail(self):
        process, visits = self.distinct_process(total=3, fail=0, fail_origin=2)
        base = process.synthesizer.run
        async def one_report(items):
            values = await base(items)
            for item in values:
                if fixture.origin(3).title in item.name:
                    weak = {q["id"] for q in load_rubric()["common_criteria"] + load_rubric()["type_criteria"]["architecture"]}
                    item.rubric_assessment = rubric_assessment(stage="final", weak_ids=weak)
            return values
        process.synthesizer.run = one_report
        # Reuse the full main -> real report renderer -> outbox -> mocked SMTP
        # acceptance harness, now with a failed deep peer and an observed peer.
        with patch.object(self, "process", return_value=process):
            await fixture.V0DeliveryTests.test_nonempty_pipeline_report_and_confirmed_delivery_with_source_failure(self)
        self.assertEqual(len(visits), 3)
        self.assertEqual(len(self.store.load_pending_deep_candidates()), 1)
        with self.store._connect() as conn:
            import json
            stats = json.loads(conn.execute("SELECT stats_json FROM report_outbox WHERE status='delivered'").fetchone()[0])
        self.assertEqual((stats["delivery_scope"]["deep_completed"], stats["delivery_scope"]["deep_failed"]), (2, 1))
        content = (self.path / f"paradigm_radar_{stats['report_date']}.md").read_text()
        self.assertIn("失败 1", content)
        self.assertNotIn("阶段性", content)

    async def test_all_68_candidates_attempted_one_failure_does_not_hold_67_routes(self):
        process, visits = self.distinct_process(68)
        stats = await process.run(reference_time=fixture.NOW)
        proof = stats["delivery_scope"]
        self.assertEqual(len(visits), 68)
        self.assertEqual((proof["deep_attempted"], proof["deep_completed"], proof["deep_failed"], proof["deep_unattempted"]), (68, 67, 1, 0))
        self.assertEqual(len(process.pending_delivery), 67)
        require_completed_research(stats, candidates=process.pending_delivery)
        job = self.store.enqueue_report(process.pending_delivery, stats, report_date=stats["report_date"])
        self.assertEqual(len(job.candidates), 67)
        failed = self.store.load_pending_deep_candidates()
        self.assertEqual(len(failed), 1)
        self.assertNotEqual(failed[0].rubric_assessment.get("decision"), "reject")
        self.store.mark_delivery_delivered(job.delivery_key, self.path / "offline-report.md")
        self.assertIsNone(self.store.campaigns.active())
        with self.store._connect() as conn:
            delivered = conn.execute("SELECT COUNT(*) FROM report_deliveries").fetchone()[0]
        self.assertEqual(delivered, 67)
        self.assertEqual(len(self.store.load_pending_deep_candidates()), 1)

    async def test_empty_selection_with_local_failure_has_bounded_conclusion(self):
        process, visits = self.distinct_process(total=3, fail=0, reject=True)
        stats = await process.run(reference_time=fixture.NOW)
        self.assertEqual(len(visits), 3)
        self.assertEqual(stats["delivery_scope"]["deep_failed"], 1)
        self.assertEqual(stats["result_kind"], "complete_no_selection")
        require_completed_research(stats, candidates=[])
        self.store.enqueue_report([], stats, report_date=stats["report_date"])
        path = await ParadigmReportGenerator(self.path, client=False).generate([], stats)
        content = path.read_text()
        self.assertIn("本期没有入选路线", content)
        self.assertIn("执行失败", content)
        self.assertNotIn("阶段性", content)
        self.assertNotIn("本周没有重要技术", content)

    async def test_total_deep_failure_is_not_successful_empty(self):
        process, _ = self.distinct_process()
        process.synthesizer.run = AsyncMock(side_effect=RuntimeError("offline all-model outage"))
        stats = await process.run(reference_time=fixture.NOW)
        self.assertEqual(stats["delivery_scope"]["deep_attempted"], 3)
        self.assertEqual(stats["delivery_scope"]["deep_failed"], 3)
        self.assertEqual(stats["result_kind"], "research_blocked")
        with self.assertRaisesRegex(ResearchNotCompleteError, "没有成功的研究判断"):
            self.store.enqueue_report([], stats, report_date=stats["report_date"])

    async def test_started_candidate_timeout_is_local_failure_not_unattempted(self):
        process, visits = self.distinct_process(total=3, fail=-1)
        base = process.synthesizer.run
        async def timeout_one(items):
            if any(fixture.origin(2).title in item.name for item in items):
                visits.update(item.key for item in items)
                raise asyncio.TimeoutError("offline started-request timeout")
            return await base(items)
        process.synthesizer.run = timeout_one
        stats = await process.run(reference_time=fixture.NOW)
        self.assertEqual(len(visits), 3)
        self.assertEqual((stats["delivery_scope"]["deep_attempted"], stats["delivery_scope"]["deep_failed"], stats["delivery_scope"]["deep_unattempted"]), (3, 1, 0))
        self.store.enqueue_report(process.pending_delivery, stats, report_date=stats["report_date"])

    async def test_unattempted_object_cannot_be_hidden_by_counts_or_nonempty_report(self):
        process, _ = self.distinct_process(total=3, fail=0)
        stats = await process.run(reference_time=fixture.NOW)
        campaign_id = stats["research_campaign"]["campaign_id"]
        # New input revision has no actual execution receipt. An old failure is
        # not an attempt against this version, even with healthy report peers.
        failed = self.store.load_pending_deep_candidates()[0]
        failed.screening_rubric["input_changed"] = True
        self.store.save_candidates([failed])
        ledger = self.store.campaigns.reconcile(campaign_id)
        self.assertEqual(ledger["deep_unattempted"], 1)
        forged = copy.deepcopy(stats)
        forged["research_campaign"] = ledger
        forged["delivery_scope"] = delivery_proof(forged, process.pending_delivery, ledger, minimum_sources=2)
        forged["result_kind"] = "complete_memo"
        with self.assertRaisesRegex(ResearchNotCompleteError, "未尝试"):
            require_completed_research(forged, candidates=process.pending_delivery)
        with self.assertRaises(ResearchNotCompleteError):
            self.store.enqueue_report(process.pending_delivery, stats, report_date=stats["report_date"])

    async def test_failure_receipt_cannot_be_forged_without_durable_attempt(self):
        process, _ = self.distinct_process(total=3, fail=0)
        stats = await process.run(reference_time=fixture.NOW)
        key = stats["research_campaign"]["campaign_id"]
        with self.store._connect() as conn:
            conn.execute("DELETE FROM research_attempts WHERE campaign_id=? AND outcome='execution_failed'", (key,))
        with self.assertRaises(ResearchNotCompleteError):
            self.store.enqueue_report(process.pending_delivery, stats, report_date=stats["report_date"])

    async def test_v3_plan_upgrade_keeps_clock_tasks_and_real_failures(self):
        item = fixture.origin()
        old = self.process(fixture.batch([item]), omit={item.fingerprint})
        old.delivery_contract_version = 3
        stats = await old.run(reference_time=fixture.NOW)
        key = stats["research_campaign"]["campaign_id"]
        before = self.store.campaigns.snapshot(key)
        migrated = self.store.campaigns.upgrade_execution_contract(key)
        after = self.store.campaigns.snapshot(key)
        self.assertEqual(migrated.stats["delivery_contract_version"], 4)
        self.assertEqual(migrated.reference_time, fixture.NOW)
        self.assertEqual(migrated.status, "researching")
        self.assertEqual((after["origin_planned"], after["origin_completed"], after["pending_total_count"]),
                         (before["origin_planned"], before["origin_completed"], before["pending_total_count"]))
        self.assertEqual(after["origin_failed"], 1)

    async def test_budget_or_interrupted_attempt_is_not_execution_success(self):
        process, _ = self.distinct_process(total=3, fail=0)
        stats = await process.run(reference_time=fixture.NOW)
        key = stats["research_campaign"]["campaign_id"]
        for outcome in ("budget_deferred", "interrupted", "running", "dependency_pending"):
            with self.store._connect() as conn:
                conn.execute("UPDATE research_attempts SET outcome=? WHERE campaign_id=? AND object_key=?", (outcome, key, self.store.load_pending_deep_candidates()[0].key))
            ledger = self.store.campaigns.snapshot(key)
            self.assertEqual(ledger["deep_failed"], 0)
            self.assertEqual(ledger["deep_unattempted"], 1)
            self.assertEqual(execution_result_kind(ledger, process.pending_delivery), "research_blocked")

    async def test_started_refresh_timeout_is_also_a_local_failure(self):
        process = self.process()
        process.enricher.refresh = AsyncMock(side_effect=asyncio.TimeoutError("offline refresh timeout"))
        done, unchanged, budget, failed, attempted = await process._refresh_in_batches(
            [candidate([fixture.origin()])], [], time.monotonic() + 30)
        self.assertEqual((done, unchanged, budget, len(failed), attempted), ([], [], [], 1, 1))


class EvidenceJudgmentTests(unittest.TestCase):
    def test_primary_exclusion_preserves_frozen_inputs_and_cas_protection(self):
        with tempfile.TemporaryDirectory() as directory:
            store = ParadigmStore(Path(directory) / "state.db")
            origins = store.observe_origins([fixture.origin(1), fixture.origin(2)])[0]
            store.mark_evidence(origins, analyzed=True)
            original = candidate(origins)
            original.status = "pending_deep"
            store.save_candidates([original])
            reviewed = copy.deepcopy(original)
            ParadigmSynthesizer._apply_synthesis_payload(reviewed, {
                "mental_model": candidate().mental_model, "innovation_types": ["architecture"],
                "rubric_answers": rubric_answers(["architecture"]), "excluded_evidence_indices": [1],
                "mechanism_support_evidence_indices": [0],
            }, visible_evidence_indices={0, 1})
            self.assertEqual(len(reviewed.evidence), 1)
            self.assertEqual(reviewed.excluded_evidence[0].fingerprint, origins[1].fingerprint)
            self.assertEqual(_deep_checkpoint_input_signature(original), _deep_checkpoint_input_signature(reviewed))
            support = [TechnicalEvidence(source="independent-blog", evidence_type=EvidenceType.SECONDARY_INTERPRETATION,
                title="Direct discussion", url="https://example.org/review", summary=origins[1].url)]
            self.assertEqual(EvidenceEnricher.supporting_signature(original, support), EvidenceEnricher.supporting_signature(reviewed, support))
            reviewed.deep_checkpoint_support_signature = "offline-support"
            store.save_synthesized_checkpoint(original, reviewed)
            restored = store.load_candidate_snapshots([original.key])[0]
            self.assertEqual(len(restored.excluded_evidence), 1)
            self.assertEqual(candidate_from_dict(restored.to_dict()).to_dict(), restored.to_dict())
            self.assertIsNotNone(store.load_synthesized_checkpoint(restored))
            # A real source revision change is still prohibited, including a
            # source which the judgment excluded as irrelevant.
            restored.excluded_evidence[0].source_revision = "foreign-revision"
            with self.assertRaisesRegex(ValueError, "输入发生变化"):
                store.save_synthesized_checkpoint(reviewed, restored)

    def test_unseen_exclusion_is_not_allowed(self):
        value = candidate([fixture.origin(1), fixture.origin(2)])
        with self.assertRaisesRegex(ValueError, "未展开"):
            ParadigmSynthesizer._apply_synthesis_payload(value, {
                "mental_model": value.mental_model, "innovation_types": ["architecture"],
                "rubric_answers": rubric_answers(["architecture"]), "excluded_evidence_indices": [1],
            }, visible_evidence_indices={0})
