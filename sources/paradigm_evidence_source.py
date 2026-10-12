"""把 GitHub 与 Hacker News 从“候选生成器”降级为范式扩散证据。"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from datetime import datetime, timedelta, timezone
from runtime_clock import research_now, research_days
from difflib import SequenceMatcher
from urllib.parse import quote

import httpx

import config
from paradigms.models import (
    ORIGIN_EVIDENCE_TYPES,
    EvidenceType,
    ParadigmCandidate,
    TechnicalEvidence,
    _evidence_datetime,
)
from sources.reddit_evidence_source import RedditEvidenceClient
from sources.social_web_search_source import SocialWebSearchClient
from paradigms.search_receipts import record_search, reusable_search, search_signature
from paradigms.async_utils import gather_scoped

logger = logging.getLogger(__name__)


class CommunityEvidenceClient:
    def __init__(self, *, budget_store=None):
        self.social_web = SocialWebSearchClient(budget_store=budget_store)
        self.reddit = RedditEvidenceClient()
        self._github_lock = asyncio.Lock()
        self._github_last_request_at = 0.0
        self._github_circuit_open = False
        self._github_failure_logged = False

    async def search(self, candidate: ParadigmCandidate) -> list[TechnicalEvidence]:
        candidate.community_search_receipts.pop("community_runtime", None)
        candidate.community_coverage.pop("community_runtime", None)
        async def execute(provider, operation, client):
            signature = search_signature(candidate, provider)
            if reusable_search(candidate, provider, signature):
                return []  # Evidence already belongs to the candidate snapshot.
            previous = candidate.community_search_receipts.get(provider, {})
            history = list(previous.get("failure_history", []))
            if previous.get("status") in {"failed", "not_executed", "not_configured"}:
                history.append({key: previous.get(key, "") for key in ("status", "reason", "input_signature")})
            record_search(candidate, provider, "not_executed", reason="query_pending")
            batch = []
            try:
                batch = await operation(client, candidate)
                batch = [item for item in batch if not (date := _evidence_datetime(item.published_at)) or date <= research_now()]
                batch = list({item.fingerprint: item for item in batch}.values())
                candidate.evidence.extend(batch)
            except asyncio.CancelledError:
                record_search(candidate, provider, "not_executed", reason="query_interrupted")
                raise
            except Exception as exc:
                receipt = candidate.community_search_receipts.get(provider, {})
                if receipt.get("status") != "failed":
                    record_search(candidate, provider, "failed", reason=type(exc).__name__)
                candidate.community_coverage[provider] = f"查询异常：{type(exc).__name__}；待重试，不代表零讨论"
                batch = []
            finally:
                receipt = candidate.community_search_receipts[provider]
                if receipt["status"] == "completed":
                    receipt["result_count"] = len(batch)
                receipt["input_signature"] = signature
                receipt["evidence_fingerprints"] = [item.fingerprint for item in batch]
                receipt["failure_history"] = history
            return batch

        async with httpx.AsyncClient(timeout=30, follow_redirects=True) as client:
            batches = await gather_scoped(
                execute("github", self._github, client),
                execute("hackernews", self._hackernews, client),
                execute("tavily_social_web", self.social_web.search, client),
                execute("reddit_official", self.reddit.search, client),
                execute("x_official", self._x_title_search, client),
            )
        results = [item for batch in batches for item in batch]
        return [item for item in results if not (date := _evidence_datetime(item.published_at)) or date <= research_now()]

    def coverage(self) -> dict[str, str]:
        return {
            "github": (
                "已配置 Token，并以搜索 API 限额串行查询；限流后本轮自动熔断"
                if config.GITHUB_TOKEN
                else "未配置 GitHub Token；本轮不调用匿名 Search API"
            ),
            "hackernews": "Algolia 搜索执行结果以逐候选回执为准；失败保留待重试",
            "tavily_social_web": (
                "已配置并尝试公开网页索引搜索 X、Reddit 与小红书；结果非平台全量"
                if config.TAVILY_SOCIAL_SEARCH_ENABLED and config.TAVILY_API_KEY
                else "未配置 Tavily，未执行跨站公开索引搜索"
            ),
            "reddit_official": (
                "已配置获批 OAuth Data API 并尝试搜索；是否命中以 evidence 为准"
                if self.reddit.configured
                else "未配置或未确认 Reddit Data API 批准；只保留网页索引覆盖"
            ),
            "x_official": (
                "已配置 X Recent Search 并尝试获取公开帖子和互动量"
                if config.KOL_X_SOURCE_ENABLED and config.TWITTER_BEARER_TOKEN
                else "X 官方 API 已关闭或未配置；不得把未检出解释为零讨论"
            ),
        }

    async def _github(
        self, client: httpx.AsyncClient, candidate: ParadigmCandidate
    ) -> list[TechnicalEvidence]:
        if not config.GITHUB_TOKEN or self._github_circuit_open:
            record_search(candidate, "github", "not_executed" if self._github_circuit_open else "disabled",
                          reason="provider_circuit_open" if self._github_circuit_open else "no_token")
            if self._github_circuit_open:
                candidate.community_coverage["github"] = (
                    "此前 GitHub Search 已鉴权/限流失败，本轮熔断后未继续查询"
                )
            return []
        headers = {
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "AI-Paradigm-Radar/2.0",
        }
        headers["Authorization"] = f"Bearer {config.GITHUB_TOKEN}"
        primary_id = next(
            (
                value
                for item in candidate.evidence
                for key, value in item.identifiers.items()
                if key in {"arxiv", "doi"} and value
            ),
            "",
        )
        query_term = primary_id or candidate.name
        # GitHub Search API 有独立且明显低于 Core API 的分钟限额。所有候选共享
        # 一个串行节流器，避免并发候选在 Actions 中触发 secondary rate limit。
        async with self._github_lock:
            if self._github_circuit_open:
                record_search(candidate, "github", "not_executed", reason="provider_circuit_open")
                return []
            elapsed = time.monotonic() - self._github_last_request_at
            if elapsed < 2.2:
                await asyncio.sleep(2.2 - elapsed)
            response = await client.get(
                "https://api.github.com/search/repositories",
                headers=headers,
                params={
                    "q": f'"{query_term}" in:name,description,readme',
                    "sort": "updated",
                    "order": "desc",
                    "per_page": 5,
                },
            )
            self._github_last_request_at = time.monotonic()
            if response.status_code in {401, 403, 429}:
                self._github_circuit_open = True
        if response.status_code >= 400:
            if response.status_code in {401, 403, 429}:
                self._github_circuit_open = True
            if not self._github_failure_logged:
                logger.warning(
                    "GitHub 范式证据搜索失败: HTTP %s；remaining=%s；本轮%s",
                    response.status_code,
                    response.headers.get("x-ratelimit-remaining", "unknown"),
                    "停止后续 GitHub 搜索" if self._github_circuit_open else "继续降级",
                )
                self._github_failure_logged = True
            candidate.community_coverage["github"] = (
                f"已尝试 GitHub Search，但返回 HTTP {response.status_code}"
            )
            record_search(candidate, "github", "failed", reason=f"http_{response.status_code}")
            return []
        payload = response.json()
        if not isinstance(payload, dict) or not isinstance(payload.get("items"), list):
            raise ValueError("Invalid GitHub search response")
        results = []
        official_repositories = {
            str(url).rstrip("/").removeprefix("https://github.com/").casefold()
            for evidence in candidate.evidence
            for url in (evidence.raw.get("github_repositories") or [])
            if url
        }
        for repo in payload["items"]:
            if not _is_relevant_repository(candidate, repo):
                continue
            updated_at = str(repo.get("updated_at", ""))
            full_name = str(repo.get("full_name", ""))
            is_official = full_name.casefold() in official_repositories
            results.append(
                TechnicalEvidence(
                    source="github",
                    evidence_type=EvidenceType.IMPLEMENTATION,
                    title=full_name,
                    url=repo.get("html_url", ""),
                    summary=repo.get("description") or "",
                    published_at=str(repo.get("created_at") or ""),
                    authors=[(repo.get("owner") or {}).get("login", "")],
                    metrics={
                        "stars": repo.get("stargazers_count", 0) or 0,
                        "forks": repo.get("forks_count", 0) or 0,
                    },
                    identifiers={"github": full_name},
                    raw={
                        "updated_at": repo.get("updated_at", ""),
                        "created_at": repo.get("created_at", ""),
                        "relationship": (
                            "paper_linked_repository"
                            if is_official
                            else "name_and_mechanism_match"
                        ),
                        "independence": "official" if is_official else "unverified",
                    },
                )
            )
        candidate.community_coverage["github"] = (
            f"已执行 GitHub Search；相关实现/仓库命中 {len(results)} 条"
        )
        record_search(candidate, "github", "failed" if payload.get("incomplete_results") else "completed",
                      reason="incomplete_search_response" if payload.get("incomplete_results") else "", count=len(results))
        return results

    async def _hackernews(
        self, client: httpx.AsyncClient, candidate: ParadigmCandidate
    ) -> list[TechnicalEvidence]:
        cutoff = int(
            (research_now() - timedelta(days=research_days())).timestamp()
        )
        query = candidate.name
        response = await client.get(
            "https://hn.algolia.com/api/v1/search",
            params={
                "query": query,
                "tags": "story",
                "numericFilters": f"created_at_i>{cutoff}",
                "hitsPerPage": 10,
            },
        )
        if response.status_code >= 400:
            candidate.community_coverage["hackernews"] = (
                f"已尝试 Hacker News 搜索，但返回 HTTP {response.status_code}"
            )
            record_search(candidate, "hackernews", "failed", reason=f"http_{response.status_code}")
            return []
        payload = response.json()
        if not isinstance(payload, dict) or not isinstance(payload.get("hits"), list):
            raise ValueError("Invalid Hacker News search response")
        results = []
        for hit in payload["hits"]:
            if not _is_related(candidate, hit.get("title") or ""):
                continue
            object_id = hit.get("objectID", "")
            results.append(
                TechnicalEvidence(
                    source="hackernews",
                    evidence_type=EvidenceType.COMMUNITY_DISCUSSION,
                    title=hit.get("title") or query,
                    url=f"https://news.ycombinator.com/item?id={quote(str(object_id))}",
                    summary=hit.get("story_text") or "",
                    published_at=str(hit.get("created_at") or ""),
                    authors=[hit.get("author", "")],
                    metrics={
                        "score": hit.get("points", 0) or 0,
                        "comments": hit.get("num_comments", 0) or 0,
                    },
                    identifiers={"hackernews": str(object_id)},
                    raw={"relationship": "exact_or_mechanism_title_match", "ephemeral_content": True, "social_platform": "hackernews"},
                )
            )
        candidate.community_coverage["hackernews"] = (
            f"已执行 Hacker News 搜索；相关讨论命中 {len(results)} 条"
        )
        record_search(candidate, "hackernews", "completed", count=len(results))
        return results

    async def _x_title_search(
        self, client: httpx.AsyncClient, candidate: ParadigmCandidate
    ) -> list[TechnicalEvidence]:
        """可选的精确标题搜索；同时提供作者本人公开发布时的身份线索。"""
        if not config.KOL_X_SOURCE_ENABLED or not config.TWITTER_BEARER_TOKEN:
            record_search(candidate, "x_official", "not_configured" if config.KOL_X_SOURCE_ENABLED else "disabled", reason="no_token" if config.KOL_X_SOURCE_ENABLED else "disabled_by_config")
            candidate.community_coverage["x_official"] = (
                "X 官方 API 已关闭或未配置；未执行标题搜索，不代表零讨论"
            )
            return []
        lead_title = next(
            (
                item.title
                for item in candidate.evidence
                if item.evidence_type in ORIGIN_EVIDENCE_TYPES
            ),
            candidate.name,
        )
        phrase = re.sub(r"[\"\n\r]+", " ", lead_title).strip()[:180]
        if not phrase:
            record_search(candidate, "x_official", "failed", reason="missing_search_identity")
            return []
        response = await client.get(
            "https://api.x.com/2/tweets/search/recent",
            headers={"Authorization": f"Bearer {config.TWITTER_BEARER_TOKEN}"},
            params={
                "query": f'"{phrase}" -is:retweet',
                "max_results": 20,
                "tweet.fields": "created_at,author_id,public_metrics",
                "expansions": "author_id",
                "user.fields": "name,username,description,public_metrics,verified",
            },
        )
        if response.status_code >= 400:
            logger.warning("X 标题搜索失败: HTTP %s", response.status_code)
            candidate.community_coverage["x_official"] = (
                f"已尝试 X Recent Search，但返回 HTTP {response.status_code}"
            )
            record_search(candidate, "x_official", "failed", reason=f"http_{response.status_code}")
            return []
        payload = response.json()
        if not isinstance(payload, dict) or payload.get("errors") or not isinstance(payload.get("data", []), list) or ("data" not in payload and (payload.get("meta") or {}).get("result_count") != 0):
            raise ValueError("Invalid X search response")
        users = {
            str(user.get("id", "")): user
            for user in (payload.get("includes") or {}).get("users", [])
        }
        results = []
        for post in payload.get("data", []):
            user = users.get(str(post.get("author_id", "")), {})
            username = str(user.get("username", ""))
            post_id = str(post.get("id", ""))
            metrics = post.get("public_metrics") or {}
            user_metrics = user.get("public_metrics") or {}
            url = f"https://x.com/{username}/status/{post_id}" if username else ""
            social_name = str(user.get("name") or username)
            author_self_release = any(
                _same_person_name(social_name, author)
                for evidence in candidate.evidence
                if evidence.evidence_type in ORIGIN_EVIDENCE_TYPES
                for author in evidence.authors
            )
            results.append(
                TechnicalEvidence(
                    source="x-title-search",
                    evidence_type=EvidenceType.SECONDARY_INTERPRETATION,
                    title=f"{social_name} 讨论 {lead_title}",
                    url=url,
                    summary=str(post.get("text", "")),
                    published_at=str(post.get("created_at") or ""),
                    authors=[str(user.get("name") or username)],
                    metrics={
                        "likes": metrics.get("like_count", 0) or 0,
                        "retweets": metrics.get("retweet_count", 0) or 0,
                        "replies": metrics.get("reply_count", 0) or 0,
                        "author_followers": user_metrics.get("followers_count", 0) or 0,
                        "author_verified": bool(user.get("verified", False)),
                    },
                    identifiers={"x": post_id},
                    raw={
                        "relationship": (
                            "author_self_release"
                            if author_self_release
                            else "exact_work_title_match"
                        ),
                        "social_author_name": str(user.get("name", "")),
                        "ephemeral_content": True,
                        "social_platform": "x",
                        "social_bio": str(user.get("description", "")),
                        "social_profile_url": (
                            f"https://x.com/{username}" if username else ""
                        ),
                    },
                )
            )
        candidate.community_coverage["x_official"] = (
            f"已执行 X 精确标题搜索；相关帖子命中 {len(results)} 条"
        )
        record_search(candidate, "x_official", "completed", count=len(results))
        return results


def _is_related(candidate: ParadigmCandidate, title: str) -> bool:
    title_tokens = {
        token.lower().strip("-_/.,:()[]")
        for token in title.split()
        if len(token.strip("-_/.,:()[]")) >= 4
    }
    candidate_tokens = {
        token.lower().strip("-_/.,:()[]")
        for value in [candidate.name, *candidate.keywords]
        for token in value.split()
        if len(token.strip("-_/.,:()[]")) >= 4
    }
    required = 1 if len(candidate_tokens) <= 2 else 2
    return len(title_tokens & candidate_tokens) >= required


def _is_relevant_repository(candidate: ParadigmCandidate, repo: dict) -> bool:
    """宁可漏掉弱信号，也不把论文聚合仓库伪装成实现。"""
    full_name = str(repo.get("full_name", ""))
    description = str(repo.get("description") or "")
    text = f"{full_name} {description}".casefold()
    noise_markers = {
        "arxiv-daily",
        "arxiv_daily",
        "paper-daily",
        "paper_daily",
        "research-collection",
        "research_collection",
        "awesome-daily",
        "awesome_papers",
        "paper-list",
        "paper_list",
        "arxiv-radar",
        "rss-feed",
        "hfpaper",
    }
    if any(marker in text for marker in noise_markers):
        return False

    compact_repo = "".join(
        character
        for character in full_name.rsplit("/", 1)[-1].casefold()
        if character.isalnum()
    )
    compact_name = "".join(character for character in candidate.name.casefold() if character.isalnum())
    if min(len(compact_repo), len(compact_name)) >= 8 and (
        compact_name in compact_repo or compact_repo in compact_name
    ):
        return True

    candidate_tokens = {
        token.casefold().strip("-_/.,:()[]")
        for value in [candidate.name, candidate.route_family, *candidate.keywords]
        for token in value.split()
        if len(token.strip("-_/.,:()[]")) >= 5
    }
    overlap = {token for token in candidate_tokens if token in text}
    return len(overlap) >= 2


def _parse_datetime(value: str) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(
            timezone.utc
        )
    except ValueError:
        return None


def _same_person_name(left: str, right: str) -> bool:
    normalize = lambda value: "".join(
        character for character in value.casefold() if character.isalnum()
    )
    left_value, right_value = normalize(left), normalize(right)
    if not left_value or not right_value:
        return False
    if left_value == right_value:
        return True
    if min(len(left_value), len(right_value)) < 6:
        return False
    return SequenceMatcher(None, left_value, right_value).ratio() >= 0.9
