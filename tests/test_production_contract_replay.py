"""Sanitized contracts from run 38696723495; real stages, fake HTTP/LLM I/O.

No production documents, credentials, databases or model responses are bundled.
The failures are reproduced by their schema and receipt states, not snapshots
of private research text. The campaign clock is explicit.
"""
import copy
import json
import tempfile
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx
import config
from agents.paradigm_orchestrator import ParadigmOrchestrator
from database.origin_state import merge_checkpoint
from database.paradigm_store import ParadigmStore
from paradigms.analyzer import (
    ParadigmAnalyzer, ParadigmSynthesizer, ResearcherTrajectoryAnalyzer,
    _report_extraction_payload, _report_mechanism_key,
)
from paradigms.enrichment import EvidenceEnricher
from paradigms.models import EvidenceType, TechnicalEvidence
from paradigms.models import primary_material_url
from paradigms.publication import is_nonresearch_page
from paradigms.rubric import (
    RubricContractError, rubric_prompt, validate_innovation_types,
    valid_saved_screening,
)
from paradigms.search_receipts import blocking_searches, record_search
from runtime_clock import research_window
from tests.test_paradigm_pipeline import candidate, rubric_answers
from sources.official_repository_release_source import OfficialRepositoryReleaseSource, _linked_primary_material

NOW = datetime(2026, 10, 15, 7, 9, tzinfo=timezone.utc)
ASYNC_CLIENT = httpx.AsyncClient


def response(payload):
    return SimpleNamespace(choices=[SimpleNamespace(finish_reason="stop",
        message=SimpleNamespace(content=json.dumps(payload, ensure_ascii=False)))])


def model(payload):
    create = AsyncMock(return_value=response(payload))
    return SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))


def synthesis_payload():
    value = candidate()
    return {"mental_model": value.mental_model,
            "innovation_types": ["architecture"],
            "rubric_answers": rubric_answers(["architecture"])}


def report():
    return TechnicalEvidence(source="priority-research-page",
        evidence_type=EvidenceType.TECHNICAL_BLOG, title="Test system report",
        url="https://research.example/report", summary="测试报告明确披露机制。",
        published_at="2026-10-14T00:00:00+00:00", raw={"origin_kind": "technical_report"})


def seed(index=1, types=None):
    return {"canonical_name": f"Test mechanism {index}", "problem_shift": "改变推理边界",
            "mechanism": "改变可学习状态接口", "innovation_types": types or ["architecture"],
            "source_evidence": ["Architecture section"]}


