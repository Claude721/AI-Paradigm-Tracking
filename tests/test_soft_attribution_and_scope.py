"""Soft people enrichment must not weaken technical proof or source identity."""
import copy
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from agents.sample_budget import SampleBudgetError
from paradigms.analyzer import ParadigmSynthesizer, ResearcherTrajectoryAnalyzer
from paradigms.completion import require_completed_research
from paradigms.enrichment import EvidenceEnricher
from paradigms.evidence_association import countable_evidence, supported_origins, distinct_origins
from paradigms.models import ResearcherProfile, candidate_from_dict
from paradigms.rubric import objective_answers, finalize_candidate_rubric
from paradigms.scoring import _assess_publisher
from paradigms.run_summary import execution_summary
from reports.paradigm_generator import (_attach_deterministic_sections, _report_input_violations,
                                        _attribution_boundary, _primary_sources)
from tests import test_v0_delivery as fixture
from tests.test_paradigm_pipeline import candidate, rubric_answers


def synthesis_payload(indices=None):
    value = {"mental_model": candidate().mental_model, "innovation_types": ["architecture"],
             "rubric_answers": rubric_answers(["architecture"])}
    if indices is not None:
        value["mechanism_support_evidence_indices"] = indices
    return value


class OriginAssociationTests(unittest.TestCase):
    def origins(self):
        one, two = fixture.origin(1), fixture.origin(2)
        two.organization = "Other Verified Lab"
        two.authors = ["Different Researcher"]
        return [one, two]

    def test_multiorigin_requires_positive_association_not_clustering(self):
        value = candidate(self.origins())
        answers = {row["criterion_id"]: row["answer"] for row in objective_answers(value)}
        self.assertEqual(answers["route_convergence"], "single_origin")
        self.assertEqual(answers["independent_validation"], "primary_claim_only")
        with self.assertRaisesRegex(ValueError, "原点机制关联"):
            ParadigmSynthesizer._apply_synthesis_payload(value, synthesis_payload(), visible_evidence_indices={0, 1})

    def test_unrelated_primary_cannot_supply_convergence_and_inputs_are_retained(self):
        value = candidate(self.origins())
        original = copy.deepcopy(value.input_evidence)
        ParadigmSynthesizer._apply_synthesis_payload(value, synthesis_payload([0]), visible_evidence_indices={0, 1})
        self.assertEqual(len(value.evidence), 1)
        self.assertEqual(value.excluded_evidence[0].fingerprint, original[1].fingerprint)
        self.assertEqual({e.fingerprint for e in value.input_evidence}, {e.fingerprint for e in original})
        self.assertEqual(len(supported_origins(value)), 1)
        self.assertEqual(_primary_sources(value), value.evidence)
        restored = candidate_from_dict(value.to_dict())
        self.assertEqual(restored.mechanism_origin_support, value.mechanism_origin_support)
        restored.mechanism += " changed"
        self.assertFalse(countable_evidence(restored, restored.evidence[0]))

    def test_distinct_confirmed_origins_can_converge(self):
        value = candidate(self.origins())
        ParadigmSynthesizer._apply_synthesis_payload(value, synthesis_payload([0, 1]), visible_evidence_indices={0, 1})
        answers = {row["criterion_id"]: row["answer"] for row in objective_answers(value)}
        self.assertEqual(answers["route_convergence"], "multiple_independent_origins")

    def test_same_paper_mirrors_never_become_independent_origins(self):
        one, two = self.origins()
        one.identifiers = two.identifiers = {"arxiv": "2610.00001"}
        two.url = "https://example.org/mirror"
        value = candidate([one, two])
        ParadigmSynthesizer._apply_synthesis_payload(value, synthesis_payload([0, 1]), visible_evidence_indices={0, 1})
        self.assertEqual(len(supported_origins(value)), 1)

    def test_same_authors_at_different_affiliations_are_not_independent(self):
        one, two = self.origins()
        two.authors = one.authors
        value = candidate([one, two])
        ParadigmSynthesizer._apply_synthesis_payload(value, synthesis_payload([0, 1]), visible_evidence_indices={0, 1})
        answers = {row["criterion_id"]: row["answer"] for row in objective_answers(value)}
        self.assertEqual(answers["route_convergence"], "multiple_related_origins")

    def test_bad_indices_and_mutated_source_cannot_authorize_facts(self):
        for indices in [[True], ["0"], [0, 0], [10], {}]:
            with self.subTest(indices=indices), self.assertRaises(ValueError):
                ParadigmSynthesizer._apply_synthesis_payload(candidate(self.origins()), synthesis_payload(indices), visible_evidence_indices={0, 1})
        value = candidate(self.origins())
        ParadigmSynthesizer._apply_synthesis_payload(value, synthesis_payload([0]), visible_evidence_indices={0, 1})
        value.evidence[0].source_revision = "new-source-revision"
        self.assertFalse(countable_evidence(value, value.evidence[0]))

    def test_late_doi_arxiv_bridge_collapses_the_same_original_work(self):
        a, b, bridge = fixture.origin(1), fixture.origin(2), fixture.origin(3)
        a.identifiers = {"doi": "10.1000/work"}
        b.identifiers = {"arxiv": "2610.00002"}
        bridge.identifiers = {"doi": "10.1000/work", "arxiv": "2610.00002"}
        self.assertEqual(len(distinct_origins([a, b, bridge])), 1)

    def test_person_missing_does_not_change_technical_admission_score(self):
        value = candidate([fixture.origin()])
        value.researchers = []
        first = finalize_candidate_rubric(value)["score"]
        value.researchers = [ResearcherProfile(name="Named Researcher", profile_urls={"homepage": "https://example.org/person"},
            trajectory_consistency=9, representative_works=[{}, {}])]
        self.assertEqual(finalize_candidate_rubric(value)["score"], first)
        row = next(r for r in value.rubric_assessment["answers"] if r["criterion_id"] == "researcher_continuity")
        self.assertEqual(row["weight"], 0)

    def test_unrelated_verified_person_cannot_certify_origin_publisher(self):
        original = fixture.origin()
        original.organization = ""
        original.raw = {}
        value = candidate([original])
        value.researchers = [ResearcherProfile(name="Yann LeCun", profile_urls={"homepage": "https://cds.nyu.edu/team/yann-lecun/"})]
        _assess_publisher(value)
        self.assertEqual(value.publisher_tier, "unknown")


