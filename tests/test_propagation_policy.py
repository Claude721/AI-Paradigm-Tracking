"""Fixed-clock first-principles contracts; no API, SMTP or production state."""
import copy
import hashlib
import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from database.paradigm_store import ParadigmStore
from paradigms.models import EvidenceType, TechnicalEvidence, assess_candidate_freshness, scrub_ephemeral_evidence
from paradigms.propagation import (certify_diffusion, verified_diffusion, history_recall_plan,
                                   diffusion_items, event_signature)
from paradigms.rubric import substantive_secondary
from paradigms.scoring import score_candidate, is_reportable
from paradigms.enrichment import EvidenceEnricher
from paradigms.analyzer import ParadigmSynthesizer
from paradigms.evidence_association import mechanism_digest, support_revision
from paradigms.search_receipts import blocking_searches, incomplete_searches, record_search
from reports.paradigm_generator import _compact_route_dossier
from runtime_clock import research_window
from tests.test_paradigm_pipeline import candidate, rubric_assessment
from tests import test_v0_delivery as fixture
from tests.test_production_failure_regressions import synthesis_payload


NOW = datetime(2026, 10, 26, tzinfo=timezone.utc)


def route(index=1, *, age=28):
    item = TechnicalEvidence(source="arxiv", evidence_type=EvidenceType.PRIMARY_PAPER,
        title=f"Predictive Latent Actions for Composable State Planning {index}",
        url=f"https://arxiv.org/abs/2609.{index:05d}", identifiers={"arxiv": f"2609.{index:05d}"},
        published_at=(NOW - timedelta(days=age)).isoformat(), authors=[f"Unknown Researcher {index}"],
        summary="A new latent state transition used for composable planning.")
    value = candidate([item]); value.key = f"route-{index}"
    return value


def post(value, *, age=1, indexed=False):
    return TechnicalEvidence(source="tavily-web" if indexed else "hackernews",
        evidence_type=EvidenceType.COMMUNITY_DISCUSSION, title=value.evidence[0].title,
        url=f"https://news.ycombinator.com/item?id={1000 + int(value.key.split('-')[-1])}",
        published_at=(NOW-timedelta(days=age)).isoformat(), summary="Worth reading!",
        authors=["Ordinary Member"], metrics={"score":100, "comments":30},
        raw={"relationship":"exact_work_reference", "ephemeral_content":True,
             "social_platform":"hackernews", **({"indexed_discovery_only":True} if indexed else {})})


