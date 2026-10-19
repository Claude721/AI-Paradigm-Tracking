"""Context/cost changes must not shorten the research queue or weaken evidence."""
import copy
import json
import os
import string
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from agents.paradigm_orchestrator import ParadigmOrchestrator, _publication_people_inputs
from database.paradigm_store import ParadigmStore
from paradigms.analyzer import (
    ParadigmSynthesizer, _bounded_synthesis_evidence, _screening_prompt_context,
)
from paradigms.models import ResearcherProfile
from paradigms.rubric import load_rubric, rubric_prompt, scoped_rubric_prompt
from run_audit import RunAudit, run_audit
from runtime_clock import research_window
from skills.loader import SkillLoader
from tests import test_production_failure_regressions as synthesis_fixture
from tests import test_v0_delivery as delivery_fixture
from tests.test_paradigm_pipeline import candidate, rubric_answers, rubric_assessment


class ContextLayoutTests(unittest.TestCase):
    def test_stable_rules_precede_changing_material_without_losing_values(self):
        loader = SkillLoader()
        for name in ("paradigm_extraction", "paradigm_synthesis", "researcher_trajectory",
                     "technical_report_index", "technical_report_mechanism", "weekly_route_draft"):
            with self.subTest(skill=name):
                template = loader.load(name)
                keys = {key for _, key, _, _ in string.Formatter().parse(template) if key}
                def values(suffix):
                    return {key: ("fixed policy" if key in {"rubric_definition", "mental_model_method"}
                                  else key + suffix + ' {"原文":"不得丢失"}') for key in keys}
                one, two = loader.render(name, **values("ONE")), loader.render(name, **values("TWO"))
                prefix = os.path.commonprefix([one, two])
                self.assertGreater(len(prefix), 1200)
                self.assertIn("只返回" if name == "technical_report_index" else "输出", prefix)
                for key, value in values("ONE").items():
                    self.assertIn(value, one)
                    if key not in {"rubric_definition", "mental_model_method"}:
                        self.assertEqual(one.count(value), 1)

    def test_scoping_keeps_identical_questions_and_all_legal_type_names(self):
        full = json.loads(rubric_prompt("final"))
        narrow_text = scoped_rubric_prompt("final", ["architecture"])
        narrow = json.loads(narrow_text)
        self.assertEqual(narrow["common_criteria"], full["common_criteria"])
        self.assertEqual(narrow["type_criteria"], {"architecture": full["type_criteria"]["architecture"]})
        self.assertEqual(set(narrow["available_innovation_types"]), set(full["type_criteria"]))
        self.assertLess(len(narrow_text), len(rubric_prompt("final")) * .5)

    def test_invalid_legacy_types_expand_all_questions_instead_of_becoming_other(self):
        for invalid in ([], ["invented_type"], [["architecture"]], True):
            with self.subTest(value=invalid):
                scoped = json.loads(scoped_rubric_prompt("final", invalid))
                self.assertEqual(set(scoped["type_criteria"]), set(load_rubric()["type_criteria"]))

    def test_screening_compaction_keeps_answers_and_does_not_mutate_saved_judgment(self):
        original = rubric_assessment(stage="screening")
        before = copy.deepcopy(original)
        compact = _screening_prompt_context(original)
        self.assertEqual(original, before)
        self.assertEqual(len(compact["answers"]), len(original["answers"]))
        for old, new in zip(original["answers"], compact["answers"]):
            self.assertEqual(new, {key: old[key] for key in ("criterion_id", "answer", "evidence")})
        self.assertNotIn("score", compact)
        self.assertLess(len(json.dumps(compact)), len(json.dumps(original)))

    def test_only_exact_visible_abstract_duplication_is_removed(self):
        item = delivery_fixture.origin()
        item.raw["document_excerpt"] = "Before. " + item.summary + " After."
        before = copy.deepcopy(item.to_dict())
        record = _bounded_synthesis_evidence([item])["records"][0]
        self.assertNotIn("summary", record)
        self.assertIn(item.summary, record["document_excerpt"])
        self.assertEqual(item.to_dict(), before)
        item.summary += " Unique condition, absent from the excerpt."
        record = _bounded_synthesis_evidence([item])["records"][0]
        self.assertEqual(record["summary"], item.summary)
        self.assertEqual(record["index"], 0)
        self.assertEqual(record["fingerprint"], item.fingerprint)


class ScopedSynthesisTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        run_audit.reset()
        self.addCleanup(run_audit.reset)

    async def test_new_type_requires_expanded_rules_even_if_first_answer_looks_complete(self):
        value = synthesis_fixture.mixed_candidate()
        payload = {**synthesis_fixture.synthesis_payload([0]), "innovation_types": ["algorithm"],
                   "rubric_answers": rubric_answers(["algorithm"])}
        client, call = synthesis_fixture.client_for(payload, payload)
        await ParadigmSynthesizer(client=client, model="offline").run([value])
        self.assertEqual(call.await_count, 2)
        criterion = load_rubric()["type_criteria"]["algorithm"][0]["id"]
        first, second = [args.kwargs["messages"][0]["content"] for args in call.await_args_list]
        self.assertNotIn(criterion, first)
        self.assertIn(criterion, second)
        self.assertIn("创新类型扩展", second)
        self.assertIn(value.input_evidence[0].summary, second)
        self.assertEqual(value.innovation_types, ["algorithm"])
        self.assertNotEqual(value.rubric_assessment["decision"], "incomplete")

    async def test_deferred_type_expansion_preserves_first_turn_and_source_binding(self):
        value = synthesis_fixture.mixed_candidate()
        original_types = copy.deepcopy(value.innovation_types)
        origin_versions = [(e.fingerprint, e.source_revision) for e in value.input_evidence]
        payload = {**synthesis_fixture.synthesis_payload([0]), "innovation_types": ["data"],
                   "rubric_answers": rubric_answers(["data"])}
        client, call = synthesis_fixture.client_for(payload, payload)
        synth = ParadigmSynthesizer(client=client, model="offline")
        synth.defer_retries = True
        await synth.run([value])
        self.assertEqual(call.await_count, 1)
        self.assertEqual(value.innovation_types, original_types)
        self.assertEqual(value.rubric_assessment["decision"], "incomplete")
        await synth.run([value])
        self.assertEqual(call.await_count, 2)
        self.assertEqual(value.innovation_types, ["data"])
        self.assertEqual([(e.fingerprint, e.source_revision) for e in value.input_evidence], origin_versions)

    async def test_further_type_switch_stays_pending_instead_of_bypassing_rules(self):
        value = synthesis_fixture.mixed_candidate()
        payloads = [{**synthesis_fixture.synthesis_payload([0]), "innovation_types": [kind],
                     "rubric_answers": rubric_answers([kind])} for kind in ("algorithm", "data")]
        client, call = synthesis_fixture.client_for(*payloads)
        await ParadigmSynthesizer(client=client, model="offline").run([value])
        self.assertEqual(call.await_count, 2)
        self.assertEqual(value.innovation_types, ["architecture"])
        self.assertEqual(value.rubric_assessment["decision"], "incomplete")

    async def test_source_change_does_not_reuse_old_type_expansion_receipt(self):
        value = synthesis_fixture.mixed_candidate()
        new_type = {**synthesis_fixture.synthesis_payload([0]), "innovation_types": ["algorithm"],
                    "rubric_answers": rubric_answers(["algorithm"])}
        client, call = synthesis_fixture.client_for(new_type, synthesis_fixture.synthesis_payload([0]))
        synth = ParadigmSynthesizer(client=client, model="offline")
        synth.defer_retries = True
        await synth.run([value])
        value.evidence[0].raw["document_excerpt"] = "Completely new public mechanism evidence."
        await synth.run([value])
        prompt = call.await_args_list[1].kwargs["messages"][0]["content"]
        self.assertIn("Completely new public mechanism evidence.", prompt)
        self.assertNotIn("确定性校验失败：创新类型扩展", prompt)
        self.assertEqual(value.innovation_types, ["architecture"])


class PeopleSchedulingTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        run_audit.reset()
        self.addCleanup(run_audit.reset)

    def test_observed_old_and_current_results_keep_their_actual_judgments(self):
        ready = candidate([delivery_fixture.origin(1)])
        observed = candidate([delivery_fixture.origin(2)])
        observed.key += "-weak"
        observed.rubric_assessment = rubric_assessment(weak_ids={row["criterion_id"] for row in rubric_answers()})
        old = candidate([delivery_fixture.origin(3)])
        old.evidence[0].published_at = "2020-01-01T00:00:00+00:00"
        for item in (ready, observed, old):
            item.researchers = [ResearcherProfile(name="A. Researcher")]
        judgments = [copy.deepcopy(item.rubric_assessment) for item in (ready, observed, old)]
        with research_window(delivery_fixture.NOW, lookback_days=7):
            selected = _publication_people_inputs([ready, observed, old], window_days=7)
        self.assertEqual(selected, [ready])
        self.assertEqual([item.rubric_assessment for item in (ready, observed, old)], judgments)
        self.assertEqual(ready.researchers[0].trajectory_consistency, 0)
        for item in (observed, old):
            self.assertEqual(item.researchers[0].attribution_analysis_status, "not_executed")
            self.assertTrue(item.researchers[0].contact_search_notes)

    def test_possible_publisher_upgrade_is_not_circularly_excluded_or_persisted(self):
        value = candidate([delivery_fixture.origin()])
        value.researchers = [ResearcherProfile(name="A. Researcher", trajectory_consistency=0)]
        before = copy.deepcopy(value.to_dict())
        def hypothetical_score(reviewed):
            reviewed.rubric_assessment["decision"] = (
                "report" if reviewed.researchers[0].trajectory_consistency == 10 else "observe")
            reviewed.rejection_reason = ""
        with research_window(delivery_fixture.NOW), patch(
                "agents.paradigm_orchestrator.score_candidate", side_effect=hypothetical_score):
            self.assertEqual(_publication_people_inputs([value], window_days=7), [value])
        self.assertEqual(value.to_dict(), before)

    def test_skipping_optional_call_never_erases_a_completed_attribution(self):
        value = candidate([delivery_fixture.origin()])
        value.evidence[0].published_at = "2020-01-01T00:00:00+00:00"
        value.researchers = [ResearcherProfile(name="A. Researcher",
            attribution_analysis_status="completed", trajectory_consistency=7)]
        before = copy.deepcopy(value.researchers[0])
        with research_window(delivery_fixture.NOW):
            self.assertEqual(_publication_people_inputs([value], window_days=7), [])
        self.assertEqual(value.researchers[0], before)

    async def test_all_technical_checkpoints_close_and_resume_without_biography_research(self):
        with tempfile.TemporaryDirectory() as directory, research_window(delivery_fixture.NOW, lookback_days=7):
            store = ParadigmStore(Path(directory) / "state.db")
            originals = store.observe_origins([delivery_fixture.origin(i) for i in (1, 2)])[0]
            values = []
            for i, source in enumerate(originals):
                value = candidate([source])
                value.key += str(i)
                value.status = "pending_deep"
                value.researchers = [ResearcherProfile(name="A. Researcher")]
                values.append(value)
            store.save_candidates(values)
            async def synthesize(items):
                for item in items:
                    if item.key == values[1].key:
                        item.rubric_assessment = rubric_assessment(weak_ids={r["criterion_id"] for r in rubric_answers()})
                return items
            process = ParadigmOrchestrator.__new__(ParadigmOrchestrator)
            process.store, process.delivery_contract_version = store, 5
            process.ordinary_discovery_lookback_days = 7
            process.enricher = SimpleNamespace(run=AsyncMock(side_effect=lambda rows, support: rows),
                                              enrich_people=AsyncMock(side_effect=lambda rows: rows))
            process.synthesizer = SimpleNamespace(run=AsyncMock(side_effect=synthesize))
            process.trajectory = SimpleNamespace(run=AsyncMock(side_effect=lambda rows: rows))
            done, budget, failed = await process._deep_analyze_in_batches(values, [], time.monotonic() + 10)
            self.assertEqual((len(done), budget, failed), (2, [], []))
            self.assertEqual({v.deep_checkpoint_stage for v in done}, {"research_complete"})
            looked_up = [v.key for args in process.enricher.enrich_people.await_args_list for v in args.args[0]]
            biographies = [v.key for args in process.trajectory.run.await_args_list for v in args.args[0]]
            self.assertCountEqual(looked_up, [v.key for v in values])
            self.assertEqual(biographies, [values[0].key])
            for value in done:
                restored = store.load_synthesized_checkpoint(value)
                self.assertIsNotNone(restored)
                self.assertEqual(restored.deep_checkpoint_stage, "research_complete")
            process.synthesizer.run.reset_mock()
            process.trajectory.run.reset_mock()
            resumed, budget, failed = await process._deep_analyze_in_batches(done, [], time.monotonic() + 10)
            self.assertEqual((len(resumed), budget, failed), (2, [], []))
            process.synthesizer.run.assert_not_awaited()
            process.trajectory.run.assert_not_awaited()


class CacheAccountingTests(unittest.TestCase):
    def test_provider_cache_metadata_is_counted_without_double_counting_input(self):
        audit = RunAudit()
        response = SimpleNamespace(choices=[], usage=SimpleNamespace(
            prompt_tokens=100, completion_tokens=20, total_tokens=120,
            prompt_cache_hit_tokens=80, prompt_cache_miss_tokens=20))
        audit.record_llm(stage="test", role="main", model="offline", subject="offline", response=response)
        summary = audit.token_totals()
        self.assertEqual(summary["llm_cached_prompt_tokens"], 80)
        self.assertEqual(summary["llm_cache_usage_missing_count"], 0)
        self.assertEqual(summary["llm_total_tokens"], 120)
        self.assertEqual(summary["llm_prompt_tokens"], 100)

    def test_missing_or_invalid_cache_metadata_stays_unknown_not_measured_zero(self):
        for value in (None, True, -1, 101, "80"):
            audit = RunAudit()
            response = SimpleNamespace(choices=[], usage=SimpleNamespace(
                prompt_tokens=100, completion_tokens=20, total_tokens=120,
                prompt_cache_hit_tokens=value))
            audit.record_llm(stage="test", role="main", model="offline", subject="offline", response=response)
            self.assertIsNone(audit.llm_calls[0]["cached_tokens"])
            self.assertEqual(audit.token_totals()["llm_cache_usage_missing_count"], 1)

    def test_standard_cache_details_are_still_supported(self):
        audit = RunAudit()
        response = SimpleNamespace(choices=[], usage=SimpleNamespace(
            prompt_tokens=100, completion_tokens=20, total_tokens=120,
            prompt_tokens_details=SimpleNamespace(cached_tokens=60)))
        audit.record_llm(stage="test", role="main", model="offline", subject="offline", response=response)
        self.assertEqual(audit.llm_calls[0]["cached_tokens"], 60)
