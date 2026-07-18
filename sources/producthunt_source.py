"""
Product Hunt 信源 - 通过官方 GraphQL API 获取每日 AI 产品
需要 API Token: https://api.producthunt.com/v2/docs
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

import httpx

from config import PRODUCTHUNT_TOKEN
from .base import BaseSource, RawProject

logger = logging.getLogger(__name__)

PH_API = "https://api.producthunt.com/v2/api/graphql"

AI_TOPIC_KEYWORDS = {
    "artificial intelligence", "machine learning", "developer tools",
    "ai", "natural language processing", "chatbot", "generative ai",
    "large language model", "llm", "data science", "agent"
}

POSTS_QUERY = """
query ($postedAfter: DateTime!, $first: Int!) {
  posts(order: VOTES, postedAfter: $postedAfter, first: $first) {
    edges {
      node {
        id
        name
        tagline
        description
        url
        website
        votesCount
        createdAt
        topics {
          edges {
            node {
              name
            }
          }
        }
        makers {
          name
          headline
        }
        thumbnail {
          url
        }
      }
    }
  }
}
"""


class ProductHuntSource(BaseSource):
    source_name = "producthunt"

    def __init__(self, lookback_days: int = 1, limit: int = 50):
        self.lookback_days = lookback_days
        self.limit = limit

    async def fetch(self) -> list[RawProject]:
        if not PRODUCTHUNT_TOKEN:
            logger.info("PRODUCTHUNT_TOKEN 未配置，跳过 Product Hunt 信源")
            return []

        posted_after = (
            datetime.now(timezone.utc) - timedelta(days=self.lookback_days)
        ).isoformat()

        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(
                PH_API,
                json={
                    "query": POSTS_QUERY,
                    "variables": {
                        "postedAfter": posted_after,
                        "first": self.limit,
                    },
                },
                headers={
                    "Authorization": f"Bearer {PRODUCTHUNT_TOKEN}",
                    "Content-Type": "application/json",
                },
            )
            resp.raise_for_status()
            data = resp.json()

        edges = (
            data.get("data", {}).get("posts", {}).get("edges", [])
        )

        results: list[RawProject] = []
        for edge in edges:
            node = edge.get("node", {})
            topic_edges = node.get("topics", {}).get("edges", [])
            topics = [
                t.get("node", {}).get("name", "")
                for t in topic_edges
                if t.get("node") and t["node"].get("name")
            ]

            if not self._is_ai_related(topics, node.get("tagline", ""), node.get("description", "")):
                continue

            makers = node.get("makers", [])
            maker_str = ", ".join(m.get("name", "") for m in makers[:3]) if makers else ""

            results.append(
                RawProject(
                    source=self.source_name,
                    name=node.get("name", ""),
                    url=node.get("website") or node.get("url", ""),
                    description=node.get("tagline", ""),
                    readme_summary=node.get("description", "")[:3000],
                    stars=node.get("votesCount", 0),
                    author=maker_str,
                    topics=topics,
                    created_at=node.get("createdAt", ""),
                    extra={
                        "ph_url": node.get("url", ""),
                        "type": "product",
                    },
                )
            )

        return results

    @staticmethod
    def _is_ai_related(topics: list[str], tagline: str, description: str) -> bool:
        combined_text = " ".join(topics).lower() + " " + tagline.lower() + " " + description.lower()
        return any(kw in combined_text for kw in AI_TOPIC_KEYWORDS)
