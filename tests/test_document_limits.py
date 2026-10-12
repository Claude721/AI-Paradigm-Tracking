"""Streaming/decompression limits shared by production and acceptance."""
import gzip
import unittest
from datetime import datetime, timezone
from unittest.mock import patch

import httpx

from sources.document_limits import bounded_document_get, DocumentSizeLimit, response_byte_limit
from source_audit import SourceAudit, Entry, diagnose_entry


class Stream(httpx.AsyncByteStream):
    def __init__(self, chunks):
        self.chunks, self.closed, self.read = chunks, False, 0

    async def __aiter__(self):
        for chunk in self.chunks:
            self.read += 1
            yield chunk

    async def aclose(self):
        self.closed = True


class DocumentLimitTests(unittest.IsolatedAsyncioTestCase):
    def test_only_real_pdf_signature_grants_larger_allowance(self):
        self.assertEqual(response_byte_limit(b"%PDF-1.7"), 40_000_000)
        self.assertEqual(response_byte_limit(b" \n%PDF-1.7"), 40_000_000)
        self.assertEqual(response_byte_limit(b"<html>application/pdf</html>"), 10_000_000)

    async def get(self, body, *, content_type="application/pdf", encoding=""):
        def handler(request):
            headers = {"Content-Type": content_type}
            if encoding:
                headers["Content-Encoding"] = encoding
            return httpx.Response(200, content=body, headers=headers, request=request)
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return await bounded_document_get(client, "https://wayve.ai/report.pdf")

    async def test_pdf_above_text_cap_is_complete_and_not_truncated(self):
        body = b"%PDF-1.7\n" + b"x" * 150
        with patch("sources.document_limits.TEXT_BYTE_LIMIT", 100), patch("sources.document_limits.PDF_BYTE_LIMIT", 200):
            value = await self.get(body)
        self.assertEqual(value.content, body)

    async def test_html_disguised_as_pdf_and_oversized_pdf_remain_failures(self):
        with patch("sources.document_limits.TEXT_BYTE_LIMIT", 100), patch("sources.document_limits.PDF_BYTE_LIMIT", 200):
            for body in (b"<html>" + b"x" * 150, b"%PDF-1.7\n" + b"x" * 201):
                with self.assertRaises(DocumentSizeLimit):
                    await self.get(body)

    async def test_gzip_cap_applies_to_decompressed_bytes(self):
        body = b"<html>" + b"x" * 150
        with patch("sources.document_limits.TEXT_BYTE_LIMIT", 100), self.assertRaises(DocumentSizeLimit):
            await self.get(gzip.compress(body), content_type="text/html", encoding="gzip")

    async def test_split_signature_and_stream_closes_on_cap(self):
        stream = Stream([b"%PD", b"F-1.7\n", b"x" * 180, b"x" * 30, b"unread"])
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda request:
            httpx.Response(200, stream=stream, request=request))) as client:
            with patch("sources.document_limits.TEXT_BYTE_LIMIT", 100), patch("sources.document_limits.PDF_BYTE_LIMIT", 200), self.assertRaises(DocumentSizeLimit):
                await bounded_document_get(client, "https://wayve.ai/report.pdf")
        self.assertEqual(stream.read, 4)
        self.assertTrue(stream.closed)

    async def test_audit_uses_same_cap_and_preserves_valid_pdf_sample(self):
        def handler(request):
            if request.url.path == "/research":
                return httpx.Response(200, text='<a href="/report.pdf">Technical Report</a>', request=request)
            return httpx.Response(200, content=b"%PDF-1.7\n" + b"x" * 150, request=request)
        with patch("sources.document_limits.TEXT_BYTE_LIMIT", 100), patch("sources.document_limits.PDF_BYTE_LIMIT", 200), \
             patch("sources.priority_research_source._extract_pdf_document", return_value=("Technical Report", "Training signal and model interfaces. " * 20, [])):
            result = await SourceAudit(transport=httpx.MockTransport(handler)).run(
                [Entry("pdf", "official", "https://wayve.ai/research")],
                reference_time=datetime(2026,10,12,tzinfo=timezone.utc))
        self.assertEqual(result["counts"], {"passed":1})
        self.assertEqual(result["request_count"], 2)
        self.assertEqual(result["entries"][0]["coverage"]["detail_parsed"], 1)
        self.assertFalse(result["campaign_coverage_complete"])

    def test_size_limit_has_own_diagnosis_not_key_or_pdf_parse_error(self):
        value = diagnose_entry({"status":"failed", "reason":"response_size_limit",
                               "coverage":{"detail_failures":1}, "http":[{"status":200}]})
        self.assertEqual(value["category"], "decompressed_document_size_limit")
