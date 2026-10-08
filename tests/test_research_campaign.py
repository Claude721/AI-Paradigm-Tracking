"""Restart and delivery replay of a finite campaign; no external services."""
from __future__ import annotations

import asyncio
import copy
import json
import re
import sqlite3
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx

import config
from agents.paradigm_orchestrator import ParadigmOrchestrator
from database.paradigm_store import ParadigmStore, _deep_checkpoint_input_signature
from database.state_migration import migrate_state
from paradigms.analyzer import ParadigmAnalyzer, ResearcherTrajectoryAnalyzer
from paradigms.completion import ResearchNotCompleteError, require_completed_research
from paradigms.discovery import DiscoveryBatch, ParadigmDiscovery
from paradigms.enrichment import EvidenceEnricher
from paradigms.models import EvidenceType, ParadigmCandidate, ParadigmExtraction, ResearcherProfile, TechnicalEvidence
from paradigms.async_utils import gather_scoped
from paradigms.clustering import cluster_extractions
from scripts.research_resume import continuation_decision
from run_audit import run_audit
from runtime_clock import observation_now, research_now, research_window
from sources.arxiv_source import ArxivSource


NOW = datetime(2026, 10, 8, tzinfo=timezone.utc)
LATER = datetime(2026, 10, 22, tzinfo=timezone.utc)


def origin(index):
    return TechnicalEvidence(source="arxiv", evidence_type=EvidenceType.PRIMARY_PAPER,
        title=f"Local optimization {index}", summary="A narrowly scoped parameter optimization.",
        url=f"https://arxiv.org/abs/2610.{index:05d}", published_at="2026-10-07T00:00:00+00:00")


def rejection(item):
    return ParadigmExtraction(evidence=item, is_candidate=False,
        canonical_name="局部调整", thesis="既有问题内的局部优化", problem_shift="未改变问题边界",
        mechanism="局部参数调整", rubric_assessment={"decision": "reject", "answer_coverage": 1.0, "score": 10})


def batch(count=4, **coverage):
    return DiscoveryBatch([origin(index) for index in range(count)], [], {"arxiv": count}, coverage)


def orchestrator(store, discover, analyze, *, refresh=None):
    value = ParadigmOrchestrator.__new__(ParadigmOrchestrator)
    value.store, value.bootstrap_mode = store, False
    value.ordinary_discovery_lookback_days, value.high_signal_discovery_lookback_days = 7, 30
    value.discovery_lookback_days = 30
    value.discovery = SimpleNamespace(run=discover)
    value.analyzer = SimpleNamespace(run=analyze)
    value.enricher = SimpleNamespace(hydrate_priority_origins=AsyncMock(return_value={}),
        finalize=EvidenceEnricher.finalize, refresh=refresh or AsyncMock(return_value=[]))
    value.pending_delivery = []
    return value


class CampaignReplayTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "state.db"
        self.store = ParadigmStore(self.path)
        run_audit.reset()

    async def test_fixed_window_closes_across_four_processes_without_rediscovery(self):
        seen, clocks, observations = [], [], []
        async def analyze(items):
            seen.extend(item.fingerprint for item in items)
            clocks.append(research_now())
            observations.append(observation_now())
            return [rejection(item) for item in items]
        discover = AsyncMock(return_value=batch())
        result = None
        for index in range(4):
            process = orchestrator(ParadigmStore(self.path), discover, analyze)
            with patch.object(config, "PARADIGM_ANALYSIS_SAFETY_LIMIT", 1):
                result = await process.run(reference_time=NOW if index == 0 else LATER)
            if index < 3:
                with self.assertRaises(ResearchNotCompleteError):
                    require_completed_research(result)
                self.assertIsNone(self.store.load_pending_report_job())
        require_completed_research(result)
        self.assertEqual(discover.await_count, 1)
        self.assertEqual(len(seen), len(set(seen)))
        self.assertEqual(clocks, [NOW] * 4)
        self.assertEqual(observations, [NOW, LATER, LATER, LATER])
        self.assertEqual(result["report_date"], "2026-10-08")
        self.assertEqual(result["research_campaign"]["origin_planned"], 4)
        self.assertEqual(result["research_campaign"]["origin_completed"], 4)
        self.assertEqual(result["research_campaign"]["pending_total_count"], 0)
        job = self.store.enqueue_report([], result, report_date=result["report_date"])
        self.assertEqual(self.store.campaigns.active().status, "waiting_delivery")
        self.store.save_rendered_report(job.delivery_key, "# 完整空报告")
        self.store.mark_delivery_delivered(job.delivery_key, Path(self.directory.name) / job.report_name)
        self.assertIsNone(self.store.campaigns.active())
        next_process = orchestrator(self.store, discover, analyze)
        with patch.object(config, "PARADIGM_ANALYSIS_SAFETY_LIMIT", 0):
            later = await next_process.run(reference_time=LATER)
        self.assertNotEqual(later["research_campaign"]["campaign_id"], result["research_campaign"]["campaign_id"])
        self.assertEqual(discover.await_count, 2)
        self.assertEqual(len(seen), 4)

    async def test_historical_checks_have_one_receipt_per_campaign(self):
        self.store.save_candidates([ParadigmCandidate(key=f"history-{index}", name=f"History {index}",
            thesis="", problem_shift="", mechanism="", status="observe") for index in range(4)])
        refresh = AsyncMock(return_value=[])
        discover = AsyncMock(return_value=batch(0))
        for index in range(4):
            process = orchestrator(self.store, discover, AsyncMock(), refresh=refresh)
            with patch.object(config, "PARADIGM_REFRESH_SAFETY_LIMIT", 1):
                result = await process.run(reference_time=NOW if index == 0 else LATER)
        require_completed_research(result)
        checked = [call.args[0][0].key for call in refresh.await_args_list]
        self.assertEqual(len(checked), 4)
        self.assertEqual(len(set(checked)), 4)
        self.assertEqual(result["research_campaign"]["refresh_completed"], 4)
        self.assertEqual(discover.await_count, 1)

    async def test_discovery_snapshot_and_origin_acknowledgement_are_atomic(self):
        process = orchestrator(self.store, AsyncMock(return_value=batch()), AsyncMock())
        with patch.object(self.store, "observe_origins", side_effect=RuntimeError("storage boundary")):
            with self.assertRaises(RuntimeError):
                await process.run(reference_time=NOW)
        self.assertIsNone(self.store.campaigns.active().discovery)
        self.assertEqual(self.store.load_pending_origins(), [])

    async def test_missing_downstream_snapshot_cannot_close_manifest(self):
        campaign = self.store.campaigns.begin(reference_time=NOW, ordinary_days=7, high_signal_days=30, bootstrap=False, seeds=[])
        self.store.campaigns.include(campaign.campaign_id, "deep", [("lost-route", "revision")])
        ledger = self.store.campaigns.reconcile(campaign.campaign_id)
        self.assertEqual(ledger["deep_pending"], 1)
        with self.assertRaises(ResearchNotCompleteError):
            self.store.enqueue_report([], {"research_incomplete": False, "coverage_incomplete": False, "research_campaign": ledger}, report_date="2026-10-08")

    async def test_receipt_recovers_after_crash_without_repeating_paid_origin(self):
        campaign = self.store.campaigns.begin(reference_time=NOW, ordinary_days=7, high_signal_days=30, bootstrap=False, seeds=[])
        item = self.store.observe_origins([origin(1)])[0][0]
        ids = self.store.campaigns.start_attempt(campaign.campaign_id, "origin", [(item.fingerprint, item.source_revision)])
        self.store.mark_evidence([item], analyzed=True)
        self.store.campaigns.begin_run(campaign.campaign_id)
        self.assertEqual(self.store.campaigns.reconcile(campaign.campaign_id)["origin_pending"], 0)
        with sqlite3.connect(self.path) as conn:
            self.assertEqual(conn.execute("SELECT outcome FROM research_attempts WHERE attempt_id=?", (ids[0],)).fetchone()[0], "interrupted")

    async def test_source_revision_reopens_a_completed_receipt(self):
        campaign = self.store.campaigns.begin(reference_time=NOW, ordinary_days=7, high_signal_days=30, bootstrap=False, seeds=[])
        item = self.store.observe_origins([origin(1)])[0][0]
        self.store.campaigns.reconcile(campaign.campaign_id)
        self.store.mark_evidence([item], analyzed=True)
        self.assertEqual(self.store.campaigns.reconcile(campaign.campaign_id)["origin_pending"], 0)
        revised = origin(1)
        revised.summary = "A changed intervention"
        self.store.observe_origins([revised])
        self.assertEqual(self.store.campaigns.reconcile(campaign.campaign_id)["origin_pending"], 1)

    async def test_resume_without_campaign_is_not_a_new_research_run(self):
        discover = AsyncMock()
        process = orchestrator(self.store, discover, AsyncMock())
        with self.assertRaisesRegex(RuntimeError, "没有待续跑"):
            await process.run(resume_only=True)
        discover.assert_not_awaited()

    async def test_discovering_campaign_cannot_be_forged_into_a_closed_empty_report(self):
        campaign = self.store.campaigns.begin(reference_time=NOW, ordinary_days=7, high_signal_days=30, bootstrap=False, seeds=[])
        with self.assertRaises(ResearchNotCompleteError):
            self.store.enqueue_report([], {"research_incomplete": False, "coverage_incomplete": False,
                "research_campaign": self.store.campaigns.snapshot(campaign.campaign_id)}, report_date="2026-10-08")
        # Omitting the campaign field must not bypass the same proof.
        with self.assertRaises(ResearchNotCompleteError):
            self.store.enqueue_report([], {"research_incomplete": False, "coverage_incomplete": False}, report_date="2026-10-08")

    async def test_strict_resume_cannot_send_an_unrelated_legacy_outbox(self):
        import main
        from tests.completion_fixtures import COMPLETED_RESEARCH
        job = self.store.enqueue_report([], dict(COMPLETED_RESEARCH), report_date="2026-10-01")
        process = SimpleNamespace(store=self.store, run=AsyncMock())
        delivery = AsyncMock()
        with patch("main._check_env"), patch("main._print_model_banner"), patch("agents.paradigm_orchestrator.ParadigmOrchestrator", return_value=process), patch("reports.paradigm_generator.ParadigmReportGenerator"), patch("main._deliver_paradigm_job", delivery):
            with self.assertRaisesRegex(RuntimeError, "没有待续跑"):
                await main._run_pipeline_once(resume_only=True)
            campaign = self.store.campaigns.begin(reference_time=NOW, ordinary_days=7, high_signal_days=30, bootstrap=False, seeds=[])
            with self.assertRaisesRegex(RuntimeError, "不属于续跑批次"):
                await main._run_pipeline_once(resume_only=True, expected_campaign_id=campaign.campaign_id)
        delivery.assert_not_awaited()
        process.run.assert_not_awaited()
        self.assertEqual(self.store.load_pending_report_job().delivery_key, job.delivery_key)

    async def test_missing_route_is_restored_from_task_snapshot_without_claiming_success(self):
        campaign = self.store.campaigns.begin(reference_time=NOW, ordinary_days=7, high_signal_days=30, bootstrap=False, seeds=[])
        candidate = ParadigmCandidate(key="restore", name="路线", thesis="假说", problem_shift="边界", mechanism="机制", status="pending_deep")
        self.store.campaigns.include(campaign.campaign_id, "deep", [(candidate.key, _deep_checkpoint_input_signature(candidate), candidate.to_dict())])
        ledger = self.store.campaigns.reconcile(campaign.campaign_id)
        self.assertEqual(ledger["deep_pending"], 1)
        self.assertEqual(self.store.load_pending_deep_candidates()[0].key, "restore")

    async def test_completed_deep_policy_change_reopens_an_executable_candidate(self):
        campaign = self.store.campaigns.begin(reference_time=NOW, ordinary_days=7, high_signal_days=30, bootstrap=False, seeds=[])
        candidate = ParadigmCandidate(key="policy-change", name="路线", thesis="假说", problem_shift="边界", mechanism="机制", status="observe")
        self.store.save_candidates([candidate])
        self.store.campaigns.include(campaign.campaign_id, "deep", [(candidate.key, "old-policy", candidate.to_dict())])
        self.assertEqual(self.store.campaigns.reconcile(campaign.campaign_id)["deep_pending"], 1)
        restored = self.store.load_pending_deep_candidates()[0]
        self.assertEqual(restored.key, candidate.key)
        with self.store._connect() as conn:
            self.assertEqual(conn.execute("SELECT input_revision FROM campaign_work WHERE object_key=?", (candidate.key,)).fetchone()[0], _deep_checkpoint_input_signature(restored))

    async def test_work_recovery_snapshot_cannot_persist_transient_community_body(self):
        campaign = self.store.campaigns.begin(reference_time=NOW, ordinary_days=7, high_signal_days=30, bootstrap=False, seeds=[])
        candidate = ParadigmCandidate(key="scrub", name="路线", thesis="", problem_shift="", mechanism="机制", evidence=[TechnicalEvidence(source="reddit", evidence_type=EvidenceType.COMMUNITY_DISCUSSION,
            title="Discussion", url="https://www.reddit.com/comments/local", summary="transient body", raw={"ephemeral_content": True, "extra_comment": "transient body"})])
        self.store.campaigns.include(campaign.campaign_id, "deep", [(candidate.key, "revision", candidate.to_dict())])
        with self.store._connect() as conn:
            self.assertNotIn("transient body", conn.execute("SELECT payload_json FROM campaign_work").fetchone()[0])

    async def test_unknown_usage_after_interruption_prevents_automatic_spending(self):
        campaign = self.store.campaigns.begin(reference_time=NOW, ordinary_days=7, high_signal_days=30, bootstrap=False, seeds=[])
        self.store.campaigns.begin_run(campaign.campaign_id)
        self.store.campaigns.begin_run(campaign.campaign_id)
        self.assertEqual(self.store.campaigns.snapshot(campaign.campaign_id)["usage_unknown_attempts"], 1)

    async def test_queue_counts_do_not_deserialize_the_entire_discovery_each_time(self):
        campaign = self.store.campaigns.begin(reference_time=NOW, ordinary_days=7, high_signal_days=30, bootstrap=False, seeds=[])
        self.store.campaigns.save_discovery(campaign.campaign_id, batch())
        with patch("database.research_campaign._batch_from_payload", side_effect=AssertionError("unnecessary large snapshot decode")):
            self.assertEqual(self.store.campaigns.snapshot(campaign.campaign_id)["pending_total_count"], 0)
            self.assertEqual(self.store.campaigns.active(load_discovery=False).campaign_id, campaign.campaign_id)
        self.assertEqual(len(self.store.campaigns.active().discovery.origins), 4)

    async def test_v7_migration_preserves_state_and_adds_empty_campaign_tables(self):
        item = self.store.observe_origins([origin(1)])[0][0]
        self.store.mark_evidence([item], analyzed=True)
        with sqlite3.connect(self.path) as conn:
            for table in ("research_stage_cache", "research_attempts", "campaign_work", "research_campaigns"):
                conn.execute(f"DROP TABLE {table}")
        self.assertEqual(migrate_state(self.path, 7), 8)
        restored = ParadigmStore(self.path)
        self.assertIsNone(restored.campaigns.active())
        self.assertEqual(restored.load_pending_origins(), [])
        self.assertEqual(restored.stats()["evidence"], 1)

    async def test_discovery_cache_does_not_persist_community_body(self):
        campaign = self.store.campaigns.begin(reference_time=NOW, ordinary_days=7, high_signal_days=30, bootstrap=False, seeds=[])
        data = batch(0)
        data.supporting = [TechnicalEvidence(source="reddit", evidence_type=EvidenceType.COMMUNITY_DISCUSSION,
            title="Discussion", url="https://www.reddit.com/comments/local", summary="private user body", raw={"comments": "private comments", "ephemeral_content": True})]
        self.store.campaigns.save_discovery(campaign.campaign_id, data)
        with sqlite3.connect(self.path) as conn:
            text = conn.execute("SELECT discovery_json FROM research_campaigns").fetchone()[0]
        self.assertNotIn("private user body", text)
        self.assertNotIn("private comments", text)

    async def test_current_schema_restore_rejects_corrupt_usage_or_attempt_identity(self):
        campaign = self.store.campaigns.begin(reference_time=NOW, ordinary_days=7, high_signal_days=30, bootstrap=False, seeds=[])
        with sqlite3.connect(self.path) as conn:
            conn.execute("UPDATE research_campaigns SET stats_json=?", (json.dumps({"cumulative_llm_total_tokens": -1}),))
        with self.assertRaises(ValueError):
            migrate_state(self.path, 8)
        with sqlite3.connect(self.path) as conn:
            conn.execute("UPDATE research_campaigns SET stats_json='{}'")
        attempts = self.store.campaigns.start_attempt(campaign.campaign_id, "origin", [("fixture", "revision")])
        with sqlite3.connect(self.path) as conn:
            conn.execute("UPDATE research_attempts SET kind='unknown' WHERE attempt_id=?", (attempts[0],))
        with self.assertRaises(ValueError):
            migrate_state(self.path, 8)


