"""OpenAlex Works API：论文发现与引用信号。"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone

import httpx

import config
from paradigms.landscape import (
    classify_frontier_domains,
    openalex_search_plan,
)
from paradigms.models import EvidenceType, TechnicalEvidence

logger = logging.getLogger(__name__)

OPENALEX_WORKS_API = "https://api.openalex.org/works"
DEFAULT_SEARCHES = openalex_search_plan()


class OpenAlexSource:
    source_name = "openalex"

    def __init__(
        self,
        lookback_days: int = 7,
        per_query: int = 20,
        searches: list[str] | None = None,
    ):
        self.lookback_days = max(lookback_days, 1)
        self.per_query = min(max(per_query, 1), 100)
        self.searches = searches or DEFAULT_SEARCHES

    async def safe_fetch(self) -> list[TechnicalEvidence]:
        if not config.OPENALEX_API_KEY:
            logger.info("[openalex] 未配置 OPENALEX_API_KEY，跳过增强论文发现")
            return []
        try:
            return await self.fetch()
        except Exception:
            logger.exception("[openalex] 获取失败")
            return []

    async def fetch(self) -> list[TechnicalEvidence]:
        cutoff = datetime.now(timezone.utc) - timedelta(days=self.lookback_days)
        date_filter = f"from_publication_date:{cutoff.date().isoformat()}"
        headers = {"User-Agent": "AI-Paradigm-Radar/3.2"}

        async def search_one(client: httpx.AsyncClient, query: str) -> list[dict]:
            cursor = "*"
            works: list[dict] = []
            consecutive_irrelevant_pages = 0
            while cursor:
                response = await client.get(
                    OPENALEX_WORKS_API,
                    params={
                        "api_key": config.OPENALEX_API_KEY,
                        "search": query,
                        "filter": date_filter,
                        # 时间窗已经由 filter 限定，先按检索相关性排序。
                        "sort": "relevance_score:desc,publication_date:desc",
                        # per_query 是传输页大小，不是每周候选上限。
                        "per-page": self.per_query,
                        "cursor": cursor,
                    },
                )
                response.raise_for_status()
                payload = response.json()
                page = payload.get("results", [])
                relevant_page = [
                    work for work in page if _work_frontier_domains(work)
                ]
                works.extend(relevant_page)
                consecutive_irrelevant_pages = (
                    0
                    if relevant_page
                    else consecutive_irrelevant_pages + 1
                )
                next_cursor = str((payload.get("meta") or {}).get("next_cursor") or "")
                # OpenAlex search 可能把弱相关结果分页到很深。这里不是固定 Top-K：
                # 只有在按相关性排序后连续两页都没有命中版本化覆盖地图时才
                # 认为该查询的有效证据已耗尽，避免 60 天冷启动无界下载。
                if (
                    not page
                    or not next_cursor
                    or next_cursor == cursor
                    or consecutive_irrelevant_pages >= 2
                ):
                    break
                cursor = next_cursor
            return works

        async with httpx.AsyncClient(timeout=30, headers=headers) as client:
            tasks = [
                search_one(client, query)
                for query in self.searches
            ]
            responses = await asyncio.gather(*tasks, return_exceptions=True)

        evidence: list[TechnicalEvidence] = []
        failed = 0
        for query, response in zip(self.searches, responses):
            if isinstance(response, Exception):
                failed += 1
                logger.warning("OpenAlex 单个查询失败 [%s]: %s", query, response)
                continue
            evidence.extend(self._parse(response))
        if failed == len(self.searches):
            raise RuntimeError("OpenAlex 所有查询均失败")
        return _dedupe(evidence)

    def _parse(self, works: list[dict]) -> list[TechnicalEvidence]:
        results = []
        for work in works:
            title = work.get("display_name", "")
            if not title:
                continue
            authorships = work.get("authorships") or []
            authors = [
                item.get("author", {}).get("display_name", "")
                for item in authorships
                if item.get("author", {}).get("display_name")
            ]
            author_ids = [
                item.get("author", {}).get("id", "")
                for item in authorships
                if item.get("author", {}).get("id")
            ]
            author_openalex_map = {
                str(item.get("author", {}).get("display_name", "")): str(
                    item.get("author", {}).get("id", "")
                )
                for item in authorships
                if item.get("author", {}).get("display_name")
                and item.get("author", {}).get("id")
            }
            author_affiliations = {
                str(item.get("author", {}).get("display_name", "")): [
                    str(institution.get("display_name", ""))
                    for institution in (item.get("institutions") or [])
                    if institution.get("display_name")
                ]
                for item in authorships
                if item.get("author", {}).get("display_name")
            }
            topic = work.get("primary_topic") or {}
            abstract = _restore_abstract(work.get("abstract_inverted_index"))
            frontier_domains = _work_frontier_domains(work)
            if not frontier_domains:
                continue
            identifiers = {
                "openalex": work.get("id", ""),
                "doi": (work.get("doi") or "").removeprefix("https://doi.org/"),
            }
            identifiers = {k: v for k, v in identifiers.items() if v}
            results.append(
                TechnicalEvidence(
                    source=self.source_name,
                    evidence_type=EvidenceType.PRIMARY_PAPER,
                    title=title,
                    url=(work.get("primary_location") or {}).get("landing_page_url")
                    or work.get("doi")
                    or work.get("id", ""),
                    summary=abstract,
                    published_at=work.get("publication_date", ""),
                    authors=authors,
                    organization=_first_institution(authorships),
                    metrics={
                        "citations": work.get("cited_by_count", 0) or 0,
                        "is_oa": bool((work.get("open_access") or {}).get("is_oa")),
                    },
                    identifiers=identifiers,
                    keywords=[topic.get("display_name", "")] if topic.get("display_name") else [],
                    raw={
                        "author_openalex_ids": author_ids,
                        "author_openalex_map": author_openalex_map,
                        "author_affiliations": author_affiliations,
                        "frontier_domains": frontier_domains,
                    },
                )
            )
        return results


def _restore_abstract(index: dict | None) -> str:
    if not index:
        return ""
    positions = [(position, word) for word, values in index.items() for position in values]
    return " ".join(word for _, word in sorted(positions))


def _work_frontier_domains(work: dict) -> list[str]:
    topic = work.get("primary_topic") or {}
    return classify_frontier_domains(
        str(work.get("display_name", "")),
        _restore_abstract(work.get("abstract_inverted_index")),
        str(topic.get("display_name", "")),
        " ".join(
            str(value.get("display_name", ""))
            for value in (work.get("topics") or [])
            if isinstance(value, dict)
        ),
    )


def _first_institution(authorships: list[dict]) -> str:
    for authorship in authorships:
        institutions = authorship.get("institutions") or []
        if institutions:
            return institutions[0].get("display_name", "")
    return ""


def _dedupe(items: list[TechnicalEvidence]) -> list[TechnicalEvidence]:
    return list({item.fingerprint: item for item in items}.values())
