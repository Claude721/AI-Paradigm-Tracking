"""把 GitHub 与 Hacker News 从“候选生成器”降级为范式扩散证据。"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from datetime import datetime, timedelta, timezone
from difflib import SequenceMatcher
from urllib.parse import quote

import httpx

import config
from paradigms.models import EvidenceType, ParadigmCandidate, TechnicalEvidence
from sources.reddit_evidence_source import RedditEvidenceClient
from sources.social_web_search_source import SocialWebSearchClient

logger = logging.getLogger(__name__)


class CommunityEvidenceClient:
    def __init__(self):
        self.social_web = SocialWebSearchClient()
        self.reddit = RedditEvidenceClient()
        self._github_lock = asyncio.Lock()
        self._github_last_request_at = 0.0
        self._github_circuit_open = False
        self._github_failure_logged = False

    async def search(self, candidate: ParadigmCandidate) -> list[TechnicalEvidence]:
        async with httpx.AsyncClient(timeout=30, follow_redirects=True) as client:
            batches = await asyncio.gather(
                self._github(client, candidate),
                self._hackernews(client, candidate),
                self.social_web.search(client, candidate),
                self.reddit.search(client, candidate),
                self._x_title_search(client, candidate),
                return_exceptions=True,
            )
        results = []
        for source_name, batch in zip(
            ("GitHub", "Hacker News", "Tavily", "Reddit", "X"), batches
        ):
            if isinstance(batch, Exception):
                logger.warning("%s 范式证据搜索失败: %s", source_name, batch)
                continue
            results.extend(batch)
        return results

    def coverage(self) -> dict[str, str]:
        return {
            "github": (
                "已配置 Token，并以搜索 API 限额串行查询；限流后本轮自动熔断"
                if config.GITHUB_TOKEN
                else "未配置 GitHub Token；本轮不调用匿名 Search API"
            ),
            "hackernews": "已尝试 Algolia 搜索；单次失败会降级",
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
                if config.TWITTER_BEARER_TOKEN
                else "未配置 X API；不得把未检出解释为零讨论"
            ),
        }

    async def _github(
        self, client: httpx.AsyncClient, candidate: ParadigmCandidate
    ) -> list[TechnicalEvidence]:
        if not config.GITHUB_TOKEN or self._github_circuit_open:
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
            return []
        results = []
        cutoff = datetime.now(timezone.utc) - timedelta(
            days=config.SOURCING_LOOKBACK_DAYS
        )
        for repo in response.json().get("items", []):
            if not _is_relevant_repository(candidate, repo):
                continue
            updated_at = str(repo.get("updated_at", ""))
            if (updated := _parse_datetime(updated_at)) and updated < cutoff:
                continue
            results.append(
                TechnicalEvidence(
                    source="github",
                    evidence_type=EvidenceType.IMPLEMENTATION,
                    title=repo.get("full_name", ""),
                    url=repo.get("html_url", ""),
                    summary=repo.get("description") or "",
                    published_at=updated_at or repo.get("created_at", ""),
                    authors=[(repo.get("owner") or {}).get("login", "")],
                    metrics={
                        "stars": repo.get("stargazers_count", 0) or 0,
                        "forks": repo.get("forks_count", 0) or 0,
                    },
                    identifiers={"github": repo.get("full_name", "")},
                    raw={
                        "updated_at": repo.get("updated_at", ""),
                        "created_at": repo.get("created_at", ""),
                        "relationship": "name_and_mechanism_match",
                        "independence": "unverified",
                    },
                )
            )
        return results

    async def _hackernews(
        self, client: httpx.AsyncClient, candidate: ParadigmCandidate
    ) -> list[TechnicalEvidence]:
        cutoff = int(
            (datetime.now(timezone.utc) - timedelta(days=config.SOURCING_LOOKBACK_DAYS)).timestamp()
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
            return []
        results = []
        for hit in response.json().get("hits", []):
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
                    published_at=hit.get("created_at", ""),
                    authors=[hit.get("author", "")],
                    metrics={
                        "score": hit.get("points", 0) or 0,
                        "comments": hit.get("num_comments", 0) or 0,
                    },
                    identifiers={"hackernews": str(object_id)},
                    raw={"relationship": "exact_or_mechanism_title_match"},
                )
            )
        return results

    async def _x_title_search(
        self, client: httpx.AsyncClient, candidate: ParadigmCandidate
    ) -> list[TechnicalEvidence]:
        """可选的精确标题搜索；同时提供作者本人公开发布时的身份线索。"""
        if not config.TWITTER_BEARER_TOKEN:
            return []
        lead_title = next(
            (
                item.title
                for item in candidate.evidence
                if item.evidence_type
                in {EvidenceType.PRIMARY_PAPER, EvidenceType.TECHNICAL_BLOG}
            ),
            candidate.name,
        )
        phrase = re.sub(r"[\"\n\r]+", " ", lead_title).strip()[:180]
        if not phrase:
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
            return []
        payload = response.json()
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
                if evidence.evidence_type
                in {EvidenceType.PRIMARY_PAPER, EvidenceType.TECHNICAL_BLOG}
                for author in evidence.authors
            )
            results.append(
                TechnicalEvidence(
                    source="x-title-search",
                    evidence_type=EvidenceType.SECONDARY_INTERPRETATION,
                    title=f"{social_name} 讨论 {lead_title}",
                    url=url,
                    summary=str(post.get("text", "")),
                    published_at=str(post.get("created_at", "")),
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
                        "social_bio": str(user.get("description", "")),
                        "social_profile_url": (
                            f"https://x.com/{username}" if username else ""
                        ),
                    },
                )
            )
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
