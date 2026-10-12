"""用 Tavily 网页搜索发现社区页面与独立技术解读线索。"""

from __future__ import annotations

import asyncio
import logging
import re
from urllib.parse import urlparse

import httpx

import config
from database.provider_budget import billing_period
from tavily_protocol import read_account_usage
from paradigms.search_receipts import record_search
from paradigms.models import (
    ORIGIN_EVIDENCE_TYPES,
    EvidenceType,
    ParadigmCandidate,
    TechnicalEvidence,
    nonnegative_number,
)

logger = logging.getLogger(__name__)
TAVILY_SEARCH_API = "https://api.tavily.com/search"


class SocialWebSearchClient:
    """搜索公开索引；结果是讨论线索，不是平台全量声量统计。"""

    def __init__(self, max_requests: int | None = None, *, budget_store=None):
        self.max_requests = (
            config.TAVILY_REQUEST_SAFETY_LIMIT
            if max_requests is None
            else max(max_requests, 0)
        )
        self.requests_used = 0
        self._budget_lock = asyncio.Lock()
        self._budget_warning_emitted = False
        self.budget_store = budget_store
        self.monthly_limit = config.TAVILY_MONTHLY_CREDIT_LIMIT
        self._verified_month = ""
        self._usage_failure = ""

    async def search(
        self,
        client: httpx.AsyncClient,
        candidate: ParadigmCandidate,
    ) -> list[TechnicalEvidence]:
        if (
            not config.TAVILY_SOCIAL_SEARCH_ENABLED
            or not config.TAVILY_API_KEY
        ):
            record_search(candidate, "tavily_social_web", "not_configured" if config.TAVILY_SOCIAL_SEARCH_ENABLED else "disabled", reason="no_key" if config.TAVILY_SOCIAL_SEARCH_ENABLED else "disabled_by_config")
            candidate.community_coverage["tavily_social_web"] = (
                "Tavily 已关闭，未执行跨站公开索引搜索"
                if not config.TAVILY_SOCIAL_SEARCH_ENABLED
                else "Tavily 已开启但未配置 Key，社区查询保留待办"
            )
            return []
        title, identifier = _search_identity(candidate)
        if not title:
            record_search(candidate, "tavily_social_web", "failed", reason="missing_search_identity")
            return []
        allowed, reservation, period, reason = await self._reserve_request(client)
        if not allowed:
            record_search(candidate, "tavily_social_web", "not_executed", reason=reason)
            candidate.community_coverage["tavily_social_web"] = (
                f"Tavily 额度检查未通过：{reason}；本候选未执行搜索，不代表零讨论"
            )
            return []
        query = (
            f'"{_clean_query(title)}" '
            "(analysis OR discussion OR notes OR implementation OR replication)"
        )
        if identifier:
            query = f'{query} OR "{_clean_query(identifier)}"'
        search_days = max(
            [
                config.SOURCING_LOOKBACK_DAYS,
                *[
                    int(
                        nonnegative_number(
                            item.raw.get("discovery_lookback_days", 0)
                        )
                    )
                    for item in candidate.evidence
                ],
            ]
        )
        request_payload = {
            "query": query,
            "search_depth": "basic",
            "max_results": config.TAVILY_SOCIAL_MAX_RESULTS,
            "topic": "general",
            "time_range": _time_range(search_days),
            "include_answer": False,
            "include_raw_content": False,
            "auto_parameters": False,
            "exact_match": True,
            "include_usage": True,
        }
        domains = config.TAVILY_DISCOVERY_DOMAINS
        if domains:
            request_payload["include_domains"] = domains
        payload, confirmed = None, False
        try:
            response = await client.post(
                TAVILY_SEARCH_API,
                headers={"Authorization": f"Bearer {config.TAVILY_API_KEY}", "Content-Type": "application/json"},
                json=request_payload, follow_redirects=False,
            )
            try:
                payload = response.json()
            except ValueError:
                pass
            if response.status_code >= 400:
                record_search(candidate, "tavily_social_web", "failed", reason=f"http_{response.status_code}")
                candidate.community_coverage["tavily_social_web"] = f"已发起 Tavily 搜索，但返回 HTTP {response.status_code}"
            if reservation is not None:
                credits = (payload.get("usage") or {}).get("credits") if isinstance(payload, dict) and isinstance(payload.get("usage"), dict) else None
                if type(credits) is int and credits >= 0:
                    self.budget_store.settle(reservation, credits=credits)
                    confirmed = True
                    if credits > 1:
                        self._usage_failure = "unexpected_credit_cost"
                        raise ValueError("Unexpected Tavily basic search credit cost")
                else:
                    if response.status_code >= 400:
                        return []  # finally keeps the unconfirmed reservation.
                    raise ValueError("Tavily search credit usage is unconfirmed")
        finally:
            if reservation is not None and not confirmed:
                self.budget_store.settle(reservation)
                self._usage_failure = "unconfirmed_request_usage"
        if response.status_code >= 400:
            candidate.community_coverage["tavily_social_web"] = (
                f"已发起 Tavily 搜索，但返回 HTTP {response.status_code}"
            )
            logger.warning("Tavily 社交网页搜索失败: HTTP %s", response.status_code)
            record_search(candidate, "tavily_social_web", "failed", reason=f"http_{response.status_code}")
            return []
        candidate.community_coverage["tavily_social_web"] = (
            "已执行社区与独立技术博客的公开网页索引搜索；结果非平台全量，"
            "未直接读取的页面只作为发现线索"
        )
        if not isinstance(payload, dict) or payload.get("error") or not isinstance(payload.get("results"), list):
            raise ValueError("Invalid Tavily search response")
        results = _parse_results(payload, candidate, title)
        record_search(candidate, "tavily_social_web", "completed", count=len(results))
        return results

    async def _reserve_request(self, client):
        async with self._budget_lock:
            if self.max_requests > 0 and self.requests_used >= self.max_requests:
                if not self._budget_warning_emitted:
                    logger.warning(
                        "Tavily 本轮请求预算已用完 (%s)，其余候选不再消耗 credits",
                        self.max_requests,
                    )
                    self._budget_warning_emitted = True
                return False, None, "", "request_safety_limit"
            period, reservation = billing_period(), None
            if self.monthly_limit > 0:
                if self.budget_store is None:
                    return False, None, period, "durable_budget_store_missing"
                if self._verified_month != period:
                    self._usage_failure = ""
                    self._verified_month = period
                    through = self.budget_store.watermark("tavily", period)
                    try:
                        account_usage = await read_account_usage(client, config.TAVILY_API_KEY)
                        # Never map remote usage to the frozen campaign month.
                        if billing_period() != period:
                            self._verified_month = ""
                            return False, None, period, "billing_period_changed"
                        self.budget_store.observe("tavily", period, account_usage, through=through)
                    except asyncio.CancelledError:
                        self._verified_month = ""
                        raise
                    except httpx.HTTPStatusError as exc:
                        self._usage_failure = f"usage_http_{exc.response.status_code}"
                    except Exception as exc:
                        self._usage_failure = "usage_check_" + type(exc).__name__
                if self._usage_failure:
                    return False, None, period, self._usage_failure
                reservation = self.budget_store.reserve("tavily", period, limit=self.monthly_limit)
                if reservation is None:
                    return False, None, period, "monthly_credit_limit"
            self.requests_used += 1
            return True, reservation, period, ""


def _parse_results(
    payload: dict,
    candidate: ParadigmCandidate,
    work_title: str,
) -> list[TechnicalEvidence]:
    results: list[TechnicalEvidence] = []
    for item in payload.get("results", []):
        url = str(item.get("url", "")).strip()
        platform = _platform_from_url(url) or "web"
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
                published_at=str(item.get("published_date") or ""),
                metrics={"search_relevance": nonnegative_number(item.get("score", 0))},
                raw={
                    "relationship": (
                        "indexed_secondary_candidate"
                        if platform == "web"
                        else "web_index_exact_work_search"
                    ),
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
        if evidence.evidence_type not in ORIGIN_EVIDENCE_TYPES:
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
