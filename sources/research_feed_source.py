"""可配置的官方研究博客 RSS/Atom 信源。"""

from __future__ import annotations

import asyncio
import logging
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from runtime_clock import research_now
from email.utils import parsedate_to_datetime

import httpx

import config
from paradigms.models import EvidenceType, TechnicalEvidence
from sources.feed_contract import feed_nodes, feed_text, feed_link

logger = logging.getLogger(__name__)


class ResearchFeedSource:
    source_name = "research-blog"

    def __init__(self, lookback_days: int = 7):
        self.lookback_days = max(lookback_days, 1)
        self.completed_feeds = 0
        self.failed_feeds = 0
        self.feed_results: dict[str, dict] = {}

    async def safe_fetch(self) -> list[TechnicalEvidence]:
        if not config.RESEARCH_FEED_URLS:
            self.fetch_status = "not_configured"
            self.fetch_error = ""
            return []
        try:
            results = await self.fetch()
            if self.failed_feeds and not self.completed_feeds:
                self.fetch_status = "query_failed"
                self.fetch_error = "AllFeedsFailed"
            elif self.failed_feeds:
                self.fetch_status = "partial"
                self.fetch_error = "PartialFeedFailure"
            else:
                self.fetch_status = "completed"
                self.fetch_error = ""
            return results
        except Exception as exc:
            self.fetch_status = "query_failed"
            self.fetch_error = type(exc).__name__
            logger.exception("[research-blog] 获取失败")
            return []

    async def fetch(self) -> list[TechnicalEvidence]:
        self.completed_feeds = 0
        self.failed_feeds = 0
        self.feed_results = {}
        async with httpx.AsyncClient(timeout=30, follow_redirects=True) as client:
            responses = await asyncio.gather(
                *(client.get(url) for url in config.RESEARCH_FEED_URLS),
                return_exceptions=True,
            )
        items: list[TechnicalEvidence] = []
        for feed_url, response in zip(config.RESEARCH_FEED_URLS, responses):
            if isinstance(response, Exception):
                self.failed_feeds += 1
                self.feed_results[feed_url] = {"status": "request_failed", "error": type(response).__name__}
                logger.warning("研究 Feed 获取失败 %s: %s", feed_url, response)
                continue
            try:
                response.raise_for_status()
                parsed = self._parse(response.text, feed_url)
                items.extend(parsed)
                self.completed_feeds += 1
                self.feed_results[feed_url] = {"status": "completed", "results": len(parsed)}
            except Exception as exc:
                self.failed_feeds += 1
                self.feed_results[feed_url] = {"status": "failed", "error": type(exc).__name__}
                logger.warning("研究 Feed 解析失败 %s: %s", feed_url, exc)
                continue
        return list({item.fingerprint: item for item in items}.values())

    def coverage(self) -> dict:
        return {"configured_feeds": len(config.RESEARCH_FEED_URLS),
                "completed_feeds": self.completed_feeds, "failed_feeds": self.failed_feeds,
                "feeds": dict(self.feed_results)}

    def _parse(self, xml_text: str, feed_url: str) -> list[TechnicalEvidence]:
        cutoff = research_now() - timedelta(days=self.lookback_days)
        items = []
        nodes = feed_nodes(xml_text)
        for node in nodes:
            title = _text(node, "title")
            link = feed_link(node, feed_url)
            summary = _text(node, "description") or _text(node, "summary") or _text(node, "content")
            published = _text(node, "pubDate") or _text(node, "published")
            modified = _text(node, "updated")
            published_dt = _parse_date(published)
            if published_dt and published_dt < cutoff:
                continue
            author = _text(node, "author") or _text(node, "creator")
            if title and link:
                items.append(
                    TechnicalEvidence(
                        source=self.source_name,
                        evidence_type=EvidenceType.TECHNICAL_BLOG,
                        title=title,
                        url=link,
                        summary=summary,
                        published_at=published_dt.isoformat() if published_dt else "",
                        authors=[author] if author else [],
                        organization=feed_url,
                        raw={"source_published_at": published, "source_modified_at": modified, "date_basis": "published" if published_dt else "unknown"},
                    )
                )
        return items


def _text(node: ET.Element, local_name: str) -> str:
    return feed_text(node, local_name)


def _link(node: ET.Element) -> str:
    return feed_link(node)


def _parse_date(value: str) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        try:
            parsed = parsedate_to_datetime(value)
        except (TypeError, ValueError):
            return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)
