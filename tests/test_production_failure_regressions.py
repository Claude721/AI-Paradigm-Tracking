"""Regression shapes from Run 38858172858; no downloaded or paid inputs."""
import copy
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

from database.paradigm_store import ParadigmStore, RouteHistoryIndex, _route_similarity
from paradigms.analyzer import ParadigmAnalyzer, ParadigmSynthesizer
from paradigms.clustering import cluster_extractions
from paradigms.completion import ResearchNotCompleteError, require_completed_research
from paradigms.models import EvidenceType, ParadigmExtraction, TechnicalEvidence
from tests import test_v0_delivery as fixture
from tests import test_execution_delivery as execution_fixture
from tests.test_paradigm_pipeline import candidate, rubric_answers, rubric_assessment


def response(payload):
    return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(
        content=json.dumps(payload, ensure_ascii=False)), finish_reason="stop")], usage=None)


def synthesis_payload(indices):
    return {"mental_model": candidate().mental_model, "innovation_types": ["architecture"],
            "rubric_answers": rubric_answers(["architecture"]),
            "mechanism_support_evidence_indices": indices,
            "excluded_evidence_indices": [], "substantive_uptake_evidence_indices": []}


def mixed_candidate():
    primary = fixture.origin()
    implementation = TechnicalEvidence(source="github", evidence_type=EvidenceType.IMPLEMENTATION,
        title="Official implementation", url="https://github.com/example/implementation",
        summary="Official code, not another original work.", raw={"independence": "official"})
    return candidate([primary, implementation])


def client_for(*payloads):
    call = AsyncMock(side_effect=[response(value) for value in payloads])
    return SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=call))), call


class MixedSynthesisContractTests(unittest.IsolatedAsyncioTestCase):
    async def test_official_code_is_not_an_origin_and_repair_gets_exact_allowed_indices(self):
        value = mixed_candidate()
        client, call = client_for(synthesis_payload([0, 1]), synthesis_payload([0]))
        synthesizer = ParadigmSynthesizer(client=client, model="offline")
        await synthesizer.run([value])
        self.assertEqual(call.await_count, 2)
        first = call.await_args_list[0].kwargs["messages"][0]["content"]
        repair = call.await_args_list[1].kwargs["messages"][0]["content"]
        self.assertIn('"mechanism_support_evidence_indices": [0]', first)
        self.assertIn("可用一手原点 index=[0]", repair)
        self.assertIn("收到=[0, 1]", repair)
        self.assertIn(value.evidence[0].summary, repair)
        self.assertEqual(set(value.mechanism_origin_support["origins"]), {value.evidence[0].fingerprint})
        self.assertEqual(len(value.input_evidence), 2)
        self.assertNotEqual(value.rubric_assessment["decision"], "incomplete")

    async def test_failed_validation_cannot_change_next_attempts_mechanism(self):
        value = mixed_candidate()
        original = value.mechanism
        invalid = {**synthesis_payload([0]), "mechanism": "不应被失败响应提前写入的机制", "rubric_answers": []}
        client, _ = client_for(invalid, invalid)
        synthesizer = ParadigmSynthesizer(client=client, model="offline")
        # Exercise the actual validator: it sees the new mechanism before the
        # later Rubric failure. Neither failed response may commit that change.
        await synthesizer.run([value])
        self.assertEqual(value.mechanism, original)
        self.assertFalse(value.mechanism_origin_support)

    async def test_deferred_repair_makes_one_request_per_turn_and_keeps_full_original_context(self):
        value = mixed_candidate()
        client, call = client_for(synthesis_payload([0, 1]), synthesis_payload([0]))
        synthesizer = ParadigmSynthesizer(client=client, model="offline")
        synthesizer.defer_retries = True
        await synthesizer.run([value])
        self.assertEqual(call.await_count, 1)
        self.assertEqual(value.rubric_assessment["decision"], "incomplete")
        self.assertIn(value.key, synthesizer._deferred_repairs)
        await synthesizer.run([value])
        self.assertEqual(call.await_count, 2)
        self.assertNotEqual(value.rubric_assessment["decision"], "incomplete")
        self.assertNotIn(value.key, synthesizer._deferred_repairs)

    async def test_changed_input_cannot_reuse_old_repair_payload(self):
        value = mixed_candidate()
        client, call = client_for(synthesis_payload([0, 1]), synthesis_payload([0]))
        synthesizer = ParadigmSynthesizer(client=client, model="offline")
        synthesizer.defer_retries = True
        await synthesizer.run([value])
        value.evidence[0].summary = "A different source revision's public material."
        await synthesizer.run([value])
        prompt = call.await_args_list[1].kwargs["messages"][0]["content"]
        self.assertNotIn("收到=[0, 1]", prompt)
        self.assertIn(value.evidence[0].summary, prompt)

    def test_foreign_bool_string_and_folded_indices_still_fail(self):
        for indices, visible in (([2], {0, 1}), ([True], {0, 1}), (["0"], {0, 1}), ([0], {1})):
            with self.subTest(indices=indices), self.assertRaises(ValueError):
                ParadigmSynthesizer._apply_synthesis_payload(mixed_candidate(), synthesis_payload(indices),
                    visible_evidence_indices=visible)

    def test_invalid_field_does_not_leak_arbitrary_response_text_into_audit_error(self):
        with self.assertRaises(ValueError) as error:
            ParadigmSynthesizer._apply_synthesis_payload(mixed_candidate(), synthesis_payload(["private body"]),
                visible_evidence_indices={0, 1})
        self.assertNotIn("private body", str(error.exception))


