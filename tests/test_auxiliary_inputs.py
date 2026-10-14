"""Auxiliary API failures must never manufacture healthy empty research."""

import asyncio
import copy
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx
import config
from agents.paradigm_orchestrator import ParadigmOrchestrator
from database.paradigm_store import ParadigmStore
from paradigms.enrichment import EvidenceEnricher
from paradigms.search_receipts import incomplete_searches
from runtime_clock import research_window
from sources.openalex_source import OpenAlexSource
from sources.researcher_profile_source import ResearcherProfileClient
from tests.test_input_closure import candidate, ASYNC_CLIENT, NOW


class OpenAlexContractTests(unittest.IsolatedAsyncioTestCase):
    async def source(self, payload):
        transport = httpx.MockTransport(lambda request: httpx.Response(200, json=payload))
        source = OpenAlexSource(searches=['"world model"'], concurrency=1)
        with patch.object(config, "OPENALEX_API_KEY", "offline-credential"), patch(
                "sources.openalex_source.httpx.AsyncClient", side_effect=lambda **kwargs: ASYNC_CLIENT(transport=transport, **kwargs)):
            values = await source.safe_fetch()
        return source, values

    async def test_malformed_success_is_failed_not_zero_hits(self):
        for payload in ({"error": "unavailable"}, {"results": []}, {"results": None, "meta": {}}, {"results": [{}], "meta": {}}):
            source, values = await self.source(payload)
            self.assertEqual(values, [])
            self.assertEqual(source.fetch_status, "query_failed")
            self.assertEqual(source.coverage()["completed_queries"], 0)

    async def test_valid_empty_is_completed_and_repeated_cursor_is_not(self):
        source, values = await self.source({"results": [], "meta": {"next_cursor": None}})
        self.assertEqual(source.fetch_status, "completed")
        source, _ = await self.source({"results": [{"display_name": "World Model"}], "meta": {"next_cursor": "*"}})
        self.assertEqual(source.fetch_status, "query_failed")

    async def test_credentials_only_in_header_in_production_and_profiles(self):
        calls = []
        def handler(request):
            calls.append(request)
            self.assertEqual(request.headers.get("Authorization"), "Bearer offline-credential")
            self.assertNotIn("offline-credential", str(request.url))
            return httpx.Response(200, json={"results": [], "meta": {"next_cursor": None}})
        transport = httpx.MockTransport(handler)
        with patch.object(config, "OPENALEX_API_KEY", "offline-credential"), patch(
                "sources.openalex_source.httpx.AsyncClient", side_effect=lambda **kwargs: ASYNC_CLIENT(transport=transport, **kwargs)):
            await OpenAlexSource(searches=['"world model"']).fetch()
        with patch.object(config, "OPENALEX_API_KEY", "offline-credential"):
            async with ASYNC_CLIENT(transport=transport) as client:
                await ResearcherProfileClient()._openalex_get(client, "https://api.openalex.org/authors", params={"search": "Known Researcher"})
        self.assertEqual(len(calls), 2)

    async def test_long_retry_after_is_not_shortened_or_retried(self):
        source = OpenAlexSource(searches=['"world model"'])
        request = httpx.Request("GET", "https://api.openalex.org/works")
        client = SimpleNamespace(get=AsyncMock(return_value=httpx.Response(429, headers={"Retry-After": "120"}, request=request)))
        with patch("sources.openalex_source.asyncio.sleep", new=AsyncMock()) as sleep:
            with self.assertRaises(httpx.HTTPStatusError):
                await source._get_with_backoff(client, {})
        client.get.assert_awaited_once()
        sleep.assert_not_awaited()


