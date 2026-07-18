"""
Twitter/X 信源 - 通过 Twitter API v2 搜索 AI 领域动态

注意: Twitter API 免费层不支持读取推文，需要 Basic 层 ($100/月) 或更高。
如果未配置 TWITTER_BEARER_TOKEN，此信源将自动跳过。
策略: 追踪精选 AI 领域大牛账号的最新推文，而非全量关键词搜索，以节省 API 配额。
"""

from __future__ import annotations

import logging

import httpx

from config import TWITTER_BEARER_TOKEN, TWITTER_WATCH_ACCOUNTS
from .base import BaseSource, RawProject

logger = logging.getLogger(__name__)

TWITTER_API = "https://api.twitter.com/2"

FILTER_KEYWORDS = [
    "building", "launched", "launching", "open source", "stealth",
    "raising", "founded", "startup", "demo", "release", "announcing",
    "agent", "llm", "model", "ai",
]


class TwitterSource(BaseSource):
    source_name = "twitter"

    def __init__(self, max_results_per_query: int = 20):
        self.max_results = max_results_per_query

    async def fetch(self) -> list[RawProject]:
        if not TWITTER_BEARER_TOKEN:
            logger.info(
                "TWITTER_BEARER_TOKEN 未配置，跳过 Twitter 信源"
                "（需要 Basic 层 $100/月）"
            )
            return []

        headers = {
            "Authorization": f"Bearer {TWITTER_BEARER_TOKEN}",
        }

        query_parts = []
        if TWITTER_WATCH_ACCOUNTS:
            from_clauses = " OR ".join(
                f"from:{acct}" for acct in TWITTER_WATCH_ACCOUNTS[:15]
            )
            query_parts.append(f"({from_clauses})")
        else:
            query_parts.append(
                "(AI OR LLM OR agent OR startup OR launched) (building OR demo OR release)"
            )

        query_parts.append("-is:retweet")
        query = " ".join(query_parts)

        async with httpx.AsyncClient(timeout=30) as client:
            try:
                resp = await client.get(
                    f"{TWITTER_API}/tweets/search/recent",
                    params={
                        "query": query,
                        "max_results": min(self.max_results, 100),
                        "tweet.fields": "created_at,author_id,public_metrics,entities",
                        "expansions": "author_id",
                        "user.fields": "name,username,description",
                    },
                    headers=headers,
                )
                resp.raise_for_status()
                data = resp.json()
            except httpx.HTTPStatusError as e:
                if e.response.status_code == 403:
                    logger.warning(
                        "Twitter API 返回 403: 免费层不支持搜索，需要 Basic 层订阅"
                    )
                else:
                    logger.warning(f"Twitter API 请求失败: {e}")
                return []

        users_map: dict[str, dict] = {}
        for user in data.get("includes", {}).get("users", []):
            users_map[user["id"]] = user

        results: list[RawProject] = []
        for tweet in data.get("data", []):
            text = tweet.get("text", "")

            if not any(kw in text.lower() for kw in FILTER_KEYWORDS):
                continue

            author_id = tweet.get("author_id", "")
            user = users_map.get(author_id, {})
            username = user.get("username", "")
            metrics = tweet.get("public_metrics", {})

            urls = []
            for entity_url in tweet.get("entities", {}).get("urls", []):
                expanded = entity_url.get("expanded_url", "")
                if expanded and "twitter.com" not in expanded and "t.co" not in expanded:
                    urls.append(expanded)

            tweet_url = f"https://x.com/{username}/status/{tweet['id']}" if username else ""

            results.append(
                RawProject(
                    source=self.source_name,
                    name=f"@{username}: {text[:80]}..." if len(text) > 80 else f"@{username}: {text}",
                    url=urls[0] if urls else tweet_url,
                    description=text,
                    stars=metrics.get("like_count", 0) + metrics.get("retweet_count", 0),
                    author=f"{user.get('name', '')} (@{username})",
                    created_at=tweet.get("created_at", ""),
                    extra={
                        "tweet_url": tweet_url,
                        "external_urls": urls,
                        "likes": metrics.get("like_count", 0),
                        "retweets": metrics.get("retweet_count", 0),
                        "user_bio": user.get("description", ""),
                        "type": "tweet",
                    },
                )
            )

        return results
