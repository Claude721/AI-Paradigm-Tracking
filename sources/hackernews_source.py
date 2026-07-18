"""
Hacker News 信源 - 通过 Firebase 公开 API 获取 "Show HN" 板块的 AI 项目
"""

from __future__ import annotations

import asyncio
import logging

import httpx

from .base import BaseSource, RawProject

logger = logging.getLogger(__name__)

HN_API = "https://hacker-news.firebaseio.com/v0"

AI_KEYWORDS = [
    "llm", "agent", "large-language-model", "generative-ai",
    "rag", "fine-tuning", "multimodal", "text-generation",
    "reasoning", "ai-agent", "mcp", "vlm", "image-generation", "video-generation",
    "agentskill", "agentic", "agentic-ai", "agentic-ai-framework", "agentic-ai-agent", "agentic-ai-mcp", "agentic-ai-vlm", "agentic-ai-image-generation", "agentic-ai-video-generation",
]

BATCH_SIZE = 20


class HackerNewsSource(BaseSource):
    source_name = "hackernews"

    def __init__(self, top_n: int = 100, lookback_hours: int = 72):
        self.top_n = top_n
        self.lookback_hours = max(lookback_hours, 1)

    async def fetch(self) -> list[RawProject]:
        async with httpx.AsyncClient(timeout=20) as client:
            # 直接拉 Show HN 列表，更贴合“新项目曝光”
            resp = await client.get(f"{HN_API}/showstories.json")
            resp.raise_for_status()
            story_ids: list[int] = resp.json()[: self.top_n]

            stories: list[dict | None] = []
            for i in range(0, len(story_ids), BATCH_SIZE):
                batch = story_ids[i : i + BATCH_SIZE]
                batch_results = await asyncio.gather(
                    *[self._fetch_item(client, sid) for sid in batch],
                    return_exceptions=True,
                )
                for r in batch_results:
                    if isinstance(r, dict):
                        stories.append(r)

        results: list[RawProject] = []
        cutoff_ts = self._now_unix() - self.lookback_hours * 3600
        for story in stories:
            if story is None:
                continue

            title: str = story.get("title", "")
            is_show_hn = title.startswith("Show HN")
            has_ai_keyword = any(kw in title.lower() for kw in AI_KEYWORDS)
            story_ts = story.get("time", 0) or 0

            if not (is_show_hn and has_ai_keyword):
                continue
            if story_ts and story_ts < cutoff_ts:
                continue

            results.append(
                RawProject(
                    source=self.source_name,
                    name=title.replace("Show HN: ", "").replace("Show HN:", "").strip(),
                    url=story.get("url", f"https://news.ycombinator.com/item?id={story.get('id')}"),
                    description=title,
                    stars=story.get("score", 0),
                    author=story.get("by", ""),
                    created_at=str(story.get("time", "")),
                    extra={
                        "hn_id": story.get("id"),
                        "comments": story.get("descendants", 0),
                        "type": "show_hn",
                    },
                )
            )

        return results

    @staticmethod
    def _now_unix() -> int:
        import time
        return int(time.time())

    @staticmethod
    async def _fetch_item(client: httpx.AsyncClient, item_id: int) -> dict | None:
        try:
            resp = await client.get(f"{HN_API}/item/{item_id}.json")
            resp.raise_for_status()
            return resp.json()
        except Exception:
            return None
