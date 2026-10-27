"""Quality contracts use fixed clocks and offline evidence, not forced nonzero reports."""
import copy
import json
import tempfile
import unittest
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

from database.paradigm_store import ParadigmStore
from paradigms.analyzer import (ParadigmAnalyzer, ParadigmSynthesizer, _bounded_synthesis_evidence,
                                _should_prefilter_origin)
from paradigms.delivery_policy import bounded_origins, origin_selection_summary
from paradigms.models import EvidenceType, TechnicalEvidence
from paradigms.rubric import evaluate_rubric, validate_model_answers, load_rubric
from reports.paradigm_generator import _compact_route_dossier
from run_audit import run_audit, RunAudit
from runtime_clock import research_window
from sources.priority_research_source import _discover_index_links, _discover_latest_changelog_entry
from sources.social_web_search_source import _parse_results, _result_is_related, _work_query
from tests.test_paradigm_pipeline import rubric_answers, candidate
from tests.test_production_failure_regressions import mixed_candidate, synthesis_payload, client_for


NOW = datetime(2026, 10, 23, 6, 6, tzinfo=timezone.utc)


def material(index, *, source="arxiv", organization="", priority=1, age=1, domains=None):
    return TechnicalEvidence(source=source, evidence_type=EvidenceType.PRIMARY_PAPER,
        title=f"Original technical mechanism {index}", url=f"https://papers.example/work/{source}/{index}",
        published_at=(NOW - timedelta(days=age)).isoformat(), authors=[f"Researcher {index}"],
        organization=organization, summary="We propose a new model state update and test controlled baselines.",
        raw={"origin_kind": "research_paper", "origin_priority": priority,
             "frontier_domains": domains or ["foundation_models"]})


class SelectionQualityTests(unittest.TestCase):
    def test_unknown_papers_are_not_one_publisher_competing_with_every_known_lab(self):
        values = [material(i) for i in range(895)]
        values += [material(i, source="openalex") for i in range(270)]
        values += [material(i, source="priority-research-page", organization=f"Known Lab {i}", priority=3)
                   for i in range(55)]
        before = copy.deepcopy([v.to_dict() for v in values])
        selected = bounded_origins(values, limit=48, reference_time=NOW, days=30)
        counts = Counter(item.source for item in selected)
        self.assertGreaterEqual(counts["arxiv"], 10)
        self.assertGreaterEqual(counts["openalex"], 10)
        self.assertGreaterEqual(counts["priority-research-page"], 10)
        self.assertEqual(len(selected), 48)
        self.assertEqual([v.to_dict() for v in values], before)
        self.assertEqual([v.fingerprint for v in selected], [v.fingerprint for v in
                         bounded_origins(list(reversed(values)), limit=48, reference_time=NOW, days=30)])

    def test_recent_unknown_work_has_turns_without_high_priority_or_brand(self):
        recent = [material(i, domains=["data_learning"]) for i in range(20)]
        older = [material(100+i, organization="Established Lab", priority=3, age=15) for i in range(20)]
        selected = bounded_origins([*older, *recent], limit=12, reference_time=NOW, days=30)
        self.assertEqual(sum(not x.organization for x in selected), 9)
        self.assertEqual(sum(bool(x.organization) for x in selected), 3)
        self.assertTrue(all(x.raw.get("origin_eligibility_decision") is None for x in selected))

    def test_domains_and_publishing_groups_share_a_source_without_topical_duplication(self):
        values = [material(i, domains=["foundation_models"], organization="One Lab") for i in range(20)]
        values += [material(100+i, domains=["ai4science"], organization=f"Other Lab {i}") for i in range(20)]
        selected = bounded_origins(values, limit=8, reference_time=NOW, days=7)
        self.assertEqual(Counter(x.raw["frontier_domains"][0] for x in selected),
                         {"foundation_models": 4, "ai4science": 4})
        self.assertEqual(origin_selection_summary(values, selected)["source_selected"], {"arxiv": 8})

    def test_source_failures_or_future_material_do_not_expand_a_frozen_scope(self):
        values = [material(i) for i in range(8)]
        future = material(100, age=-1)
        self.assertNotIn(future, bounded_origins([future, *values], limit=4, reference_time=NOW, days=7))
        with tempfile.TemporaryDirectory() as directory:
            store = ParadigmStore(Path(directory) / "state.db")
            campaign = store.campaigns.begin(reference_time=NOW, ordinary_days=7, high_signal_days=30,
                                             bootstrap=False, seeds=[], delivery_contract_version=5)
            first = bounded_origins(values, limit=4, reference_time=NOW, days=7)
            store.campaigns.set_origin_scope(campaign.campaign_id, first)
            store.campaigns.set_origin_scope(campaign.campaign_id, values)
            self.assertEqual(store.campaigns.get(campaign.campaign_id).stats["origin_scope"],
                             [x.fingerprint for x in first])

    def test_audit_exposes_per_source_selection_without_claiming_exhaustive_coverage(self):
        selection = origin_selection_summary([material(i) for i in range(10)], [material(1)])
        with tempfile.TemporaryDirectory() as directory:
            RunAudit().write({"origin_selection": selection}, output_dir=directory)
            payload = json.loads((Path(directory) / "run_audit_latest.json").read_text())
            content = (Path(directory) / "run_audit_latest.md").read_text()
        self.assertEqual(payload["pipeline_stats"]["origin_selection"], selection)
        self.assertIn("arxiv：选定 1 / 发现 10", content)
        self.assertIn("未选材料不作技术否定", content)