class OrdinaryOriginIdentityTests(unittest.TestCase):
    def test_same_ordinary_original_in_two_languages_is_one_hypothesis(self):
        one = mixed_candidate()
        two = copy.deepcopy(one)
        two.key, two.name, two.route_family, two.keywords = "中文改名", "中文机制", "另一种说法", ["完全不同词汇"]
        self.assertEqual(_route_similarity(one, two), 1.0)
        self.assertEqual([key for key, _ in RouteHistoryIndex([(one.key, one)]).relevant(two)], [one.key])
        def extraction(value):
            return ParadigmExtraction(evidence=value.evidence[0], canonical_name=value.name,
                is_candidate=True, thesis=value.thesis,
                problem_shift=value.problem_shift, mechanism=value.mechanism, keywords=value.keywords,
                rubric_assessment=rubric_assessment(stage="screening"))
        self.assertEqual(len(cluster_extractions([extraction(one), extraction(two)])), 1)

    def test_formal_report_children_are_never_collapsed_by_same_origin(self):
        one = mixed_candidate()
        one.evidence[0].raw["origin_kind"] = "technical_report"
        two = copy.deepcopy(one)
        two.key = "independent-report-mechanism"
        self.assertEqual(_route_similarity(one, two), 0.0)

    def test_origin_index_removes_replaced_or_rejected_entries(self):
        one = mixed_candidate()
        two = copy.deepcopy(one)
        index = RouteHistoryIndex([(one.key, one)])
        one = copy.deepcopy(one)
        one.status = "rejected"
        index.upsert(one)
        self.assertEqual(index.relevant(two), [])

    def test_unrelated_excluded_primary_does_not_supply_dedup_identity(self):
        one = mixed_candidate()
        other = fixture.origin(2)
        one.excluded_evidence = [other]
        two = copy.deepcopy(one)
        two.evidence, two.excluded_evidence = [other], []
        two.key, two.name, two.route_family = "different", "另外对象", "另一路线"
        two.mechanism, two.problem_shift, two.lineage_parent, two.keywords = "独立对象", "另一接口", "", ["唯一术语"]
        self.assertNotEqual(_route_similarity(one, two), 1.0)
        self.assertEqual(RouteHistoryIndex([(one.key, one)]).relevant(two), [])

    def test_in_place_origin_replacement_cannot_leave_stale_identity_posting(self):
        one = mixed_candidate()
        old = copy.deepcopy(one)
        index = RouteHistoryIndex([(one.key, one)])
        one.evidence = [fixture.origin(2)]
        one.name, one.route_family, one.problem_shift = "另对象", "另一路线", "另外约束"
        one.mechanism, one.lineage_parent, one.keywords = "独立接口", "", ["唯一术语"]
        index.upsert(one)
        self.assertEqual(index.relevant(old), [])