class FullDeepChainTests(unittest.IsolatedAsyncioTestCase):
    def test_enrichment_cas_rejects_foreign_newer_snapshot(self):
        with tempfile.TemporaryDirectory() as directory:
            store = ParadigmStore(Path(directory) / "radar.db")
            original = candidate()
            store.save_candidates([original])
            newer = copy.deepcopy(original)
            newer.technical_explanation = "另一个执行者已提交的新事实"
            store.save_candidates([newer])
            progress = copy.deepcopy(original)
            progress.community_search_receipts["hackernews"] = {"status": "completed", "result_count": 0}
            with self.assertRaises(ValueError):
                store.save_enrichment_checkpoint(original, progress)
            self.assertEqual(store.load_pending_deep_candidates()[0].technical_explanation, newer.technical_explanation)

    async def test_isolated_synthesis_failure_does_not_repeat_completed_empty_queries(self):
        with tempfile.TemporaryDirectory() as directory, research_window(NOW), patch.multiple(config,
                GITHUB_TOKEN="", TAVILY_SOCIAL_SEARCH_ENABLED=True, TAVILY_API_KEY="offline-only",
                TAVILY_MONTHLY_CREDIT_LIMIT=0, TAVILY_REQUEST_SAFETY_LIMIT=0,
                SEMANTIC_SCHOLAR_ENABLED=False, REDDIT_API_ACCESS_APPROVED=False, KOL_X_SOURCE_ENABLED=False):
            store = ParadigmStore(Path(directory) / "radar.db")
            values = [candidate("bad"), candidate("healthy")]
            for value in values:
                value.evidence = store.observe_origins(value.evidence)[0]
            store.save_candidates(values)
            process = ParadigmOrchestrator.__new__(ParadigmOrchestrator)
            process.store, process.enricher = store, EvidenceEnricher()
            process.enricher.documents.hydrate = AsyncMock(return_value={})
            process.enricher.researchers.enrich = AsyncMock(return_value=[])
            async def synthesize(items):
                if any(item.key == "bad" for item in items):
                    raise RuntimeError("isolated model error")
                for item in items:
                    item.rubric_assessment = {"decision": "observe"}
                return items
            process.synthesizer = SimpleNamespace(run=AsyncMock(side_effect=synthesize))
            process.trajectory = SimpleNamespace(run=AsyncMock(side_effect=lambda items: items))
            calls = []
            def handler(request):
                calls.append(request.url.host)
                return httpx.Response(200, json={"results": []} if request.url.host == "api.tavily.com" else {"hits": []})
            with patch("sources.paradigm_evidence_source.httpx.AsyncClient", side_effect=lambda **kwargs: ASYNC_CLIENT(transport=httpx.MockTransport(handler), **kwargs)):
                done, budget, failed = await process._deep_analyze_in_batches(values, [], time.monotonic() + 10)
            self.assertEqual(([item.key for item in done], budget, [item.key for item in failed]), (["healthy"], [], ["bad"]))
            self.assertEqual(calls.count("api.tavily.com"), 2)
            self.assertEqual(calls.count("hn.algolia.com"), 2)

    async def test_actual_enrichment_checkpoint_resumes_trajectory_without_queries(self):
        settings = patch.multiple(config, GITHUB_TOKEN="", TAVILY_SOCIAL_SEARCH_ENABLED=True,
            TAVILY_API_KEY="offline-only", TAVILY_MONTHLY_CREDIT_LIMIT=0,
            TAVILY_REQUEST_SAFETY_LIMIT=0, SEMANTIC_SCHOLAR_ENABLED=False,
            REDDIT_API_ACCESS_APPROVED=False, KOL_X_SOURCE_ENABLED=False)
        settings.start()
        self.addCleanup(settings.stop)
        with tempfile.TemporaryDirectory() as directory, research_window(NOW):
            store = ParadigmStore(Path(directory) / "radar.db")
            item = candidate()
            item.evidence = store.observe_origins(item.evidence)[0]
            store.save_candidates([item])
            process = ParadigmOrchestrator.__new__(ParadigmOrchestrator)
            process.store = store
            process.enricher = EvidenceEnricher()
            process.enricher.documents.hydrate = AsyncMock(return_value={})
            process.enricher.researchers.enrich = AsyncMock(return_value=[])
            calls = []
            def handler(request):
                calls.append(request.url.host)
                return httpx.Response(200, json={"results": []} if request.url.host == "api.tavily.com" else {"hits": []})
            async def synthesize(values):
                for value in values:
                    value.rubric_assessment = {"decision": "observe"}
                    value.status = "watchlist"
                return values
            process.synthesizer = SimpleNamespace(run=AsyncMock(side_effect=synthesize))
            process.trajectory = SimpleNamespace(run=AsyncMock(side_effect=RuntimeError("trajectory outage")))
            with patch("sources.paradigm_evidence_source.httpx.AsyncClient", side_effect=lambda **kwargs: ASYNC_CLIENT(transport=httpx.MockTransport(handler), **kwargs)):
                done, budget, failed = await process._deep_analyze_in_batches([item], [], time.monotonic() + 10)
            self.assertEqual((done, budget), ([], []))
            self.assertEqual(len(failed), 1)
            self.assertEqual(failed[0].deep_checkpoint_stage, "synthesized")
            self.assertEqual(incomplete_searches(failed[0]), [])
            store.save_candidates(failed)
            process.store = ParadigmStore(store.db_path)
            process.trajectory.run = AsyncMock(side_effect=lambda values: values)
            process.enricher.run = AsyncMock(side_effect=AssertionError("completed enrichment must not repeat"))
            done, budget, failed = await process._deep_analyze_in_batches(process.store.load_pending_deep_candidates(), [], time.monotonic() + 10)
            self.assertEqual((budget, failed), ([], []))
            self.assertEqual(len(done), 1)
            self.assertEqual(incomplete_searches(done[0]), [])
            self.assertEqual(calls.count("api.tavily.com"), 1)
            process.synthesizer.run.assert_awaited_once()