class ProductionChainReplayTests(unittest.IsolatedAsyncioTestCase):
    async def run_chain(self, *, cap=1, total=3, outage=False):
        """Keep orchestrator, enrichers, synthesis, trajectory and SQLite real."""
        calls = []
        def handler(request):
            calls.append(request.url.host)
            if outage:
                return httpx.Response(503, request=request)
            if request.url.host == "api.tavily.com":
                return httpx.Response(200, json={"results": [], "usage": {"credits": 1}}, request=request)
            if request.url.host == "hn.algolia.com":
                return httpx.Response(200, json={"hits": []}, request=request)
            raise AssertionError("Unexpected external request: " + request.url.host)

        with tempfile.TemporaryDirectory() as directory, research_window(NOW), patch.multiple(config,
            GITHUB_TOKEN="", TAVILY_API_KEY="offline-only", TAVILY_SOCIAL_SEARCH_ENABLED=True,
            TAVILY_REQUEST_SAFETY_LIMIT=cap, TAVILY_MONTHLY_CREDIT_LIMIT=0,
            SEMANTIC_SCHOLAR_ENABLED=False, OPENALEX_API_KEY="",
            REDDIT_API_ACCESS_APPROVED=False, KOL_X_SOURCE_ENABLED=False):
            store = ParadigmStore(Path(directory) / "radar.db")
            values = []
            for index in range(total):
                origin = report()
                origin.title, origin.url = f"Test mechanism {index}", f"https://research.example/{index}"
                origin.raw = {}  # HTML material is already available, no arXiv hydration.
                origin = store.observe_origins([origin])[0][0]
                value = candidate([origin])
                value.key = f"route-{index}"
                value.status = "pending_deep"
                values.append(value)
            store.save_candidates(values)
            process = ParadigmOrchestrator.__new__(ParadigmOrchestrator)
            process.delivery_contract_version = 3
            process.store = store
            process.enricher = EvidenceEnricher()
            llm = model(synthesis_payload())
            # Default strict synthesizer deliberately exercises policy propagation.
            process.synthesizer = ParadigmSynthesizer(client=llm, model="offline")
            process.trajectory = ResearcherTrajectoryAnalyzer(client=llm, model="offline")
            with patch("httpx.AsyncClient", side_effect=lambda **kw: ASYNC_CLIENT(
                    transport=httpx.MockTransport(handler), **kw)):
                done, budget, failed = await process._deep_analyze_in_batches(
                    values, [], time.monotonic() + 30)
            return done, budget, failed, calls, llm.chat.completions.create.await_count

    async def test_real_chain_partial_tavily_cap_never_truncates_cohort(self):
        done, budget, failed, calls, models = await self.run_chain(cap=1, total=3)
        self.assertEqual((len(done), budget, failed, models), (3, [], [], 3))
        self.assertEqual(calls.count("api.tavily.com"), 1)
        self.assertEqual(calls.count("hn.algolia.com"), 3)
        gaps = [item for item in done if item.community_search_receipts["tavily_social_web"]["status"] == "not_executed"]
        self.assertEqual(len(gaps), 2)
        self.assertTrue(all(not blocking_searches(item, allow_partial=True) for item in done))
        self.assertTrue(all(item.deep_checkpoint_stage == "research_complete" for item in done))

    async def test_real_chain_all_providers_failed_stays_pending_without_model(self):
        done, budget, failed, _, models = await self.run_chain(outage=True)
        self.assertEqual((done, budget, len(failed), models), ([], [], 3, 0))
        self.assertTrue(all(item.status == "pending_deep" for item in failed))

    async def test_production_shaped_64_candidate_cohort_survives_12_request_cap(self):
        done, budget, failed, calls, models = await self.run_chain(cap=12, total=64)
        self.assertEqual((len(done), budget, failed, models), (64, [], [], 64))
        self.assertEqual(calls.count("api.tavily.com"), 12)
        self.assertEqual(calls.count("hn.algolia.com"), 64)


