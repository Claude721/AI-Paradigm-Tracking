"""Exercise the real default constructor and durable V0 delivery, offline."""
import copy
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import config
import main
from agents.paradigm_orchestrator import ParadigmOrchestrator
from database.paradigm_store import ParadigmStore
from healthcheck import _model_check
from notifications.email_notifier import _send_sync
from paradigms.completion import ResearchNotCompleteError, require_completed_research
from paradigms.delivery_policy import acquisition_summary, bounded_origins
from paradigms.rubric import load_rubric
from paradigms.discovery import DiscoveryBatch
from paradigms.models import EvidenceType, ParadigmExtraction, TechnicalEvidence
from paradigms.search_receipts import blocking_searches, record_search
from reports.paradigm_generator import ParadigmReportGenerator
from run_audit import run_audit
from source_audit import v0_source_readiness
from tests.test_paradigm_pipeline import candidate, rubric_assessment, verified_researcher

NOW = datetime(2026, 10, 8, tzinfo=timezone.utc)


def origin(index=1):
    return TechnicalEvidence(source="arxiv", evidence_type=EvidenceType.PRIMARY_PAPER,
        title=f"Predictive latent actions {index}", url=f"https://arxiv.org/abs/2610.{index:05d}",
        summary="Prediction changes the action interface.", published_at="2026-10-07T00:00:00+00:00",
        organization="OpenAI", authors=["A. Researcher"],
        raw={"publisher_tier": "established", "origin_kind": "official_research"})


def batch(items=None, *, outage=False):
    items = [origin()] if items is None else items
    return DiscoveryBatch(items, [], {"arxiv": len(items)}, {
        "source_health": {
            "arxiv": {"status": "query_failed" if outage else "completed", "results": 0 if outage else len(items)},
            "openalex": {"status": "query_failed" if outage else "completed", "results": 0},
            "priority-research-page": {"status": "query_failed", "results": 0},
        },
        "official_pages": {"total_pages": 2, "checked_pages": 2, "request_failed": ["blocked-website"]},
    })