class DiscoveryResumeTests(unittest.IsolatedAsyncioTestCase):
    async def test_scope_signature_tracks_queries_not_credentials(self):
        discovery = ParadigmDiscovery(lookback_days=7, high_signal_lookback_days=30)
        original = discovery._plan_signatures()
        with patch.object(config, "LLM_API_KEY", "changed-offline-only"), patch.object(config, "OPENALEX_API_KEY", "changed-offline-only"):
            self.assertEqual(original, discovery._plan_signatures())
        with patch("paradigms.discovery.arxiv_query_plan", return_value=[{"group": "new", "query": "new-scope"}]):
            self.assertNotEqual(original["arxiv"], discovery._plan_signatures()["arxiv"])

    async def test_minority_feed_failure_is_still_an_unclosed_configured_scope(self):
        from paradigms.completion import discovery_retry_sources
        coverage = {"source_health": {"curated-kol-feeds": {"status": "completed_with_warnings"}},
            "curated_kol_sources": {"configured_feeds": 19, "completed_feeds": 18, "failed_feeds": ["fixture:403"]}}
        self.assertEqual(discovery_retry_sources(coverage), {"curated-kol-feeds"})
        with self.assertRaises(ResearchNotCompleteError):
            require_completed_research({"research_incomplete": False, "coverage_incomplete": False, "frontier_coverage": coverage})

    async def test_only_failed_source_retried_without_resetting_healthy_coverage(self):
        original = batch(1, source_health={"arxiv": {"status": "partial"}, "openalex": {"status": "completed"}},
            academic_indexes={"arxiv": {"status": "partial"}, "openalex": {"status": "completed", "queries": 1, "completed_queries": 1}})
        fresh = batch(2, source_health={"arxiv": {"status": "completed"}, "openalex": {"status": "cached"}},
            academic_indexes={"arxiv": {"status": "completed", "queries": 1, "completed_queries": 1}}, domains={}, recall_lanes={}, query_failures=[])
        discovery = ParadigmDiscovery.__new__(ParadigmDiscovery)
        discovery.run = AsyncMock(return_value=fresh)
        result = await discovery.retry(original)
        self.assertEqual(discovery.run.await_args.kwargs, {"source_names": {"arxiv"}})
        self.assertEqual(result.coverage["source_health"]["openalex"]["status"], "completed")
        self.assertEqual(len(result.origins), 2)
        require_completed_research({"research_incomplete": False, "coverage_incomplete": False, "frontier_coverage": result.coverage})

    async def test_removing_failed_configuration_is_not_completed_coverage(self):
        original = batch(0, source_health={"openreview": {"status": "query_failed"}}, academic_indexes={"openreview": {"status": "query_failed"}})
        fresh = batch(0, source_health={"openreview": {"status": "not_configured"}}, academic_indexes={"openreview": {"status": "not_configured"}})
        discovery = ParadigmDiscovery.__new__(ParadigmDiscovery)
        discovery.run = AsyncMock(return_value=fresh)
        result = await discovery.retry(original)
        self.assertEqual(result.coverage["source_health"]["openreview"]["status"], "configuration_changed")
        with self.assertRaises(ResearchNotCompleteError):
            require_completed_research({"research_incomplete": False, "coverage_incomplete": False, "frontier_coverage": result.coverage})


class StageReceiptTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.store = ParadigmStore(Path(self.directory.name) / "state.db")
        run_audit.reset()

    async def test_prefilter_reuses_healthy_rows_and_retries_only_missing_identity(self):
        items = self.store.observe_origins([origin(index) for index in range(3)])[0]
        call = AsyncMock(side_effect=[SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=json.dumps({"decisions": [
            {"fingerprint": item.fingerprint, "decision": "full_review", "reason": "需要核验"} for item in items[:2]]})))], usage=None),
            SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=json.dumps({"decisions": [
                {"fingerprint": items[2].fingerprint, "decision": "screen_out", "reason": "局部优化"}]})))], usage=None)])
        client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=call)))
        analyzer = ParadigmAnalyzer(client=client, model="offline", stage_cache=self.store.campaigns)
        self.assertEqual(len(await analyzer._screen_origin_batch(items)), 2)
        self.assertEqual(len(await analyzer._screen_origin_batch(items)), 3)
        self.assertEqual(len(await analyzer._screen_origin_batch(items)), 3)
        self.assertEqual(call.await_count, 2)
        last_prompt = call.await_args_list[1].kwargs["messages"][0]["content"]
        self.assertNotIn(items[0].fingerprint, last_prompt)
        self.assertIn(items[2].fingerprint, last_prompt)

    async def test_person_success_survives_other_person_failure(self):
        profiles = [ResearcherProfile(name=name, role="共同第一作者", identifiers={"openalex": name}, representative_works=[{"title": "公开工作"}]) for name in ("Alice Researcher", "Bob Researcher")]
        candidate = ParadigmCandidate(key="route", name="研究路线", thesis="", problem_shift="", mechanism="机制", researchers=profiles)
        calls = []
        async def answer(**kwargs):
            prompt = kwargs["messages"][0]["content"]
            person = "Alice" if "Alice Researcher" in prompt else "Bob"
            calls.append(person)
            if person == "Bob" and calls.count("Bob") == 1:
                raise RuntimeError("temporary person provider failure")
            payload = {"background_summary": "可确认的公开职业背景", "trajectory_summary": "已核验代表作的连续性", "trajectory_consistency": 4,
                "key_person_reason": "沿当前机制持续研究", "current_role_note": ""}
            return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=json.dumps(payload)))], usage=None)
        client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=AsyncMock(side_effect=answer))))
        for index in range(2):
            analyzer = ResearcherTrajectoryAnalyzer(concurrency=1, client=client, model="offline", stage_cache=self.store.campaigns)
            if index == 0:
                with self.assertRaises(RuntimeError):
                    await analyzer.run([copy.deepcopy(candidate)])
            else:
                await analyzer.run([copy.deepcopy(candidate)])
        self.assertEqual(calls, ["Alice", "Bob", "Bob"])
        self.assertEqual(run_audit.token_totals()["llm_call_count"], 3)

    async def test_duplicate_prefilter_identity_does_not_discard_healthy_peer(self):
        items = self.store.observe_origins([origin(index) for index in range(2)])[0]
        rows = [{"fingerprint": item.fingerprint, "decision": "full_review", "reason": "需要核验"} for item in items]
        rows.append({**rows[0], "decision": "screen_out"})
        response = SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=json.dumps({"decisions": rows})))], usage=None)
        client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=AsyncMock(return_value=response))))
        analyzer = ParadigmAnalyzer(client=client, model="offline", stage_cache=self.store.campaigns)
        decisions = await analyzer._screen_origin_batch(items)
        self.assertEqual(set(decisions), {items[1].fingerprint})
        self.assertEqual(client.chat.completions.create.await_count, 1)

    async def test_person_bad_json_is_not_cached_as_completed_research(self):
        client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=AsyncMock(return_value=SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content="{}"))], usage=None)))))
        analyzer = ResearcherTrajectoryAnalyzer(client=client, model="offline", stage_cache=self.store.campaigns)
        candidate = ParadigmCandidate(key="route", name="研究路线", thesis="", problem_shift="", mechanism="机制",
            researchers=[ResearcherProfile(name="Person Researcher", representative_works=[{"title": "Work"}])])
        with self.assertRaises(RuntimeError):
            await analyzer.run([candidate])
        with self.store._connect() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM research_stage_cache").fetchone()[0], 0)

    async def test_failed_parallel_stage_cancels_and_settles_other_calls(self):
        started, cancelled = asyncio.Event(), asyncio.Event()
        async def slow():
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()
        async def fail():
            await started.wait()
            raise RuntimeError("stage failed")
        with self.assertRaises(RuntimeError):
            await gather_scoped(slow(), fail())
        self.assertTrue(cancelled.is_set())

    async def test_report_seed_identity_survives_prose_rename_without_merging_other_seed(self):
        first = rejection(origin(1))
        first.rubric_assessment["decision"] = "deep_dive"
        first.is_candidate = True
        first.evidence.raw["origin_kind"] = "technical_report"
        first.mechanism_id = "report-fixed-seed-a"
        renamed = copy.deepcopy(first)
        renamed.canonical_name = "Another Chinese description"
        sibling = copy.deepcopy(renamed)
        sibling.mechanism_id = "report-fixed-seed-b"
        values = cluster_extractions([first, renamed, sibling])
        self.assertEqual({item.key for item in values}, {first.mechanism_id, sibling.mechanism_id})

    async def test_report_mechanisms_checkpoint_before_slow_peer_finishes(self):
        report = origin(1)
        report.raw["origin_kind"] = "technical_report"
        report = self.store.observe_origins([report])[0][0]
        analyzer = ParadigmAnalyzer(technical_report_mechanism_slice=2)
        seeds = [{"canonical_name": name, "problem_shift": "能力边界改变", "mechanism": name} for name in ("Mechanism A", "Mechanism B")]
        analyzer._request_report_index = AsyncMock(return_value=(seeds, "", ""))
        fast_done, slow_started = asyncio.Event(), asyncio.Event()
        async def assess(item, seed, *, index):
            if index == 2:
                slow_started.set()
                await asyncio.Event().wait()
            value = ParadigmExtraction(evidence=item, is_candidate=True, canonical_name=seed["canonical_name"],
                thesis="新的机制假说", problem_shift=seed["problem_shift"], mechanism=seed["mechanism"],
                rubric_assessment={"decision": "deep_dive", "score": 80, "answer_coverage": 1.0})
            return value, ""
        analyzer._assess_report_mechanism = assess
        process = orchestrator(self.store, AsyncMock(), AsyncMock())
        process.analyzer = analyzer
        original_saver = self.store.mark_evidence
        def save(*args, **kwargs):
            result = original_saver(*args, **kwargs)
            if self.store.load_pending_deep_candidates():
                fast_done.set()
            return result
        with patch.object(self.store, "mark_evidence", side_effect=save):
            work = asyncio.create_task(process._analyze_origins_in_batches([report], float("inf")))
            await asyncio.wait_for(fast_done.wait(), 1)
            self.assertTrue(slow_started.is_set())
            work.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await work
        pending = self.store.load_pending_origins()[0]
        self.assertEqual(len(pending.raw["technical_report_completed_mechanisms"]), 1)
        self.assertEqual(len(pending.raw["technical_report_committed_candidates"]), 1)
        self.assertEqual(len(self.store.load_pending_deep_candidates()), 1)
        # Only the missing mechanism needs a new paid assessment after restart.
        async def finish(item, seed, *, index):
            value = rejection(item)
            value.canonical_name, value.mechanism = seed["canonical_name"], seed["mechanism"]
            return value, ""
        analyzer._assess_report_mechanism = AsyncMock(side_effect=finish)
        await process._analyze_origins_in_batches([pending], float("inf"))
        self.assertEqual(analyzer._request_report_index.await_count, 1)
        self.assertEqual(analyzer._assess_report_mechanism.await_count, 1)
        self.assertEqual(self.store.load_pending_origins(), [])
        self.assertEqual(len(self.store.load_pending_deep_candidates()), 1)

    async def test_long_report_requeues_progress_in_same_run_and_counts_origin_once(self):
        report = origin(1)
        report.raw["origin_kind"] = "technical_report"
        report.raw["arxiv_comment"] = "Technical report"
        discover = AsyncMock(return_value=DiscoveryBatch([report], [], {"arxiv": 1}, {}))
        analyzer = ParadigmAnalyzer(technical_report_mechanism_slice=1)
        seeds = [{"canonical_name": f"Seed {index}", "problem_shift": "局部变化", "mechanism": f"Mechanism {index}"} for index in range(5)]
        analyzer._request_report_index = AsyncMock(return_value=(seeds, "", ""))
        async def assess(item, seed, *, index):
            value = rejection(item)
            value.canonical_name, value.mechanism = seed["canonical_name"], seed["mechanism"]
            return value, ""
        analyzer._assess_report_mechanism = AsyncMock(side_effect=assess)
        process = orchestrator(self.store, discover, AsyncMock())
        process.analyzer = analyzer
        result = await process.run(reference_time=NOW)
        require_completed_research(result)
        self.assertEqual(analyzer._request_report_index.await_count, 1)
        self.assertEqual(analyzer._assess_report_mechanism.await_count, 5)
        self.assertEqual(result["research_campaign"]["origin_planned"], 1)
        self.assertEqual(result["research_campaign"]["origin_completed"], 1)
        self.assertEqual(result["analysis_completed_count"], 1)
        self.assertEqual(len(run_audit.origin_decisions), 5)
        self.assertTrue(all(value["rubric_decision"] == "reject" for value in run_audit.origin_decisions))
        self.assertEqual(self.store.load_pending_origins(), [])


