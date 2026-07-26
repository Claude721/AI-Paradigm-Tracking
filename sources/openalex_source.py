"""OpenAlex Works API：论文发现与引用信号。"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone

import httpx

import config
from paradigms.models import EvidenceType, TechnicalEvidence

logger = logging.getLogger(__name__)

OPENALEX_WORKS_API = "https://api.openalex.org/works"
DEFAULT_SEARCHES = [
    '"world model" AND (video OR robotics OR embodied)',
    '(reasoning OR inference-time) AND ("reinforcement learning" OR verifier)',
    '"vision language action" OR "vision-language-action"',
    '(agent OR agentic) AND (memory OR planning OR "tool use")',
    '"self-supervised" AND (video OR multimodal)',
    '("state space model" OR "test-time learning") AND (neural OR model)',
    '("synthetic data" OR "data curation") AND ("foundation model" OR LLM)',
]


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
        async with httpx.AsyncClient(timeout=30, headers=headers) as client:
            tasks = [
                client.get(
                    OPENALEX_WORKS_API,
                    params={
                        "api_key": config.OPENALEX_API_KEY,
                        "search": query,
                        "filter": date_filter,
                        # 时间窗已经由 filter 限定，先按检索相关性排序，避免
                        # “最新但无关”的跨学科论文挤掉真正的技术候选。
                        "sort": "relevance_score:desc,publication_date:desc",
                        "per-page": self.per_query,
                    },
                )
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
            if response.status_code >= 400:
                failed += 1
                logger.warning(
                    "OpenAlex 单个查询返回 HTTP %s [%s]",
                    response.status_code,
                    query,
                )
                continue
            evidence.extend(self._parse(response.json().get("results", [])))
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
            topic = work.get("primary_topic") or {}
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
                    summary=_restore_abstract(work.get("abstract_inverted_index")),
                    published_at=work.get("publication_date", ""),
                    authors=authors,
                    organization=_first_institution(authorships),
                    metrics={
                        "citations": work.get("cited_by_count", 0) or 0,
                        "is_oa": bool((work.get("open_access") or {}).get("is_oa")),
                    },
                    identifiers=identifiers,
                    keywords=[topic.get("display_name", "")] if topic.get("display_name") else [],
                    raw={"author_openalex_ids": author_ids},
                )
            )
        return results


def _restore_abstract(index: dict | None) -> str:
    if not index:
        return ""
    positions = [(position, word) for word, values in index.items() for position in values]
    return " ".join(word for _, word in sorted(positions))


def _first_institution(authorships: list[dict]) -> str:
    for authorship in authorships:
        institutions = authorship.get("institutions") or []
        if institutions:
            return institutions[0].get("display_name", "")
    return ""


def _dedupe(items: list[TechnicalEvidence]) -> list[TechnicalEvidence]:
    return list({item.fingerprint: item for item in items}.values())