class AttentionContracts(unittest.TestCase):
    def test_unknown_team_without_attention_can_pass_technical_eligibility(self):
        value = route(age=1)
        with research_window(NOW):
            score_candidate(value)
            self.assertTrue(is_reportable(value))
            self.assertEqual(value.publisher_tier, "unknown")
            self.assertEqual(assess_candidate_freshness(value,window_days=7)["decision"], "include")

    def test_attention_does_not_rescue_a_failed_technical_judgment(self):
        value = route(age=1); value.evidence.append(post(value))
        weak = {row["criterion_id"] for row in value.rubric_assessment["answers"]}
        value.rubric_assessment = rubric_assessment(stage="final", weak_ids=weak)
        score_candidate(value)
        self.assertFalse(is_reportable(value))

    def test_popular_short_share_is_attention_but_not_independent_validation(self):
        value=route(); share=post(value); value.evidence.append(share)
        with research_window(NOW):
            score_candidate(value)
            self.assertTrue(verified_diffusion(value,share))
            self.assertEqual(substantive_secondary(value)[0],0)
            self.assertEqual(assess_candidate_freshness(value,window_days=7)["classification"],"historical_reactivated")
            dossier=_compact_route_dossier(value)
            self.assertEqual(len(dossier["momentum_evidence"]),1)
            self.assertIn("不证明技术验证",dossier["objective_momentum_signals"][0])

    def test_fame_and_audience_do_not_change_the_technical_score(self):
        quiet,loud=route(age=1),route(age=1)
        loud.evidence.append(post(loud)); loud.evidence[-1].metrics.update(score=100000,comments=50000)
        score_candidate(quiet);score_candidate(loud)
        self.assertEqual(quiet.total_score,loud.total_score)
        self.assertTrue(is_reportable(quiet));self.assertTrue(is_reportable(loud))

    def test_index_is_a_research_lead_not_a_verified_public_event(self):
        value=route(); value.evidence.append(post(value,indexed=True));certify_diffusion(value)
        self.assertFalse(diffusion_items(value))
        self.assertEqual(assess_candidate_freshness(value,reference_time=NOW,window_days=7)["decision"],"defer")
        plan,stats=history_recall_plan([route()], [post(value,indexed=True)],limit=0,reference_time=NOW)
        self.assertEqual(len(plan),1);self.assertEqual(stats["event_recalled_routes"],1)

    def test_old_or_undated_or_future_share_cannot_manufacture_current_freshness(self):
        for age in (8,-1,-2):
            value=route();value.evidence.append(post(value,age=age));certify_diffusion(value)
            self.assertEqual(assess_candidate_freshness(value,reference_time=NOW,window_days=7)["decision"],"defer")
        value=route();value.evidence.append(post(value));value.evidence[-1].published_at=""
        certify_diffusion(value);self.assertFalse(diffusion_items(value))

    def test_unrelated_or_foreign_host_cannot_be_certified(self):
        for change in ("title","url"):
            value=route();share=post(value)
            if change=="title":share.title="Unrelated clinical decision models"
            else:share.url="https://foreign.example/item?id=1001"
            value.evidence.append(share);certify_diffusion(value)
            self.assertFalse(diffusion_items(value))

    def test_title_prefix_is_not_another_work_identity(self):
        value=route(1);value.evidence.append(post(route(10)))
        certify_diffusion(value);self.assertFalse(diffusion_items(value))

    def test_receipt_survives_scrubbing_and_paraphrase_not_another_route_or_source_revision(self):
        value=route();share=post(value);value.evidence.append(share);certify_diffusion(value)
        scrub_ephemeral_evidence(share);self.assertTrue(verified_diffusion(value,share))
        value.mechanism="同一工作机制的另一种表述"
        self.assertTrue(verified_diffusion(value,share))
        for change in ("key","source"):
            other=copy.deepcopy(value)
            if change=="source":other.evidence[0].source_revision="new-source-revision"
            else:setattr(other,change,"another")
            self.assertFalse(verified_diffusion(other,other.evidence[1]))

    def test_sqlite_then_main_paraphrase_keeps_current_work_event_not_technical_uptake(self):
        value=route();value.evidence.append(post(value));certify_diffusion(value)
        with tempfile.TemporaryDirectory() as directory, research_window(NOW):
            store=ParadigmStore(Path(directory)/"state.db");store.save_candidates([value])
            restored=store.load_candidate_snapshots([value.key])[0]
            self.assertNotEqual(restored.evidence[1].title,value.evidence[0].title)
            self.assertEqual(restored.evidence[1].summary, "")
            payload=synthesis_payload([0])
            payload["mechanism"]="从未标注视频学习离散的潜在动作，并用这些动作预测未来状态。"
            ParadigmSynthesizer._apply_synthesis_payload(restored,payload,visible_evidence_indices={0,1})
            score_candidate(restored)
            self.assertTrue(is_reportable(restored))
            self.assertEqual(len(diffusion_items(restored)),1)
            self.assertEqual(substantive_secondary(restored)[0],0)
            self.assertEqual(assess_candidate_freshness(restored,window_days=7)["classification"],"historical_reactivated")
            self.assertEqual(len(_compact_route_dossier(restored)["momentum_evidence"]),1)
            store.save_candidates([restored])
            again=store.load_candidate_snapshots([restored.key])[0]
            self.assertEqual(again.report_signature,restored.report_signature)
            self.assertEqual(len(diffusion_items(again)),1)

    def test_work_receipt_depends_on_its_actual_origin_not_an_unrelated_cluster_member(self):
        value=route();share=post(value);value.evidence.append(share);certify_diffusion(value)
        other=route(2).evidence[0];value.evidence.insert(1,other)
        self.assertTrue(verified_diffusion(value,share))
        self.assertEqual(value.evidence[-1].raw["diffusion_receipt"]["origins"],
                         {value.evidence[0].fingerprint:support_revision(value.evidence[0])})
        scrub_ephemeral_evidence(share)
        payload=synthesis_payload([1]);payload["excluded_evidence_indices"]=[0]
        ParadigmSynthesizer._apply_synthesis_payload(value,payload,visible_evidence_indices={0,1,2})
        self.assertEqual(diffusion_items(value),[])
        self.assertIn(route().evidence[0].fingerprint,{x.fingerprint for x in value.excluded_evidence})

    def test_unverified_origin_cannot_lend_identity_to_a_visibility_receipt(self):
        value=route();share=post(value);value.evidence.append(share)
        value.evidence[0].raw["association_status"]="unverified"
        certify_diffusion(value)
        self.assertEqual(diffusion_items(value),[])
        self.assertNotIn("diffusion_receipt",share.raw)

    def test_post_url_date_or_source_change_cannot_reuse_a_scrubbed_receipt(self):
        value=route();value.evidence.append(post(value));certify_diffusion(value)
        scrub_ephemeral_evidence(value.evidence[1])
        for field,changed in (("url","https://news.ycombinator.com/item?id=9001"),
                              ("published_at",NOW.isoformat()),("source","tavily-web")):
            with self.subTest(field=field):
                other=copy.deepcopy(value);setattr(other.evidence[1],field,changed)
                certify_diffusion(other)
                self.assertFalse(verified_diffusion(other,other.evidence[1]))

    def test_only_a_still_valid_legacy_receipt_can_migrate_before_main_rewrites(self):
        value=route();share=post(value);value.evidence.append(share)
        binding={"route_key":value.key,"mechanism":mechanism_digest(value),
                 "origins":[(value.evidence[0].fingerprint,support_revision(value.evidence[0]))]}
        share.raw["diffusion_receipt"]={"version":1,
            "binding":hashlib.sha256(json.dumps(binding,sort_keys=True).encode()).hexdigest(),
            "fingerprint":share.fingerprint,"url":share.url,"published_at":share.published_at,
            "association":"exact_work_title"}
        scrub_ephemeral_evidence(share)
        for changed in (False,True):
            with self.subTest(source_changed=changed):
                other=copy.deepcopy(value)
                if changed:other.evidence[0].source_revision="new-source-revision"
                payload=synthesis_payload([0]);payload["mechanism"]="学习离散的潜在动作，用动作约束预测未来状态。"
                ParadigmSynthesizer._apply_synthesis_payload(other,payload,visible_evidence_indices={0,1})
                self.assertEqual(bool(diffusion_items(other)),not changed)
                if not changed:self.assertEqual(other.evidence[1].raw["diffusion_receipt"]["version"],2)

    def test_same_work_publisher_post_is_visibility_not_independent_support(self):
        value=route();share=post(value);share.raw["relationship"]="author_self_release"
        value.evidence.append(share);certify_diffusion(value)
        self.assertEqual(value.propagation_assessment["verified_public_post_count"],1)
        self.assertTrue(value.propagation_assessment["public_posts"][0]["publisher_self_release"])
        self.assertEqual(substantive_secondary(value)[0],0)

    def test_complete_supplementary_outage_is_not_a_veto_or_successful_negative(self):
        value=route();record_search(value,"hackernews","failed",reason="http_503")
        self.assertTrue(incomplete_searches(value));self.assertEqual(blocking_searches(value,allow_partial=True),[])
        self.assertTrue(blocking_searches(value,allow_partial=False))

    def test_attention_receipt_and_metrics_survive_sqlite_without_temporary_text(self):
        value=route();value.evidence.append(post(value));certify_diffusion(value)
        with tempfile.TemporaryDirectory() as directory:
            store=ParadigmStore(Path(directory)/"state.db");store.save_candidates([value])
            restored=store.load_candidate_snapshots([value.key])[0]
            self.assertTrue(diffusion_items(restored))
            self.assertEqual(restored.evidence[-1].summary,"")
            self.assertEqual(restored.evidence[-1].authors,[])
            self.assertEqual(restored.report_signature,value.report_signature)

    def test_repost_growth_of_an_old_post_is_a_dated_event_not_a_perpetual_update(self):
        value=route();share=post(value,age=20);value.evidence.append(share);certify_diffusion(value)
        previous=value.report_signature
        share.raw.update(metric_delta={"retweets":20},metric_delta_observed_at=NOW.isoformat())
        self.assertNotEqual(value.report_signature,previous)
        self.assertEqual(assess_candidate_freshness(value,reference_time=NOW,window_days=7)["decision"],"include")
        self.assertEqual(assess_candidate_freshness(value,reference_time=NOW+timedelta(days=8),window_days=7)["decision"],"defer")