class ReportContractReplayTests(unittest.IsolatedAsyncioTestCase):
    async def test_fresh_index_rejects_bad_types_and_identifies_repair(self):
        bad = {"mechanisms": [seed(types=[["architecture"]])]}
        good = {"mechanisms": [seed()]}
        client = model(good)
        client.chat.completions.create.side_effect = [response(bad), response(good)]
        values, error, _ = await ParadigmAnalyzer(client=client, model="offline")._request_report_index(report(), "index")
        self.assertEqual((len(values), error), (1, ""))
        retry = client.chat.completions.create.call_args.kwargs["messages"][0]["content"]
        self.assertIn("innovation_types", retry)
        self.assertIn("learning_paradigm", retry)

    async def test_malformed_sibling_cannot_silently_shrink_index(self):
        client = model({"mechanisms": [seed(), {"canonical_name": "broken"}]})
        values, error, _ = await ParadigmAnalyzer(client=client, model="offline")._request_report_index(report(), "index")
        self.assertEqual(values, [])
        self.assertIn("mechanisms[2]", error)

    async def test_legacy_shapes_and_unknown_labels_repair_without_changing_ids(self):
        for types in ([["architecture"]], [{"type": "architecture", "detail": "旧索引"}],
                      ["training_methodology"]):
            with self.subTest(types=types), research_window(NOW):
                item = report()
                seeds = [seed(types=types)]
                key = _report_mechanism_key(seeds[0], 1)
                item.raw.update({"technical_report_checkpoint_version": "technical-report-checkpoint-v1",
                    "technical_report_mechanism_seeds": copy.deepcopy(seeds)})
                client = model({"assessment": {"innovation_types": ["learning_paradigm"],
                                               "rubric_answers": rubric_answers(["learning_paradigm"])}})
                values = await ParadigmAnalyzer(client=client, model="offline").extract(item)
                self.assertEqual(client.chat.completions.create.await_count, 1)
                self.assertEqual(values[0].innovation_types, ["learning_paradigm"])
                self.assertEqual(values[0].mechanism_id, f"report-{item.fingerprint[:16]}-{key}")
                self.assertEqual(item.raw["technical_report_mechanism_seeds"], seeds)
                self.assertNotIn("technical_report_slice_pending", item.raw)
                actual_prompt = client.chat.completions.create.call_args.kwargs["messages"][0]["content"]
                self.assertIn("learning_signal_change", actual_prompt)
                self.assertIn("architecture", actual_prompt)

    async def test_healthy_report_sibling_reused_across_sqlite_restore(self):
        with tempfile.TemporaryDirectory() as directory, research_window(NOW):
            store = ParadigmStore(Path(directory) / "state.db")
            item = store.observe_origins([report()])[0][0]
            seeds = [seed(1), seed(2, [["architecture"]])]
            healthy = ParadigmAnalyzer._from_payload(item, {
                **seeds[0], "rubric_answers": rubric_answers(["architecture"])})
            key = _report_mechanism_key(seeds[0], 1)
            item.raw.update({"technical_report_checkpoint_version": "technical-report-checkpoint-v1",
                "technical_report_mechanism_seeds": seeds,
                "technical_report_completed_mechanisms": {key: _report_extraction_payload(healthy)}})
            store.mark_evidence([item], analyzed=False)
            restored = ParadigmStore(store.db_path).load_pending_origins()[0]
            client = model({"assessment": {"innovation_types": ["architecture"],
                                            "rubric_answers": rubric_answers(["architecture"])}})
            values = await ParadigmAnalyzer(client=client, model="offline").extract(restored)
            self.assertEqual(client.chat.completions.create.await_count, 1)
            self.assertEqual(len(values), 1)  # Only the newly completed sibling.
            self.assertEqual(len(restored.raw["technical_report_completed_mechanisms"]), 2)
            store.mark_evidence([restored], analyzed=True)
            self.assertEqual(ParadigmStore(store.db_path).load_pending_origins(), [])

    async def test_missing_answers_repair_exposes_actual_questions_and_keeps_pending_if_unfixed(self):
        item = report()
        item.raw = {}
        partial = {"hypotheses": [seed()]}
        fixed = {"hypotheses": [{**seed(), "rubric_answers": rubric_answers(["architecture"])}]}
        client = model(fixed)
        client.chat.completions.create.side_effect = [response(partial), response(fixed)]
        values = await ParadigmAnalyzer(client=client, model="offline").extract(item)
        self.assertTrue(values[0].canonical_name)
        retry = client.chat.completions.create.call_args.kwargs["messages"][0]["content"]
        self.assertIn("previous_hypothesis", retry)
        self.assertIn("architecture", retry)
        client = model(partial)
        values = await ParadigmAnalyzer(client=client, model="offline").extract(item)
        self.assertFalse(values[0].is_candidate)
        self.assertIn("抽取失败", values[0].rejection_reason)
        self.assertNotEqual(values[0].rubric_assessment.get("decision"), "reject")

    def test_live_types_never_silently_become_other(self):
        for types in (None, [], [True], [["architecture"]], ["training_methodology"],
                      [{"type": "architecture"}]):
            with self.subTest(types=types), self.assertRaises(RubricContractError):
                validate_innovation_types(types)
        self.assertEqual(validate_innovation_types(["other"]), ["other"])

    def test_live_extraction_cannot_replace_malformed_type_object_with_other(self):
        for types in (None, {"type": "architecture"}, "architecture", []):
            with self.subTest(types=types), self.assertRaises(RubricContractError):
                ParadigmAnalyzer._from_payload(report(), {**seed(), "innovation_types": types,
                    "rubric_answers": rubric_answers(["other"])})

    async def test_conflicting_no_mechanism_disposition_cannot_claim_success(self):
        client = model({"report_disposition": "no_independent_mechanism",
                        "disposition_reason": "没有机制", "mechanisms": [seed()]})
        values, error, _ = await ParadigmAnalyzer(client=client, model="offline")._request_report_index(report(), "index")
        self.assertEqual(values, [])
        self.assertIn("矛盾", error)

    def test_validated_repair_replaces_only_invalid_completed_cache(self):
        item = report()
        healthy = _report_extraction_payload(ParadigmAnalyzer._from_payload(item,
            {**seed(), "rubric_answers": rubric_answers(["architecture"])}))
        self.assertTrue(valid_saved_screening(healthy))
        broken = copy.deepcopy(healthy)
        broken["innovation_types"] = [["architecture"]]
        previous = {"technical_report_completed_mechanisms": {"one": broken, "two": healthy}}
        incoming = {"technical_report_completed_mechanisms": {"one": healthy, "two": broken}}
        merged = merge_checkpoint(previous, incoming)["technical_report_completed_mechanisms"]
        self.assertEqual(merged, {"one": healthy, "two": healthy})