class ContributionTests(unittest.TestCase):
    def answers(self, answer):
        rows = rubric_answers()
        row = next(x for x in rows if x["criterion_id"] == "current_work_intervention")
        row.update(answer=answer, evidence="本文是既有机制的讲解或非 AI 制度；不是本文新增的学习/运行机制。")
        return rows

    def test_explainer_or_non_ai_institution_cannot_be_rescued_by_other_high_scores(self):
        for stage in ("screening", "final"):
            for answer in ("explanation_or_restatement", "non_ai_process"):
                with self.subTest(stage=stage, answer=answer):
                    result = evaluate_rubric(stage=stage, innovation_types=["architecture"],
                                             answers=self.answers(answer))
                    self.assertGreater(result["score"], 70)
                    self.assertEqual(result["decision"], "reject")
                    self.assertIn("本文", result["decision_reason"])

    def test_unclear_contribution_is_observation_not_technical_denial(self):
        result = evaluate_rubric(stage="screening", innovation_types=["architecture"], answers=self.answers("unknown"))
        self.assertEqual(result["decision"], "observe")
        self.assertIn("尚无法确认", result["decision_reason"])

    def test_original_testable_ideas_are_not_required_to_have_a_paper_or_brand(self):
        result = evaluate_rubric(stage="screening", innovation_types=["architecture"],
                                 answers=self.answers("own_testable_proposal"))
        self.assertEqual(result["decision"], "deep_dive")

    def test_missing_new_question_is_a_contract_failure_not_a_negative_judgment(self):
        rows = [x for x in self.answers("own_technical_intervention")
                if x["criterion_id"] != "current_work_intervention"]
        with self.assertRaisesRegex(ValueError, "current_work_intervention"):
            validate_model_answers("screening", ["architecture"], rows)

    def test_official_blog_seed_priority_and_old_raw_approval_do_not_bypass_qualification(self):
        value = material(1, source="priority-research-page", organization="Known Lab", priority=3)
        value.evidence_type = EvidenceType.TECHNICAL_BLOG
        value.raw.update(origin_kind="official_model_release", explicit_seed=True,
                         priority_researcher_match=True, origin_eligibility_decision="full_review")
        self.assertTrue(_should_prefilter_origin(value))
        value.raw["origin_kind"] = "technical_report"
        self.assertFalse(_should_prefilter_origin(value))  # Separate index + same mechanism Rubric.


class CompactReviewTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        run_audit.reset()
        self.addCleanup(run_audit.reset)

    async def test_branded_explainers_can_close_cheap_gate_without_full_extraction(self):
        values = [material(i, source="priority-research-page", organization="Known Lab", priority=3) for i in range(2)]
        for value in values:
            value.evidence_type = EvidenceType.TECHNICAL_BLOG
            value.raw["origin_kind"] = "official_research"
        analyzer = ParadigmAnalyzer(enable_batch_prefilter=True)
        analyzer._screen_uncached_origin_batch = AsyncMock(return_value={v.fingerprint:
            ("screen_out", "本文只解释已有预训练知识，没有提出自己的 AI intervention") for v in values})
        analyzer.extract = AsyncMock(side_effect=AssertionError("Must not call the full review"))
        result = await analyzer.run(values)
        self.assertEqual(len(result), 2)
        self.assertTrue(all(v.rubric_assessment["decision"] == "reject" for v in result))
        analyzer.extract.assert_not_awaited()

    async def test_short_core_payload_preserves_causal_rubric_and_source_contracts(self):
        value = mixed_candidate()
        payload = synthesis_payload([0])
        for key in ("name", "route_family", "application_value", "why_now", "lineage_path",
                    "objective_momentum_signals", "secondary_discussion_summary", "marketing_overclaim_risk"):
            payload.pop(key, None)
        original_inputs = copy.deepcopy(value.input_evidence)
        client, call = client_for(payload)
        await ParadigmSynthesizer(client=client, model="offline").run([value])
        self.assertEqual(call.await_count, 1)
        self.assertNotEqual(value.rubric_assessment["decision"], "incomplete")
        self.assertEqual(value.input_evidence, original_inputs)
        self.assertEqual(_compact_route_dossier(value)["current_work_contribution"]["answer"],
                         "own_technical_intervention")

    async def test_corrective_non_technical_judgment_needs_no_fictitious_training_chain(self):
        value = mixed_candidate()
        payload = synthesis_payload([0])
        payload["mental_model"] = {}
        payload["rubric_answers"] = ContributionTests().answers("non_ai_process")
        client, call = client_for(payload)
        await ParadigmSynthesizer(client=client, model="offline").run([value])
        self.assertEqual(call.await_count, 1)
        self.assertEqual(value.rubric_assessment["decision"], "reject")

    async def test_real_technical_work_cannot_omit_its_mental_model(self):
        value = mixed_candidate()
        payload = {**synthesis_payload([0]), "mental_model": {}}
        client, call = client_for(payload, payload)
        await ParadigmSynthesizer(client=client, model="offline").run([value])
        self.assertEqual(call.await_count, 2)
        self.assertEqual(value.rubric_assessment["decision"], "incomplete")