class SoftAttributionTests(unittest.IsolatedAsyncioTestCase):
    setUp = fixture.V0DeliveryTests.setUp
    process = fixture.V0DeliveryTests.process

    async def test_identity_io_is_after_durable_synthesis_and_cannot_veto_it(self):
        process = self.process(accepted=True)
        async def failed_identity(items):
            saved = self.store.load_candidate_snapshots([items[0].key])[0]
            self.assertEqual(saved.deep_checkpoint_stage, "synthesized")
            raise TimeoutError("optional identity provider slow")
        process.enricher.enrich_people = AsyncMock(side_effect=failed_identity)
        stats = await process.run(reference_time=fixture.NOW)
        process.enricher.enrich_people.assert_awaited_once()
        self.assertEqual(stats["delivery_scope"]["deep_completed"], 1)
        self.assertEqual(stats["delivery_scope"]["deep_failed"], 0)
        require_completed_research(stats, candidates=process.pending_delivery)

    async def test_identity_provider_exception_is_retained_without_a_technical_rejection(self):
        enricher = EvidenceEnricher()
        enricher.researchers.enrich = AsyncMock(side_effect=TimeoutError("optional identity provider"))
        value = candidate([fixture.origin()])
        await enricher.enrich_people([value])
        self.assertIn("TimeoutError", value.community_coverage["researcher_attribution"])
        self.assertNotEqual(value.status, "rejected")

    async def test_nonempty_full_pipeline_delivers_without_a_verified_person(self):
        process = self.process(accepted=True)
        base = process.enricher.run
        async def anonymous(items, supporting):
            values = await base(items, supporting)
            for item in values:
                item.researchers = []
            return values
        process.enricher.run = anonymous
        with patch.object(self, "process", return_value=process):
            await fixture.V0DeliveryTests.test_nonempty_pipeline_report_and_confirmed_delivery_with_source_failure(self)
        reports = list(self.path.glob("paradigm_radar_*.md"))
        self.assertEqual(len(reports), 1)
        content = reports[0].read_text()
        self.assertIn("原文署名：A. Researcher", content)
        self.assertIn("人物身份、当前任职和履历补充未执行或未完成", content)
        self.assertNotIn("现有材料未能可靠归因到关键自然人", content)

    async def test_profile_failure_does_not_lose_technical_result_or_prevent_outbox(self):
        process = self.process(accepted=True)
        base = process.enricher.run
        async def no_identity(items, supporting):
            values = await base(items, supporting)
            for item in values:
                item.researchers = [ResearcherProfile(name="Named Researcher", identity_search_status="failed", contact_search_status="failed")]
            return values
        process.enricher.run = no_identity
        process.trajectory.run = AsyncMock(side_effect=RuntimeError("optional person unavailable"))
        stats = await process.run(reference_time=fixture.NOW)
        self.assertEqual(stats["delivery_scope"]["deep_completed"], 1)
        self.assertEqual(stats["delivery_scope"]["deep_failed"], 0)
        self.assertEqual(len(process.pending_delivery), 1)
        result = process.pending_delivery[0]
        self.assertEqual(result.researchers[0].attribution_analysis_status, "failed")
        self.assertIn("不能解释为没有相关人物", _attribution_boundary(result))
        require_completed_research(stats, candidates=[result])
        self.assertEqual(_report_input_violations([result]), [])
        self.store.enqueue_report([result], stats, report_date=stats["report_date"])
        content = _attach_deterministic_sections("## 本期研究 Memo\n\n技术判断。", stats, [result])
        self.assertIn("未能可靠归因", content)
        self.assertNotIn("Named Researcher", content)
        self.assertIn(fixture.origin().url, content)
        result.evidence = []
        self.assertTrue(_report_input_violations([result]))

    async def test_model_can_use_current_authored_material_without_bibliography(self):
        primary = fixture.origin()
        primary.summary = "原文明确披露的研究对象与署名背景。"
        person = ResearcherProfile(name=primary.authors[0])
        value = candidate([primary]); value.researchers = [person]
        payload = {"background_summary": "原文署名作者，暂无其他可核验履历。", "trajectory_summary": "缺少代表作，无法判断长期连续性。",
                   "trajectory_consistency": 0, "key_person_reason": "仅可确认当前署名，贡献角色未披露。", "current_role_note": ""}
        call = AsyncMock(return_value=SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=json.dumps(payload)))], usage=None))
        client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=call)))
        await ResearcherTrajectoryAnalyzer(client=client, model="offline").run([value])
        self.assertEqual(person.attribution_analysis_status, "completed")
        self.assertIn(primary.summary, call.await_args.kwargs["messages"][0]["content"])
        self.assertEqual(person.public_contacts, {})
        self.assertIn("未能可靠归因", _attribution_boundary(value))

        # Old HTML-only snapshots have a real document/role but no summary or
        # authors array. The recovered byline must reach the optional model,
        # not be mislabeled insufficient material before the prompt is built.
        primary.summary = ""
        primary.authors = []
        primary.raw.update(author_roles={person.name: "第一作者"},
                           document_excerpt=f"Technical Paper {person.name} Affiliation: Example University Abstract 原文机制。")
        person.attribution_analysis_status = ""
        await ResearcherTrajectoryAnalyzer(client=client, model="offline").run([value])
        self.assertEqual(call.await_count, 2)
        self.assertEqual(person.attribution_analysis_status, "completed")
        prompt = call.await_args.kwargs["messages"][0]["content"]
        self.assertIn("Example University", prompt)
        self.assertIn("'name_is_public_byline': True", prompt)

    async def test_spend_stop_is_not_reclassified_as_person_not_found(self):
        call = AsyncMock(side_effect=SampleBudgetError("confirmed spend stop"))
        client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=call)))
        value = candidate(); value.researchers = [ResearcherProfile(name="Named Researcher", representative_works=[{"title": "Public work"}])]
        with self.assertRaises(SampleBudgetError):
            await ResearcherTrajectoryAnalyzer(client=client, model="offline").run([value])
        self.assertEqual(value.researchers[0].attribution_analysis_status, "")

    async def test_person_socket_or_timeout_error_is_optional_not_a_storage_failure(self):
        call = AsyncMock(side_effect=TimeoutError("optional person timeout"))
        client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=call)))
        value = candidate(); value.researchers = [ResearcherProfile(name="Named Researcher", representative_works=[{"title": "Public work"}])]
        await ResearcherTrajectoryAnalyzer(client=client, model="offline").run([value])
        self.assertEqual(value.researchers[0].attribution_analysis_status, "failed")


