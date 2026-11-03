"""Read a bounded subset of existing web leads, without another search credit.

Reading a page proves neither independence nor substantive uptake. Synthesis
still has to select its visible index, and the normal actor/route checks apply.
Social platforms remain index-only; no login, browser automation or bypass.
"""
from __future__ import annotations

import asyncio
import copy
import hashlib
import ipaddress
import socket
from urllib.parse import urljoin, urlsplit
from types import SimpleNamespace

import httpx

from paradigms.models import ORIGIN_EVIDENCE_TYPES, _canonical_material_url, _evidence_datetime
from paradigms.evidence_association import countable_evidence, support_revision
from paradigms.publication import nonresearch_page_reason
from sources.document_limits import bounded_document_get
from sources.priority_research_source import _ArticleParser
from sources.arxiv_document_source import _distributed_text_excerpt
from sources.social_web_search_source import work_reference_reason
from runtime_clock import research_now


async def _public_host(url):
    """Validate every redirect before I/O; credentials and local nets forbidden."""
    parsed = urlsplit(url)
    host = (parsed.hostname or "").casefold()
    social = {"x.com", "twitter.com", "reddit.com", "xiaohongshu.com"}
    if (parsed.scheme != "https" or parsed.port not in {None, 443}
            or not _canonical_material_url(url)
            or any(host == domain or host.endswith("." + domain) for domain in social)):
        raise ValueError("Unsafe secondary URL")
    addresses = await asyncio.to_thread(socket.getaddrinfo, parsed.hostname, 443, type=socket.SOCK_STREAM)
    if not addresses or any(not ipaddress.ip_address(row[4][0]).is_global for row in addresses):
        raise ValueError("Secondary host is not public")


class SecondaryPageReader:
    def __init__(self, *, client_factory=None):
        self.client_factory = client_factory or httpx.AsyncClient
        self._cache = {}
        self._locks = {}

    async def read(self, candidate):
        targets = [item for item in candidate.evidence if item.source == "tavily-web"
                   and item.raw.get("indexed_discovery_only")]
        attempted, checked = 0, 0
        # The whole optional phase is bounded, not multiplied by backlog size.
        try:
            async with asyncio.timeout(20):
                for item in targets[:2]:
                    attempted += 1
                    try:
                        page = await self._page(item.url)
                        date = _evidence_datetime(page["published_at"])
                        if date and date > research_now():
                            item.raw["secondary_read_status"] = "future_date"
                            continue
                        read_text = page["title"] + " " + page["text"]
                        matched = {origin.fingerprint: support_revision(origin)
                            for origin in candidate.evidence
                            if origin.evidence_type in ORIGIN_EVIDENCE_TYPES
                            and countable_evidence(candidate, origin)
                            and work_reference_reason(SimpleNamespace(evidence=[origin]), read_text)}
                        if not matched:
                            item.raw["secondary_read_status"] = "unrelated_body"
                            continue  # The index's title must not lend false identity.
                        original_hosts = {urlsplit(origin.url).hostname for origin in candidate.evidence
                                          if origin.evidence_type in ORIGIN_EVIDENCE_TYPES}
                        if urlsplit(page["url"]).hostname in original_hosts:
                            item.raw["secondary_read_status"] = "same_publisher"
                            continue  # Same-publisher commentary is not independent.
                        item.raw["index_summary"] = item.summary
                        item.raw["index_title"] = item.title
                        item.summary = page["text"]
                        item.title = page["title"] or item.title
                        item.authors = list(page["authors"])
                        # Unknown page dates stay unknown, never borrow a search
                        # engine crawl/modified date to awaken an old route.
                        item.published_at = page["published_at"]
                        item.source = "read-web-article"
                        item.raw.update(secondary_read_status="body_read_not_yet_audited", ephemeral_content=True,
                            indexed_discovery_only=False, independence="unverified",
                            relationship="read_secondary_candidate", document_source_url=page["url"],
                            secondary_read_receipt={"version": 1, "url": item.url,
                                "route_key": candidate.key, "origins": matched,
                                "body_sha256": hashlib.sha256(item.summary.encode()).hexdigest()})
                        checked += 1
                    except Exception as exc:
                        # Retain the index object on access/parse/identity failure.
                        # No false technical negative and no raw body in the log.
                        item.raw["secondary_read_status"] = "failed_" + type(exc).__name__
                        continue
        except TimeoutError:
            for item in targets[:attempted]:
                item.raw.setdefault("secondary_read_status", "time_budget_interrupted")
        if targets:
            candidate.community_coverage["secondary_pages"] = (
                f"已有网页索引 {len(targets)} 条；有界读取 {attempted} 条，取得同工作具名正文 {checked} 条；"
                "其余仍为线索，读取成功不自动代表独立承接")

    async def _page(self, url):
        lock = self._locks.setdefault(url, asyncio.Lock())
        async with lock:
            if url in self._cache:
                page = self._cache[url]
                if page is None:
                    raise ValueError("Secondary page previously unavailable")
                return copy.deepcopy(page)
            self._cache[url] = None
            current = url
            async with self.client_factory(timeout=8, follow_redirects=False,
                                           headers={"User-Agent": "AI-Paradigm-Radar/4.0"}) as client:
                for _ in range(3):
                    await _public_host(current)
                    response = await bounded_document_get(client, current)
                    if response.is_redirect:
                        current = urljoin(current, response.headers.get("location", ""))
                        continue
                    response.raise_for_status()
                    if ("html" not in response.headers.get("content-type", "").casefold()
                            or nonresearch_page_reason(current, response.text)
                            or response.headers.get("cf-mitigated", "").casefold() == "challenge"
                            or response.headers.get("x-vercel-mitigated", "").casefold() == "challenge"):
                        raise ValueError("Secondary response is not a readable article")
                    parser = _ArticleParser(current)
                    parser.feed(response.text)
                    text = _distributed_text_excerpt(parser.text, limit=6000)
                    if len(text) < 200 or not parser.authors:
                        raise ValueError("Secondary article body/byline missing")
                    page = {"url": current, "title": parser.title, "text": text,
                            "authors": parser.authors, "published_at": parser.published_at}
                    self._cache[url] = page
                    return copy.deepcopy(page)
            raise ValueError("Secondary redirect limit")
