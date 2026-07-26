"""用 Tavily 免费网页搜索补足 X、Reddit 与小红书的公开索引盲区。"""

from __future__ import annotations

import asyncio
import logging
import re
from urllib.parse import urlparse

import httpx

import config
from paradigms.models import EvidenceType, ParadigmCandidate, TechnicalEvidence

logger = logging.getLogger(__name__)
TAVILY_SEARCH_API = "https://api.tavily.com/search"


class SocialWebSearchClient:
    """搜索公开索引；结果是讨论线索，不是平台全量声量统计。"""

    def __init__(self, max_requests: int | None = None):
        self.max_requests = (
            config.TAVILY_MAX_REQUESTS_PER_RUN
            if max_requests is None
            else max(max_requests, 0)
        )
        self.requests_used = 0
        self._budget_lock = asyncio.Lock()
        self._budget_warning_emitted = False

    async def search(
        self,
        client: httpx.AsyncClient,
        candidate: ParadigmCandidate,
    ) -> list[TechnicalEvidence]:
        if (
            not config.TAVILY_SOCIAL_SEARCH_ENABLED
            or not config.TAVILY_API_KEY
            or not config.TAVILY_SOCIAL_SEARCH_DOMAINS
        ):
            return []
        title, identifier = _search_identity(candidate)
        if not title:
            return []
        if not await self._reserve_request():
            return []
        query = f'"{_clean_query(title)}"'
        if identifier:
            query = f'{query} OR "{_clean_query(identifier)}"'
        response = await client.post(
            TAVILY_SEARCH_API,
            headers={
                "Authorization": f"Bearer {config.TAVILY_API_KEY}",
                "Content-Type": "application/json",
            },
            json={
                "query": query,
                "search_depth": "basic",
                "max_results": config.TAVILY_SOCIAL_MAX_RESULTS,
                "topic": "general",
                "time_range": _time_range(config.SOURCING_LOOKBACK_DAYS),
                "include_answer": False,
                "include_raw_content": False,
                "include_domains": config.TAVILY_SOCIAL_SEARCH_DOMAINS,
                "auto_parameters": False,
                "exact_match": True,
                "include_usage": True,
            },
        )
        if response.status_code >= 400:
            logger.warning("Tavily 社交网页搜索失败: HTTP %s", response.status_code)
            return []
        return _parse_results(response.json(), candidate, title)

    async def _reserve_request(self) -> bool:
        async with self._budget_lock:
            if self.requests_used >= self.max_requests:
                if not self._budget_warning_emitted:
                    logger.warning(
                        "Tavily 本轮请求预算已用完 (%s)，其余候选不再消耗 credits",
                        self.max_requests,
                    )
                    self._budget_warning_emitted = True
                return False
            self.requests_used += 1
            return True


def _parse_results(
    payload: dict,
    candidate: ParadigmCandidate,
    work_title: str,
) -> list[TechnicalEvidence]:
    results: list[TechnicalEvidence] = []
    for item in payload.get("results", []):
        url = str(item.get("url", "")).strip()
        platform = _platform_from_url(url)
        if not platform:
            continue
        title = str(item.get("title", "")).strip() or f"{platform} 公开讨论"
        summary = str(item.get("content", "")).strip()
        if not _result_is_related(candidate, work_title, f"{title} {summary} {url}"):
            continue
        evidence_type = (
            EvidenceType.COMMUNITY_DISCUSSION
            if platform == "reddit"
            else EvidenceType.SECONDARY_INTERPRETATION
        )
        social_name, profile_url = _public_profile_hint(platform, title, url)
        results.append(
            TechnicalEvidence(
                source=f"tavily-{platform}",
                evidence_type=evidence_type,
                title=title,
                url=url,
                summary=summary[:3000],
                published_at=str(item.get("published_date", "")),
                metrics={"search_relevance": float(item.get("score", 0) or 0)},
                raw={
                    "relationship": "web_index_exact_work_search",
                    "indexed_discovery_only": True,
                    "metrics_unavailable": True,
                    "coverage": "partial_web_index",
                    "ephemeral_content": True,
                    "social_platform": platform,
                    "social_author_name": social_name,
                    "social_profile_url": profile_url,
                    "tavily_request_id": str(payload.get("request_id", "")),
                },
            )
        )
    return results


def _search_identity(candidate: ParadigmCandidate) -> tuple[str, str]:
    for evidence in candidate.evidence:
        if evidence.evidence_type not in {
            EvidenceType.PRIMARY_PAPER,
            EvidenceType.TECHNICAL_BLOG,
        }:
            continue
        identifier = evidence.identifiers.get("arxiv") or evidence.identifiers.get(
            "doi", ""
        )
        return evidence.title.strip(), str(identifier).strip()
    return candidate.name.strip(), ""


def _platform_from_url(url: str) -> str:
    host = (urlparse(url).hostname or "").casefold().removeprefix("www.")
    if host in {"x.com", "twitter.com", "mobile.twitter.com"}:
        return "x"
    if host == "reddit.com" or host.endswith(".reddit.com"):
        return "reddit"
    if host == "xiaohongshu.com" or host.endswith(".xiaohongshu.com"):
        return "xiaohongshu"
    return ""


def _public_profile_hint(platform: str, title: str, url: str) -> tuple[str, str]:
    if platform == "x":
        parts = [value for value in urlparse(url).path.split("/") if value]
        handle = parts[0] if parts and parts[0] not in {"i", "search", "home"} else ""
        display_match = re.match(r"\s*([^(@|]+?)\s*(?:\(@?[^)]+\)|\|)", title)
        display_name = display_match.group(1).strip() if display_match else ""
        return display_name, f"https://x.com/{handle}" if handle else ""
    if platform == "xiaohongshu" and "/user/profile/" in url:
        display_name = re.split(r"[-|—]", title, maxsplit=1)[0].strip()
        return display_name, url.split("?", 1)[0]
    return "", ""


def _result_is_related(
    candidate: ParadigmCandidate,
    work_title: str,
    text: str,
) -> bool:
    normalized_text = _normalize(text)
    normalized_title = _normalize(work_title)
    if len(normalized_title) >= 12 and normalized_title in normalized_text:
        return True
    stable_ids = {
        _normalize(value)
        for evidence in candidate.evidence
        for key, value in evidence.identifiers.items()
        if key in {"arxiv", "doi"} and value
    }
    if any(len(value) >= 6 and value in normalized_text for value in stable_ids):
        return True
    tokens = {
        token.casefold()
        for value in [candidate.name, candidate.route_family, *candidate.keywords]
        for token in re.findall(r"[A-Za-z0-9][A-Za-z0-9-]{3,}", value)
    }
    return sum(token in text.casefold() for token in tokens) >= 2


def _time_range(days: int) -> str:
    if days <= 1:
        return "day"
    if days <= 7:
        return "week"
    if days <= 31:
        return "month"
    return "year"


def _clean_query(value: str) -> str:
    return re.sub(r'["\r\n]+', " ", value).strip()[:300]


def _normalize(value: str) -> str:
    return "".join(character for character in value.casefold() if character.isalnum())