class RunScopeTests(unittest.TestCase):
    def test_resumed_work_is_not_new_discovery_or_a_global_backlog_failure(self):
        stats = {"discovery_resumed": True, "analysis_completed_count": 0, "deep_candidate_count": 1,
            "pending_work_count": 2063, "delivery_scope": {"scoped_pending_count": 0},
            "research_campaign": {"attempt_count": 3, "origin_completed": 48, "deep_completed": 68}}
        summary = execution_summary(stats)
        self.assertEqual(summary["run_kind"], "resumed_research")
        self.assertFalse(summary["discovery_executed"])
        self.assertEqual(summary["current_run"]["deep_completed"], 1)
        self.assertEqual(summary["campaign_cumulative"]["deep_completed"], 68)
        self.assertEqual(summary["campaign_pending_count"], 0)
        self.assertEqual(summary["global_backlog_count"], 2063)

    def test_delivery_only_does_not_recount_previous_research(self):
        summary = execution_summary({"recovered_delivery_only": True, "discovery_resumed": False, "analysis_completed_count": 48})
        self.assertEqual(summary["run_kind"], "delivery_only")
        self.assertFalse(summary["research_executed"])
        self.assertFalse(summary["discovery_executed"])
        self.assertEqual(summary["current_run"]["origin_completed"], 0)

    def test_new_discovery_and_unknown_are_not_conflated(self):
        self.assertEqual(execution_summary({"discovery_resumed": False})["run_kind"], "new_research")
        self.assertEqual(execution_summary({})["run_kind"], "unknown")
        resumed = execution_summary({"discovery_resumed": False, "research_campaign": {"attempt_count": 3}})
        self.assertEqual(resumed["run_kind"], "resumed_research")
        self.assertTrue(resumed["discovery_executed"])