class RecallContracts(unittest.TestCase):
    def test_all_event_matches_bypass_background_four_not_the_rubric(self):
        values=[route(i) for i in range(1,13)]
        for value in values:value.status="observe"
        values[-1].status="pending_deep";values[-2].status="rejected"
        events=[post(value) for value in values[4:]]
        selected,stats=history_recall_plan(values,events,limit=4,reference_time=NOW)
        self.assertEqual({x.key for x in selected},{x.key for x in values})
        self.assertEqual(stats["event_recalled_routes"],8)
        self.assertTrue(all(x.status in {"observe","pending_deep","rejected"} for x in selected))

    def test_identical_record_does_not_retrigger_event_every_week(self):
        value=route();share=post(value);value.evidence.append(share)
        selected,stats=history_recall_plan([value],[share],limit=0,reference_time=NOW)
        self.assertEqual(selected,[])
        changed=copy.deepcopy(share);changed.metrics["score"]=10000
        self.assertNotEqual(event_signature(changed),event_signature(share))
        self.assertEqual(history_recall_plan([value],[changed],limit=0,reference_time=NOW)[1]["event_recalled_routes"],1)

    def test_previously_screened_unlinked_origin_reopens_once_without_fake_source_change(self):
        value=route();share=post(value)
        with tempfile.TemporaryDirectory() as directory:
            store=ParadigmStore(Path(directory)/"state.db")
            original=store.observe_origins(value.evidence)[0][0];store.mark_evidence([original],analyzed=True)
            recalled=store.load_event_recalled_origins([share],reference_time=NOW,window_days=7)
            self.assertEqual(len(recalled),1)
            revisited=store.observe_origins(recalled)[0]
            self.assertEqual(len(revisited),1);self.assertEqual(revisited[0].source_revision,original.source_revision)
            store.mark_evidence(revisited,analyzed=True)
            self.assertEqual(store.load_event_recalled_origins([share],reference_time=NOW,window_days=7),[])

    def test_disappearing_old_event_does_not_reopen_a_completed_origin_on_the_same_remaining_share(self):
        value=route();one,two=post(value),post(value)
        two.url+="&thread=second"
        with tempfile.TemporaryDirectory() as directory:
            store=ParadigmStore(Path(directory)/"state.db")
            original=store.observe_origins(value.evidence)[0][0];store.mark_evidence([original],analyzed=True)
            recalled=store.load_event_recalled_origins([one,two],reference_time=NOW,window_days=7)
            revisited=store.observe_origins(recalled)[0];store.mark_evidence(revisited,analyzed=True)
            self.assertEqual(store.load_event_recalled_origins([two],reference_time=NOW,window_days=7),[])

    def test_stale_event_recall_cannot_overwrite_a_newer_source_observation(self):
        value=route()
        with tempfile.TemporaryDirectory() as directory:
            store=ParadigmStore(Path(directory)/"state.db")
            original=store.observe_origins(value.evidence)[0][0];store.mark_evidence([original],analyzed=True)
            recalled=store.load_event_recalled_origins([post(value)],reference_time=NOW,window_days=7)
            newer=copy.deepcopy(original);newer.summary="A newer original source publication body."
            observed=store.observe_origins([newer])[0][0];store.mark_evidence([observed],analyzed=True)
            selected,_,receipt=store.observe_origins(recalled)
            self.assertEqual(selected,[]);self.assertEqual(receipt.stale_revision_count,1)
            with store._connect() as con:
                signature=con.execute("SELECT source_signature FROM origin_research_state WHERE fingerprint=?",(original.fingerprint,)).fetchone()[0]
            self.assertEqual(signature,observed.source_revision)