class V0DeliveryTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.path = Path(temporary.name)
        self.store = ParadigmStore(self.path / "state.db")
        run_audit.reset()

    def process(self, discovered=None, *, accepted=False, omit=()):
        with patch("agents.paradigm_orchestrator.ParadigmStore", return_value=self.store):
            value = ParadigmOrchestrator()
        self.assertEqual(value.delivery_contract_version, 4)
        self.assertTrue(value.discovery.bounded)
        self.assertEqual(value.discovery.arxiv.query_result_limit, config.PARADIGM_SOURCE_QUERY_LIMIT)
        self.assertEqual(value.discovery.priority_pages.per_page, config.PARADIGM_OFFICIAL_DETAIL_LIMIT)
        value.discovery = SimpleNamespace(run=AsyncMock(return_value=discovered or batch()))

        async def analyze(items):
            return [ParadigmExtraction(evidence=item, is_candidate=accepted,
                canonical_name="潜在动作世界模型", route_family="可行动状态预测", thesis="改变状态预测接口。",
                problem_shift="从像素预测到可行动状态。", mechanism="离散动作约束状态转移。",
                innovation_types=["architecture"], keywords=["latent action"],
                rubric_assessment=rubric_assessment(stage="screening") if accepted else
                    {"decision": "reject", "answer_coverage": 1.0})
                for item in items if item.fingerprint not in omit]

        async def enrich(items, supporting):
            for item in items:
                item.researchers = [verified_researcher()]
                record_search(item, "hackernews", "completed")
                record_search(item, "tavily_social_web", "failed", reason="http_503")
                item.community_coverage["tavily_social_web"] = "本轮接口失败，不能判断该平台讨论。"
            return items

        async def synthesize(items):
            for item in items:
                template = candidate(item.evidence)
                for key in ("mental_model", "technical_explanation", "background", "design_philosophy", "application_value", "rubric_assessment"):
                    setattr(item, key, copy.deepcopy(getattr(template, key)))
            return items

        value.analyzer = SimpleNamespace(run=analyze)
        value.enricher.run = enrich
        value.enricher.hydrate_priority_origins = AsyncMock(return_value={})
        value.enricher.refresh = AsyncMock(return_value=[])
        value.synthesizer.run = synthesize
        value.trajectory.run = AsyncMock(side_effect=lambda items: items)
        return value

    async def test_failed_website_and_old_backlog_do_not_block_completed_empty_scope(self):
        old = origin(99)
        old.published_at = "2020-01-01T00:00:00+00:00"
        self.store.observe_origins([old])
        process = self.process()
        result = await process.run(reference_time=NOW)
        self.assertTrue(result["coverage_incomplete"])
        self.assertTrue(result["research_incomplete"])
        require_completed_research(result, candidates=[])
        self.assertEqual(result["result_kind"], "complete_no_selection")
        self.assertEqual(result["research_campaign"]["origin_planned"], 1)
        job = self.store.enqueue_report([], result, report_date=result["report_date"])
        generator = ParadigmReportGenerator(self.path, client=False)
        path = await generator.generate([], result)
        content = path.read_text()
        self.assertNotIn("阶段性", content)
        self.assertNotIn("已完成配置信源", content)
        self.assertEqual(job.status, "pending_render")
        self.assertEqual(self.store.load_pending_origins()[0].fingerprint, old.fingerprint)

    async def test_healthy_route_delivers_after_bad_selected_peer_was_attempted(self):
        bad = origin(2)
        process = self.process(batch([origin(), bad]), accepted=True, omit={bad.fingerprint})
        result = await process.run(reference_time=NOW)
        self.assertEqual(len(process.pending_delivery), 1, result)
        self.assertEqual(result["research_campaign"]["origin_pending"], 1)
        self.assertEqual(process.pending_delivery[0].community_search_receipts["tavily_social_web"]["status"], "failed")
        require_completed_research(result, candidates=process.pending_delivery)
        job = self.store.enqueue_report(process.pending_delivery, result, report_date=result["report_date"])
        self.assertEqual(job.status, "pending_render")
        self.assertEqual(result["delivery_scope"]["origin_failed"], 1)
        self.assertEqual(result["delivery_scope"]["origin_unattempted"], 0)
        self.assertEqual(result["result_kind"], "complete_memo")
        self.assertEqual(len(self.store.load_pending_origins()), 1)

    async def test_no_usable_inputs_cannot_be_successful_empty(self):
        process = self.process(batch([], outage=True))
        result = await process.run(reference_time=NOW)
        with self.assertRaises(ResearchNotCompleteError):
            self.store.enqueue_report([], result, report_date=result["report_date"])
        self.assertIsNone(self.store.load_pending_report_job())

    async def test_scoped_email_does_not_claim_all_tasks_completed(self):
        bad = origin(2)
        bad.published_at = "2020-01-01T00:00:00+00:00"
        self.store.observe_origins([bad])
        process = self.process(accepted=True)
        result = await process.run(reference_time=NOW)
        require_completed_research(result, candidates=process.pending_delivery)
        report = self.path / ("paradigm_radar_" + result["report_date"] + ".md")
        report.write_text("# 已核验的中文路线报告", encoding="utf-8")
        with (
            patch.object(config, "SMTP_TO", ["offline@example.invalid"]),
            patch.object(config, "SMTP_USE_SSL", True),
            patch("notifications.email_notifier.smtplib.SMTP_SSL") as smtp,
        ):
            _send_sync(report, result)
        message = smtp.return_value.__enter__.return_value.send_message.call_args.args[0]
        body = message.get_body(preferencelist=("plain",)).get_content()
        self.assertIn("候选深挖已尝试：1/1", body)
        self.assertNotIn("全部闭合", body)
        self.assertNotIn("待续", body)
        self.assertNotIn("阶段性", message["Subject"] + body)
        self.assertIn("新路线", message["Subject"])

    async def test_model_missing_all_outputs_cannot_be_no_signal(self):
        item = origin()
        process = self.process(batch([item]), omit={item.fingerprint})
        result = await process.run(reference_time=NOW)
        with self.assertRaises(ResearchNotCompleteError):
            require_completed_research(result, candidates=[])
        self.assertEqual(result["result_kind"], "research_blocked")

    async def test_new_week_supersedes_unfinished_plan_without_claiming_completion(self):
        item = origin()
        first = await self.process(omit={item.fingerprint}).run(reference_time=NOW)
        later = NOW + timedelta(days=8)
        new = origin(3)
        new.published_at = (later - timedelta(days=1)).isoformat()
        second = await self.process(batch([new])).run(reference_time=later)
        self.assertNotEqual(first["research_campaign"]["campaign_id"], second["research_campaign"]["campaign_id"])
        prior = self.store.campaigns.get(first["research_campaign"]["campaign_id"])
        self.assertEqual(prior.status, "superseded")
        self.assertEqual(self.store.campaigns.snapshot(prior.campaign_id)["origin_pending"], 1)
        require_completed_research(second)
        self.assertEqual(second["reference_time"], later.isoformat())

    async def test_durable_candidate_change_invalidates_ready_delivery_proof(self):
        process = self.process(accepted=True)
        result = await process.run(reference_time=NOW)
        self.assertTrue(process.pending_delivery)
        changed = copy.deepcopy(process.pending_delivery[0])
        changed.mechanism += "已修改"
        self.store.save_candidates([changed])
        with self.assertRaises(ResearchNotCompleteError):
            self.store.enqueue_report(process.pending_delivery, result, report_date=result["report_date"])
        self.assertIsNone(self.store.load_pending_report_job())

    async def test_forged_empty_receipt_is_recomputed_from_durable_queue(self):
        item = origin()
        result = await self.process(omit={item.fingerprint}).run(reference_time=NOW)
        result["result_kind"] = "complete_no_signal"
        result["delivery_scope"].update(origin_completed=1, scoped_pending_count=0)
        result["research_campaign"].update(origin_completed=1, pending_total_count=0)
        self.store.campaigns.save_stats(result["research_campaign"]["campaign_id"], result, ready=True)
        with self.assertRaises(ResearchNotCompleteError):
            self.store.enqueue_report([], result, report_date=result["report_date"])

    async def test_nonempty_pipeline_report_and_confirmed_delivery_with_source_failure(self):
        process = self.process(accepted=True)
        fixed_run = process.run
        async def run_at_fixed_time():
            return await fixed_run(reference_time=NOW)
        process.run = run_at_fixed_time
        url = origin().url
        route = ("### 状态预测开始容纳可行动表示\n\n" +
                 "这条路线解释训练信号、推理接口与能力边界之间的因果关系。" * 30 +
                 f"\n\nA. Researcher 在推动这条路线。[论文原文]({url})。\n\n" +
                 "**讨论势能判断：** 当前主要是已核验发布方提出的机制，尚无独立复现；Tavily 本轮不可用，不据此推断没有讨论。")
        memo = "本期技术路线从旧方法的边界出发，解释设计思想如何落到训练与推理。" * 18
        frame = f"## 本期研究 Memo\n\n{memo}\n\n## 接下来真正值得盯的信号\n\n观察独立复现。"
        def response(text):
            return SimpleNamespace(choices=[SimpleNamespace(finish_reason="stop", message=SimpleNamespace(content=text))])
        editor = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(
            create=AsyncMock(side_effect=[response(route), response(frame)]))))
        generator = ParadigmReportGenerator(self.path, client=editor, model="offline-editor")
        with (
            patch("main._check_env"), patch("main._print_model_banner"),
            patch("agents.paradigm_orchestrator.ParadigmOrchestrator", return_value=process),
            patch("reports.paradigm_generator.ParadigmReportGenerator", return_value=generator),
            patch("notifications.email_notifier.send_report_email", AsyncMock(return_value=True)),
            patch("main._write_pipeline_result"), patch.object(run_audit, "write", return_value={"audit_markdown_path": str(self.path / "audit.md")}),
            patch.object(config, "EMAIL_PUSH_ENABLED", True), patch.object(config, "EMAIL_PUSH_REQUIRED", True),
        ):
            result = await main._run_pipeline_once()
        self.assertTrue(result["email_sent"])
        self.assertEqual(result["result_kind"], "complete_memo")
        content = Path(result["report_path"]).read_text()
        self.assertIn(url, content)
        self.assertIn("本期取材范围", content)
        self.assertNotIn("阶段性", content)
        self.assertIsNone(self.store.load_pending_report_job())
        self.assertIsNone(self.store.campaigns.active())

    async def test_bad_selected_input_cannot_become_successful_empty(self):
        item = origin()
        item.published_at = {"unexpected": "object"}
        process = self.process(batch([item]))
        result = await process.run(reference_time=NOW)
        self.assertEqual(result["discovery_rejected_origins"], 1)
        with self.assertRaises(ResearchNotCompleteError):
            require_completed_research(result)
        self.assertIsNone(self.store.load_pending_report_job())

    async def test_bad_input_is_isolated_before_campaign_persistence(self):
        item = origin(2)
        item.published_at = {"unexpected": "object"}
        process = self.process(batch([origin(), item]), accepted=True)
        result = await process.run(reference_time=NOW)
        self.assertEqual(len(process.pending_delivery), 1)
        self.assertEqual(result["discovery_rejected_origins"], 1)
        self.store.enqueue_report(process.pending_delivery, result, report_date=result["report_date"])

    async def test_non_json_source_record_does_not_poison_healthy_campaign(self):
        bad = origin(2)
        bad.raw["unexpected"] = {"not", "json"}
        process = self.process(batch([origin(), bad]), accepted=True)
        result = await process.run(reference_time=NOW)
        self.assertEqual(result["discovery_rejected_origins"], 1)
        self.assertEqual(len(process.pending_delivery), 1)
        self.store.enqueue_report(process.pending_delivery, result, report_date=result["report_date"])

    async def test_verified_historical_update_can_deliver_without_new_primary_release(self):
        first = self.process(accepted=True)
        stats = await first.run(reference_time=NOW)
        job = self.store.enqueue_report(first.pending_delivery, stats, report_date=stats["report_date"])
        self.store.mark_delivery_delivered(job.delivery_key, self.path / "offline-first.md")
        later = NOW + timedelta(days=8)
        process = self.process(batch([]), accepted=True)
        async def refresh(items, supporting):
            for item in items:
                item.evidence.append(TechnicalEvidence(source="independent-lab",
                    evidence_type=EvidenceType.PRODUCT_ADOPTION, title="Independent adoption",
                    url="https://example.org/adoption", published_at=(later - timedelta(days=1)).isoformat(),
                    raw={"independence": "independent"}))
                record_search(item, "hackernews", "completed")
            return items
        process.enricher.refresh = refresh
        stats = await process.run(reference_time=later)
        self.assertEqual(len(process.pending_delivery), 1, stats)
        self.assertEqual(stats["delivery_scope"]["origin_completed"], 0)
        self.assertEqual(stats["delivery_scope"]["refresh_completed"], 1)
        self.assertEqual(process.pending_delivery[0].freshness_assessment["classification"], "historical_reactivated")
        self.store.enqueue_report(process.pending_delivery, stats, report_date=stats["report_date"])

    async def test_every_qualified_hypothesis_is_deeply_researched_despite_old_cap(self):
        process = self.process(batch([origin(i) for i in range(1, 4)]))
        async def extract(items):
            return [ParadigmExtraction(evidence=item, is_candidate=True,
                canonical_name=item.title, thesis="机制假说", problem_shift="改变状态接口", mechanism="动作约束转移",
                keywords=[item.title], innovation_types=["architecture"],
                rubric_assessment=rubric_assessment(stage="screening")) for item in items]
        async def reject(items):
            rubric = load_rubric()
            weak = {value["id"] for value in rubric["common_criteria"] + rubric["type_criteria"]["architecture"]}
            for item in items:
                item.rubric_assessment = rubric_assessment(stage="final", weak_ids=weak)
            return items
        process.analyzer.run = extract
        process.synthesizer.run = reject
        with patch.object(config, "PARADIGM_RESEARCH_DEEP_LIMIT", 1):
            result = await process.run(reference_time=NOW)
            require_completed_research(result, candidates=[])
            self.store.enqueue_report([], result, report_date=result["report_date"])
        self.assertEqual(result["delivery_scope"]["deep_planned"], 3)
        self.assertEqual(result["delivery_scope"]["deep_completed"], 3)
        self.assertEqual(len(result["delivery_scope"]["deep_scope_keys"]), 3)
        self.assertTrue(result["delivery_scope"]["deep_scope_sealed"])
        self.assertEqual(len(self.store.load_pending_deep_candidates()), 0)

    async def test_failed_discovery_commit_rolls_back_scope_and_source_acknowledgement(self):
        process = self.process()
        with patch.object(self.store, "observe_origins", side_effect=OSError("offline storage failure")):
            with self.assertRaises(OSError):
                await process.run(reference_time=NOW)
        campaign = self.store.campaigns.active()
        self.assertIsNone(campaign.discovery)
        self.assertNotIn("origin_scope", campaign.stats)
        self.assertEqual(self.store.load_pending_origins(), [])
        result = await self.process().run(reference_time=NOW)
        require_completed_research(result, candidates=[])

    async def test_full_58_hypothesis_cohort_is_not_a_top_k_sample(self):
        origins = [origin(index) for index in range(1, 4)]
        for item in origins:
            item.raw["origin_kind"] = "technical_report"
        process = self.process(batch(origins))
        origins_seen, deep_seen = set(), set()

        async def extract(items):
            origins_seen.update(item.fingerprint for item in items)
            return [ParadigmExtraction(evidence=item, is_candidate=True,
                canonical_name=f"Independent mechanism {item.title}-{index}",
                thesis="独立机制假说",
                problem_shift=f"Distinct causal interface {item.title}-{index}",
                mechanism=f"Distinct intervention {item.title}-{index}",
                keywords=[f"mechanism-{item.fingerprint}-{index}"], innovation_types=["architecture"],
                rubric_assessment=rubric_assessment(stage="screening"))
                for item in items for index in range(18 if item.fingerprint == origins[-1].fingerprint else 20)]

        async def conclude(items):
            self.assertEqual(len(origins_seen), 3, "deep work cannot precede cohort discovery")
            for item in items:
                deep_seen.add(item.key)
                weak = {value["id"] for value in load_rubric()["common_criteria"] + load_rubric()["type_criteria"]["architecture"]}
                item.rubric_assessment = rubric_assessment(stage="final", weak_ids=weak)
            return items

        process.analyzer.run = extract
        process.synthesizer.run = conclude
        with patch.object(config, "PARADIGM_RESEARCH_DEEP_LIMIT", 1), patch.object(config, "PARADIGM_DEEP_SAFETY_LIMIT", 1):
            result = await process.run(reference_time=NOW)
        require_completed_research(result, candidates=[])
        self.assertEqual(len(deep_seen), 58)
        self.assertEqual(result["delivery_scope"]["deep_planned"], 58)
        self.assertEqual(result["delivery_scope"]["deep_completed"], 58)
        self.assertEqual(set(result["delivery_scope"]["deep_scope_keys"]), deep_seen)
        self.assertEqual(self.store.load_pending_deep_candidates(), [])

    async def test_missing_deep_output_is_local_failure_and_healthy_peers_deliver(self):
        process = self.process(batch([origin(index) for index in range(1, 4)]), accepted=True)
        async def extract(items):
            return [ParadigmExtraction(evidence=item, is_candidate=True, canonical_name=item.title,
                thesis="独立机制假说",
                problem_shift=item.title, mechanism=item.title, keywords=[item.title],
                innovation_types=["architecture"], rubric_assessment=rubric_assessment(stage="screening")) for item in items]
        base = process.synthesizer.run
        async def omit_one(items):
            return await base([item for item in items if item.name != origin(2).title])
        process.analyzer.run = extract
        process.synthesizer.run = omit_one
        result = await process.run(reference_time=NOW)
        self.assertEqual(result["delivery_scope"]["deep_planned"], 3)
        self.assertEqual(result["delivery_scope"]["deep_completed"], 2)
        self.assertEqual(len(self.store.load_pending_deep_candidates()), 1)
        self.assertEqual(result["delivery_scope"]["deep_attempted"], 3)
        self.assertEqual(result["delivery_scope"]["deep_failed"], 1)
        self.assertEqual(len(process.pending_delivery), 2)
        self.store.enqueue_report(process.pending_delivery, result, report_date=result["report_date"])

    async def test_completed_identity_search_without_match_is_observation_not_execution_pending(self):
        process = self.process(accepted=True)
        base = process.enricher.run
        async def no_match(items, supporting):
            result = await base(items, supporting)
            for item in result:
                item.researchers[0].profile_urls = {}
                item.researchers[0].identifiers = {}
                item.researchers[0].contact_search_notes = ["已检索 OpenAlex，无当前论文匹配；无已核验主页"]
                item.researchers[0].identity_search_status = "completed"
                item.researchers[0].contact_search_status = "completed"
            return result
        process.enricher.run = no_match
        result = await process.run(reference_time=NOW)
        self.assertEqual(result["delivery_profile_observed_count"], 1)
        self.assertEqual(result["delivery_profile_deferred_count"], 0)
        self.assertEqual(result["delivery_scope"]["deep_completed"], 1)
        require_completed_research(result, candidates=[])

    async def test_no_named_authors_is_observation_after_full_research_not_permanent_pending(self):
        item = origin()
        item.authors = []
        process = self.process(batch([item]), accepted=True)
        base = process.enricher.run
        async def no_person(items, supporting):
            values = await base(items, supporting)
            for value in values:
                value.researchers = []
            return values
        process.enricher.run = no_person
        result = await process.run(reference_time=NOW)
        self.assertEqual(result["delivery_profile_observed_count"], 1)
        self.assertEqual(result["delivery_profile_deferred_count"], 0)
        self.assertEqual(result["delivery_scope"]["deep_completed"], 1)
        require_completed_research(result, candidates=[])

    async def test_missing_profile_for_named_author_remains_execution_pending(self):
        process = self.process(accepted=True)
        base = process.enricher.run
        async def lost_profile(items, supporting):
            values = await base(items, supporting)
            for value in values:
                value.researchers = []
            return values
        process.enricher.run = lost_profile
        result = await process.run(reference_time=NOW)
        self.assertEqual(result["delivery_profile_observed_count"], 0)
        self.assertEqual(result["delivery_profile_deferred_count"], 1)
        with self.assertRaises(ResearchNotCompleteError):
            require_completed_research(result, candidates=[])

    async def test_final_gate_matches_partial_provider_policy_but_blocks_total_outage(self):
        process = self.process(accepted=True)
        result = await process.run(reference_time=NOW)
        self.assertEqual(len(process.pending_delivery), 1)
        require_completed_research(result, candidates=process.pending_delivery)
        record_search(process.pending_delivery[0], "hackernews", "failed", reason="http_503")
        with self.assertRaisesRegex(ResearchNotCompleteError, "未闭合的社区查询"):
            require_completed_research(result, candidates=process.pending_delivery)

    async def test_manifest_reconciliation_cannot_drop_a_completed_qualified_candidate(self):
        process = self.process(accepted=True)
        result = await process.run(reference_time=NOW)
        campaign_id = result["research_campaign"]["campaign_id"]
        with self.store._connect() as conn:
            conn.execute("DELETE FROM campaign_work WHERE campaign_id=? AND kind='deep'", (campaign_id,))
        ledger = self.store.campaigns.reconcile(campaign_id)
        self.assertEqual(ledger["deep_scope_keys"], [process.pending_delivery[0].key])
        self.assertEqual(ledger["deep_planned"], 1)
        self.assertEqual(ledger["deep_completed"], 1)

    async def test_legacy_explicit_resume_stops_before_discovery_without_changing_state(self):
        campaign = self.store.campaigns.begin(reference_time=NOW, ordinary_days=7,
            high_signal_days=60, bootstrap=True, seeds=[])
        process = self.process()
        with self.assertRaisesRegex(RuntimeError, "旧版研究计划"):
            await process.run(reference_time=NOW, resume_only=True)
        process.discovery.run.assert_not_awaited()
        self.assertEqual(self.store.campaigns.active().campaign_id, campaign.campaign_id)


