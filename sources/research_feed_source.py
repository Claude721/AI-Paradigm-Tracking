"""可配置的官方研究博客 RSS/Atom 信源。"""

from __future__ import annotations

import asyncio
import logging
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime

import httpx

import config
from paradigms.models import EvidenceType, TechnicalEvidence

logger = logging.getLogger(__name__)


class ResearchFeedSource:
    source_name = "research-blog"

    def __init__(self, lookback_days: int = 7):
        self.lookback_days = max(lookback_days, 1)

    async def safe_fetch(self) -> list[TechnicalEvidence]:
        if not config.RESEARCH_FEED_URLS:
            return []
        try:
            return await self.fetch()
        except Exception:
            logger.exception("[research-blog] 获取失败")
            return []

    async def fetch(self) -> list[TechnicalEvidence]:
        async with httpx.AsyncClient(timeout=30, follow_redirects=True) as client:
            responses = await asyncio.gather(
                *(client.get(url) for url in config.RESEARCH_FEED_URLS),
                return_exceptions=True,
            )
        items: list[TechnicalEvidence] = []
        for feed_url, response in zip(config.RESEARCH_FEED_URLS, responses):
            if isinstance(response, Exception):
                logger.warning("研究 Feed 获取失败 %s: %s", feed_url, response)
                continue
            try:
                response.raise_for_status()
                items.extend(self._parse(response.text, feed_url))
            except Exception as exc:
                logger.warning("研究 Feed 解析失败 %s: %s", feed_url, exc)
                continue
        return list({item.fingerprint: item for item in items}.values())

    def _parse(self, xml_text: str, feed_url: str) -> list[TechnicalEvidence]:
        root = ET.fromstring(xml_text)
        cutoff = datetime.now(timezone.utc) - timedelta(days=self.lookback_days)
        items = []
        nodes = root.findall(".//item") or root.findall("{http://www.w3.org/2005/Atom}entry")
        for node in nodes:
            title = _text(node, "title")
            link = _link(node)
            summary = _text(node, "description") or _text(node, "summary") or _text(node, "content")
            published = _text(node, "pubDate") or _text(node, "published") or _text(node, "updated")
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
                        published_at=published_dt.isoformat() if published_dt else published,
                        authors=[author] if author else [],
                        organization=feed_url,
                    )
                )
        return items


def _text(node: ET.Element, local_name: str) -> str:
    for child in node.iter():
        if child.tag.rsplit("}", 1)[-1] == local_name and child.text:
            return child.text.strip()
    return ""


def _link(node: ET.Element) -> str:
    for child in node.iter():
        if child.tag.rsplit("}", 1)[-1] != "link":
            continue
        if child.text and child.text.strip():
            return child.text.strip()
        if child.get("href"):
            return child.get("href", "")
    return ""


def _parse_date(value: str) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)
    except ValueError:
        try:
            return parsedate_to_datetime(value).astimezone(timezone.utc)
        except (TypeError, ValueError):
            return None
