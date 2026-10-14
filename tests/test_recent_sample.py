"""Injected clock, mocked HTTP/LLM only: recent sample and publication semantics."""
import json
import tempfile
import unittest
from datetime import datetime, timezone, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx

from runtime_clock import research_window
from sources.hf_papers_source import HuggingFacePapersSource
from paradigms.discovery import _raw_to_origin, _hf_to_support
from scripts.one_route_check import _official_origin, _require_recent_primary, _write_sample_review

NOW = datetime(2026, 10, 14, 2, tzinfo=timezone.utc)
ARTICLE = "https://www.anthropic.com/research/offline-sample"
INDEX = "https://www.anthropic.com/research"


class PublicationDateTests(unittest.TestCase):
    def parse(self, paper_date, *, featured="2026-10-13T12:00:00Z"):
        paper = {"id": "2610.00001", "title": "Offline mechanism", "summary": "Facts", "authors": []}
        if paper_date is not None:
            paper["publishedAt"] = paper_date
        with research_window(NOW, lookback_days=3):
            return HuggingFacePapersSource(lookback_days=3)._parse([{"paper": paper, "publishedAt": featured}])

    def test_recent_feature_does_not_make_old_paper_recent(self):
        self.assertEqual(self.parse("2026-10-09T12:00:00Z"), [])

    def test_primary_and_platform_dates_stay_separate(self):
        rows = self.parse("2026-10-12T12:00:00Z")
        self.assertEqual(_raw_to_origin(rows[0]).published_at, "2026-10-12T12:00:00Z")
        self.assertEqual(_hf_to_support(rows[0]).published_at, "2026-10-13T12:00:00Z")
        self.assertEqual(rows[0].extra["date_basis"], "paper_published")

    def test_missing_or_bad_primary_date_is_not_replaced_by_feature(self):
        for date in (None, "", "not-a-date"):
            with self.subTest(date=date):
                rows = self.parse(date)
                self.assertEqual(rows[0].created_at, "")
                self.assertEqual(rows[0].extra["date_basis"], "unknown")
                with self.assertRaises(ValueError):
                    _require_recent_primary(_raw_to_origin(rows[0]), NOW, 3)

    def test_future_primary_is_not_current_research(self):
        self.assertEqual(self.parse("2026-10-15T00:00:00Z"), [])

    def test_window_is_exact_and_uses_injected_time(self):
        edge = NOW - timedelta(days=3)
        _require_recent_primary(SimpleNamespace(published_at=edge.isoformat()), NOW, 3)
        for date in (edge - timedelta(seconds=1), NOW + timedelta(seconds=1)):
            with self.assertRaises(ValueError):
                _require_recent_primary(SimpleNamespace(published_at=date.isoformat()), NOW, 3)


class OfficialSampleTests(unittest.IsolatedAsyncioTestCase):
    async def probe(self, *, article_date="2026-10-12T09:30:00Z", listed=True, modified=None):
        paths = []
        index = f'<html><a href="{ARTICLE}">A new mechanism</a></html>' if listed else '<html><a href="/research/other">Other</a></html>'
        dates = f'<meta property="article:published_time" content="{article_date}">' if article_date else ""
        if modified:
            dates += f'<meta property="article:modified_time" content="{modified}">'
        article = '<html><head><title>A new mechanism</title>' + dates + '</head><body><article>' + ("Technical source facts. " * 40) + '</article></body></html>'
        async def get(client, url):
            paths.append(url)
            return httpx.Response(200, text=index if url == INDEX else article, request=httpx.Request("GET", url))
        with patch("sources.document_limits.bounded_document_get", new=AsyncMock(side_effect=get)):
            with research_window(NOW, lookback_days=3):
                origin = await _official_origin(ARTICLE, NOW, 3)
        return origin, paths

    async def test_exact_registered_link_and_production_parser_before_model(self):
        origin, paths = await self.probe()
        self.assertEqual(paths, [INDEX, ARTICLE])
        self.assertEqual(origin.url, ARTICLE)
        self.assertEqual(origin.organization, "Anthropic")
        self.assertTrue(origin.raw["document_excerpt"])
        self.assertEqual(origin.published_at, "2026-10-12T09:30:00+00:00")

    async def test_unlisted_old_or_modified_only_material_not_paid(self):
        for kwargs in ({"listed": False}, {"article_date": "2026-10-09T09:30:00Z"},
                       {"article_date": "", "modified": "2026-10-13T12:00:00Z"}):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                await self.probe(**kwargs)

    async def test_arbitrary_or_credential_url_never_accessed(self):
        with patch("sources.document_limits.bounded_document_get", new=AsyncMock()) as get:
            for url in ("https://unregistered.example/research/paper", "http://www.anthropic.com/research/a",
                        "https://user:private@www.anthropic.com/research/a", ARTICLE + "?Signature=x",
                        "https://www.anthropic.com/research/a#x", "https://127.0.0.1/research/a"):
                with self.subTest(url=url), self.assertRaises(ValueError):
                    await _official_origin(url, NOW, 3)
            get.assert_not_awaited()


class SampleReviewTests(unittest.TestCase):
    def result(self):
        return {"status": "screening_closed", "lookback_days": 3,
                "primary": {"title": "原点", "url": ARTICLE, "published_at": "2026-10-12"},
                "screening_decision": "reject", "screening_reason": "只有局部优化，未改变学习信号。"}

    def test_completed_reject_has_clear_scope_not_weekly_empty_claim(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary)
            _write_sample_review(self.result(), path)
            text = (path / "sample_review.md").read_text()
            self.assertIn("不符合路线准入", text)
            self.assertIn("只有局部优化", text)
            self.assertIn(ARTICLE, text)
            self.assertIn("不推断整个窗口没有重要技术", text)
            for forbidden in ("研究未完成", "阶段性", "待续研"):
                self.assertNotIn(forbidden, text)

    def test_internal_score_is_not_published(self):
        result = self.result()
        result["screening_reason"] = "Rubric 确定性得分 42.1，未达到本阶段阈值 70.0；主要未闭合问题：是否有跨任务验证"
        with tempfile.TemporaryDirectory() as temporary:
            _write_sample_review(result, Path(temporary))
            text = (Path(temporary) / "sample_review.md").read_text()
        self.assertIn("是否有跨任务验证", text)
        for private in ("42.1", "70.0", "Rubric", "得分"):
            self.assertNotIn(private, text)

    def test_execution_failure_never_written_as_completed_conclusion(self):
        result = self.result()
        result["status"] = "failed"
        with tempfile.TemporaryDirectory() as temporary:
            with self.assertRaises(ValueError):
                _write_sample_review(result, Path(temporary))
            self.assertEqual(list(Path(temporary).iterdir()), [])


if __name__ == "__main__":
    unittest.main()