class OriginPageReplayTests(unittest.IsolatedAsyncioTestCase):
    def test_api_key_and_login_pages_never_become_primary_material(self):
        url = "https://dashboard.cohere.com/api-keys"
        self.assertEqual(_linked_primary_material("cohere-labs", f"Get [API key]({url})", ""), "")
        item = report()
        item.url = url
        self.assertEqual(primary_material_url(item), "")
        self.assertTrue(is_nonresearch_page("https://example.org/report", "<title>Login | Research Lab</title>"))
        self.assertFalse(is_nonresearch_page("https://example.org/research/api-key-representation",
                                            "<title>Learning representations</title><nav>Login</nav>"))

    async def test_legacy_account_origin_is_explicit_rejection_not_model_failure(self):
        item = report()
        item.url = "https://dashboard.cohere.com/api-keys"
        client = model({})
        values = await ParadigmAnalyzer(client=client, model="offline").extract(item)
        self.assertEqual(client.chat.completions.create.await_count, 0)
        self.assertEqual(values[0].rubric_assessment["decision"], "reject")

    async def test_redirect_to_login_falls_back_to_native_readme_not_html(self):
        readme = "# Adaptive agents\n\n## Architecture\n" + (
            "A language model agent changes its memory representation during inference. " * 20)
        readme += "\n[Technical Report](https://cohere.com/research/test-report)"
        def handler(request):
            if request.url.path == "/orgs/cohere-ai/repos":
                return httpx.Response(200, json=[{"name": "adaptive-agents", "full_name": "cohere-ai/adaptive-agents",
                    "html_url": "https://github.com/cohere-ai/adaptive-agents", "created_at": "2026-10-14T00:00:00Z"}])
            if request.url.path.endswith("/readme"):
                return httpx.Response(200, text=readme)
            if request.url.path == "/research/test-report":
                return httpx.Response(302, headers={"Location": "https://dashboard.cohere.com/welcome/login"})
            if request.url.path == "/welcome/login":
                return httpx.Response(200, text="<title>Login | Cohere</title>")
            raise AssertionError(str(request.url))
        with research_window(NOW), patch.object(config, "GITHUB_TOKEN", "offline"), patch(
            "httpx.AsyncClient", side_effect=lambda **kw: ASYNC_CLIENT(transport=httpx.MockTransport(handler), **kw)):
            source = OfficialRepositoryReleaseSource(organizations=[{"login": "cohere-ai", "owner": "cohere-labs"}])
            values = await source.safe_fetch()
        self.assertEqual(source.coverage()["external_primary_validation_failures"], 1)
        self.assertTrue(any(item.evidence_type == EvidenceType.ORIGINAL_IMPLEMENTATION for item in values))
        self.assertFalse(any(item.url.startswith("https://cohere.com") for item in values))


if __name__ == "__main__":
    unittest.main()
