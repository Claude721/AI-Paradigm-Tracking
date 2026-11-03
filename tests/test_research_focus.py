"""Opportunity screening precedes expensive calls; fixed clock, no network."""
import copy
import json
import tempfile
import time
import unittest
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import config
from agents.paradigm_orchestrator import ParadigmOrchestrator
from database.origin_state import source_signature
from database.paradigm_store import ParadigmStore
from paradigms.analyzer import ParadigmAnalyzer
from paradigms.enrichment import EvidenceEnricher
from paradigms.models import EvidenceType, TechnicalEvidence, candidate_from_dict
from paradigms.research_focus import assess, probe, benefit_receipt, receipt_current, select
from paradigms.scoring import score_candidate, is_reportable
from runtime_clock import research_window
from tests.test_propagation_policy import route, post, NOW
from tests import test_v0_delivery as fixture

SUMMARY = "We compare against a matched Transformer baseline. In measured experiments, latency falls from 100ms to 60ms at equal accuracy."
BENEFIT = {"kind": "measured_comparison", "baseline_quote": "a matched Transformer baseline",
           "result_quote": "latency falls from 100ms to 60ms at equal accuracy"}


def focus_process():
    process = ParadigmOrchestrator.__new__(ParadigmOrchestrator)
    process.high_signal_discovery_lookback_days = 30
    process.ordinary_discovery_lookback_days = 7
    process.analyzer = SimpleNamespace(enable_batch_prefilter=True, origin_benefits={},
        prefetch_origin_eligibility=AsyncMock(side_effect=lambda items: {x.fingerprint: ("full_review", "可能有机制") for x in items}))
    process.enricher = SimpleNamespace(_attach_support=EvidenceEnricher._attach_support,
        direction_signals=SimpleNamespace(attach=AsyncMock()))
    return process


