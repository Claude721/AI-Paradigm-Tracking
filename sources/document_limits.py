"""Shared decompressed-size policy for official indexes and research PDFs."""
from __future__ import annotations

import httpx

TEXT_BYTE_LIMIT = 10_000_000
PDF_BYTE_LIMIT = 40_000_000


class DocumentSizeLimit(ValueError):
    pass


def response_byte_limit(prefix: bytes) -> int:
    # A .pdf path or MIME header alone is NOT proof; a disguised HTML response
    # retains the text cap and is still rejected by the production PDF parser.
    return PDF_BYTE_LIMIT if prefix[:1024].lstrip().startswith(b"%PDF-") else TEXT_BYTE_LIMIT


async def bounded_document_get(client, url: str):
    async with client.stream("GET", url) as response:
        chunks, prefix, length = [], b"", 0
        async for chunk in response.aiter_bytes():
            prefix = (prefix + chunk[:1024])[:1024]
            length += len(chunk)
            if length > response_byte_limit(prefix):
                raise DocumentSizeLimit("Official document exceeds decompressed byte limit")
            chunks.append(chunk)
        headers = dict(response.headers)
        headers.pop("content-encoding", None)
        headers.pop("content-length", None)
        return httpx.Response(response.status_code, content=b"".join(chunks),
                              headers=headers, request=response.request)
