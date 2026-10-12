"""Finite Qwen public catalogs, canonical URLs and publication identity."""

import copy
import unittest
from datetime import timedelta
from unittest.mock import patch

import httpx

from sources.priority_research_source import PriorityResearchPageSource
from sources.qwen_catalog import fetch_catalog
from runtime_clock import research_window
from tests.test_source_recovery import NOW

QWEN = "https://qwenlm.github.io/publication/"
ASYNC_CLIENT = httpx.AsyncClient


def row(identity="one", *, date=None, url="https://qwenlm.github.io/blog/one/"):
    date = date or (NOW - timedelta(days=1)).isoformat()
    return {"id": identity, "path": identity, "type": "qwen_ai", "language": "en-US",
        "title": "Adaptive memory", "extra": {"date": date},
        "content": f'<html><head><title>Adaptive memory | Qwen</title><meta property="article:published_time" content="{date}"><link rel="canonical" href="{url}"></head><body><article>' + "真实对象训练信号和推理信息流。" * 35 + '</article></body></html>'}


class QwenCatalogTests(unittest.IsolatedAsyncioTestCase):
    async def catalog(self, articles=None, historical=None, *, data_extra=None):
        data = {"articles": articles or [], **(data_extra or {})}
        async def get(url):
            return httpx.Response(200, json=historical or [] if "page_config" in url else {"success": True, "data": data}, request=httpx.Request("GET", url))
        return await fetch_catalog(get, cutoff=NOW - timedelta(days=30), reference_time=NOW)

    async def test_registered_frontend_url_uses_record_path_not_title_or_dead_canonical(self):
        links, diagnostics = await self.catalog([row(identity="unrelated-id", url="https://qwenlm.github.io/blog/actual-work/")])
        self.assertTrue(diagnostics["complete"])
        self.assertEqual(links[0]["url"], "https://qwen.ai/blog?id=unrelated-id")
        self.assertIn("path=unrelated-id", links[0]["fetch_url"])

    async def test_missing_or_temporary_historical_canonical_never_enters_report_target(self):
        for url in ("https://qwenlm.github.io/blog/one/?Signature=temporary", "https://unverified.example/blog/one/", ""):
            links, diagnostics = await self.catalog([row(url=url)])
            self.assertEqual(links[0]["url"], "https://qwen.ai/blog?id=one")
            self.assertTrue(diagnostics["complete"])
            self.assertNotIn("Signature", str(links))

    async def test_unknown_or_modified_only_date_cannot_replace_article_date(self):
        for mutation in (lambda item: item["extra"].update(date="2026"),
                         lambda item: item["extra"].update(date=""),
                         lambda item: item["extra"].update(date=None, dateModified=NOW.isoformat())):
            item = row()
            mutation(item)
            links, diagnostics = await self.catalog([item])
            self.assertFalse(diagnostics["complete"])
            self.assertEqual(links, [])

    async def test_current_api_date_preserved_with_embedded_hugo_conflict_accounted(self):
        item = row()
        item["content"] = item["content"].replace(item["extra"]["date"], (NOW - timedelta(days=90)).isoformat())
        links, diagnostics = await self.catalog([item])
        self.assertTrue(diagnostics["complete"])
        self.assertEqual(links[0]["published_at"], item["extra"]["date"])
        self.assertEqual(diagnostics["embedded_date_conflicts"], 1)

    async def test_both_finite_empty_and_verified_old_catalog_are_valid_zero(self):
        for items in ([], [row(date=(NOW - timedelta(days=80)).isoformat())]):
            links, diagnostics = await self.catalog(items)
            self.assertEqual(links, [])
            self.assertTrue(diagnostics["complete"])

    async def test_duplicate_identity_and_healthy_peers_are_separated(self):
        links, diagnostics = await self.catalog([row("one"), row("one"), row("two", url="https://qwenlm.github.io/blog/two/")])
        self.assertEqual(len(links), 2)
        self.assertFalse(diagnostics["complete"])

    async def test_historical_current_record_without_article_cannot_use_tokenlinks_as_landing(self):
        old = {"id": "unknown", "date": (NOW - timedelta(days=1)).isoformat(), "tokenLinks": "https://docs.qwenlm.ai/research/unknown/index.json"}
        links, diagnostics = await self.catalog([], [old])
        self.assertEqual(links, [])
        self.assertEqual(diagnostics["pending_records"][0]["reason"], "current_research_has_no_verified_article")

    async def test_future_server_pagination_and_foreign_origins_fail_closed(self):
        for extra in ({"has_more": True}, {"next_cursor": "next"}, {"count": 2}):
            with self.assertRaises(ValueError):
                await self.catalog([row()], data_extra=extra)
        async def get(url):
            return httpx.Response(200, json=[], request=httpx.Request("GET", "https://unverified.example/catalog"))
        with self.assertRaises(ValueError):
            await fetch_catalog(get, cutoff=NOW - timedelta(days=30), reference_time=NOW)

    async def test_production_adapter_uses_real_detail_parser_and_keeps_manifest_identity(self):
        item = row()
        calls = []
        def handler(request):
            calls.append(str(request.url))
            if request.url.path == "/research":
                return httpx.Response(200, text='<html><script src="/public.js"></script></html>')
            if request.url.path == "/api/page_config":
                return httpx.Response(200, json=[])
            if request.url.path == "/api/v2/article/retrieval":
                return httpx.Response(200, json={"success": True, "data": {"articles": [item]}})
            if request.url.path == "/api/v2/article/":
                return httpx.Response(200, json={"success": True, "data": item})
            return httpx.Response(200, text=item["content"])
        source = PriorityResearchPageSource(pages=[QWEN], client_factory=lambda **kwargs: ASYNC_CLIENT(transport=httpx.MockTransport(handler), **kwargs))
        with research_window(NOW):
            evidence = await source.safe_fetch()
        self.assertEqual(len(evidence), 1)
        self.assertEqual(source.fetch_status, "completed")
        self.assertEqual(evidence[0].raw["research_index_url"], QWEN)
        self.assertEqual(evidence[0].url, "https://qwen.ai/blog?id=one")
        self.assertEqual(source.page_coverage[QWEN]["catalog_diagnostics"]["complete"], True)
        self.assertEqual(len(calls), 5)

    async def test_different_detail_identity_or_date_does_not_close(self):
        from sources.qwen_catalog import decode_article
        from sources.priority_research_source import _ResearchLink
        link = _ResearchLink(**(await self.catalog([row()]))[0][0])
        for changed in ({"path": "other"}, {"extra": {"date": (NOW - timedelta(days=3)).isoformat()}}):
            item = row()
            item.update(changed)
            response = httpx.Response(200, json={"success": True, "data": item}, request=httpx.Request("GET", link.fetch_url))
            with self.assertRaises(ValueError):
                decode_article(response, link)