class SearchIdentityTests(unittest.TestCase):
    def setUp(self):
        self.value = candidate([material(1)])
        self.value.evidence[0].title = "How To Use Decision Models for Evals"
        self.value.evidence[0].url = "https://hamel.example/decision-model-evals"
        self.value.evidence[0].authors = ["Hamel Husain"]
        self.value.name = "Decision Model Threshold Tuning and Calibration for Agent Evals"
        self.value.keywords = ["decision", "models", "calibration", "classification"]

    def test_generic_route_words_do_not_admit_unrelated_decision_literature(self):
        for text in ("Clinical decision models and calibration in medicine",
                     "Decision Modeling And Analysis for business classification",
                     "AI agent evaluation and decision models for healthcare"):
            self.assertFalse(_result_is_related(self.value, self.value.evidence[0].title, text))
        self.assertTrue(_result_is_related(self.value, "", "Independent analysis of How To Use Decision Models for Evals"))
        self.assertTrue(_result_is_related(self.value, "", "Hamel Husain: decision model evals and calibration"))

    def test_stable_ids_must_belong_to_an_origin_and_cannot_match_longer_ids(self):
        self.value.evidence[0].identifiers = {"arxiv": "2610.31521v1"}
        self.assertTrue(_result_is_related(self.value, "", "See https://arxiv.org/abs/2610.31521v2"))
        self.assertFalse(_result_is_related(self.value, "", "Different work 2610.315210"))

    def test_work_url_requires_a_complete_url_not_a_shared_path_prefix(self):
        url = self.value.evidence[0].url
        for suffix in ("", "/", "?utm_source=discussion", ". "):
            self.assertTrue(_result_is_related(self.value, "", url + suffix))
        for suffix in ("-unrelated-work", ".another-work"):
            self.assertFalse(_result_is_related(self.value, "", url + suffix))

    def test_account_and_challenge_search_hits_do_not_become_technical_evidence(self):
        payload = {"results": [
            {"title": self.value.evidence[0].title, "url": "https://console.example/api-keys"},
            {"title": "Just a moment", "url": "https://forum.example/thread/1",
             "content": self.value.evidence[0].title},
            {"title": self.value.evidence[0].title, "url": "https://reviewer.example/blog/work",
             "content": "A detailed independent analysis"},
        ]}
        records = _parse_results(payload, self.value, self.value.evidence[0].title)
        self.assertEqual([row.url for row in records], ["https://reviewer.example/blog/work"])
        self.assertTrue(records[0].raw["indexed_discovery_only"])

    def test_query_uses_actual_full_work_title_not_route_or_generic_prefix(self):
        text = _work_query(self.value, "Decision Models: A calibrated evaluation method", "")
        self.assertIn("A calibrated evaluation method", text)
        self.assertIn("Hamel Husain", text)
        self.assertNotIn("Threshold Tuning", text)
        self.assertIn("2610.31521", _work_query(self.value, "Title", "2610.31521"))

    def test_stale_index_leads_are_not_read_or_rejected_and_input_versions_survive(self):
        irrelevant = TechnicalEvidence(source="tavily-web", evidence_type=EvidenceType.SECONDARY_INTERPRETATION,
            title="Clinical decision models and calibration", url="https://medical.example/decision-models",
            summary="Classification and calibrated decisions in oncology", raw={"indexed_discovery_only": True})
        self.value.evidence.append(irrelevant)
        before = copy.deepcopy(self.value.input_evidence)
        payload = _bounded_synthesis_evidence(self.value.evidence)
        self.assertEqual(len(payload["records"]), 1)
        self.assertEqual(payload["overflow"]["work_identity_unconfirmed_count"], 1)
        self.assertEqual(self.value.input_evidence, before)
        self.assertEqual(self.value.excluded_evidence, [])


class OfficialCatalogTests(unittest.TestCase):
    def test_english_kimi_index_accepts_current_articles_without_fixed_model_seed(self):
        html = '<main><a href="/en/blog/new-model"><h4>A new model</h4>2026-10-24</a></main>'
        links = _discover_index_links(html, "https://www.kimi.com/en/blog/", source_url="https://www.kimi.com/blog/")
        self.assertEqual(links[0].url, "https://www.kimi.com/en/blog/new-model")
        self.assertEqual(links[0].published_at, "2026-10-24")

    def test_changelog_date_buttons_are_metadata_but_future_release_is_not_current(self):
        html = ('<main><button>2026-10-24</button><h2>Future Model</h2><p>' + 'Future data. ' * 15 + '</p>'
                '<button><span>2026-10-22</span></button><h2>Current Model</h2><p>' + 'Actual mechanism. ' * 15 + '</p>'
                '<button>Sign up</button></main>')
        with research_window(NOW):
            value = _discover_latest_changelog_entry(html, "https://docs.z.ai/release-notes/new-released", NOW-timedelta(days=7))
        self.assertEqual(value.published_at, "2026-10-22")
        self.assertIn("Current Model", value.body_hint)
        self.assertNotIn("Sign up", value.body_hint)