class FocusContracts(unittest.TestCase):
    def setUp(self):
        clock = research_window(NOW); clock.__enter__()
        self.addCleanup(clock.__exit__, None, None, None)

    def test_quiet_novel_work_is_watch_not_technical_rejection(self):
        item = route(age=1)
        before = copy.deepcopy(item.to_dict())
        self.assertEqual(assess(item)["decision"], "watch")
        self.assertEqual(item.to_dict(), before)
        self.assertEqual(assess(item, source_health={"media": "failed_TimeoutError"})["decision"], "watch")
        self.assertIn("不等于", assess(item)["reason"])

    def test_benefit_requires_exact_quantitative_source_comparison_not_model_prose(self):
        origin = route(age=1).evidence[0]; origin.summary = SUMMARY
        self.assertTrue(benefit_receipt(BENEFIT, origin))
        for mutation in ({"kind": "future_prediction"}, {"result_quote": "模型性能显著提升"},
                         {"baseline_quote": "not in this source"}, {"result_quote": "a matched Transformer baseline"}):
            self.assertFalse(benefit_receipt(BENEFIT | mutation, origin))

    def test_quiet_tested_work_can_be_studied_without_kol_or_brand(self):
        item = route(age=1); item.evidence[0].summary = SUMMARY
        benefit = benefit_receipt(BENEFIT, item.evidence[0])
        self.assertEqual(assess(item, benefit=benefit)["decision"], "research")
        item.evidence[0].summary += " Revised claim."
        self.assertEqual(assess(item, benefit=benefit)["decision"], "watch")

    def test_fame_zero_interaction_and_index_only_do_not_admit(self):
        item = route(age=1); item.publisher_tier = "established"
        share = post(item); share.metrics = {}; item.evidence.append(share)
        self.assertEqual(assess(item)["decision"], "watch")
        share.metrics = {"author_followers": 1000000}
        self.assertEqual(assess(item)["decision"], "watch")
        share.raw["indexed_discovery_only"] = True; share.metrics = {"score": 1000000}
        self.assertEqual(assess(item)["decision"], "watch")

    def test_measured_public_attention_not_a_technical_pass(self):
        item = route(age=1); item.evidence.append(post(item))
        self.assertEqual(assess(item)["decision"], "research")
        self.assertNotIn("rubric_assessment", assess(item))
        item.evidence[-1].published_at = (NOW - timedelta(days=10)).isoformat()
        self.assertEqual(assess(item)["decision"], "watch")

    def test_duplicates_do_not_become_multiple_low_attention_posts(self):
        item = route(age=1); share = post(item); share.metrics = {}
        item.evidence.extend([share, copy.deepcopy(share)])
        self.assertEqual(assess(item)["decision"], "watch")
        other = copy.deepcopy(share); other.url += "2"; item.evidence.append(other)
        self.assertEqual(assess(item)["decision"], "watch")
        other.authors = ["Another Member"]
        self.assertEqual(assess(item)["decision"], "research")

    def test_original_code_attention_can_qualify_without_manufacturing_replication(self):
        item = route(age=1)
        origin = item.evidence[0]
        origin.source = "official-repository-release"
        origin.evidence_type = EvidenceType.ORIGINAL_IMPLEMENTATION
        origin.url = "https://github.com/verified-team/new-mechanism"
        origin.metrics = {"stars": 100}
        value = assess(item)
        self.assertEqual(value["decision"], "research")
        self.assertIn("不代表独立复现", value["basis"][0]["scope"])

    def test_hf_synced_paper_needs_real_metrics_and_same_work(self):
        item = route(age=1)
        evidence = TechnicalEvidence(source="huggingface-papers", evidence_type=EvidenceType.COMMUNITY_DISCUSSION,
            title=item.evidence[0].title, url="https://huggingface.co/papers/2609.00001",
            identifiers=item.evidence[0].identifiers, published_at=(NOW-timedelta(days=1)).isoformat(), metrics={"upvotes": 0})
        item.evidence.append(evidence)
        self.assertEqual(assess(item)["decision"], "watch")
        evidence.metrics["upvotes"] = 20
        self.assertEqual(assess(item)["decision"], "research")
        evidence.title = "Another paper"; evidence.identifiers = {"arxiv": "2609.99999"}
        self.assertEqual(assess(item)["decision"], "watch")

    def test_single_job_not_enough_two_direction_dimensions_can_guide_research(self):
        item = route(age=1)
        background = {"topics": [{"topic": "某方向", "selection_scope": "specific_direction", "jobs": [{"url": "https://jobs.example/1"}], "media": []}]}
        with patch("paradigms.research_focus.context_for_report", return_value=background):
            self.assertEqual(assess(item)["decision"], "watch")
            background["topics"][0]["media"] = [{"url": "https://news.example/financing"}]
            value = assess(item)
            self.assertEqual(value["decision"], "research")
            self.assertIn("方向级", value["basis"][0]["scope"])
            background["topics"][0]["selection_scope"] = "broad_domain"
            self.assertEqual(assess(item)["decision"], "watch")

    def test_incomplete_paper_counts_cannot_admit_with_one_job(self):
        item = route(age=1)
        background = {"topics": [{"topic": "某方向", "selection_scope": "specific_direction", "jobs": [{"url": "https://jobs.example/1"}],
            "paper_activity": {"status": "incomplete", "current_count": 200, "previous_count": 2}}]}
        with patch("paradigms.research_focus.context_for_report", return_value=background):
            self.assertEqual(assess(item)["decision"], "watch")

    def test_watch_retains_original_technical_judgment_and_roundtrips(self):
        item = route(age=1); score_candidate(item)
        verdict = copy.deepcopy(item.rubric_assessment)
        item.evidence[0].raw["research_focus"] = assess(item)
        with patch.object(config, "PARADIGM_RESEARCH_FOCUS_ENABLED", True):
            self.assertFalse(is_reportable(item))
            self.assertEqual(item.rubric_assessment, verdict)
            self.assertEqual(candidate_from_dict(item.to_dict()).to_dict(), item.to_dict())
        item.evidence[0].summary += " new version"
        self.assertFalse(receipt_current(item.evidence[0]))


class FocusPipelineTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        clock = research_window(NOW); clock.__enter__(); self.addCleanup(clock.__exit__, None, None, None)
        for name, value in (("PARADIGM_RESEARCH_FOCUS_ENABLED", True), ("PARADIGM_DIRECTION_SIGNALS_ENABLED", False)):
            control = patch.object(config, name, value); control.start(); self.addCleanup(control.stop)

    async def test_scope_filters_before_full_analyzer_and_never_backfills_quiet_tail(self):
        process = focus_process()
        quiet, loud = route(1, age=1), route(2, age=1)
        before = source_signature(quiet.evidence[0])
        stats = {}
        selected = await process._select_origin_scope([quiet.evidence[0], loud.evidence[0]],
            time.monotonic()+100, stats, supporting=[post(loud)])
        self.assertEqual([x.fingerprint for x in selected], [loud.evidence[0].fingerprint])
        self.assertEqual(stats["research_focus"]["watch"], 1)
        self.assertEqual(source_signature(quiet.evidence[0]), before)
        self.assertEqual(quiet.evidence[0].raw["research_focus"]["decision"], "watch")

    async def test_pending_watch_saved_without_completed_or_failed_deep_and_later_event_wakes(self):
        process = focus_process(); value = route(age=1); value.status = "pending_deep"
        with tempfile.TemporaryDirectory() as folder:
            process.store = ParadigmStore(Path(folder)/"state.db")
            observed, _, _ = process.store.observe_origins(value.evidence)
            value.evidence = observed
            process.store.save_candidates([value])
            original = process.store.load_candidate_snapshots([value.key])[0]
            kept, held, failed = await process._focus_before_deep([original], [], time.monotonic()+100, {})
            self.assertFalse(failed)
            self.assertFalse(kept); self.assertEqual(len(held), 1)
            saved = process.store.load_candidate_snapshots([value.key])[0]
            self.assertEqual(saved.status, "pending_deep")
            self.assertEqual(saved.deep_checkpoint_stage, "")
            self.assertEqual(saved.execution_failure_count, 0)
            # New week must re-check rather than use a permanent popularity ban.
            with research_window(NOW+timedelta(days=7)):
                event = post(saved); event.published_at = (NOW+timedelta(days=6)).isoformat()
                kept, held, failed = await process._focus_before_deep([saved], [event], time.monotonic()+100, {})
                self.assertFalse(failed)
                self.assertEqual(len(kept), 1); self.assertFalse(held)

    async def test_current_admission_does_not_repeat_cheap_call_or_free_queries(self):
        process = focus_process(); item = route(age=1)
        item.evidence.append(post(item)); item.evidence[0].raw["research_focus"] = assess(item)
        kept, held, failed = await process._focus_before_deep([item], [], time.monotonic()+100, {}, persist=False)
        self.assertFalse(failed)
        self.assertEqual(kept, [item]); self.assertFalse(held)
        process.analyzer.prefetch_origin_eligibility.assert_not_awaited()
        process.enricher.direction_signals.attach.assert_not_awaited()

    async def test_direction_timeout_is_unknown_not_global_exception(self):
        process = focus_process(); item = route(age=1)
        process.enricher.direction_signals.attach.side_effect = TimeoutError()
        with patch.object(config, "PARADIGM_DIRECTION_SIGNALS_ENABLED", True):
            selected = await select(item.evidence, [post(item)], enricher=process.enricher,
                analyzer=process.analyzer, deadline=time.monotonic()+100, stats={})
        self.assertEqual(len(selected), 1)
        self.assertIn("unknown", item.evidence[0].raw["research_focus"]["source_health"]["direction_context"])

    async def test_model_benefit_roundtrip_uses_existing_call_and_invalid_rows_only_lose_benefit(self):
        item = route(age=1).evidence[0]; item.summary = SUMMARY
        payload = {"decisions": [{"fingerprint": item.fingerprint, "decision": "full_review",
            "reason": "有机制", "tested_benefit": BENEFIT}]}
        response = SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=json.dumps(payload)), finish_reason="stop")], usage=None)
        analyzer = ParadigmAnalyzer(client=object(), model="offline")
        with patch("paradigms.analyzer.run_audit.chat_completion", AsyncMock(return_value=response)) as call:
            result = await analyzer.prefetch_origin_eligibility([item])
        self.assertEqual(result[item.fingerprint][0], "full_review")
        self.assertEqual(call.await_count, 1)
        self.assertTrue(analyzer.origin_benefits[item.fingerprint])
        self.assertEqual(assess(probe(item), benefit=analyzer.origin_benefits[item.fingerprint])["decision"], "research")

    async def test_benefit_cache_reused_without_second_model_request(self):
        item = route(age=1).evidence[0]; item.summary = SUMMARY
        payload = {"decisions": [{"fingerprint": item.fingerprint, "decision": "full_review",
            "reason": "有机制", "tested_benefit": BENEFIT}]}
        response = SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=json.dumps(payload)), finish_reason="stop")], usage=None)
        with tempfile.TemporaryDirectory() as folder:
            store = ParadigmStore(Path(folder)/"state.db")
            with patch("paradigms.analyzer.run_audit.chat_completion", AsyncMock(return_value=response)) as call:
                first = ParadigmAnalyzer(client=object(), model="offline", stage_cache=store.campaigns)
                await first.prefetch_origin_eligibility([item])
                again = ParadigmAnalyzer(client=object(), model="offline", stage_cache=store.campaigns)
                await again.prefetch_origin_eligibility([item])
            self.assertEqual(call.await_count, 1)
            self.assertEqual(first.origin_benefits, again.origin_benefits)

    async def test_current_week_watch_rechecks_new_support(self):
        process = focus_process(); item = route(age=1)
        item.evidence[0].raw["research_focus"] = assess(item)
        kept, held, failed = await process._focus_before_deep([item], [post(item)], time.monotonic()+100, {}, persist=False)
        self.assertFalse(failed)
        self.assertEqual(kept, [item]); self.assertFalse(held)

    async def test_old_origin_with_current_event_reaches_focus_outside_acquisition_window(self):
        process = focus_process(); item = route(age=90)
        item.evidence[0].raw["event_recall_key"] = "current-public-event"
        selected = await process._select_origin_scope(item.evidence, time.monotonic()+100,
            {}, supporting=[post(item)])
        self.assertEqual(selected, item.evidence)

    async def test_wrong_or_missing_model_benefit_keeps_technical_qualification(self):
        item = route(age=1).evidence[0]; item.summary = SUMMARY
        payload = {"decisions": [{"fingerprint": item.fingerprint, "decision": "full_review",
            "reason": "有机制", "tested_benefit": BENEFIT | {"result_quote": "invented 900% profit"}}]}
        response = SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=json.dumps(payload)), finish_reason="stop")], usage=None)
        analyzer = ParadigmAnalyzer(client=object(), model="offline")
        with patch("paradigms.analyzer.run_audit.chat_completion", AsyncMock(return_value=response)):
            result = await analyzer.prefetch_origin_eligibility([item])
        self.assertEqual(result[item.fingerprint][0], "full_review")
        self.assertFalse(analyzer.origin_benefits)


