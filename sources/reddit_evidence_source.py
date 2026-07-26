"""Reddit 官方 OAuth Data API：搜索帖子并读取少量高赞评论。"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from datetime import datetime, timedelta, timezone

import httpx

import config
from paradigms.models import EvidenceType, ParadigmCandidate, TechnicalEvidence

logger = logging.getLogger(__name__)
REDDIT_TOKEN_URL = "https://www.reddit.com/api/v1/access_token"
REDDIT_API_BASE = "https://oauth.reddit.com"


class RedditEvidenceClient:
    """仅在用户明确确认已获 Reddit Data API 批准后调用。"""

    def __init__(self):
        self._access_token = ""
        self._expires_at = 0.0
        self._token_lock = asyncio.Lock()

    @property
    def configured(self) -> bool:
        return bool(
            config.REDDIT_API_ACCESS_APPROVED
            and config.REDDIT_CLIENT_ID
            and config.REDDIT_CLIENT_SECRET
            and config.REDDIT_USER_AGENT
        )

    async def search(
        self,
        client: httpx.AsyncClient,
        candidate: ParadigmCandidate,
    ) -> list[TechnicalEvidence]:
        if not self.configured:
            return []
        token = await self._token(client)
        if not token:
            candidate.community_coverage["reddit_official"] = (
                "已配置 Reddit Data API，但本轮 OAuth Token 获取失败"
            )
            return []
        work_title, identifier = _search_identity(candidate)
        if not work_title:
            return []
        query = f'"{_clean_query(work_title)}"'
        if identifier:
            query = f'{query} OR "{_clean_query(identifier)}"'
        headers = self._headers(token)
        response = await client.get(
            f"{REDDIT_API_BASE}/search",
            headers=headers,
            params={
                "q": query,
                "sort": "new",
                "t": _reddit_period(config.SOURCING_LOOKBACK_DAYS),
                "limit": 25,
                "type": "link",
                "raw_json": 1,
            },
        )
        if response.status_code >= 400:
            logger.warning("Reddit 官方搜索失败: HTTP %s", response.status_code)
            candidate.community_coverage["reddit_official"] = (
                f"已尝试 Reddit OAuth 搜索，但返回 HTTP {response.status_code}"
            )
            return []
        cutoff = datetime.now(timezone.utc) - timedelta(
            days=config.SOURCING_LOOKBACK_DAYS
        )
        matches = []
        for child in ((response.json().get("data") or {}).get("children") or []):
            post = child.get("data") or {}
            if not _post_is_related(candidate, work_title, post):
                continue
            created = datetime.fromtimestamp(
                float(post.get("created_utc", 0) or 0), timezone.utc
            )
            if created < cutoff:
                continue
            matches.append(post)
        comment_batches = await asyncio.gather(
            *(self._top_comments(client, headers, post) for post in matches[:5]),
            return_exceptions=True,
        )
        results = []
        for post, comments in zip(matches[:5], comment_batches):
            if isinstance(comments, Exception):
                comments = []
            post_id = str(post.get("id", ""))
            permalink = str(post.get("permalink", ""))
            summary_parts = [str(post.get("selftext", "")).strip()]
            summary_parts.extend(comments)
            results.append(
                TechnicalEvidence(
                    source="reddit",
                    evidence_type=EvidenceType.COMMUNITY_DISCUSSION,
                    title=str(post.get("title", "")) or "Reddit 技术讨论",
                    url=f"https://www.reddit.com{permalink}" if permalink else "",
                    summary="\n\n".join(value for value in summary_parts if value)[:6000],
                    published_at=datetime.fromtimestamp(
                        float(post.get("created_utc", 0) or 0), timezone.utc
                    ).isoformat(),
                    authors=[str(post.get("author", ""))],
                    metrics={
                        "score": int(post.get("score", 0) or 0),
                        "comments": int(post.get("num_comments", 0) or 0),
                        "upvote_ratio": float(post.get("upvote_ratio", 0) or 0),
                    },
                    identifiers={"reddit": post_id},
                    raw={
                        "relationship": "official_reddit_exact_work_search",
                        "subreddit": str(post.get("subreddit", "")),
                        "ephemeral_content": True,
                        "retention_policy": "scrub_after_synthesis",
                    },
                )
            )
        candidate.community_coverage["reddit_official"] = (
            f"已执行 Reddit OAuth 搜索；相关讨论命中 {len(results)} 条"
        )
        return results

    async def _token(self, client: httpx.AsyncClient) -> str:
        if self._access_token and time.monotonic() < self._expires_at:
            return self._access_token
        async with self._token_lock:
            if self._access_token and time.monotonic() < self._expires_at:
                return self._access_token
            response = await client.post(
                REDDIT_TOKEN_URL,
                auth=(config.REDDIT_CLIENT_ID, config.REDDIT_CLIENT_SECRET),
                headers={"User-Agent": config.REDDIT_USER_AGENT},
                data={"grant_type": "client_credentials"},
            )
            if response.status_code >= 400:
                logger.warning("Reddit OAuth 获取失败: HTTP %s", response.status_code)
                return ""
            payload = response.json()
            self._access_token = str(payload.get("access_token", ""))
            expires_in = max(float(payload.get("expires_in", 3600) or 3600), 60.0)
            self._expires_at = time.monotonic() + expires_in - 30
            return self._access_token

    async def _top_comments(
        self,
        client: httpx.AsyncClient,
        headers: dict[str, str],
        post: dict,
    ) -> list[str]:
        post_id = str(post.get("id", ""))
        if not post_id:
            return []
        response = await client.get(
            f"{REDDIT_API_BASE}/comments/{post_id}",
            headers=headers,
            params={"sort": "top", "limit": 10, "depth": 1, "raw_json": 1},
        )
        if response.status_code >= 400:
            return []
        payload = response.json()
        if not isinstance(payload, list) or len(payload) < 2:
            return []
        children = ((payload[1].get("data") or {}).get("children") or [])
        comments = []
        for child in children:
            value = str((child.get("data") or {}).get("body", "")).strip()
            if value and value not in {"[deleted]", "[removed]"}:
                comments.append(value[:1200])
            if len(comments) >= 5:
                break
        return comments

    @staticmethod
    def _headers(token: str) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {token}",
            "User-Agent": config.REDDIT_USER_AGENT,
        }


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


def _post_is_related(
    candidate: ParadigmCandidate,
    work_title: str,
    post: dict,
) -> bool:
    text = " ".join(
        str(value)
        for value in (
            post.get("title", ""),
            post.get("selftext", ""),
            post.get("url", ""),
        )
    )
    normalized = _normalize(text)
    if len(_normalize(work_title)) >= 12 and _normalize(work_title) in normalized:
        return True
    identifiers = {
        _normalize(value)
        for evidence in candidate.evidence
        for key, value in evidence.identifiers.items()
        if key in {"arxiv", "doi"} and value
    }
    if any(len(value) >= 6 and value in normalized for value in identifiers):
        return True
    tokens = {
        token.casefold()
        for value in [candidate.name, candidate.route_family, *candidate.keywords]
        for token in re.findall(r"[A-Za-z0-9][A-Za-z0-9-]{3,}", value)
    }
    return sum(token in text.casefold() for token in tokens) >= 2


def _reddit_period(days: int) -> str:
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
