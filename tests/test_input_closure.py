"""Input closure is independent of empty search results; entirely offline."""

import asyncio
import copy
import json
import tempfile
import time
import unittest
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import httpx
import config
from agents.paradigm_orchestrator import ParadigmOrchestrator
from database.paradigm_store import ParadigmStore
from paradigms.analyzer import ParadigmSynthesizer
from paradigms.completion import ResearchNotCompleteError, require_completed_research
from paradigms.enrichment import EvidenceEnricher
from paradigms.models import EvidenceType, ParadigmCandidate, TechnicalEvidence, candidate_from_dict
from paradigms.search_receipts import incomplete_searches, record_search
from runtime_clock import research_window
from sources.bibliography_resolver import BibliographyResolver, match_work
from sources.paradigm_evidence_source import CommunityEvidenceClient
from sources.priority_research_source import PriorityResearchPageSource
from sources.reddit_evidence_source import RedditEvidenceClient
from source_audit import Entry, SourceAudit
from tests.completion_fixtures import COMPLETED_RESEARCH
from tests.test_source_recovery import NOW, nuxt_page

ASYNC_CLIENT = httpx.AsyncClient
NYU = "https://wp.nyu.edu/cilvr/cilvr-group-publications/"


def candidate(key="route"):
    return ParadigmCandidate(key=key, name="Adaptive Memory", thesis="original",
        problem_shift="test", mechanism="test", status="pending_deep", evidence=[
            TechnicalEvidence(source="arxiv", evidence_type=EvidenceType.PRIMARY_PAPER,
                title="Adaptive Memory", url="https://arxiv.org/abs/2610.00001",
                identifiers={"arxiv": "2610.00001"}, authors=["Yann LeCun"])])


def record(key="one", title="Adaptive Memory"):
    return {"key": key, "title": title, "citation_year": 2026, "author_text": "LeCun, Y."}


def work(title="Adaptive Memory", *, identity="W1", author="Yann LeCun", url="https://arxiv.org/abs/2610.00001"):
    return {"id": "https://openalex.org/" + identity, "display_name": title,
            "publication_year": 2026, "authorships": [{"author": {"display_name": author}}],
            "locations": [{"landing_page_url": url}]}


def payload(*works):
    return {"results": list(works), "meta": {"count": len(works)}}


class CommunityClosureTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.config = patch.multiple(config, GITHUB_TOKEN="", TAVILY_API_KEY="offline-test",
            TAVILY_SOCIAL_SEARCH_ENABLED=True, REDDIT_API_ACCESS_APPROVED=False,
            KOL_X_SOURCE_ENABLED=False, TWITTER_BEARER_TOKEN="", TAVILY_REQUEST_SAFETY_LIMIT=0,
            TAVILY_MONTHLY_CREDIT_LIMIT=0)
        self.config.start()
        self.addCleanup(self.config.stop)

    async def search(self, item, handler, client=None):
        community = client or CommunityEvidenceClient()
        transport = httpx.MockTransport(handler)
        with patch("sources.paradigm_evidence_source.httpx.AsyncClient",
                   side_effect=lambda **kwargs: ASYNC_CLIENT(transport=transport, **kwargs)):
            return await community.search(item)

    async def test_http_failure_is_not_successful_zero(self):
        item = candidate()
        def handler(request):
            return httpx.Response(503) if request.url.host == "api.tavily.com" else httpx.Response(200, json={"hits": []})
        with research_window(NOW):
            self.assertEqual(await self.search(item, handler), [])
        self.assertEqual(incomplete_searches(item), ["tavily_social_web"])
        self.assertEqual(item.community_search_receipts["tavily_social_web"]["reason"], "http_503")
        self.assertEqual(item.community_search_receipts["hackernews"]["status"], "completed")
        self.assertEqual(item.community_search_receipts["x_official"]["status"], "disabled")

    async def test_healthy_zero_reused_after_failure_and_sqlite_restore(self):
        item, calls = candidate(), []
        def handler(request):
            calls.append(request.url.host)
            if request.url.host == "api.tavily.com":
                return httpx.Response(500 if calls.count("api.tavily.com") == 1 else 200, json={"results": []})
            return httpx.Response(200, json={"hits": []})
        with tempfile.TemporaryDirectory() as directory, research_window(NOW):
            store = ParadigmStore(Path(directory) / "state.db")
            await self.search(item, handler)
            store.save_candidates([item])
            restored = ParadigmStore(Path(directory) / "state.db").load_pending_deep_candidates()[0]
            await self.search(restored, handler)
            self.assertEqual(incomplete_searches(restored), [])
            self.assertEqual(restored.community_search_receipts["tavily_social_web"]["failure_history"][0]["reason"], "http_500")
            self.assertEqual(calls.count("hn.algolia.com"), 1)
            self.assertEqual(calls.count("api.tavily.com"), 2)

    async def test_new_window_or_origin_revision_invalidates_receipt(self):
        item, calls = candidate(), []
        def handler(request):
            calls.append(request.url.host)
            return httpx.Response(200, json={"results": []} if request.url.host == "api.tavily.com" else {"hits": []})
        with research_window(NOW):
            await self.search(item, handler)
            item.evidence[0].source_revision = "changed"
            await self.search(item, handler)
        with research_window(NOW + timedelta(days=7)):
            await self.search(item, handler)
        self.assertEqual(calls.count("api.tavily.com"), 3)

    async def test_200_error_object_remains_pending(self):
        item = candidate()
        with research_window(NOW):
            await self.search(item, lambda request: httpx.Response(200, json={"error": "do not persist this body"}))
        self.assertEqual(set(incomplete_searches(item)), {"hackernews", "tavily_social_web"})
        self.assertNotIn("do not persist", json.dumps(item.to_dict()))

    async def test_200_explicit_partial_or_error_is_not_complete(self):
        item = candidate()
        def handler(request):
            if request.url.host == "api.github.com":
                return httpx.Response(200, json={"items": [], "incomplete_results": True})
            if request.url.host == "api.tavily.com":
                return httpx.Response(200, json={"results": [], "error": "internal detail must not be persisted"})
            return httpx.Response(200, json={"hits": []})
        with patch.object(config, "GITHUB_TOKEN", "offline-key"), research_window(NOW):
            await self.search(item, handler)
        self.assertEqual(set(incomplete_searches(item)), {"github", "tavily_social_web"})
        self.assertNotIn("internal detail", json.dumps(item.to_dict()))

    async def test_missing_enabled_key_and_request_limit_are_not_zero(self):
        item = candidate()
        community = CommunityEvidenceClient()
        community.social_web.max_requests = 1
        community.social_web.requests_used = 1
        with research_window(NOW):
            await self.search(item, lambda request: httpx.Response(200, json={"hits": []}), community)
            self.assertEqual(item.community_search_receipts["tavily_social_web"]["status"], "not_executed")
        with patch.object(config, "TAVILY_API_KEY", ""), research_window(NOW):
            await self.search(item, lambda request: httpx.Response(200, json={"hits": []}))
        self.assertEqual(item.community_search_receipts["tavily_social_web"]["status"], "not_configured")

    async def test_cancellation_retains_completed_peer_receipt(self):
        item, entered = candidate(), asyncio.Event()
        community = CommunityEvidenceClient()
        async def slow(*args):
            entered.set()
            await asyncio.Future()
        community.social_web.search = slow
        with research_window(NOW), patch("sources.paradigm_evidence_source.httpx.AsyncClient",
                side_effect=lambda **kwargs: ASYNC_CLIENT(transport=httpx.MockTransport(lambda request: httpx.Response(200, json={"hits": []})), **kwargs)):
            task = asyncio.create_task(community.search(item))
            await entered.wait()
            await asyncio.sleep(0)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
        self.assertEqual(item.community_search_receipts["hackernews"]["status"], "completed")
        self.assertEqual(item.community_search_receipts["tavily_social_web"]["reason"], "query_interrupted")

    async def test_deep_partitions_failed_candidate_before_model(self):
        first, second = candidate("bad"), candidate("healthy")
        async def enrich(values, supporting):
            for item in values:
                record_search(item, "tavily_social_web", "failed" if item.key == "bad" else "completed")
            return values
        orchestrator = ParadigmOrchestrator.__new__(ParadigmOrchestrator)
        orchestrator.enricher = SimpleNamespace(run=enrich)
        orchestrator.synthesizer = SimpleNamespace(run=AsyncMock(side_effect=lambda values: values))
        orchestrator.trajectory = SimpleNamespace(run=AsyncMock(side_effect=lambda values: values))
        done, budget, failures = await orchestrator._deep_analyze_in_batches([first, second], [], time.monotonic() + 5)
        self.assertEqual(([item.key for item in done], budget, [item.key for item in failures]), (["healthy"], [], ["bad"]))
        self.assertEqual(failures[0].status, "pending_deep")
        self.assertEqual(failures[0].execution_failure_count, 1)
        for args in orchestrator.synthesizer.run.await_args_list:
            self.assertNotIn("bad", [item.key for item in args.args[0]])

    async def test_refresh_failed_zero_never_becomes_unchanged(self):
        item = candidate()
        item.status = "watchlist"
        community = SimpleNamespace(search=AsyncMock(return_value=[]), coverage=lambda: {})
        async def failed(value):
            record_search(value, "hackernews", "failed", reason="http_502")
            return []
        community.search.side_effect = failed
        enricher = EvidenceEnricher.__new__(EvidenceEnricher)
        enricher.community, enricher.concurrency = community, 1
        orchestrator = ParadigmOrchestrator.__new__(ParadigmOrchestrator)
        orchestrator.enricher = enricher
        orchestrator.synthesizer = SimpleNamespace(run=AsyncMock())
        changed, stable, budget, failed, count = await orchestrator._refresh_in_batches([item], [], time.monotonic() + 5)
        self.assertEqual((changed, stable, budget, count), ([], [], [], 1))
        self.assertEqual(len(failed), 1)
        orchestrator.synthesizer.run.assert_not_awaited()

    async def test_synthesis_and_delivery_gate_reject_unclosed_receipts(self):
        item = candidate()
        item.status = "selected"
        record_search(item, "hackernews", "failed")
        model = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=AsyncMock())))
        with self.assertRaises(RuntimeError):
            await ParadigmSynthesizer(client=model, model="mock").run([item])
        model.chat.completions.create.assert_not_awaited()
        with self.assertRaises(ResearchNotCompleteError):
            require_completed_research(COMPLETED_RESEARCH, candidates=[item])

    async def test_full_enrichment_success_reaches_completion_gate(self):
        item = candidate()
        enricher = EvidenceEnricher()
        enricher.documents.hydrate = AsyncMock(return_value={})
        enricher.semantic_scholar.enrich_paper = AsyncMock(return_value=(None, []))
        enricher.researchers.enrich = AsyncMock(return_value=[])
        transport = httpx.MockTransport(lambda request: httpx.Response(200,
            json={"results": []} if request.url.host == "api.tavily.com" else {"hits": []}))
        with research_window(NOW), patch("sources.paradigm_evidence_source.httpx.AsyncClient",
                side_effect=lambda **kwargs: ASYNC_CLIENT(transport=transport, **kwargs)):
            self.assertEqual(await enricher.run([item], []), [item])
        self.assertEqual(incomplete_searches(item), [])
        self.assertNotIn("community_runtime", item.community_search_receipts)
        enricher.researchers.enrich.assert_not_awaited()
        await enricher.enrich_people([item])  # Optional stage, after technical synthesis.
        enricher.researchers.enrich.assert_awaited_once()
        item.status = "watchlist"  # A completed technical judgment, not enrichment alone.
        require_completed_research(COMPLETED_RESEARCH, candidates=[item])

    async def test_full_enrichment_structural_failure_blocks_downstream(self):
        item = candidate()
        enricher = EvidenceEnricher()
        enricher.documents.hydrate = AsyncMock(return_value={})
        enricher.semantic_scholar.enrich_paper = AsyncMock(return_value=(None, []))
        enricher.community.search = AsyncMock(side_effect=RuntimeError("private error body"))
        enricher.researchers.enrich = AsyncMock()
        await enricher.run([item], [])
        self.assertEqual(incomplete_searches(item), ["community_runtime"])
        self.assertEqual(item.community_search_receipts["community_runtime"]["reason"], "RuntimeError")
        self.assertNotIn("private error body", json.dumps(item.to_dict()))
        enricher.researchers.enrich.assert_not_awaited()

    def test_legacy_payload_and_corrupt_receipts(self):
        raw = candidate().to_dict()
        raw.pop("community_search_receipts")
        self.assertEqual(candidate_from_dict(raw).community_search_receipts, {})
        for value in ([], {"status": "success"}, {"status": "completed", "result_count": True}):
            raw["community_search_receipts"] = {"hackernews": value}
            with self.assertRaises(ValueError):
                candidate_from_dict(raw)

    def test_legacy_explicit_failure_reopens_but_disabled_or_valid_zero_does_not(self):
        with tempfile.TemporaryDirectory() as directory:
            store = ParadigmStore(Path(directory) / "state.db")
            bad, disabled, healthy = candidate("bad"), candidate("disabled"), candidate("healthy")
            for item in (bad, disabled, healthy):
                item.status = "rejected"
            bad.community_coverage = {"tavily_social_web": "已发起 Tavily 搜索，但返回 HTTP 503"}
            disabled.community_coverage = {"x_official": "X 官方 API 已关闭或未配置；未执行标题搜索，不代表零讨论"}
            healthy.community_coverage = {"hackernews": "已执行 Hacker News 搜索；相关讨论命中 0 条"}
            store.save_candidates([bad, disabled, healthy])
            with store._connect() as conn:
                conn.execute("UPDATE paradigms SET last_reported_signature='delivered-signature', last_reported_at='2026-09-01' WHERE paradigm_key='bad'")
            self.assertEqual(store.reopen_failed_community_judgments(), 1)
            self.assertEqual(store.reopen_failed_community_judgments(), 0)
            restored = store.load_pending_deep_candidates()[0]
            self.assertEqual((restored.key, restored.status, restored.thesis), ("bad", "pending_deep", "original"))
            with store._connect() as conn:
                self.assertEqual(conn.execute("SELECT last_reported_signature, last_reported_at FROM paradigms WHERE paradigm_key='bad'").fetchone(), ("delivered-signature", "2026-09-01"))
            self.assertEqual({item.status for item in store.load_candidate_snapshots({"disabled", "healthy"})}, {"rejected"})

    def test_reopen_is_atomic_when_a_later_stored_identity_is_corrupt(self):
        with tempfile.TemporaryDirectory() as directory:
            store = ParadigmStore(Path(directory) / "state.db")
            values = [candidate("first"), candidate("second")]
            for item in values:
                item.status = "watchlist"
                item.community_coverage = {"github": "已尝试 GitHub Search，但返回 HTTP 429"}
            store.save_candidates(values)
            corrupt = values[1].to_dict()
            corrupt["key"] = "foreign"
            with store._connect() as conn:
                conn.execute("UPDATE paradigms SET payload_json=? WHERE paradigm_key='second'", (json.dumps(corrupt),))
            with self.assertRaises(ValueError):
                store.reopen_failed_community_judgments()
            with store._connect() as conn:
                self.assertEqual(conn.execute("SELECT status FROM paradigms WHERE paradigm_key='first'").fetchone()[0], "watchlist")

    async def test_github_queued_peer_honors_circuit_before_request(self):
        entered, release = asyncio.Event(), asyncio.Event()
        requests = []
        async def handler(request):
            requests.append(request)
            entered.set()
            await release.wait()
            return httpx.Response(403)
        community = CommunityEvidenceClient()
        first, second = candidate("first"), candidate("second")
        with patch.object(config, "GITHUB_TOKEN", "offline-key"):
            async with ASYNC_CLIENT(transport=httpx.MockTransport(handler)) as client:
                initial = asyncio.create_task(community._github(client, first))
                await entered.wait()
                queued = asyncio.create_task(community._github(client, second))
                await asyncio.sleep(0)
                release.set()
                await asyncio.gather(initial, queued)
        self.assertEqual(len(requests), 1)
        self.assertEqual(second.community_search_receipts["github"]["status"], "not_executed")

    async def test_scrubbed_index_links_reuse_search_without_claiming_discussion(self):
        item, calls = candidate(), []
        def handler(request):
            calls.append(request.url.host)
            if request.url.host == "api.tavily.com":
                return httpx.Response(200, json={"results": [{"title": "Adaptive Memory", "url": "https://x.com/example/status/1", "content": "Adaptive Memory 2610.00001 discussion"}]})
            return httpx.Response(200, json={"hits": []})
        with research_window(NOW):
            await self.search(item, handler)
            self.assertTrue(any(evidence.source == "tavily-x" for evidence in item.evidence))
            item = EvidenceEnricher.finalize([candidate_from_dict(item.to_dict())])[0]
            await self.search(item, handler)
        self.assertEqual(calls.count("api.tavily.com"), 1)
        index = next(evidence for evidence in item.evidence if evidence.source == "tavily-x")
        self.assertEqual(index.summary, "")
        self.assertTrue(index.raw["indexed_discovery_only"])