class ProductionExecutionTests(unittest.IsolatedAsyncioTestCase):
    setUp = fixture.V0DeliveryTests.setUp
    process = fixture.V0DeliveryTests.process

    def partial_report_process(self, *, failure=True):
        report = fixture.origin(1)
        report.raw.update(origin_kind="technical_report",
            origin_classification_reason="explicit_document_metadata:technical_report")
        process = self.process(fixture.batch([report, fixture.origin(2)]), accepted=True)
        original = process.analyzer.run
        async def extract(items):
            values = []
            for item in items:
                if item.fingerprint == report.fingerprint:
                    item.raw["technical_report_slice_pending"] = True
                    if failure:
                        item.raw["technical_report_last_run_failure"] = True
                    values.append(ParadigmExtraction(evidence=item, is_candidate=False,
                        canonical_name="", thesis="", problem_shift="", mechanism="",
                        rejection_reason="partial mechanism failure"))
                else:
                    values.extend(await original([item]))
            return values
        process.analyzer.run = extract
        return process

    async def test_partial_report_failure_is_attempted_not_dependency_and_healthy_route_delivers(self):
        process = self.partial_report_process()
        stats = await process.run(reference_time=fixture.NOW)
        proof = stats["delivery_scope"]
        self.assertEqual((proof["origin_attempted"], proof["origin_failed"], proof["origin_unattempted"]), (2, 1, 0))
        self.assertTrue(proof["deep_scope_sealed"])
        self.assertEqual(len(self.store.load_pending_origins()), 1)
        require_completed_research(stats, candidates=process.pending_delivery)
        self.assertTrue(process.pending_delivery)

    async def test_healthy_slice_with_unvisited_mechanisms_is_not_promoted_to_failed(self):
        process = self.partial_report_process(failure=False)
        stats = await process.run(reference_time=fixture.NOW)
        proof = stats["delivery_scope"]
        self.assertEqual((proof["origin_failed"], proof["origin_unattempted"]), (0, 1))
        # The origin remains unattempted, but cannot veto healthy peers'
        # optional biography once runnable first visits are drained.
        self.assertTrue(process.trajectory.run.await_count)
        with self.assertRaises(ResearchNotCompleteError):
            require_completed_research(stats, candidates=process.pending_delivery)

    async def test_failed_report_slice_does_not_strand_unvisited_sibling_mechanisms(self):
        report = fixture.origin()
        report.raw.update(origin_kind="technical_report",
            origin_classification_reason="explicit_document_metadata:technical_report")
        process = self.process(fixture.batch([report]), accepted=True)
        analyzer = ParadigmAnalyzer(technical_report_mechanism_slice=2)
        seeds = [{"canonical_name": "Mechanism " + str(i), "problem_shift": "interface-" + str(i),
                  "mechanism": "intervention-" + str(i), "innovation_types": ["architecture"]}
                 for i in range(1, 4)]
        analyzer._request_report_index = AsyncMock(return_value=(seeds, "", ""))
        visited = set()
        async def assess(item, seed, *, index):
            visited.add(index)
            if index == 2:
                return None, "offline invalid assessment"
            return ParadigmExtraction(evidence=item, is_candidate=True,
                canonical_name=seed["canonical_name"], thesis="可核验假说",
                mechanism=seed["mechanism"], problem_shift=seed["problem_shift"],
                innovation_types=["architecture"], rubric_assessment=rubric_assessment(stage="screening")), ""
        analyzer._assess_report_mechanism = assess
        process.analyzer = analyzer
        stats = await process.run(reference_time=fixture.NOW)
        self.assertEqual(visited, {1, 2, 3})
        self.assertEqual(stats["delivery_scope"]["origin_failed"], 1)
        self.assertEqual(stats["delivery_scope"]["origin_unattempted"], 0)
        self.assertEqual(stats["delivery_scope"]["deep_attempted"], 2)
        self.assertEqual(len(self.store.load_pending_origins()), 1)
        require_completed_research(stats, candidates=process.pending_delivery)

    async def test_all_technical_visits_precede_optional_people(self):
        process = self.process(fixture.batch([fixture.origin(i) for i in range(1, 6)]), accepted=True)
        original = process.analyzer.run
        async def extract(items):
            values = await original(items)
            for item in values:
                item.canonical_name = "独立机制" + item.evidence.title
                item.keywords = ["unique-" + item.evidence.fingerprint]
                item.route_family = "route-" + item.evidence.fingerprint
                item.mechanism = "intervention-" + item.evidence.fingerprint
                item.problem_shift = "interface-" + item.evidence.fingerprint
            return values
        process.analyzer.run = extract
        visited = set()
        synthesize = process.synthesizer.run
        async def first_turn(items):
            visited.update(item.key for item in items)
            return await synthesize(items)
        process.synthesizer.run = first_turn
        async def people(items):
            self.assertEqual(len(visited), 5)
            return items
        process.trajectory.run = AsyncMock(side_effect=people)
        stats = await process.run(reference_time=fixture.NOW)
        self.assertEqual(stats["delivery_scope"]["deep_attempted"], 5)
        self.assertTrue(process.trajectory.run.await_count)

    async def test_real_synthesizer_repairs_only_after_every_candidate_first_turn(self):
        process = self.process(fixture.batch([fixture.origin(i) for i in range(1, 4)]), accepted=True)
        base_extract = process.analyzer.run
        async def extract(items):
            values = await base_extract(items)
            for value in values:
                value.canonical_name = "独立机制" + value.evidence.title
                value.keywords = ["unique-" + value.evidence.fingerprint]
                value.route_family = "route-" + value.evidence.fingerprint
                value.mechanism = "intervention-" + value.evidence.fingerprint
                value.problem_shift = "interface-" + value.evidence.fingerprint
            return values
        process.analyzer.run = extract
        calls = []
        async def complete(**kwargs):
            prompt = kwargs["messages"][0]["content"]
            repaired = "确定性校验失败：" in prompt
            calls.append(repaired)
            return response(synthesis_payload([0] if repaired else [999]))
        client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=complete)))
        process.synthesizer = ParadigmSynthesizer(client=client, model="offline", allow_partial_sources=True)
        stats = await process.run(reference_time=fixture.NOW)
        self.assertEqual(calls, [False, False, False, True, True, True])
        self.assertEqual(stats["delivery_scope"]["deep_completed"], 3)
        self.assertEqual(stats["delivery_scope"]["deep_unattempted"], 0)
        require_completed_research(stats, candidates=process.pending_delivery)

    async def test_full_67_candidate_contract_pass_before_13_repairs(self):
        process, _ = execution_fixture.ExecutionDeliveryTests.distinct_process(self, total=67, fail=-1)
        subjects = set()
        order = []
        failed_once = set()
        async def complete(**kwargs):
            prompt = kwargs["messages"][0]["content"]
            name = next(line for line in prompt.splitlines() if line.startswith("暂定名称："))
            repeated = name in subjects
            if not repeated:
                subjects.add(name)
                if len(subjects) % 5 == 0:
                    failed_once.add(name)
            order.append(repeated)
            return response(synthesis_payload([999] if name in failed_once and not repeated else [0]))
        client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=complete)))
        process.synthesizer = ParadigmSynthesizer(client=client, model="offline", allow_partial_sources=True)
        stats = await process.run(reference_time=fixture.NOW)
        self.assertEqual(order, [False] * 67 + [True] * 13)
        self.assertEqual((stats["delivery_scope"]["deep_planned"], stats["delivery_scope"]["deep_completed"]), (67, 67))
        require_completed_research(stats, candidates=process.pending_delivery)

    async def test_unreadable_length_repair_runs_after_all_first_visits_and_commits_healthy_peers(self):
        process, _ = execution_fixture.ExecutionDeliveryTests.distinct_process(self, total=3, fail=-1)
        process.delivery_contract_version = 5
        subjects, order = [], []
        async def complete(**kwargs):
            prompt = kwargs["messages"][0]["content"]
            name = next(line for line in prompt.splitlines() if line.startswith("暂定名称："))
            repeated = name in subjects
            if not repeated:
                subjects.append(name)
            order.append(repeated)
            # First two are unreadable, and only the first can be repaired.
            if name == subjects[0] and not repeated or len(subjects) > 1 and name == subjects[1]:
                return SimpleNamespace(choices=[SimpleNamespace(finish_reason="length",
                    message=SimpleNamespace(content='{"mechanism":"未结束'))], usage=None)
            return response(synthesis_payload([0]))
        client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=complete)))
        process.synthesizer = ParadigmSynthesizer(client=client, model="offline", allow_partial_sources=True)
        stats = await process.run(reference_time=fixture.NOW)
        self.assertEqual(order, [False, False, False, True, True])
        self.assertEqual(stats["delivery_scope"]["deep_completed"], 2)
        self.assertEqual(stats["delivery_scope"]["deep_failed"], 1)
        self.assertEqual(len(self.store.load_pending_deep_candidates()), 1)
        self.assertEqual(len(process.pending_delivery), 2)
        require_completed_research(stats, candidates=process.pending_delivery)

