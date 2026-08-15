"""
Follow Builders 信源 — 追踪 AI 领域顶级建造者的社交媒体动态

支持两种运行模式（通过 .env 配置）：

方案 A（默认）：直接从原作者 GitHub 仓库读取已抓取的 JSON Feed
  - 零成本，无需任何 API Key
  - 依赖原作者每日更新

方案 B（自建）：Fork 仓库后，本地运行 generate-feed.js 抓取数据
  - 需要配置 X_BEARER_TOKEN 和 SUPADATA_API_KEY
  - 完全自主，不依赖第三方

信源内容包含三类：
  1. X/Twitter：25 位精选 AI Builder 的最新推文
  2. Podcasts：顶级 AI 播客的 YouTube 字幕/摘要
  3. Blogs：Anthropic Engineering / Claude Blog 的完整文章
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path

import httpx
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

import config
from .base import BaseSource, RawProject

logger = logging.getLogger(__name__)

# 原作者仓库的 raw URL（方案 A 默认源）
DEFAULT_FEED_BASE = "https://raw.githubusercontent.com/zarazhangrui/follow-builders/main"


class FollowBuildersSource(BaseSource):
    """从 follow-builders 的 JSON Feed 中获取 AI Builder 动态"""

    source_name = "follow-builders"

    def __init__(self):
        self.feed_base_url = (
            config.FOLLOW_BUILDERS_FEED_URL.rstrip("/")
            if config.FOLLOW_BUILDERS_FEED_URL
            else DEFAULT_FEED_BASE
        )
        self.completed_feeds = 0
        self.failed_feeds = 0
        self.missing_feeds = 0

    async def safe_fetch(self) -> list[RawProject]:
        if not config.FOLLOW_BUILDERS_ENABLED:
            self.fetch_status = "not_configured"
            self.fetch_error = ""
            return []
        try:
            projects = await self.fetch()
        except Exception as exc:
            self.fetch_status = "query_failed"
            self.fetch_error = type(exc).__name__
            logger.exception("[follow-builders] 获取失败")
            return []
        unavailable = self.failed_feeds + self.missing_feeds
        if unavailable and not self.completed_feeds:
            self.fetch_status = "query_failed"
            self.fetch_error = (
                "AllFeedsFailed"
                if self.failed_feeds and not self.missing_feeds
                else "AllFeedsMissing"
                if self.missing_feeds and not self.failed_feeds
                else "AllFeedsUnavailable"
            )
        elif unavailable:
            self.fetch_status = "partial"
            self.fetch_error = (
                "PartialFeedFailure"
                if self.failed_feeds
                else "PartialFeedMissing"
            )
        else:
            self.fetch_status = "completed"
            self.fetch_error = ""
        return projects

    async def fetch(self) -> list[RawProject]:
        if not config.FOLLOW_BUILDERS_ENABLED:
            logger.debug("[follow-builders] 信源未启用，跳过")
            return []

        self.completed_feeds = 0
        self.failed_feeds = 0
        self.missing_feeds = 0
        projects: list[RawProject] = []

        feeds: dict[str, dict | None] = {}
        for filename in ("feed-x.json", "feed-podcasts.json", "feed-blogs.json"):
            try:
                payload = await self._fetch_json(filename)
                feeds[filename] = payload
                if payload is None:
                    # 单个可选内容类型可以暂时不存在，但全部 404 通常表示
                    # base URL 配错，不能伪装成一次成功的零命中。
                    self.missing_feeds += 1
                else:
                    self.completed_feeds += 1
            except Exception as exc:
                self.failed_feeds += 1
                feeds[filename] = None
                logger.warning("[follow-builders] 获取 %s 失败: %s", filename, exc)

        feed_x = feeds["feed-x.json"]
        feed_podcasts = feeds["feed-podcasts.json"]
        feed_blogs = feeds["feed-blogs.json"]

        if feed_x:
            projects.extend(self._parse_x_feed(feed_x))
        if feed_podcasts:
            projects.extend(self._parse_podcast_feed(feed_podcasts))
        if feed_blogs:
            projects.extend(self._parse_blog_feed(feed_blogs))

        logger.info(
            f"[follow-builders] 获取 {len(projects)} 条内容 "
            f"(tweets={len(self._parse_x_feed(feed_x)) if feed_x else 0}, "
            f"podcasts={len(self._parse_podcast_feed(feed_podcasts)) if feed_podcasts else 0}, "
            f"blogs={len(self._parse_blog_feed(feed_blogs)) if feed_blogs else 0})"
        )
        return projects

    @retry(
        wait=wait_exponential(multiplier=1, min=2, max=10),
        stop=stop_after_attempt(3),
        retry=retry_if_exception_type(httpx.HTTPError),
    )
    async def _fetch_json(self, filename: str) -> dict | None:
        base = self.feed_base_url
        if base.startswith("file://"):
            return self._read_local_json(base[7:], filename)

        url = f"{base}/{filename}"
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.get(url)
            if resp.status_code == 404:
                logger.debug(f"[follow-builders] {filename} 不存在 (404)")
                return None
            resp.raise_for_status()
            return resp.json()

    @staticmethod
    def _read_local_json(directory: str, filename: str) -> dict | None:
        path = Path(directory) / filename
        if not path.exists():
            logger.debug(f"[follow-builders] 本地文件不存在: {path}")
            return None
        return json.loads(path.read_text(encoding="utf-8"))

    def _parse_x_feed(self, feed: dict) -> list[RawProject]:
        projects: list[RawProject] = []
        for builder in feed.get("x", []):
            name = builder.get("name", "")
            handle = builder.get("handle", "")
            bio = builder.get("bio", "")
            tweets = builder.get("tweets", [])

            for tweet in tweets:
                text = tweet.get("text", "").strip()
                if not text:
                    continue

                likes = tweet.get("likes", 0)
                retweets = tweet.get("retweets", 0)
                replies = tweet.get("replies", 0)
                engagement = likes + retweets * 3 + replies * 2

                projects.append(
                    RawProject(
                        source="follow-builders-x",
                        name=f"@{handle}: {text[:60]}",
                        url=tweet.get("url", f"https://x.com/{handle}"),
                        description=text,
                        readme_summary=f"[{name} (@{handle})]\n{bio}\n\n{text}",
                        stars=engagement,
                        language="",
                        topics=["twitter", "ai-builder", handle],
                        author=f"{name} (@{handle})",
                        created_at=tweet.get("createdAt", ""),
                        extra={
                            "likes": likes,
                            "retweets": retweets,
                            "replies": replies,
                            "builder_bio": bio,
                            "tweet_id": tweet.get("id", ""),
                        },
                    )
                )

        return projects

    def _parse_podcast_feed(self, feed: dict) -> list[RawProject]:
        projects: list[RawProject] = []
        for episode in feed.get("podcasts", []):
            title = episode.get("title", "").strip()
            if not title:
                continue

            podcast_name = episode.get("name", "")
            transcript = episode.get("transcript", "")
            url = episode.get("url", "")

            projects.append(
                RawProject(
                    source="follow-builders-podcast",
                    name=f"[{podcast_name}] {title}",
                    url=url,
                    description=f"AI 播客《{podcast_name}》最新一期：{title}",
                    readme_summary=transcript[:6000] if transcript else title,
                    stars=0,
                    language="",
                    topics=["podcast", "ai-podcast", podcast_name.lower().replace(" ", "-")],
                    author=podcast_name,
                    created_at=episode.get("publishedAt", ""),
                    extra={
                        "video_id": episode.get("videoId", ""),
                        "podcast_name": podcast_name,
                    },
                )
            )

        return projects

    def _parse_blog_feed(self, feed: dict) -> list[RawProject]:
        projects: list[RawProject] = []
        for article in feed.get("blogs", []):
            title = article.get("title", "").strip()
            if not title:
                continue

            blog_name = article.get("name", "")
            content = article.get("content", "")
            url = article.get("url", "")
            author = article.get("author", "")
            description = article.get("description", "")

            projects.append(
                RawProject(
                    source="follow-builders-blog",
                    name=f"[{blog_name}] {title}",
                    url=url,
                    description=description or f"{blog_name} 技术博客：{title}",
                    readme_summary=content[:6000] if content else title,
                    stars=0,
                    language="",
                    topics=["blog", "ai-blog", blog_name.lower().replace(" ", "-")],
                    author=author or blog_name,
                    created_at=article.get("publishedAt", ""),
                    extra={
                        "blog_name": blog_name,
                    },
                )
            )

        return projects