class CatalogClosureTests(unittest.IsolatedAsyncioTestCase):
    async def fetch(self, handler, cache=None):
        source = PriorityResearchPageSource(pages=["https://modelbest.cn/"], lookback_days=7,
            stage_cache=cache, catalog_sleeper=AsyncMock(),
            client_factory=lambda **kwargs: ASYNC_CLIENT(transport=httpx.MockTransport(handler), **kwargs))
        with research_window(NOW):
            values = await source.safe_fetch()
        return source, values

    async def test_pagination_exhausted_old_catalog_is_valid_empty(self):
        calls = []
        def handler(request):
            calls.append(request)
            if request.method == "GET":
                return httpx.Response(200, text=nuxt_page([{"title": "Old work", "publishTime": "2024-01-01", "link": ""}], more=True))
            self.assertEqual(json.loads(request.content), {"cateId": 7, "pageNum": 2, "pageSize": 30})
            return httpx.Response(200, json={"code": 1, "data": {"hasMore": False, "list": None}})
        source, values = await self.fetch(handler)
        self.assertEqual((values, source.fetch_status), ([], "completed"))
        page = source.coverage()["pages"]["https://modelbest.cn/"]
        self.assertEqual(page["status"], "no_recent_links")
        self.assertTrue(page["catalog_diagnostics"]["complete"])
        self.assertEqual(len(calls), 2)

    async def test_failed_page_resumes_without_refetching_completed_pages(self):
        calls, fail = [], True
        def handler(request):
            if request.method == "GET":
                return httpx.Response(200, text=nuxt_page([{"title": "Old root", "publishTime": "2024-01-01"}], more=True))
            number = json.loads(request.content)["pageNum"]
            calls.append(number)
            if number == 3 and fail:
                return httpx.Response(503)
            return httpx.Response(200, json={"code": 1, "data": {"hasMore": number == 2,
                "list": [{"title": f"Old page {number}", "publishTime": "2024-02-01"}]}})
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.db"
            first, _ = await self.fetch(handler, ParadigmStore(path).campaigns)
            self.assertEqual(first.fetch_status, "partial")
            fail = False
            second, _ = await self.fetch(handler, ParadigmStore(path).campaigns)
        self.assertEqual(second.fetch_status, "completed")
        self.assertEqual(calls, [2, 3, 3])

    async def test_repeated_page_and_missing_category_block_empty(self):
        card = {"title": "Old root", "publishTime": "2024-01-01"}
        def repeated(request):
            return httpx.Response(200, text=nuxt_page([card], more=True)) if request.method == "GET" else httpx.Response(200, json={"code": 1, "data": {"hasMore": True, "list": [card]}})
        source, _ = await self.fetch(repeated)
        self.assertNotEqual(source.fetch_status, "completed")
        def missing(request):
            return httpx.Response(200, text=nuxt_page([]).replace("news-cate-13-1-30", "other-data"))
        source, _ = await self.fetch(missing)
        self.assertNotEqual(source.fetch_status, "completed")


