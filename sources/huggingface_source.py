"""
HuggingFace 信源 - 通过公开 REST API 拉取“近期 + 热度”模型与空间

策略（面向 VC 早期项目）：
1) 优先近期活跃：按 lastModified 拉取并过滤最近窗口
2) 再补充热度：按 likes 拉取，避免完全错过高势能项目
3) 双池去重并打标签，供后续 LLM 筛选
"""

from __future__ import annotations

import logging

import httpx

from .base import BaseSource, RawProject
from tenacity import retry, wait_exponential, stop_after_attempt, retry_if_exception_type

logger = logging.getLogger(__name__)

HF_API = "https://huggingface.co/api"


class HuggingFaceSource(BaseSource):
    source_name = "huggingface"

    def __init__(self, limit: int = 30, recent_days: int = 14):
        self.limit = limit
        self.recent_days = max(recent_days, 1)
        self.min_recent_likes = 2
        self.min_recent_downloads = 200

    async def fetch(self) -> list[RawProject]:
        projects: list[RawProject] = []
        async with httpx.AsyncClient(timeout=30, follow_redirects=True) as client:
            models = await self._fetch_models(client)
            spaces = await self._fetch_spaces(client)
            projects.extend(models)
            projects.extend(spaces)
        return projects

    @retry(
        wait=wait_exponential(multiplier=1, min=4, max=10),
        stop=stop_after_attempt(3),
        retry=retry_if_exception_type(httpx.HTTPError),
        before_sleep=lambda retry_state: logger.warning(
            f"HuggingFace Models 获取失败，重试... (异常: {retry_state.outcome.exception()})"
        )
    )
    async def _fetch_models(
        self, client: httpx.AsyncClient
    ) -> list[RawProject]:
        # Pool A: 近期热门（trendingScore）
        recent_resp = await client.get(
            f"{HF_API}/models",
            params={
                "sort": "trendingScore",
                "direction": "-1",
                "limit": max(self.limit * 2, 40),
                "filter": "text-generation",
            },
        )
        recent_resp.raise_for_status()

        # Pool B: 高热度补充（likes）
        hot_resp = await client.get(
            f"{HF_API}/models",
            params={
                "sort": "likes",
                "direction": "-1",
                "limit": self.limit,
                "filter": "text-generation",
            },
        )
        hot_resp.raise_for_status()

        results = []
        seen: set[str] = set()
        cutoff_ts = self._days_ago_ts(self.recent_days)

        # 先放近期池，再补热度池
        for item in recent_resp.json() + hot_resp.json():
            model_id = item.get("modelId", item.get("id", ""))
            if not model_id or model_id in seen:
                continue

            likes = item.get("likes", 0) or 0
            downloads = item.get("downloads", 0) or 0
            last_modified = item.get("lastModified", "") or ""
            is_recent = self._is_recent(last_modified, cutoff_ts)

            # 近期池做轻过滤，避免把大量 0 互动噪声送进 LLM
            if is_recent and likes < self.min_recent_likes and downloads < self.min_recent_downloads:
                continue

            # 非近期样本仅保留“显著热度”头部，作为趋势参照
            if not is_recent and likes < 500:
                continue

            seen.add(model_id)
            results.append(
                RawProject(
                    source="huggingface-model",
                    name=model_id,
                    url=f"https://huggingface.co/{model_id}",
                    description=item.get("pipeline_tag", ""),
                    stars=likes,
                    author=model_id.split("/")[0] if "/" in model_id else "",
                    topics=item.get("tags", [])[:10],
                    created_at=item.get("createdAt", ""),
                    extra={
                        "downloads": downloads,
                        "last_modified": last_modified,
                        "is_recent": is_recent,
                        "type": "model",
                        "fetch_full_readme_func": lambda n=model_id: self.fetch_full_readme(n)
                    },
                )
            )
        return results

    @retry(
        wait=wait_exponential(multiplier=1, min=4, max=10),
        stop=stop_after_attempt(3),
        retry=retry_if_exception_type(httpx.HTTPError),
        before_sleep=lambda retry_state: logger.warning(
            f"HuggingFace Spaces 获取失败，重试... (异常: {retry_state.outcome.exception()})"
        )
    )
    async def _fetch_spaces(
        self, client: httpx.AsyncClient
    ) -> list[RawProject]:
        # Spaces 也采用“近期热门 + 历史热度”双池
        recent_resp = await client.get(
            f"{HF_API}/spaces",
            params={"sort": "trendingScore", "direction": "-1", "limit": 40},
        )
        recent_resp.raise_for_status()

        hot_resp = await client.get(
            f"{HF_API}/spaces",
            params={"sort": "likes", "direction": "-1", "limit": 20},
        )
        hot_resp.raise_for_status()

        results = []
        seen: set[str] = set()
        cutoff_ts = self._days_ago_ts(self.recent_days)

        for item in recent_resp.json() + hot_resp.json():
            space_id = item.get("id", "")
            if not space_id or space_id in seen:
                continue

            likes = item.get("likes", 0) or 0
            last_modified = item.get("lastModified", "") or ""
            is_recent = self._is_recent(last_modified, cutoff_ts)

            # 近期样本：至少有最基本互动；历史样本：需要明显热度
            if is_recent and likes < 2:
                continue
            if not is_recent and likes < 150:
                continue

            seen.add(space_id)
            results.append(
                RawProject(
                    source="huggingface-space",
                    name=space_id,
                    url=f"https://huggingface.co/spaces/{space_id}",
                    description=item.get("cardData", {}).get("short_description", "")
                    if isinstance(item.get("cardData"), dict)
                    else "",
                    stars=likes,
                    author=space_id.split("/")[0] if "/" in space_id else "",
                    topics=item.get("tags", [])[:10],
                    created_at=item.get("createdAt", ""),
                    extra={
                        "last_modified": last_modified,
                        "is_recent": is_recent,
                        "type": "space",
                        "fetch_full_readme_func": lambda n=f"spaces/{space_id}": self.fetch_full_readme(n)
                    },
                )
            )
        return results

    async def fetch_full_readme(self, model_id: str) -> str:
        """On-Demand 获取完整未截断的 README (用于深度分析)"""
        async with httpx.AsyncClient(timeout=30, follow_redirects=True) as client:
            return await self._fetch_readme(client, model_id, truncate=False)

    @retry(
        wait=wait_exponential(multiplier=1, min=2, max=5),
        stop=stop_after_attempt(3),
        retry=retry_if_exception_type(httpx.HTTPError),
        before_sleep=lambda retry_state: logger.warning(
            f"HuggingFace README 获取失败，重试... (异常: {retry_state.outcome.exception()})"
        )
    )
    async def _fetch_readme(self, client: httpx.AsyncClient, repo_id: str, truncate: bool = True) -> str:
        try:
            resp = await client.get(f"https://huggingface.co/{repo_id}/resolve/main/README.md")
            if resp.status_code == 200:
                return resp.text[:3000] if truncate else resp.text
        except Exception:
            logger.debug(f"无法获取 {repo_id} 的 README")
        return ""

    @staticmethod
    def _days_ago_ts(days: int) -> float:
        import time
        return time.time() - days * 24 * 3600

    @staticmethod
    def _is_recent(iso_text: str, cutoff_ts: float) -> bool:
        if not iso_text:
            return False
        try:
            # 格式示例: 2026-03-07T01:45:43.000Z
            from datetime import datetime
            dt = datetime.fromisoformat(iso_text.replace("Z", "+00:00"))
            return dt.timestamp() >= cutoff_ts
        except Exception:
            return False
