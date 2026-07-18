"""
微信公众号信源 — 通过本地部署的 we-mp-rss 服务获取公众号文章

依赖：本地运行的 we-mp-rss 实例（Docker 部署）
API：GET /api/v1/wx/articles?offset=0&limit=50
认证：Bearer Token 或 AK-SK（在 we-mp-rss 管理界面生成）

默认不启用。启用方式：在 .env 中设置 WECHAT_SOURCE_ENABLED=true
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

import httpx
from tenacity import retry, wait_exponential, stop_after_attempt

import config
from .base import BaseSource, RawProject

logger = logging.getLogger(__name__)


class WeChatSource(BaseSource):
    source_name = "wechat"

    def __init__(self, limit: int = 50):
        self.limit = limit
        self.base_url = config.WECHAT_RSS_BASE_URL.rstrip("/")
        self.token = config.WECHAT_RSS_TOKEN

    async def fetch(self) -> list[RawProject]:
        if not config.WECHAT_SOURCE_ENABLED:
            logger.debug("[wechat] 信源未启用，跳过")
            return []

        if not self.base_url or not self.token:
            logger.warning(
                "[wechat] 缺少配置：WECHAT_RSS_BASE_URL 或 WECHAT_RSS_TOKEN 未设置"
            )
            return []

        articles = await self._fetch_articles()
        projects: list[RawProject] = []

        for article in articles:
            title = article.get("title", "").strip()
            if not title:
                continue

            mp_name = article.get("mp_name", "未知公众号")
            link = article.get("link", "")
            content = article.get("content", "")
            publish_time = article.get("publish_time", "")

            summary = content[:1500] if content else ""

            projects.append(
                RawProject(
                    source="wechat",
                    name=title,
                    url=link or f"wechat://{mp_name}/{title}",
                    description=f"[{mp_name}] {title}",
                    readme_summary=summary,
                    stars=0,
                    language="",
                    topics=["wechat", "公众号", mp_name],
                    author=mp_name,
                    created_at=publish_time,
                    extra={
                        "mp_name": mp_name,
                        "article_id": article.get("id", ""),
                    },
                )
            )

        logger.info(f"[wechat] 获取 {len(projects)} 篇公众号文章")
        return projects

    @retry(
        wait=wait_exponential(multiplier=1, min=2, max=10),
        stop=stop_after_attempt(3),
    )
    async def _fetch_articles(self) -> list[dict]:
        headers = {
            "Authorization": f"Bearer {self.token}",
            "Accept": "application/json",
        }

        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.get(
                f"{self.base_url}/api/v1/wx/articles",
                params={"offset": 0, "limit": self.limit},
                headers=headers,
            )
            resp.raise_for_status()
            data = resp.json()

        if isinstance(data, dict):
            if "data" in data:
                articles = data["data"]
                if isinstance(articles, list):
                    return articles
                if isinstance(articles, dict) and "data" in articles:
                    return articles["data"]
            return []

        if isinstance(data, list):
            return data

        return []
