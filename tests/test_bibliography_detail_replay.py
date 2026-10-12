"""Resolved bibliography identities and detail receipts must close together."""

import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

import httpx
import config
from database.paradigm_store import ParadigmStore
from runtime_clock import research_window
from sources.bibliography_resolver import BibliographyResolver
from sources.official_detail_queue import OfficialDetailQueue, TASK_STAGE
from sources.priority_research_source import PriorityResearchPageSource


NOW = datetime(2026, 5, 7, tzinfo=timezone.utc)
INDEX = "https://wp.nyu.edu/cilvr/cilvr-group-publications/"


class BibliographyDetailReplayTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.path = Path(temporary.name) / "state.db"
        self.calls = []
        self.published = "2026-05-06"
        self.names = ("a", "b")
        self.fail = set()
        self.misdirect_b = False

    def handler(self, request):
        self.calls.append((request.url.host, request.url.path))
        if request.url.host == "wp.nyu.edu":
            return httpx.Response(200, text="".join(
                f'<p>Chen, A. (2026). “Adaptive {name} architecture”. Journal.</p>' for name in self.names))
        if request.url.host == "api.openalex.org":
            name = "a" if " a " in request.url.params["search"] else "b"
            number = 1 if name == "a" else 2
            return httpx.Response(200, json={"meta": {"count": 1}, "results": [{
                "id": f"https://openalex.org/W{number}", "display_name": f"Adaptive {name} architecture",
                "publication_year": 2026, "authorships": [{"author": {"display_name": "Ada Chen"}}],
                "primary_location": {"landing_page_url": f"https://arxiv.org/abs/2605.1000{1 if self.misdirect_b else number}"}}]})
        name = "a" if request.url.path.endswith("1") else "b"
        if name in self.fail:
            return httpx.Response(503)
        return httpx.Response(200, text=(f'<meta name="citation_title" content="Adaptive {name} architecture">'
            '<meta name="citation_author" content="Ada Chen">'
            f'<meta name="citation_date" content="{self.published}"><main>'
            + "The mechanism changes the training signal and inference state. " * 12 + '</main>'))

    def source(self, *, limit=1):
        cache = ParadigmStore(self.path).campaigns
        return PriorityResearchPageSource(pages=[INDEX], lookback_days=7, per_page=limit, stage_cache=cache,
            bibliography_resolver=BibliographyResolver(stage_cache=cache, enabled=True, max_requests=8),
            client_factory=lambda **kwargs: httpx.AsyncClient(transport=httpx.MockTransport(self.handler), **kwargs))

    async def test_bounded_details_close_on_restart_without_reopening_verified_peer(self):
        with research_window(NOW), patch.object(config, "OPENALEX_API_KEY", "offline-only"):
            first = self.source()
            values = await first.safe_fetch()
            self.assertEqual((len(values), first.fetch_status, first.coverage()["detail_pending"]), (1, "partial", 1))
            self.calls.clear()
            second = self.source()
            values = await second.safe_fetch()
        self.assertEqual((len(values), second.fetch_status, second.coverage()["detail_pending"]), (2, "completed", 0))
        self.assertEqual(second.coverage()["unresolved_citations"], 0)
        self.assertEqual(self.calls, [("wp.nyu.edu", "/cilvr/cilvr-group-publications/"), ("arxiv.org", "/abs/2605.10002")])

    async def test_verified_outside_window_receipts_close_and_resume_without_details(self):
        self.published = "2026-04-01"
        with research_window(NOW), patch.object(config, "OPENALEX_API_KEY", "offline-only"):
            first = self.source()
            self.assertEqual(await first.safe_fetch(), [])
            self.assertEqual(first.fetch_status, "partial")
            second = self.source()
            self.assertEqual(await second.safe_fetch(), [])
            self.assertEqual(second.fetch_status, "completed")
            self.calls.clear()
            third = self.source()
            self.assertEqual(await third.safe_fetch(), [])
        self.assertEqual(third.fetch_status, "completed")
        self.assertEqual(third.page_coverage[INDEX]["detail_outside_window"], 2)
        self.assertEqual(third.coverage()["unresolved_citations"], 0)
        self.assertEqual(self.calls, [("wp.nyu.edu", "/cilvr/cilvr-group-publications/")])

    async def test_failed_detail_commit_cannot_acknowledge_primary_metadata(self):
        with research_window(NOW), patch.object(config, "OPENALEX_API_KEY", "offline-only"):
            first = self.source(limit=0)
            save = first.stage_cache.cache_save
            def fail_one(stage, signature, result):
                if stage == TASK_STAGE and result["status"] == "completed" and result["link"]["url"].endswith("1"):
                    raise OSError("detail commit failure")
                return save(stage, signature, result)
            with patch.object(first.stage_cache, "cache_save", side_effect=fail_one):
                values = await first.safe_fetch()
            self.assertEqual((len(values), first.fetch_status, first.coverage()["detail_pending"]), (1, "partial", 1))
            rows = first.page_coverage[INDEX]["bibliography_diagnostics"]["resolution_tasks"]
            row = next(row for row in rows if row["title"] == "Adaptive a architecture")
            self.assertEqual(first.stage_cache.cache_get("bibliography_identity", row["input_signature"])["status"], "resolved_url")
            self.calls.clear()
            second = self.source()
            values = await second.safe_fetch()
        self.assertEqual((len(values), second.fetch_status), (2, "completed"))
        self.assertEqual(self.calls, [("wp.nyu.edu", "/cilvr/cilvr-group-publications/"), ("arxiv.org", "/abs/2605.10001")])

    async def test_failed_identity_acknowledgement_reuses_committed_primary_body(self):
        with research_window(NOW), patch.object(config, "OPENALEX_API_KEY", "offline-only"):
            first = self.source(limit=0)
            save = first.stage_cache.cache_save
            def fail_one(stage, signature, result):
                if stage == "bibliography_identity" and result["status"] == "primary_verified" and result["url"].endswith("1"):
                    raise OSError("identity acknowledgement failure")
                return save(stage, signature, result)
            with patch.object(first.stage_cache, "cache_save", side_effect=fail_one):
                values = await first.safe_fetch()
            self.assertEqual((len(values), first.fetch_status, first.coverage()["detail_pending"]), (2, "partial", 0))
            self.assertEqual(first.coverage()["unresolved_citations"], 1)
            self.calls.clear()
            second = self.source()
            values = await second.safe_fetch()
        self.assertEqual((len(values), second.fetch_status), (2, "completed"))
        self.assertEqual(second.coverage()["unresolved_citations"], 0)
        self.assertEqual(self.calls, [("wp.nyu.edu", "/cilvr/cilvr-group-publications/")])

    async def test_verified_identity_without_completed_detail_cannot_close(self):
        with research_window(NOW), patch.object(config, "OPENALEX_API_KEY", "offline-only"):
            first = self.source(limit=0)
            await first.safe_fetch()
            queue = OfficialDetailQueue(INDEX, 7, first.stage_cache)
            url = "https://arxiv.org/abs/2605.10001"
            old = first.stage_cache.cache_get(TASK_STAGE, queue.task_signature(url))
            first.stage_cache.cache_save(TASK_STAGE, queue.task_signature(url), {
                "link": old["link"], "status": "pending", "attempts": old["attempts"], "contract": old["contract"]})
            self.fail.add("a")
            self.calls.clear()
            second = self.source()
            values = await second.safe_fetch()
        self.assertEqual((len(values), second.fetch_status, second.coverage()["detail_pending"]), (1, "partial", 1))
        self.assertEqual(second.coverage()["unresolved_citations"], 1)
        self.assertEqual(self.calls, [("wp.nyu.edu", "/cilvr/cilvr-group-publications/"), ("arxiv.org", "/abs/2605.10001")])

    async def test_adapter_upgrade_revalidates_each_detail_once_and_can_close(self):
        with research_window(NOW), patch.object(config, "OPENALEX_API_KEY", "offline-only"):
            await self.source(limit=0).safe_fetch()
            self.calls.clear()
            with patch("sources.official_detail_queue.source_record", return_value={"changed_parser": True}):
                first = self.source()
                self.assertEqual(len(await first.safe_fetch()), 1)
                self.assertEqual(first.fetch_status, "partial")
                second = self.source()
                self.assertEqual(len(await second.safe_fetch()), 2)
                self.assertEqual(second.fetch_status, "completed")
        self.assertEqual([path for host, path in self.calls if host == "arxiv.org"], ["/abs/2605.10001", "/abs/2605.10002"])
        self.assertFalse(any(host == "api.openalex.org" for host, _ in self.calls))

    async def test_current_index_omission_does_not_erase_old_identity_or_detail(self):
        with research_window(NOW), patch.object(config, "OPENALEX_API_KEY", "offline-only"):
            await self.source().safe_fetch()
            self.names = ("b",)
            self.calls.clear()
            second = self.source()
            values = await second.safe_fetch()
        self.assertEqual((len(values), second.fetch_status), (2, "completed"))
        self.assertEqual(self.calls, [("wp.nyu.edu", "/cilvr/cilvr-group-publications/"), ("arxiv.org", "/abs/2605.10002")])

    async def test_one_primary_url_cannot_verify_two_different_citation_titles(self):
        self.misdirect_b = True
        with research_window(NOW), patch.object(config, "OPENALEX_API_KEY", "offline-only"):
            source = self.source(limit=0)
            self.assertEqual(await source.safe_fetch(), [])
        self.assertEqual(source.fetch_status, "partial")
        self.assertEqual(source.coverage()["unresolved_citations"], 2)

    async def test_outside_window_receipt_cannot_be_reused_with_inside_date(self):
        self.published = "2026-04-01"
        with research_window(NOW), patch.object(config, "OPENALEX_API_KEY", "offline-only"):
            first = self.source(limit=0)
            await first.safe_fetch()
            rows = first.page_coverage[INDEX]["bibliography_diagnostics"]["resolution_tasks"]
            row = next(row for row in rows if row["title"] == "Adaptive a architecture")
            saved = first.stage_cache.cache_get("bibliography_identity", row["input_signature"])
            saved["primary_published_at"] = "2026-05-06T00:00:00+00:00"
            first.stage_cache.cache_save("bibliography_identity", row["input_signature"], saved)
            self.calls.clear()
            second = self.source()
            self.assertEqual(await second.safe_fetch(), [])
        self.assertEqual(second.fetch_status, "completed")
        self.assertEqual(self.calls, [("wp.nyu.edu", "/cilvr/cilvr-group-publications/"), ("arxiv.org", "/abs/2605.10001")])