class FocusProductionPathTests(unittest.IsolatedAsyncioTestCase):
    process = fixture.V0DeliveryTests.process

    def setUp(self):
        fixture.V0DeliveryTests.setUp(self)
        for name, value in (("PARADIGM_RESEARCH_FOCUS_ENABLED", True), ("PARADIGM_DIRECTION_SIGNALS_ENABLED", False)):
            control = patch.object(config, name, value); control.start(); self.addCleanup(control.stop)

    async def test_real_orchestration_only_analyses_focused_work_and_keeps_quiet_input(self):
        quiet, loud = route(1, age=1), route(2, age=1)
        batch = fixture.batch([quiet.evidence[0], loud.evidence[0]])
        batch.supporting = [post(loud)]
        process = self.process(batch, accepted=True); process.delivery_contract_version = 5
        process.synthesizer.run = AsyncMock(wraps=process.synthesizer.run)
        result = await process.run(reference_time=NOW)
        self.assertEqual(result["research_scope_origin_count"], 1, result)
        self.assertEqual(result["analysis_count"], 1, result)
        self.assertEqual(result["planned_deep_candidate_count"], 1, result)
        self.assertEqual(len(process.pending_delivery), 1, result)
        self.assertEqual(process.synthesizer.run.await_count, 1)
        pending = self.store.load_pending_origins()
        self.assertIn(quiet.evidence[0].fingerprint, {item.fingerprint for item in pending})
        saved_quiet = next(item for item in pending if item.fingerprint == quiet.evidence[0].fingerprint)
        self.assertEqual(saved_quiet.raw["research_focus"]["decision"], "watch")

    async def test_all_watch_still_returns_weekly_receipt_without_deep_requests(self):
        process = self.process(fixture.batch([route(age=1).evidence[0]]), accepted=True)
        process.delivery_contract_version = 5
        process.synthesizer.run = AsyncMock(side_effect=AssertionError("must not deeply analyse quiet material"))
        result = await process.run(reference_time=NOW)
        self.assertTrue(result["delivery_ready"], result)
        self.assertEqual(len(process.pending_delivery), 0)
        self.assertEqual(result["research_scope_origin_count"], 0)
        process.synthesizer.run.assert_not_awaited()

    async def test_old_pending_is_observation_not_failed_or_completed_deep(self):
        item = route(age=1); item.status = "pending_deep"
        item.evidence, _, _ = self.store.observe_origins(item.evidence)
        self.store.save_candidates([item])
        process = self.process(fixture.batch([item.evidence[0]]), accepted=True)
        process.delivery_contract_version = 5
        process.synthesizer.run = AsyncMock(side_effect=AssertionError("old quiet task must not bypass selection"))
        result = await process.run(reference_time=NOW)
        self.assertTrue(result["delivery_ready"], result)
        self.assertEqual(result["candidate_focus_watch_count"], 1, result)
        self.assertEqual(result["deep_candidate_count"], 0)
        self.assertEqual(result["candidate_execution_deferred_count"], 0)
        self.assertEqual(result["candidate_budget_deferred_count"], 0)
        self.assertEqual(result["delivery_scope"]["deep_unattempted"], 1)
        self.assertEqual(result["delivery_scope"]["deep_failed"], 0)
        saved = self.store.load_candidate_snapshots([item.key])[0]
        self.assertEqual(saved.status, "pending_deep")
        process.synthesizer.run.assert_not_awaited()