class V0PolicyTests(unittest.TestCase):
    def test_malformed_source_health_cannot_supply_usable_inputs(self):
        for value in (None, [], {"source_health": []}, {"source_health": {"arxiv": "completed"}}):
            self.assertEqual(acquisition_summary(value)["usable_primary_sources"], [])
        value = {"source_health": {"arxiv": {"status": "completed"}},
                 "academic_indexes": {"arxiv": {"planned_queries": 1, "completed_queries": 0}}}
        self.assertEqual(acquisition_summary(value)["usable_primary_sources"], [])

    def test_audit_readiness_does_not_greenify_failure_rows_or_full_acceptance(self):
        value = {"code_changed_during_run": False, "planned_entries": 3, "returned_entries": 3,
            "acceptance_complete": False, "entries": [{"kind": "official", "status": "failed"},
                {"kind": "arxiv", "status": "passed"}, {"kind": "hf", "status": "passed"}]}
        self.assertTrue(v0_source_readiness(value)["ready"])
        self.assertFalse(value["acceptance_complete"])
        self.assertEqual(value["entries"][0]["status"], "failed")
        value["selected_entries"] = ["arxiv:one"]
        self.assertFalse(v0_source_readiness(value)["ready"])
        value.pop("selected_entries")
        value["code_changed_during_run"] = True
        self.assertFalse(v0_source_readiness(value)["ready"])

    def test_many_queries_on_one_provider_are_not_multiple_input_capabilities(self):
        value = {"code_changed_during_run": False, "planned_entries": 50, "returned_entries": 50,
                 "entries": [{"kind": "arxiv", "status": "passed"} for _ in range(50)]}
        self.assertFalse(v0_source_readiness(value)["ready"])
    def test_diverse_selection_does_not_turn_unselected_materials_into_rejections(self):
        items = [origin(i) for i in range(6)]
        other = origin(10)
        other.organization, other.source = "Other Lab", "openalex"
        selected = bounded_origins([*items, other], limit=2, reference_time=NOW, days=7)
        self.assertEqual({item.organization for item in selected}, {"OpenAI", "Other Lab"})
        self.assertTrue(all("rejection_reason" not in item.raw for item in items))

    def test_partial_platform_failure_is_not_zero_uptake_and_total_outage_blocks(self):
        item = candidate()
        record_search(item, "tavily_social_web", "failed")
        self.assertEqual(blocking_searches(item, allow_partial=True), ["tavily_social_web"])
        record_search(item, "hackernews", "completed")
        self.assertEqual(blocking_searches(item, allow_partial=True), [])
        self.assertEqual(item.community_search_receipts["tavily_social_web"]["status"], "failed")

    def test_model_validation_accepts_configurable_compatible_endpoint(self):
        model = SimpleNamespace(provider="custom", model="my-json-model", api_key="offline-only",
                                base_url="https://inference.example/v1")
        self.assertEqual(_model_check("test", model).status, "ready")
        model.model = "deepseek-chat"
        self.assertEqual(_model_check("test", model).status, "ready")
        model.base_url = ""
        self.assertEqual(_model_check("test", model).status, "missing")