class ArxivTransportReceiptTests(unittest.IsolatedAsyncioTestCase):
    async def test_http_success_cannot_hide_error_feed_or_non_atom_response(self):
        source = ArxivSource(seed_arxiv_ids=[], reference_time=NOW)
        error = '<feed xmlns="http://www.w3.org/2005/Atom"><entry><id>http://arxiv.org/api/errors#incorrect_query</id><title>Error</title></entry></feed>'
        for text in (error, "<html><body>blocked</body></html>"):
            with self.subTest(text=text), self.assertRaises(ValueError):
                source._parse_atom_feed(text)
            with self.subTest(text=text), self.assertRaises(ValueError):
                source._page_state(text)
        self.assertEqual(source._parse_atom_feed('<feed xmlns="http://www.w3.org/2005/Atom"/>'), [])

    async def test_all_requests_share_polite_spacing_without_real_wait(self):
        elapsed = 0.0
        starts = []
        async def sleep(seconds):
            nonlocal elapsed
            elapsed += seconds
        async def get(*args, **kwargs):
            starts.append(elapsed)
            return httpx.Response(200, request=httpx.Request("GET", args[0]))
        source = ArxivSource(seed_arxiv_ids=[])
        client = SimpleNamespace(get=AsyncMock(side_effect=get))
        with patch("sources.arxiv_source.time", SimpleNamespace(monotonic=lambda: elapsed)), patch("sources.arxiv_source.asyncio.sleep", side_effect=sleep):
            await source._get_with_retry(client, {})
            await source._get_with_retry(client, {})
            await source._get_with_retry(client, {})
        self.assertEqual(starts, [0.0, 3.0, 6.0])

    async def test_long_retry_after_is_not_truncated_into_an_early_retry(self):
        source = ArxivSource(seed_arxiv_ids=[])
        client = SimpleNamespace(get=AsyncMock(return_value=httpx.Response(429, headers={"Retry-After": "120"}, request=httpx.Request("GET", "https://export.arxiv.org/api/query"))))
        with patch("sources.arxiv_source.asyncio.sleep", new=AsyncMock()) as sleep:
            with self.assertRaises(httpx.HTTPStatusError):
                await source._get_with_retry(client, {})
        self.assertEqual(client.get.await_count, 1)
        sleep.assert_not_awaited()
        self.assertEqual(source.circuit_reason, "retry_after_exceeds_visit")

    async def test_repeated_page_cannot_be_a_complete_recall(self):
        source = ArxivSource(seed_arxiv_ids=[], reference_time=NOW)
        text = '<feed xmlns="http://www.w3.org/2005/Atom">' + ''.join(
            f'<entry><id>https://arxiv.org/abs/2610.{index:05d}</id><title>Work {index}</title><published>2026-10-07T00:00:00Z</published><updated>2026-10-07T00:00:00Z</updated></entry>' for index in range(500)) + '</feed>'
        source._request = AsyncMock(return_value=httpx.Response(200, text=text))
        with self.assertRaisesRegex(ValueError, "分页重复"):
            await source._fetch_query(None, "fixture")
        self.assertEqual(source._request.await_count, 2)


