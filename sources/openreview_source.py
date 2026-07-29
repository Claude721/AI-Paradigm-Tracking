"""OpenReview API：发现论文，并把公开评审/回复计数作为扎实度证据。"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone

import httpx

import config
from paradigms.models import EvidenceType, TechnicalEvidence

logger = logging.getLogger(__name__)
OPENREVIEW_SEARCH_API = "https://api2.openreview.net/notes/search"
DEFAULT_SEARCHES = [
    "world model",
    "reasoning model",
    "vision language action",
    "agent memory planning",
    "self supervised video",
    "test time learning",
]


class OpenReviewSource:
    source_name = "openreview"

    def __init__(
        self,
        lookback_days: int = 7,
        limit: int = 50,
        venues: list[str] | None = None,
        searches: list[str] | None = None,
        concurrency: int = 2,
    ):
        self.lookback_days = max(lookback_days, 1)
        self.limit = min(max(limit, 1), 100)
        self.venues = config.OPENREVIEW_VENUES if venues is None else venues
        self.searches = searches or DEFAULT_SEARCHES
        self.concurrency = max(concurrency, 1)
        self.request_count = 0
        self.rate_limited_requests = 0
        self.failed_queries = 0
        self.completed_queries = 0
        self.result_count = 0

    async def safe_fetch(self) -> list[TechnicalEvidence]:
        if not self.venues:
            return []
        try:
            return await self.fetch()
        except Exception:
            logger.exception("[openreview] 获取失败")
            return []

    async def fetch(self) -> list[TechnicalEvidence]:
        semaphore = asyncio.Semaphore(self.concurrency)
        cutoff_ms = int(
            (datetime.now(timezone.utc) - timedelta(days=self.lookback_days)).timestamp()
            * 1000
        )

        async def search_one(client, venue: str, query: str):
            async with semaphore:
                offset = 0
                notes: list[dict] = []
                while True:
                    params = {
                        # /notes 目前可能要求浏览器 Challenge；官方 search
                        # 端点仍允许公开检索，并支持 venueid 过滤。
                        "query": query,
                        "venueid": venue,
                        # limit 是 API 传输页大小，不是候选上限。
                        "limit": self.limit,
                        "offset": offset,
                        "sort": "tmdate:desc",
                        "details": "replyCount",
                    }
                    response = await self._get_with_backoff(client, params)
                    page = response.json().get("notes", [])
                    notes.extend(page)
                    dates = [
                        int(note.get("tmdate") or note.get("cdate") or 0)
                        for note in page
                    ]
                    if (
                        len(page) < self.limit
                        or not page
                        or any(value and value < cutoff_ms for value in dates)
                    ):
                        break
                    offset += self.limit
                return notes

        async with httpx.AsyncClient(
            timeout=30,
            follow_redirects=True,
            headers={
                "Accept": "application/json",
                "User-Agent": "AI-Paradigm-Radar/3.2",
            },
        ) as client:
            tasks = [
                search_one(client, venue, query)
                for venue in self.venues
                for query in self.searches
            ]
            responses = await asyncio.gather(*tasks, return_exceptions=True)

        items: list[TechnicalEvidence] = []
        for notes in responses:
            if isinstance(notes, Exception):
                self.failed_queries += 1
                logger.warning("OpenReview 单个 venue 获取失败: %s", notes)
                continue
            self.completed_queries += 1
            for note in notes:
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
        deduped = list({item.fingerprint: item for item in items}.values())
        self.result_count = len(deduped)
        return deduped

    async def _get_with_backoff(
        self,
        client: httpx.AsyncClient,
        params: dict,
    ) -> httpx.Response:
        last_response: httpx.Response | None = None
        for attempt in range(3):
            self.request_count += 1
            response = await client.get(OPENREVIEW_SEARCH_API, params=params)
            last_response = response
            if response.status_code != 429:
                response.raise_for_status()
                return response
            self.rate_limited_requests += 1
            if attempt < 2:
                retry_after = response.headers.get("Retry-After", "")
                try:
                    delay = float(retry_after)
                except (TypeError, ValueError):
                    delay = float(2**attempt)
                await asyncio.sleep(max(0.25, min(delay, 5.0)))
        assert last_response is not None
        last_response.raise_for_status()
        return last_response

    def coverage(self) -> dict[str, int | str]:
        return {
            "status": (
                "not_executed"
                if not self.completed_queries and not self.failed_queries
                else "query_failed"
                if self.failed_queries and not self.completed_queries
                else "partial"
                if self.failed_queries
                else "completed_after_retry"
                if self.rate_limited_requests
                else "completed"
            ),
            "queries": len(self.venues) * len(self.searches),
            "completed_queries": self.completed_queries,
            "failed_queries": self.failed_queries,
            "requests": self.request_count,
            "rate_limited_requests": self.rate_limited_requests,
            "results": self.result_count,
        }


def _value(value):
    if isinstance(value, dict) and "value" in value:
        return value["value"]
    return value
