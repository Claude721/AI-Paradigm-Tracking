"""Real official source, SQLite queue, coverage and cancellation integration."""

import asyncio
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import AsyncMock, patch

import httpx
import config
from database.paradigm_store import ParadigmStore
from paradigms.completion import discovery_retry_sources, research_completion_violations
from paradigms.discovery import DiscoveryBatch, ParadigmDiscovery
from runtime_clock import research_window
from sources.official_detail_queue import OfficialDetailQueue, STAGE, TASK_STAGE
from sources.priority_research_source import PriorityResearchPageSource, _ArticleParser


NOW = datetime(2026, 5, 7, tzinfo=timezone.utc)
INDEX = "https://lab.example/research"
ASYNC_CLIENT = httpx.AsyncClient


def index(*names):
    return "".join(f'<a href="/research/{name}">Adaptive {name} architecture</a>' for name in names)


def article(name, *, date="2026-05-06", modified=""):
    return (f'<meta property="article:published_time" content="{date}">'
            f'<meta property="article:modified_time" content="{modified}">'
            f'<article><h1>Adaptive {name} architecture</h1><p>'
            + "A public mechanism changes the training signal and inference state. " * 4 + '</p></article>')


class OfficialQueueTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.path = Path(temporary.name) / "state.db"
        self.calls, self.names, self.fail = [], ["a", "b"], set()

    def handler(self, request):
        self.calls.append(request.url.path)
        if request.url.path == "/research":
            return httpx.Response(200, text=index(*self.names))
        name = request.url.path.rsplit("/", 1)[-1]
        return httpx.Response(503 if name in self.fail else 200, text=article(name))

    def source(self, handler=None, *, limit=0):
        with patch.object(config, "PRIORITY_RESEARCH_LINK_SAFETY_LIMIT", limit):
            return PriorityResearchPageSource(pages=[INDEX], lookback_days=7,
                stage_cache=ParadigmStore(self.path).campaigns,
                client_factory=lambda **kwargs: ASYNC_CLIENT(transport=httpx.MockTransport(handler or self.handler), **kwargs))

    async def fetch(self, *, limit=0, handler=None):
        source = self.source(handler, limit=limit)
        with research_window(NOW):
            values = await source.safe_fetch()
        return source, values

    async def test_cap_keeps_tail_pending_and_restart_only_fetches_tail(self):
        first, values = await self.fetch(limit=1)
        self.assertEqual((len(values), first.fetch_status, first.coverage()["detail_pending"]), (1, "partial", 1))
        self.calls.clear()
        second, values = await self.fetch(limit=1)
        self.assertEqual((len(values), second.fetch_status, second.coverage()["detail_pending"]), (2, "completed", 0))
        self.assertEqual(self.calls, ["/research", "/research/b"])
        self.assertEqual(second.page_coverage[INDEX]["detail_cached"], 1)

    async def test_failed_first_detail_does_not_starve_peer_or_disappear(self):
        self.fail.add("a")
        first, _ = await self.fetch(limit=1)
        self.assertEqual(first.coverage()["detail_pending"], 2)
        self.calls.clear()
        second, values = await self.fetch(limit=1)
        self.assertEqual(len(values), 1)
        self.assertEqual(self.calls, ["/research", "/research/b"])
        self.names, self.fail = ["b"], set()
        self.calls.clear()
        third, values = await self.fetch(limit=1)
        self.assertEqual((len(values), third.fetch_status), (2, "completed"))
        self.assertEqual(self.calls, ["/research", "/research/a"])

    async def test_unexpected_article_exception_isolates_only_its_detail(self):
        feed = _ArticleParser.feed
        def faulty(parser, content):
            if "Adaptive a architecture" in content:
                raise RuntimeError("private diagnostic must not be persisted")
            return feed(parser, content)
        with patch.object(_ArticleParser, "feed", faulty):
            source, values = await self.fetch()
        self.assertEqual((len(values), source.fetch_status, source.coverage()["detail_pending"]), (1, "partial", 1))
        self.assertEqual(source.coverage()["detail_failures"], 1)

    async def test_cancellation_retains_already_committed_healthy_detail(self):
        blocked = asyncio.Event()
        async def slow(request):
            if request.url.path == "/research/b":
                blocked.set()
                await asyncio.Future()
            return self.handler(request)
        source = self.source(slow)
        with research_window(NOW):
            task = asyncio.create_task(source.safe_fetch())
            await blocked.wait()
            self.assertEqual(len(source.partial_results), 1)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
        self.calls.clear()
        resumed, values = await self.fetch()
        self.assertEqual((len(values), resumed.fetch_status), (2, "completed"))
        self.assertEqual(self.calls, ["/research", "/research/b"])

    async def test_discovery_timeout_returns_committed_peer_without_closing_source(self):
        async def slow(request):
            if request.url.path == "/research/b":
                await asyncio.Future()
            return self.handler(request)
        source = self.source(slow)
        discovery = ParadigmDiscovery(lookback_days=7)
        discovery.source_timeout_seconds = 0.2
        with research_window(NOW):
            values, health = await discovery._bounded_fetch(source)
        self.assertEqual((len(values), health["status"], health["results"]), (1, "timed_out", 1))
        self.assertEqual(source.coverage()["detail_pending"], 1)
        self.calls.clear()
        resumed, values = await self.fetch()
        self.assertEqual((len(values), resumed.fetch_status), (2, "completed"))
        self.assertEqual(self.calls, ["/research", "/research/b"])

    async def test_corrupt_cached_identity_cannot_become_completed_coverage(self):
        await self.fetch()
        cache = ParadigmStore(self.path).campaigns
        with research_window(NOW):
            queue = OfficialDetailQueue(INDEX, 7, cache)
            signature = queue.task_signature(INDEX + "/a")
            saved = cache.cache_get(TASK_STAGE, signature)
            saved["evidence"][0]["url"] = "https://foreign.example/other"
            cache.cache_save(TASK_STAGE, signature, saved)
        self.calls.clear()
        source, values = await self.fetch()
        self.assertEqual((values, source.fetch_status), ([], "query_failed"))
        self.assertEqual(self.calls, ["/research"])

    async def test_contract_change_reopens_without_erasing_missing_old_task(self):
        await self.fetch()
        self.names = ["b"]
        self.calls.clear()
        with patch("sources.official_detail_queue.source_record", return_value={"parser": "changed"}):
            source, values = await self.fetch()
        self.assertEqual((len(values), source.fetch_status), (2, "completed"))
        self.assertEqual(set(self.calls), {"/research", "/research/a", "/research/b"})

    async def test_completed_receipt_without_contract_is_not_reused(self):
        await self.fetch()
        cache = ParadigmStore(self.path).campaigns
        with research_window(NOW):
            queue = OfficialDetailQueue(INDEX, 7, cache)
            signature = queue.task_signature(INDEX + "/a")
            saved = cache.cache_get(TASK_STAGE, signature)
            saved.pop("contract")
            cache.cache_save(TASK_STAGE, signature, saved)
        self.calls.clear()
        source, values = await self.fetch()
        self.assertEqual((values, source.fetch_status), ([], "query_failed"))
        self.assertEqual(self.calls, ["/research"])

    async def legacy_queue(self):
        await self.fetch(limit=1)
        cache = ParadigmStore(self.path).campaigns
        queue = OfficialDetailQueue(INDEX, 7, cache)
        old = {"version": 1, "contract": "0" * 64, "tasks": {
            url: {**task, "attempts": (1 if url.endswith("/a") else 7)}
            for url, task in queue.tasks.items()}}
        cache.cache_save(STAGE, queue.signature, old)
        return cache, old, queue.signature

    async def test_inline_v1_migration_preserves_omitted_tasks_and_attempts(self):
        with research_window(NOW):
            cache, old, signature = await self.legacy_queue()
            queue = OfficialDetailQueue(INDEX, 7, cache)
            queue.merge([])  # A changed current index cannot shrink history.
            manifest = cache.cache_get(STAGE, signature)
            self.assertEqual(manifest["version"], 2)
            self.assertEqual(set(manifest["tasks"]), set(old["tasks"]))
            self.assertTrue(all(set(task) == {"link"} for task in manifest["tasks"].values()))
            resumed = OfficialDetailQueue(INDEX, 7, cache)
            self.assertEqual(resumed.pending_count, 2)  # Policy repair reopens.
            self.assertEqual([task["attempts"] for task in resumed.tasks.values()], [1, 7])
            self.assertEqual(len(resumed.cached()[0]), 0)

    async def test_inline_migration_failure_keeps_old_parent_until_children_saved(self):
        with research_window(NOW):
            cache, old, signature = await self.legacy_queue()
            queue = OfficialDetailQueue(INDEX, 7, cache)
            save = cache.cache_save
            def fail_one(stage, key, result):
                if stage == TASK_STAGE and result["link"]["url"].endswith("/b"):
                    raise OSError("child migration failure")
                return save(stage, key, result)
            with patch.object(cache, "cache_save", side_effect=fail_one):
                with self.assertRaises(OSError):
                    queue.merge([])
            self.assertEqual(cache.cache_get(STAGE, signature), old)
            retry = OfficialDetailQueue(INDEX, 7, cache)
            retry.merge([])
            restored = OfficialDetailQueue(INDEX, 7, cache)
            self.assertEqual([task["attempts"] for task in restored.tasks.values()], [1, 7])

    async def test_batched_receipt_load_spans_parameter_chunks_and_is_stage_scoped(self):
        cache = ParadigmStore(self.path).campaigns
        keys = [f"task-{index}" for index in range(513)]
        for index, key in enumerate(keys):
            cache.cache_save(TASK_STAGE, key, {"index": index})
        cache.cache_save(STAGE, keys[0], {"wrong_stage": True})
        self.assertEqual(cache.cache_get_many(TASK_STAGE, []), {})
        found = cache.cache_get_many(TASK_STAGE, [*keys, keys[0], "missing"])
        self.assertEqual(found, {key: {"index": index} for index, key in enumerate(keys)})

    async def test_modified_only_date_is_not_a_publication_exclusion(self):
        self.names = ["a"]
        def handler(request):
            return httpx.Response(200, text=index("a") if request.url.path == "/research" else article("a", date="", modified="2024-01-01"))
        source, values = await self.fetch(handler=handler)
        self.assertEqual(len(values), 1)
        self.assertEqual(values[0].published_at, "")
        self.assertEqual(values[0].raw["date_basis"], "modified_only")
        self.assertEqual(source.page_coverage[INDEX]["detail_outside_window"], 0)

    async def test_future_publication_is_outside_the_frozen_window(self):
        self.names = ["a"]
        def handler(request):
            return httpx.Response(200, text=index("a") if request.url.path == "/research" else article("a", date="2026-05-08"))
        source, values = await self.fetch(handler=handler)
        self.assertEqual((values, source.fetch_status), ([], "completed"))
        self.assertEqual(source.page_coverage[INDEX]["detail_outside_window"], 1)

    async def test_summary_flags_cannot_hide_pending_detail_receipt(self):
        first, _ = await self.fetch(limit=1)
        frontier = {"official_pages": first.coverage()}
        stats = {"research_incomplete": False, "coverage_incomplete": False, "frontier_coverage": frontier}
        self.assertTrue(research_completion_violations(stats))
        frontier["official_pages"]["detail_pending"] = 0
        self.assertTrue(research_completion_violations(stats))
        self.assertEqual(discovery_retry_sources(frontier), {"priority-research-page"})

    async def test_one_index_parse_exception_preserves_other_entry(self):
        from sources.priority_research_source import _discover_index_links
        bad = "https://broken.example/research"
        def discover(html, base, **kwargs):
            if base.startswith("https://broken.example/"):
                raise RuntimeError("invalid index")
            return _discover_index_links(html, base, **kwargs)
        source = self.source()
        source.pages = [bad, INDEX]
        with patch("sources.priority_research_source._discover_index_links", side_effect=discover), research_window(NOW):
            values = await source.safe_fetch()
        self.assertEqual((len(values), source.fetch_status), (2, "partial"))
        self.assertEqual(source.coverage()["parse_zero_links"], 1)

    async def test_failed_result_save_keeps_task_pending_and_peer_committed(self):
        source = self.source()
        save = source.stage_cache.cache_save
        def fail_one(stage, signature, result):
            if stage == TASK_STAGE and result["link"]["url"] == INDEX + "/a" and result["status"] == "completed":
                raise OSError("checkpoint save failure")
            return save(stage, signature, result)
        with patch.object(source.stage_cache, "cache_save", side_effect=fail_one), research_window(NOW):
            values = await source.safe_fetch()
        self.assertEqual((len(values), source.coverage()["detail_pending"]), (1, 1))
        self.calls.clear()
        resumed, values = await self.fetch()
        self.assertEqual((len(values), resumed.fetch_status), (2, "completed"))
        self.assertEqual(self.calls, ["/research", "/research/a"])

    async def test_failed_attempt_save_blocks_only_its_http_and_preserves_peer(self):
        source = self.source()
        save = source.stage_cache.cache_save
        def fail_one(stage, signature, result):
            if stage == TASK_STAGE and result["link"]["url"] == INDEX + "/a" and result["status"] == "pending":
                raise OSError("attempt checkpoint failure")
            return save(stage, signature, result)
        with patch.object(source.stage_cache, "cache_save", side_effect=fail_one), research_window(NOW):
            values = await source.safe_fetch()
            queue = OfficialDetailQueue(INDEX, 7, source.stage_cache)
        self.assertEqual((len(values), source.fetch_status, source.coverage()["detail_pending"]), (1, "partial", 1))
        self.assertEqual(source.page_coverage[INDEX]["detail_checkpoint_failures"], 1)
        self.assertEqual(queue.tasks[INDEX + "/a"]["attempts"], 0)
        self.assertEqual(self.calls, ["/research", "/research/b"])
        self.calls.clear()
        resumed, values = await self.fetch()
        self.assertEqual((len(values), resumed.fetch_status), (2, "completed"))
        self.assertEqual(self.calls, ["/research", "/research/a"])

    async def test_all_attempt_checkpoint_failures_are_not_no_recent_links(self):
        source = self.source()
        save = source.stage_cache.cache_save
        def fail_attempts(stage, signature, result):
            if stage == TASK_STAGE and result["status"] == "pending":
                raise OSError("attempt storage unavailable")
            return save(stage, signature, result)
        with patch.object(source.stage_cache, "cache_save", side_effect=fail_attempts), research_window(NOW):
            values = await source.safe_fetch()
        self.assertEqual((values, source.fetch_status, source.coverage()["detail_pending"]), ([], "partial", 2))
        self.assertEqual(source.page_coverage[INDEX]["status"], "detail_checkpoint_failed")
        self.assertEqual(source.coverage()["no_recent_links"], 0)
        self.assertEqual(self.calls, ["/research"])