class ContinuationPolicyTests(unittest.TestCase):
    def basis(self):
        current = {"campaign_id": "fixed-campaign", "reference_time": NOW.isoformat(), "pending_total_count": 1,
            "attempt_count": 1, "reserved_research_seconds": 3600, "cumulative_llm_total_tokens": 1000}
        result = {"result_kind": "research_blocked", "delivery_failure_kind": "research_not_complete",
            "coverage_incomplete": False, "run_budget_exhausted": True, "research_campaign": current}
        return result, current

    def test_automatic_continuation_requires_opt_in_saved_state_and_pure_budget_deferral(self):
        result, current = self.basis()
        self.assertFalse(continuation_decision(result, current, state_saved=True)["dispatch"])
        self.assertFalse(continuation_decision(result, current, enabled=True)["dispatch"])
        self.assertTrue(continuation_decision(result, current, enabled=True, state_saved=True)["dispatch"])
        for detail in ({"coverage_incomplete": True}, {"analysis_failed_count": 1}, {"candidate_input_deferred_count": 1},
                       {"delivery_profile_deferred_count": 1}, {"analysis_safety_deferred_count": 1}, {"result_kind": "complete_no_signal"}):
            with self.subTest(detail=detail):
                self.assertFalse(continuation_decision({**result, **detail}, current, enabled=True, state_saved=True)["dispatch"])

    def test_chain_identity_time_runs_tokens_and_unknown_usage_are_bounded(self):
        result, current = self.basis()
        for change in ({"campaign_id": "different"}, {"attempt_count": 4}, {"reserved_research_seconds": 14400},
                       {"cumulative_llm_total_tokens": 4000000}, {"usage_unknown_attempts": 1}, {"cumulative_unreported_usage_calls": 1}):
            with self.subTest(change=change):
                self.assertFalse(continuation_decision(result, {**current, **change}, enabled=True, state_saved=True)["dispatch"])

    def test_missing_model_usage_is_not_a_measured_zero(self):
        from run_audit import RunAudit
        audit = RunAudit()
        response = SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content="公开结果"))], usage=None)
        audit.record_llm(stage="test", role="sub", model="offline", subject="local", response=response)
        self.assertEqual(audit.token_totals()["llm_unreported_usage_count"], 1)
        self.assertEqual(audit.token_totals()["llm_total_tokens"], 0)