class BibliographyClosureTests(unittest.IsolatedAsyncioTestCase):
    async def test_public_audit_never_implicitly_authorizes_identity_api(self):
        requests = []
        def handler(request):
            requests.append(request.url.host)
            return httpx.Response(200, text='<p>LeCun, Y. (2026). “Adaptive Memory”. Journal.</p>')
        with patch.object(config, "PRIORITY_BIBLIOGRAPHY_RESOLUTION_ENABLED", True), patch.object(config, "OPENALEX_API_KEY", "offline-key"):
            result = await SourceAudit(transport=httpx.MockTransport(handler)).run([Entry("nyu", "official", NYU)], reference_time=NOW)
        self.assertEqual(requests, ["wp.nyu.edu"])
        self.assertEqual(result["counts"], {"failed": 1})

    def test_exact_title_needs_unique_author_and_explicit_primary_url(self):
        self.assertEqual(match_work(record(), payload(work()))["status"], "resolved_url")
        for works in ((work(author="Another Person"),), (work(author="Another LeCun"),), (work(), work(identity="W2")),
                      (work(url="https://index.example/search?q=paper"),), (work(url="https://doi.org/10.1234/item?Signature=x"),)):
            self.assertEqual(match_work(record(), payload(*works))["status"], "pending")
        truncated = payload(work())
        truncated["meta"]["count"] = 2
        self.assertEqual(match_work(record(), truncated)["reason"], "identity_search_truncated")

    async def test_disabled_resolver_uses_no_key_or_network(self):
        client = SimpleNamespace(get=AsyncMock())
        with research_window(NOW):
            rows = await BibliographyResolver().resolve(client, NYU, [record()], lookback_days=7)
        client.get.assert_not_awaited()
        self.assertEqual(rows[0]["reason"], "resolution_disabled")

    async def test_request_bound_and_durable_manifest_do_not_drop_old_pending(self):
        calls = []
        def handler(request):
            self.assertNotIn("api_key", request.url.params)
            calls.append(request)
            return httpx.Response(200, json=payload(work()))
        with tempfile.TemporaryDirectory() as directory, research_window(NOW), patch.object(config, "OPENALEX_API_KEY", "offline-key"):
            path = Path(directory) / "state.db"
            async with ASYNC_CLIENT(transport=httpx.MockTransport(handler)) as client:
                first = BibliographyResolver(stage_cache=ParadigmStore(path).campaigns, enabled=True, max_requests=1)
                rows = await first.resolve(client, NYU, [record(), record("two")], lookback_days=7)
                self.assertEqual([row["status"] for row in rows], ["resolved_url", "pending"])
                second = BibliographyResolver(stage_cache=ParadigmStore(path).campaigns, enabled=True, max_requests=1)
                rows = await second.resolve(client, NYU, [], lookback_days=7)
        self.assertEqual(len(rows), 2)
        self.assertEqual(len(calls), 2)
        self.assertEqual([row["status"] for row in rows], ["resolved_url", "resolved_url"])

    async def test_429_opens_circuit_and_preserves_denominator(self):
        client = SimpleNamespace(get=AsyncMock(return_value=httpx.Response(429, request=httpx.Request("GET", "https://api.openalex.org/works"))))
        with research_window(NOW), patch.object(config, "OPENALEX_API_KEY", "offline-key"):
            rows = await BibliographyResolver(enabled=True).resolve(client, NYU, [record(), record("two")], lookback_days=7)
        self.assertEqual(client.get.await_count, 1)
        self.assertEqual([row["reason"] for row in rows], ["http_429", "provider_circuit_open"])

    async def test_ambiguous_first_record_cannot_starve_later_first_lookups(self):
        queries = []
        def handler(request):
            query = request.url.params["search.title"].strip('"')
            queries.append(query)
            return httpx.Response(200, json=payload(work(query), work(query, identity="W2")) if query == "ambiguous work" else payload(work(query)))
        with tempfile.TemporaryDirectory() as directory, research_window(NOW), patch.object(config, "OPENALEX_API_KEY", "offline-key"):
            path = Path(directory) / "state.db"
            async with ASYNC_CLIENT(transport=httpx.MockTransport(handler)) as client:
                records = [record("first", "Ambiguous Work"), record("second", "Adaptive Memory")]
                first = BibliographyResolver(stage_cache=ParadigmStore(path).campaigns, enabled=True, max_requests=1)
                await first.resolve(client, NYU, records, lookback_days=7)
                second = BibliographyResolver(stage_cache=ParadigmStore(path).campaigns, enabled=True, max_requests=1)
                rows = await second.resolve(client, NYU, [], lookback_days=7)
        self.assertEqual(queries, ["ambiguous work", "adaptive memory"])
        self.assertEqual({row["key"] for row in rows}, {"first", "second"})

    async def test_identity_search_does_not_supply_publication_date(self):
        title = "Adaptive Memory"
        citation = '<p>LeCun, Y. (2026). “Adaptive Memory”. Journal.</p>'
        def handler(request):
            if request.url.host == "api.openalex.org":
                item = work()
                item["publication_date"] = "2026-10-08"
                return httpx.Response(200, json=payload(item))
            if request.url.host == "wp.nyu.edu":
                return httpx.Response(200, text=citation)
            return httpx.Response(200, text='<meta name="citation_title" content="Adaptive Memory"><meta name="citation_author" content="Yann LeCun"><main>' + "Actual primary description. " * 12 + '</main>')
        with tempfile.TemporaryDirectory() as directory, research_window(NOW), patch.object(config, "OPENALEX_API_KEY", "offline-key"):
            store = ParadigmStore(Path(directory) / "state.db")
            source = PriorityResearchPageSource(pages=[NYU], lookback_days=7,
                bibliography_resolver=BibliographyResolver(enabled=True, stage_cache=store.campaigns),
                client_factory=lambda **kwargs: ASYNC_CLIENT(transport=httpx.MockTransport(handler), **kwargs))
            self.assertEqual(await source.safe_fetch(), [])
            self.assertNotEqual(source.fetch_status, "completed")
            self.assertEqual(source.coverage()["unresolved_citations"], 1)

    async def test_primary_verified_old_date_closes_without_recent_origin(self):
        def handler(request):
            if request.url.host == "api.openalex.org":
                return httpx.Response(200, json=payload(work()))
            if request.url.host == "wp.nyu.edu":
                return httpx.Response(200, text='<p>LeCun, Y. (2026). “Adaptive Memory”. Journal.</p>')
            return httpx.Response(200, text='<meta name="citation_title" content="Adaptive Memory"><meta name="citation_author" content="Yann LeCun"><meta name="citation_date" content="2026-06-01"><main>' + "Actual primary description. " * 12 + '</main>')
        with research_window(NOW), patch.object(config, "OPENALEX_API_KEY", "offline-key"):
            source = PriorityResearchPageSource(pages=[NYU], lookback_days=7,
                bibliography_resolver=BibliographyResolver(enabled=True),
                client_factory=lambda **kwargs: ASYNC_CLIENT(transport=httpx.MockTransport(handler), **kwargs))
            self.assertEqual(await source.safe_fetch(), [])
        self.assertEqual(source.fetch_status, "completed")
        self.assertEqual(source.coverage()["unresolved_citations"], 0)


if __name__ == "__main__":
    unittest.main()
