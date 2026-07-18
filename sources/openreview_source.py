"""OpenReview API：发现论文，并把公开评审/回复计数作为扎实度证据。"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone

import httpx

import config
from paradigms.models import EvidenceType, TechnicalEvidence

logger = logging.getLogger(__name__)
OPENREVIEW_NOTES_API = "https://api2.openreview.net/notes"


class OpenReviewSource:
    source_name = "openreview"

    def __init__(self, lookback_days: int = 7, limit: int = 300):
        self.lookback_days = max(lookback_days, 1)
        self.limit = min(max(limit, 1), 1000)

    async def safe_fetch(self) -> list[TechnicalEvidence]:
        if not config.OPENREVIEW_VENUES:
            return []
        try:
            return await self.fetch()
        except Exception:
            logger.exception("[openreview] 获取失败")
            return []

    async def fetch(self) -> list[TechnicalEvidence]:
        async with httpx.AsyncClient(timeout=30) as client:
            tasks = [
                client.get(
                    OPENREVIEW_NOTES_API,
                    params={
                        "content.venueid": venue,
                        "limit": self.limit,
                        "sort": "tmdate:desc",
                        "details": "replyCount",
                    },
                )
                for venue in config.OPENREVIEW_VENUES
            ]
            responses = await asyncio.gather(*tasks, return_exceptions=True)

        cutoff_ms = int(
            (datetime.now(timezone.utc) - timedelta(days=self.lookback_days)).timestamp()
            * 1000
        )
        items: list[TechnicalEvidence] = []
        for response in responses:
            if isinstance(response, Exception):
                logger.warning("OpenReview 单个 venue 获取失败: %s", response)
                continue
            if response.status_code >= 400:
                logger.warning("OpenReview 单个 venue 返回 HTTP %s", response.status_code)
                continue
            response.raise_for_status()
            for note in response.json().get("notes", []):
                modified = int(note.get("tmdate") or note.get("cdate") or 0)
                if modified and modified < cutoff_ms:
                    continue
                content = note.get("content") or {}
                title = _value(content.get("title"))
                abstract = _value(content.get("abstract"))
                if not title or not abstract:
                    continue
                authors = _value(content.get("authors")) or []
                author_ids = _value(content.get("authorids")) or []
                venue = _value(content.get("venueid")) or ""
                details = note.get("details") or {}
                reply_count = int(details.get("replyCount", 0) or 0)
                note_id = note.get("id", "")
                items.append(
                    TechnicalEvidence(
                        source=self.source_name,
                        evidence_type=EvidenceType.PRIMARY_PAPER,
                        title=title,
                        url=f"https://openreview.net/forum?id={note_id}",
                        summary=abstract,
                        published_at=datetime.fromtimestamp(
                            modified / 1000, timezone.utc
                        ).isoformat()
                        if modified
                        else "",
                        authors=list(authors) if isinstance(authors, list) else [],
                        organization=venue,
                        metrics={"review_replies": reply_count},
                        identifiers={"openreview": note_id},
                        raw={"author_openreview_ids": author_ids},
                    )
                )
        return list({item.fingerprint: item for item in items}.values())


def _value(value):
    if isinstance(value, dict) and "value" in value:
        return value["value"]
    return value
