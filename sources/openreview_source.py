"""OpenReview API：发现论文，并把公开评审/回复计数作为扎实度证据。"""

from __future__ import annotations

import asyncio
import logging
import re
from datetime import datetime, timedelta, timezone

import httpx

import config
from paradigms.models import EvidenceType, TechnicalEvidence, nonnegative_number

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


class OpenReviewCircuitOpen(RuntimeError):
    """A shared rate-limit circuit stopped queries that were never attempted."""


class OpenReviewSource:
    source_name = "openreview"

    def __init__(
        self,
        lookback_days: int = 7,
        limit: int = 50,
        venues: list[str] | None = None,
        searches: list[str] | None = None,
        concurrency: int = 1,
    ):
        self.lookback_days = max(lookback_days, 1)
        self.limit = min(max(limit, 1), 100)
        self.venues = config.OPENREVIEW_VENUES if venues is None else venues
        self.searches = searches or DEFAULT_SEARCHES
        self.concurrency = max(concurrency, 1)
        self.request_count = 0
        self.rate_limited_requests = 0
        self.failed_queries = 0
        self.not_executed_queries = 0
        self.completed_queries = 0
        self.result_count = 0
        self.relevance_filtered_count = 0
        self.undated_filtered_count = 0
        self._circuit_open = False

    async def safe_fetch(self) -> list[TechnicalEvidence]:
        if not self.venues:
            self.fetch_status = "not_configured"
            self.fetch_error = ""
            return []
        try:
            results = await self.fetch()
            incomplete = bool(self.failed_queries or self.not_executed_queries)
            self.fetch_status = "partial" if incomplete else "completed"
            self.fetch_error = "PartialQueryFailure" if incomplete else ""
            return results
        except Exception as exc:
            self.fetch_status = "query_failed"
            self.fetch_error = type(exc).__name__
            logger.exception("[openreview] 获取失败")
            return []

    async def fetch(self) -> list[TechnicalEvidence]:
        self.request_count = 0
        self.rate_limited_requests = 0
        self.failed_queries = 0
        self.not_executed_queries = 0
        self.completed_queries = 0
        self.result_count = 0
        self.relevance_filtered_count = 0
        self.undated_filtered_count = 0
        self._circuit_open = False
        semaphore = asyncio.Semaphore(self.concurrency)
        cutoff_ms = int(
            (datetime.now(timezone.utc) - timedelta(days=self.lookback_days)).timestamp()
            * 1000
        )

        async def search_one(client, venue: str, query: str):
            async with semaphore:
                if self._circuit_open:
                    raise OpenReviewCircuitOpen(
                        "OpenReview rate-limit circuit already open"
                    )
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
                        # tmdate 是最后讨论/修改时间，会让数年前的
                        # 投稿因新回复被伪装成本周论文。原点召回必须
                        # 以投稿创建时间 cdate 排序和截断。
                        "sort": "cdate:desc",
                        "details": "replyCount",
                    }
                    response = await self._get_with_backoff(client, params)
                    page = response.json().get("notes", [])
                    dated = []
                    for note in page:
                        submitted = _submission_timestamp(note)
                        if not submitted:
                            self.undated_filtered_count += 1
                            continue
                        dated.append(submitted)
                        if submitted < cutoff_ms:
                            continue
                        if not _note_matches_query(note, query):
                            self.relevance_filtered_count += 1
                            continue
                        notes.append(note)
                    if (
                        len(page) < self.limit
                        or not page
                        or any(value < cutoff_ms for value in dated)
                        or not dated
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
                if isinstance(notes, OpenReviewCircuitOpen):
                    self.not_executed_queries += 1
                    continue
                self.failed_queries += 1
                logger.warning("OpenReview 单个 venue 获取失败: %s", notes)
                continue
            self.completed_queries += 1
            for note in notes:
                submitted = _submission_timestamp(note)
                if not submitted or submitted < cutoff_ms:
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
                reply_count = int(nonnegative_number(details.get("replyCount", 0)))
                note_id = note.get("id", "")
                items.append(
                    TechnicalEvidence(
                        source=self.source_name,
                        evidence_type=EvidenceType.PRIMARY_PAPER,
                        title=title,
                        url=f"https://openreview.net/forum?id={note_id}",
                        summary=abstract,
                        published_at=datetime.fromtimestamp(
                            submitted / 1000, timezone.utc
                        ).isoformat()
                        if submitted
                        else "",
                        authors=list(authors) if isinstance(authors, list) else [],
                        organization=venue,
                        metrics={"review_replies": reply_count},
                        identifiers={"openreview": note_id},
                        raw={
                            "author_openreview_ids": author_ids,
                            "origin_date_basis": "submission_created_at",
                            "discussion_last_modified_at": int(
                                nonnegative_number(note.get("tmdate"))
                            ),
                            "discovery_lookback_days": self.lookback_days,
                        },
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
        if self._circuit_open:
            raise OpenReviewCircuitOpen(
                "OpenReview rate-limit circuit already open"
            )
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
        # 同一轮已经证明上游持续限流后，停止其余 venue/query 继续消耗请求。
        # 这些查询会在 coverage 中记为 not_executed，而不是伪装成零命中。
        self._circuit_open = True
        last_response.raise_for_status()
        return last_response

    def coverage(self) -> dict[str, int | str]:
        return {
            "status": (
                "not_executed"
                if not self.completed_queries
                and not self.failed_queries
                and not self.not_executed_queries
                else "rate_limited"
                if self._circuit_open and not self.completed_queries
                else "partial_rate_limited"
                if self._circuit_open
                else "query_failed"
                if self.failed_queries and not self.completed_queries
                else "partial"
                if self.failed_queries or self.not_executed_queries
                else "completed_after_retry"
                if self.rate_limited_requests
                else "completed"
            ),
            "queries": len(self.venues) * len(self.searches),
            "completed_queries": self.completed_queries,
            "failed_queries": self.failed_queries,
            "not_executed_queries": self.not_executed_queries,
            "requests": self.request_count,
            "rate_limited_requests": self.rate_limited_requests,
            "results": self.result_count,
            "circuit_open": self._circuit_open,
            "lookback_days": self.lookback_days,
            "lookback_basis": "submission_created_at",
            "relevance_filtered": self.relevance_filtered_count,
            "undated_filtered": self.undated_filtered_count,
        }


def _value(value):
    if isinstance(value, dict) and "value" in value:
        return value["value"]
    return value


def _submission_timestamp(note: dict) -> int:
    """只返回能表示论文原点的时间。

    cdate 是 OpenReview note 创建时间；pdate/tcdate 是可接受的公开/
    创建备用字段。tmdate 只能用作“本周讨论有更新”的支持证据，
    不能决定论文是否进入本周原点池。
    """

    for field in ("cdate", "pdate", "tcdate"):
        try:
            value = int(note.get(field) or 0)
        except (TypeError, ValueError):
            value = 0
        if value > 0:
            return value
    return 0


def _note_matches_query(note: dict, query: str) -> bool:
    """对 OpenReview 全文搜索做本地强相关性闸门。"""

    content = note.get("content") or {}
    text = " ".join(
        str(_value(content.get(field)) or "")
        for field in ("title", "abstract", "keywords")
    )
    normalized_text = re.sub(r"[^a-z0-9]+", " ", text.casefold()).strip()
    normalized_query = re.sub(r"[^a-z0-9]+", " ", query.casefold()).strip()
    if not normalized_query:
        return False
    if normalized_query in normalized_text:
        return True
    terms = [term for term in normalized_query.split() if len(term) >= 3]
    return bool(terms) and all(
        re.search(rf"\b{re.escape(term)}\b", normalized_text)
        for term in terms
    )