class AdapterScopeTests(unittest.IsolatedAsyncioTestCase):
    def discovery(self):
        result = ParadigmDiscovery(lookback_days=7, high_signal_lookback_days=30)
        result.priority_pages.pages = [INDEX]
        return result

    def snapshot(self, discovery, *, legacy=False, status="partial"):
        coverage = {"source_health": {"priority-research-page": {"status": status}},
                    "source_plan_signatures": discovery._plan_signatures(),
                    "official_pages": {"pages": {INDEX: {"status": "parsed"}}},
                    "recall_windows": {"source_windows": {"official_research_pages": 30}}}
        coverage["source_plan_signatures"]["priority-research-page"] = "old-adapter-contract"
        if not legacy:
            coverage["source_scope_signatures"] = discovery._plan_signatures(include_adapters=False)
        return DiscoveryBatch([], [], {}, coverage)

    def fresh(self, discovery):
        return DiscoveryBatch([], [], {}, {"source_health": {"priority-research-page": {"status": "completed"}},
            "source_plan_signatures": discovery._plan_signatures(),
            "source_scope_signatures": discovery._plan_signatures(include_adapters=False),
            "official_pages": {"pages": {INDEX: {"status": "parsed"}}}})

    async def test_adapter_change_retries_same_scope_and_updates_contract(self):
        for legacy in (False, True):
            discovery = self.discovery()
            prior = self.snapshot(discovery, legacy=legacy)
            discovery.run = AsyncMock(return_value=self.fresh(discovery))
            result = await discovery.retry(prior)
            self.assertEqual(discovery.run.await_args.kwargs, {"source_names": {"priority-research-page"}})
            self.assertEqual(result.coverage["source_plan_signatures"], discovery._plan_signatures())

    async def test_healthy_old_contract_is_not_silently_reused(self):
        discovery = self.discovery()
        prior = self.snapshot(discovery, status="completed")
        self.assertEqual(discovery.retry_sources(prior), {"priority-research-page"})
        discovery.run = AsyncMock(return_value=self.fresh(discovery))
        await discovery.retry(prior)
        discovery.run.assert_awaited_once()

    async def test_detail_layout_change_also_invalidates_positive_source_receipt(self):
        discovery = self.discovery()
        prior = self.snapshot(discovery, status="completed")
        prior.coverage["source_plan_signatures"] = discovery._plan_signatures()
        with patch("paradigms.discovery.official_detail_policy", return_value="new-layout-policy"):
            self.assertEqual(discovery.retry_sources(prior), {"priority-research-page"})
            discovery.run = AsyncMock(return_value=self.fresh(discovery))
            await discovery.retry(prior)
        discovery.run.assert_awaited_once()

    async def test_removed_or_changed_window_scope_is_still_rejected(self):
        for legacy in (False, True):
            for change in ("pages", "window"):
                discovery = self.discovery()
                prior = self.snapshot(discovery, legacy=legacy)
                if change == "pages":
                    discovery.priority_pages.pages = []
                else:
                    discovery.priority_pages.lookback_days = 7
                discovery.run = AsyncMock()
                with self.assertRaises(RuntimeError):
                    await discovery.retry(prior)
                discovery.run.assert_not_awaited()

    async def test_legacy_count_without_full_manifest_is_not_scope_proof(self):
        discovery = self.discovery()
        prior = self.snapshot(discovery, legacy=True)
        prior.coverage["official_pages"] = {"total_pages": 1, "checked_pages": 1}
        discovery.run = AsyncMock()
        with self.assertRaises(RuntimeError):
            await discovery.retry(prior)
        discovery.run.assert_not_awaited()

    async def test_production_orchestrator_calls_contract_aware_retry_planner(self):
        from tests.test_research_campaign import orchestrator
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        store = ParadigmStore(Path(temporary.name) / "state.db")
        prior = DiscoveryBatch([], [], {}, {"source_health": {"priority-research-page": {"status": "completed"}}})
        process = orchestrator(store, AsyncMock(return_value=prior), AsyncMock(return_value=[]))
        await process.run(reference_time=NOW)
        next_process = orchestrator(store, AsyncMock(), AsyncMock(return_value=[]))
        planner = next_process.discovery.retry_sources = unittest.mock.Mock(return_value={"priority-research-page"})
        retry = next_process.discovery.retry = AsyncMock(return_value=prior)
        result = await next_process.run(reference_time=NOW)
        planner.assert_called_once()
        retry.assert_awaited_once()
        self.assertEqual(result["discovery_retry_sources"], ["priority-research-page"])