async def pressure_replay(*, count=10000, latency_seconds=20, budget_seconds=3600):
    """Deterministic latency model, NOT a claim about real provider throughput."""
    simulated = 0.0
    calls, inputs, pending = [], [], []
    def clock():
        return simulated
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / "state.db"
        store = ParadigmStore(path)
        data = batch(count)
        for item in data.origins:
            item.published_at = "2026-09-01T00:00:00+00:00"
        discover = AsyncMock(return_value=data)
        async def answer(**kwargs):
            nonlocal simulated
            prompt = kwargs["messages"][0]["content"]
            records = json.loads(re.search(r"<origin_records>\s*(.*?)\s*</origin_records>", prompt, re.S).group(1))
            simulated += latency_seconds
            calls.append(len(records))
            inputs.extend(row["fingerprint"] for row in records)
            payload = {"decisions": [{"fingerprint": row["fingerprint"], "decision": "screen_out", "reason": "材料明确仅调整既有任务的局部参数"} for row in records]}
            return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=json.dumps(payload)))],
                usage=SimpleNamespace(prompt_tokens=1000, completion_tokens=500, total_tokens=1500))
        client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=AsyncMock(side_effect=answer))))
        for iteration in range(12):
            process = orchestrator(ParadigmStore(path), discover, AsyncMock())
            process.analyzer = ParadigmAnalyzer(client=client, model="offline", enable_batch_prefilter=True, stage_cache=process.store.campaigns)
            run_audit.reset()
            with patch.object(config, "PARADIGM_RUN_BUDGET_SECONDS", budget_seconds), patch("agents.paradigm_orchestrator.time.monotonic", side_effect=clock):
                result = await process.run(reference_time=NOW if iteration == 0 else LATER)
            remaining = result["research_campaign"]["pending_total_count"]
            pending.append(remaining)
            if remaining == 0:
                require_completed_research(result)
                job = store.enqueue_report([], result, report_date=result["report_date"])
                from main import _deliver_paradigm_job
                from reports.paradigm_generator import ParadigmReportGenerator
                generator = ParadigmReportGenerator(Path(directory) / "reports", client=False)
                mail = AsyncMock(return_value=True)
                with patch("notifications.email_notifier.send_report_email", mail), patch.object(config, "EMAIL_PUSH_ENABLED", True), patch.object(config, "EMAIL_PUSH_REQUIRED", True), patch.dict("os.environ", {"AI_RADAR_AUDIT_DIR": str(Path(directory) / "audit")}):
                    delivery = await _deliver_paradigm_job(store, generator, job, recovered=False)
                    duplicate = await _deliver_paradigm_job(store, generator, store.get_report_job(job.delivery_key), recovered=True)
                if not delivery["email_sent"] or mail.await_count != 1 or not duplicate.get("delivery_duplicate_skipped"):
                    raise AssertionError("Closed replay did not confirm exactly one simulated delivery")
                break
            if store.load_pending_report_job() is not None:
                raise AssertionError("Incomplete pressure replay reached delivery")
        else:
            raise AssertionError("Finite campaign did not close within replay bound")
        if len(set(inputs)) != len(inputs) or discover.await_count != 1 or any(right >= left for left, right in zip(pending, pending[1:])):
            raise AssertionError("Replay duplicated research or did not make durable progress")
        return {"simulation_only": True, "origins": count, "assumed_eligibility_request_seconds": latency_seconds,
            "research_budget_seconds": budget_seconds, "processes": len(pending), "pending_after_each_process": pending,
            "discovery_calls": discover.await_count, "eligibility_calls": len(calls), "unique_origin_judgments": len(set(inputs)),
            "simulated_seconds": simulated, "delivered_outbox": store.delivery_queue_snapshot()["status_counts"]["delivered"]}


class CampaignPressureTests(unittest.IsolatedAsyncioTestCase):
    async def test_ten_thousand_origins_close_without_repeating_discovery_or_model_work(self):
        with patch.object(asyncio.get_running_loop(), "slow_callback_duration", float("inf")):
            result = await pressure_replay()
        self.assertTrue(result["simulation_only"])
        self.assertGreater(result["processes"], 1)
        self.assertEqual(result["unique_origin_judgments"], 10000)
        self.assertEqual(result["pending_after_each_process"][-1], 0)
        self.assertEqual(result["delivered_outbox"], 1)


if __name__ == "__main__":
    unittest.main()