class ReceiptRecoveryTests(unittest.TestCase):
    def test_recovery_requires_same_revision_failure_inside_that_exact_attempt(self):
        with tempfile.TemporaryDirectory() as directory:
            store = ParadigmStore(Path(directory) / "state.db")
            campaign = store.campaigns.begin(reference_time=fixture.NOW, ordinary_days=7,
                high_signal_days=30, bootstrap=False, seeds=[], delivery_contract_version=4)
            items = store.observe_origins([fixture.origin(i) for i in range(1, 5)])[0]
            store.campaigns.set_origin_scope(campaign.campaign_id, items)
            attempts = store.campaigns.start_attempt(campaign.campaign_id, "origin",
                [(item.fingerprint, item.source_revision) for item in items])
            store.campaigns.finish_attempts(attempts, outcome="dependency_pending")
            with store._connect() as conn:
                for index, item in enumerate(items):
                    checkpoint = {"technical_report_last_run_failure": True,
                        "technical_report_mechanism_failure_counts": {"seed": 1},
                        "last_analysis_failure_at": "2026-10-08T01:00:30+00:00"}
                    if index == 1:
                        checkpoint.pop("technical_report_last_run_failure")  # healthy slice
                    if index == 2:
                        checkpoint["last_analysis_failure_at"] = "2026-10-07T01:00:30+00:00"
                    conn.execute("UPDATE origin_research_state SET checkpoint_json=? WHERE fingerprint=?",
                        (json.dumps(checkpoint), item.fingerprint))
                conn.execute("UPDATE research_attempts SET started_at='2026-10-08T01:00:00+00:00',finished_at='2026-10-08T01:01:00+00:00'")
                conn.execute("UPDATE research_attempts SET input_revision='different-source' WHERE attempt_id=?", (attempts[3],))
            store.campaigns.upgrade_execution_contract(campaign.campaign_id)
            with store._connect() as conn:
                outcomes = [r[0] for r in conn.execute("SELECT outcome FROM research_attempts ORDER BY attempt_id")]
            self.assertEqual(outcomes, ["execution_failed", "dependency_pending", "dependency_pending", "dependency_pending"])
            self.assertEqual(len(store.load_pending_origins()), 4)

