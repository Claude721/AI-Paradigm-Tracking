"""Offline cases for official article identity and linked document fetching."""

from __future__ import annotations

import asyncio
import unittest
from datetime import datetime, timezone
from unittest.mock import patch

import httpx

from paradigms.models import EvidenceType, TechnicalEvidence
from runtime_clock import research_window
from sources.arxiv_document_source import ArxivDocumentClient
from sources.priority_research_source import (
    PriorityResearchPageSource,
    _ArticleParser,
    _is_pdf_response,
)


class SourceDocumentBoundaryTests(unittest.TestCase):
    def test_article_parser_ignores_navigation_related_links_and_dates(self):
        parser = _ArticleParser("https://lab.example/news/cedar")
        parser.feed("""
            <nav><a href="/report/unrelated.pdf">Full Report</a></nav>
            <div class="related-posts"><time datetime="2030-01-01"></time>
              <div><a href="/report/recommended.pdf">Technical Report</a></div>
            </div>
            <main><article><h1>Cedar</h1><time datetime="2026-09-20"></time>
              <p>Current mechanism description.</p>
              <a href="/files/cedar.pdf">Read the full report</a>
            </article></main>
            <footer><a href="/report/other.pdf">Other report</a></footer>
        """)
        self.assertEqual(parser.published_at, "2026-09-20")
        self.assertEqual(parser.links, [
            ("Read the full report", "https://lab.example/files/cedar.pdf")
        ])
        self.assertNotIn("Technical Report", parser.text)

    def test_navigation_document_does_not_promote_current_article(self):
        index_url = "https://lab.example/research"

        async def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/research":
                return httpx.Response(
                    200, text='<a href="/research/cedar">Cedar update</a>',
                    request=request,
                )
            return httpx.Response(200, text=(
                '<meta property="article:published_time" content="2026-09-20">'
                '<nav><a href="/reports/unrelated.pdf">Full Technical Report</a></nav>'
                '<main><article><h1>Cedar update</h1><p>'
                + "We improve a small interface. " * 12
                + '</p></article></main>'
            ), request=request)

        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        source = PriorityResearchPageSource(pages=[index_url], lookback_days=7)
        with (
            research_window(datetime(2026, 9, 23, tzinfo=timezone.utc)),
            patch("sources.priority_research_source.httpx.AsyncClient", return_value=client),
        ):
            received = asyncio.run(source.fetch())
        self.assertEqual(len(received), 1)
        self.assertNotEqual(received[0].raw["origin_kind"], "technical_report")
        self.assertEqual(received[0].raw["linked_research_documents"], [])

    def test_pdf_extension_and_header_cannot_override_html_body(self):
        request = httpx.Request("GET", "https://lab.example/report.pdf")
        fake_pdf = httpx.Response(
            200, content=b"<html><body>Login required</body></html>",
            headers={"content-type": "application/pdf"}, request=request,
        )
        real_pdf = httpx.Response(200, content=b"%PDF-1.7\n", request=request)
        self.assertFalse(_is_pdf_response(fake_pdf, str(request.url)))
        self.assertTrue(_is_pdf_response(real_pdf, str(request.url)))

    def test_official_pdf_detail_with_html_payload_is_recorded_as_failure(self):
        index_url = "https://lab.example/research"

        async def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/research":
                return httpx.Response(
                    200, text='<a href="/reports/cedar.pdf">Full Report</a>',
                    request=request,
                )
            return httpx.Response(
                200, text="<html><body>Sign in to continue</body></html>",
                headers={"content-type": "application/pdf"}, request=request,
            )

        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        source = PriorityResearchPageSource(pages=[index_url], lookback_days=7)
        with (
            research_window(datetime(2026, 9, 23, tzinfo=timezone.utc)),
            patch("sources.priority_research_source.httpx.AsyncClient", return_value=client),
        ):
            received = asyncio.run(source.fetch())
        self.assertEqual(received, [])
        self.assertEqual(source.coverage()["detail_failures"], 1)

    def test_hf_blob_uses_resolve_and_persists_stable_url(self):
        stable = "https://huggingface.co/lab/report/blob/main/full.pdf"
        seen = []

        async def handler(request: httpx.Request) -> httpx.Response:
            seen.append(str(request.url))
            return httpx.Response(200, content=b"%PDF-1.7\n", request=request)

        evidence = TechnicalEvidence(
            source="priority-research-page", evidence_type=EvidenceType.TECHNICAL_BLOG,
            title="Cedar", url="https://lab.example/cedar",
            raw={"linked_research_documents": [{"title": "Full Report", "url": stable}]},
        )
        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        with (
            patch("sources.arxiv_document_source.httpx.AsyncClient", return_value=client),
            patch("sources.arxiv_document_source.parse_arxiv_pdf", return_value="Report text"),
        ):
            asyncio.run(ArxivDocumentClient().hydrate(evidence))
        self.assertEqual(seen, [stable.replace("/blob/", "/resolve/")])
        self.assertEqual(evidence.raw["document_source_url"], stable)
        self.assertEqual(evidence.raw["document_source_kind"], "official_linked_pdf")

    def test_fake_linked_pdf_is_left_pending(self):
        stable = "https://lab.example/cedar.pdf"

        async def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200, content=b"<html>Sign in</html>",
                headers={"content-type": "application/pdf"}, request=request,
            )

        evidence = TechnicalEvidence(
            source="priority-research-page", evidence_type=EvidenceType.TECHNICAL_BLOG,
            title="Cedar", url="https://lab.example/cedar",
            raw={"linked_research_documents": [{"title": "Full Report", "url": stable}]},
        )
        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        with patch("sources.arxiv_document_source.httpx.AsyncClient", return_value=client):
            coverage = asyncio.run(ArxivDocumentClient().hydrate(evidence))
        self.assertNotIn("document_excerpt", evidence.raw)
        self.assertIn("全部读取失败", coverage["primary_document"])


if __name__ == "__main__":
    unittest.main()
