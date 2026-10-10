"""OpenReview API：发现论文，并把公开评审/回复计数作为扎实度证据。"""

from __future__ import annotations

import asyncio
import logging
import re
import hashlib
import json
import time
from email.utils import parsedate_to_datetime
from datetime import datetime, timedelta, timezone
from runtime_clock import research_now

import httpx

import config
from paradigms.models import EvidenceType, TechnicalEvidence, nonnegative_number, technical_evidence_from_dict

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
        stage_cache=None,
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
        self.stage_cache = stage_cache
        self.query_progress: dict[str, dict] = {}
        self._request_lock = asyncio.Lock()
        self._last_request_started = 0.0

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
        self.query_progress = {}
        semaphore = asyncio.Semaphore(self.concurrency)
        cutoff_ms = int(
            (research_now() - timedelta(days=self.lookback_days)).timestamp()
            * 1000
        )

        async def search_one(client, venue: str, query: str):
            async with semaphore:
                signature = hashlib.sha256(json.dumps([2, research_now().isoformat(), self.lookback_days, venue, query, self.limit]).encode()).hexdigest()
                cached = self.stage_cache.cache_get("openreview_pages", signature) if self.stage_cache else None
                offset, seen_pages, evidence, complete = 0, set(), {}, False
                if cached is not None:
                    if (not isinstance(cached, dict) or type(cached.get("offset")) is not int
                            or cached["offset"] < 0 or type(cached.get("complete")) is not bool
                            or not isinstance(cached.get("seen_pages"), list)
                            or any(not isinstance(value, str) for value in cached["seen_pages"])
                            or not isinstance(cached.get("evidence"), list)):
                        raise ValueError("OpenReview page checkpoint is invalid")
                    offset, complete = cached["offset"], cached["complete"]
                    seen_pages = set(cached["seen_pages"])
                    for value in cached["evidence"]:
                        item = technical_evidence_from_dict(value)
                        if item.source != self.source_name or item.evidence_type != EvidenceType.PRIMARY_PAPER or not item.identifiers.get("openreview") or item.raw.get("discovery_venue_scope") != venue:
                            raise ValueError("OpenReview page checkpoint identity mismatch")
                        evidence[item.fingerprint] = item
                progress = {"venue": venue, "query": query, "offset": offset, "pages": len(seen_pages),
                            "status": "completed_cached" if complete else "pending", "results": len(evidence)}
                self.query_progress[signature] = progress
                if complete:
                    return list(evidence.values()), True
                if self._circuit_open:
                    self.not_executed_queries += 1
                    progress["status"] = "not_executed_rate_limit"
                    return list(evidence.values()), False
                try:
                    while True:
                        params = {
                            # Public API v2 /notes/search contract: term is the
                            # search expression; group scopes the invitation.
                            # No undocumented chronological ordering is assumed.
                            "term": query,
                            "content": "all",
                            "group": venue,
                            # Primary discovery needs forum submissions, not
                            # reviews/replies mentioning those submissions.
                            "source": "forum",
                            "count": True,
                            # Transport page size, never a candidate cap.
                            "limit": self.limit,
                            "offset": offset,
                        }
                        response = await self._get_with_backoff(client, params)
                        payload = response.json()
                        page, total = _search_page(payload, offset)
                        page_signature = hashlib.sha256(json.dumps([note["id"] for note in page]).encode()).hexdigest()
                        if page and page_signature in seen_pages:
                            raise ValueError("OpenReview search repeated a page; offset did not advance")
                        selected = []
                        for note in page:
                            if not _note_matches_venue(note, venue) or not _note_matches_query(note, query):
                                self.relevance_filtered_count += 1
                                continue
                            submitted = _submission_timestamp(note)
                            if not submitted:
                                self.undated_filtered_count += 1
                                continue
                            if submitted >= cutoff_ms:
                                selected.append(note)
                        for item in self._parse_notes(selected):
                            item.raw["discovery_venue_scope"] = venue
                            evidence[item.fingerprint] = item
                        seen_pages.add(page_signature)
                        offset += len(page)
                        complete = offset >= total if total is not None else len(page) < self.limit
                        # Commit the downstream evidence snapshot BEFORE the
                        # upstream page/query receipt. A retry resumes offset,
                        # rather than losing all healthy pages to the final 429.
                        if self.stage_cache:
                            self.stage_cache.cache_save("openreview_pages", signature, {
                                "offset": offset, "complete": complete,
                                "seen_pages": sorted(seen_pages), "evidence": [item.to_dict() for item in evidence.values()],
                            })
                        progress.update(offset=offset, pages=len(seen_pages), results=len(evidence), status="completed" if complete else "pending")
                        if complete:
                            return list(evidence.values()), True
                except Exception as exc:
                    self.failed_queries += 1
                    progress.update(status="failed", error=type(exc).__name__)
                    logger.warning("OpenReview 查询未闭合 [%s] offset=%s: %s", venue, offset, type(exc).__name__)
                    return list(evidence.values()), False

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
        for result in responses:
            if isinstance(result, Exception):
                self.failed_queries += 1
                logger.warning("OpenReview 单个 venue 获取失败: %s", type(result).__name__)
                continue
            evidence, completed = result
            self.completed_queries += int(completed)
            items.extend(evidence)
        deduped = list({item.fingerprint: item for item in items}.values())
        self.result_count = len(deduped)
        return deduped

    def _parse_notes(self, notes: list[dict]) -> list[TechnicalEvidence]:
        """Same production conversion for paginated discovery and acceptance."""
        cutoff_ms = int((research_now() - timedelta(days=self.lookback_days)).timestamp() * 1000)
        items = []
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
            if not note_id:
                raise ValueError("OpenReview submission is missing id")
            items.append(TechnicalEvidence(
                source=self.source_name, evidence_type=EvidenceType.PRIMARY_PAPER,
                title=title, url=f"https://openreview.net/forum?id={note_id}", summary=abstract,
                published_at=datetime.fromtimestamp(submitted / 1000, timezone.utc).isoformat(),
                authors=list(authors) if isinstance(authors, list) else [], organization=venue,
                metrics={"review_replies": reply_count} if "replyCount" in details else {},
                identifiers={"openreview": note_id},
                raw={"author_openreview_ids": author_ids, "origin_date_basis": "submission_created_at",
                     "discussion_last_modified_at": int(nonnegative_number(note.get("tmdate"))),
                     "discovery_lookback_days": self.lookback_days},
            ))
        return items

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
            async with self._request_lock:
                await asyncio.sleep(max(0, 1.0 - (time.monotonic() - self._last_request_started)))
                self._last_request_started = time.monotonic()
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
                    try:
                        date = parsedate_to_datetime(retry_after)
                        delay = (date - datetime.now(timezone.utc)).total_seconds()
                    except (TypeError, ValueError):
                        delay = float(2**attempt)
                if delay > 60:
                    self._circuit_open = True
                    response.raise_for_status()
                await asyncio.sleep(max(1.0, delay))
        assert last_response is not None
        # 同一轮已经证明上游持续限流后，停止其余 venue/query 继续消耗请求。
        # 这些查询会在 coverage 中记为 not_executed，而不是伪装成零命中。
        self._circuit_open = True
        last_response.raise_for_status()
        return last_response

    def coverage(self) -> dict[str, object]:
        return {
            "status": (
                "not_configured"
                if not self.venues
                else "not_executed"
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
            "query_progress": self.query_progress,
        }


def _search_page(payload: object, offset: int) -> tuple[list[dict], int | None]:
    """Shared production/acceptance page identity and pagination contract."""
    if not isinstance(payload, dict) or not isinstance(payload.get("notes"), list):
        raise ValueError("OpenReview search response is missing valid notes")
    page = payload["notes"]
    if any(not isinstance(note, dict) or not note.get("id") for note in page):
        raise ValueError("OpenReview note is missing identity")
    total = payload.get("count")
    if total is not None and (type(total) is not int or total < offset + len(page)):
        raise ValueError("OpenReview search count is inconsistent")
    if not page and total is not None and offset < total:
        raise ValueError("OpenReview search ended before its declared count")
    return page, total


def _value(value):
    if isinstance(value, dict) and "value" in value:
        return value["value"]
    return value


def _note_matches_venue(note: dict, venue: str) -> bool:
    """A broad search hit or reply is not a configured-venue submission."""
    note_id, forum = note.get("id"), note.get("forum")
    if forum and forum != note_id:
        return False
    content = note.get("content") or {}
    declared = _value(content.get("venueid")) or note.get("domain") or ""
    if declared:
        return str(declared) == venue or str(declared).startswith(venue + "/")
    return any(
        str(invitation).startswith(venue + "/-/")
        for invitation in (note.get("invitations") or [])
    )


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