class RefreshContracts(unittest.IsolatedAsyncioTestCase):
    async def test_real_refresh_accepts_short_direct_post_without_a_model_substantive_label(self):
        value=route();enricher=EvidenceEnricher()
        enricher.community=SimpleNamespace(search=AsyncMock(return_value=[post(value)]),coverage=lambda:{})
        with research_window(NOW):
            result=await enricher.refresh([value],[])
            self.assertEqual(len(result),1);self.assertTrue(diffusion_items(result[0]))


class DurableWeeklyRecallTests(unittest.IsolatedAsyncioTestCase):
    setUp=fixture.V0DeliveryTests.setUp
    process=fixture.V0DeliveryTests.process

    def attach_events(self,process):
        base=process.enricher.run
        async def enrich(items,supporting):
            values=await base(items,supporting)
            for value in values:
                EvidenceEnricher._attach_support(value,supporting)
                certify_diffusion(value)
            return values
        process.enricher.run=enrich

    async def test_old_observation_outside_four_is_recalled_delivered_then_deduplicated(self):
        values=[route(i) for i in range(1,7)]
        for value in values:
            value.evidence= self.store.observe_origins(value.evidence)[0]
            self.store.mark_evidence(value.evidence,analyzed=True)
            value.status="observe";self.store.save_candidates([value])
        hot=values[-1];event=post(hot)
        discovered=fixture.batch([]);discovered.supporting=[event]
        process=self.process(discovered,accepted=True)
        actual=EvidenceEnricher();actual.community=SimpleNamespace(search=AsyncMock(return_value=[]),coverage=lambda:{})
        process.enricher.refresh=actual.refresh
        stats=await process.run(reference_time=NOW)
        self.assertEqual(stats["history_recall"]["event_recalled_routes"],1)
        delivered=[x for x in process.pending_delivery if x.key==hot.key]
        self.assertEqual(len(delivered),1,stats)
        self.assertEqual(delivered[0].freshness_assessment["classification"],"historical_reactivated")
        self.assertEqual(delivered[0].report_kind,"update")
        job=self.store.enqueue_report(process.pending_delivery,stats,report_date=stats["report_date"])
        self.store.mark_delivery_delivered(job.delivery_key,self.path/"offline.md")
        self.assertEqual(self.store.prepare_report(delivered),[])

    async def test_prior_pending_deep_can_join_a_new_week_without_reset_or_current_origin(self):
        value=route();value.evidence=self.store.observe_origins(value.evidence)[0]
        self.store.mark_evidence(value.evidence,analyzed=True)
        value.status="pending_deep";self.store.save_candidates([value])
        process=self.process(fixture.batch([]),accepted=True)
        stats=await process.run(reference_time=NOW)
        self.assertEqual(stats["research_campaign"]["deep_planned"],1)
        self.assertEqual(stats["research_campaign"]["deep_completed"],1)

    async def test_raw_observation_without_a_route_is_revisited_on_a_current_event(self):
        value=route();original=self.store.observe_origins(value.evidence)[0][0]
        self.store.mark_evidence([original],analyzed=True)
        discovered=fixture.batch([]);discovered.supporting=[post(value)]
        process=self.process(discovered,accepted=True);self.attach_events(process)
        stats=await process.run(reference_time=NOW)
        self.assertEqual(stats["event_recalled_origins"],1)
        self.assertEqual(stats["research_campaign"]["origin_completed"],1)
        self.assertEqual(len(process.pending_delivery),1,stats)
        self.assertEqual(process.pending_delivery[0].evidence[0].source_revision,original.source_revision)
        self.assertEqual(process.pending_delivery[0].report_kind,"update")

    async def test_fresh_origin_overlap_keeps_seen_events_through_real_runs(self):
        value=route(age=10);original=self.store.observe_origins(value.evidence)[0][0]
        self.store.mark_evidence([original],analyzed=True)
        one,two=post(value),post(value);two.url="https://news.ycombinator.com/item?id=2001"

        async def run_with(events):
            discovered=fixture.batch(copy.deepcopy(value.evidence));discovered.supporting=events
            process=self.process(discovered,accepted=False);process.delivery_contract_version=5
            process.analyzer.run=AsyncMock(side_effect=process.analyzer.run)
            stats=await process.run(reference_time=NOW)
            job=self.store.enqueue_report(process.pending_delivery,stats,report_date=stats["report_date"])
            self.store.mark_delivery_delivered(job.delivery_key,self.path/"offline-receipt.md")
            return process,stats

        first,stats=await run_with([one,two])
        self.assertEqual(stats["event_recalled_origins"],1)
        self.assertEqual(stats["research_campaign"]["origin_completed"],1)
        self.assertEqual(first.analyzer.run.await_count,1)
        self.assertEqual(self.store.load_event_recalled_origins([two],reference_time=NOW,window_days=7),[])
        second,stats=await run_with([two])
        self.assertEqual(second.analyzer.run.await_count,0)
        self.assertEqual(stats["event_recalled_origins"],0)
        third=copy.deepcopy(two);third.url="https://news.ycombinator.com/item?id=3001"
        renewed,stats=await run_with([two,third])
        self.assertEqual(renewed.analyzer.run.await_count,1)
        self.assertEqual(stats["event_recalled_origins"],1)

    async def test_event_overlap_does_not_replace_a_new_source_with_the_stored_snapshot(self):
        value=route(age=10);original=self.store.observe_origins(value.evidence)[0][0]
        self.store.mark_evidence([original],analyzed=True)
        fresh=copy.deepcopy(value.evidence[0]);fresh.summary="The source now discloses a different new experiment."
        fresh.published_at=(NOW-timedelta(days=9)).isoformat()
        discovered=fixture.batch([fresh]);discovered.supporting=[post(value)]
        process=self.process(discovered,accepted=False);process.delivery_contract_version=5
        process.analyzer.run=AsyncMock(side_effect=process.analyzer.run)
        stats=await process.run(reference_time=NOW)
        self.assertEqual(stats["event_recalled_origins"],1)
        self.assertEqual(stats["research_campaign"]["origin_completed"],1)
        analyzed=process.analyzer.run.await_args.args[0][0]
        self.assertEqual(analyzed.summary,fresh.summary)
        self.assertEqual(analyzed.published_at,fresh.published_at)
        self.assertNotEqual(analyzed.source_revision,original.source_revision)
        with self.store._connect() as con:
            payload=con.execute("SELECT source_payload_json FROM origin_research_state WHERE fingerprint=?",(fresh.fingerprint,)).fetchone()[0]
        self.assertEqual(json.loads(payload)["summary"],fresh.summary)

    async def test_event_wakes_pending_route_even_when_background_polling_is_zero(self):
        value=route();value.evidence=self.store.observe_origins(value.evidence)[0]
        self.store.mark_evidence(value.evidence,analyzed=True)
        value.status="pending_deep";self.store.save_candidates([value])
        discovered=fixture.batch([]);discovered.supporting=[post(value)]
        process=self.process(discovered,accepted=True);self.attach_events(process)
        with patch("config.PARADIGM_RESEARCH_REFRESH_LIMIT",0):
            stats=await process.run(reference_time=NOW)
        self.assertEqual(stats["history_recall"]["event_recalled_routes"],1)
        self.assertEqual(len(process.pending_delivery),1,stats)
