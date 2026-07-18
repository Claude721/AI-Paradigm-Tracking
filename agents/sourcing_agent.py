"""
Sourcing Agent - 爬虫 Agent
负责并发调度所有信源，汇总原始项目数据
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone

import config

from sources.base import BaseSource, RawProject
from sources import (
    GitHubSource,
    GitHubTrendingSource,
    HuggingFaceSource,
    HuggingFacePapersSource,
    ArxivSource,
    HackerNewsSource,
    ProductHuntSource,
    TwitterSource,
    WeChatSource,
    FollowBuildersSource,
)

logger = logging.getLogger(__name__)


class SourcingAgent:
    """爬虫 Agent：并发拉取所有信源，返回统一格式的项目列表"""

    def __init__(self):
        lookback_days = config.SOURCING_LOOKBACK_DAYS
        self.sources: list[BaseSource] = [
            # ── 🟢 Level 1: MCP 等价 / 直连 ──
            GitHubSource(lookback_days=lookback_days),
            GitHubTrendingSource(since="daily", languages=["python", "typescript", ""]),
            HuggingFaceSource(limit=30, recent_days=lookback_days),
            HuggingFacePapersSource(),
            ArxivSource(max_results=30, lookback_days=lookback_days),
            # ── 🟡 Level 2: REST / GraphQL API ──
            HackerNewsSource(top_n=100, lookback_hours=lookback_days * 24),
            ProductHuntSource(lookback_days=lookback_days, limit=50),
            TwitterSource(max_results_per_query=20),
            # ── 🟠 Level 2.5: 社交媒体聚合（默认方案A零成本）──
            FollowBuildersSource(),
            # ── 🔵 Level 3: 本地自建服务（默认不启用）──
            WeChatSource(limit=50),
        ]

    async def run(self) -> list[RawProject]:
        logger.info(f"SourcingAgent 启动，共 {len(self.sources)} 个信源")

        tasks = [source.safe_fetch() for source in self.sources]
        results = await asyncio.gather(*tasks)

        all_projects: list[RawProject] = []
        for batch in results:
            all_projects.extend(batch)

        all_projects = self._filter_by_lookback(all_projects)
        all_projects = self._deduplicate(all_projects)
        logger.info(f"SourcingAgent 完成，去重后共 {len(all_projects)} 个项目")
        return all_projects

    @staticmethod
    def _filter_by_lookback(projects: list[RawProject]) -> list[RawProject]:
        """按最近活跃时间过滤；没有可靠时间字段的 Trending 记录予以保留。"""
        cutoff = datetime.now(timezone.utc) - timedelta(
            days=config.SOURCING_LOOKBACK_DAYS
        )
        kept: list[RawProject] = []
        dropped = 0

        for project in projects:
            candidates = [
                project.extra.get("last_modified"),
                project.extra.get("pushed_at"),
                project.extra.get("updated_at"),
                project.created_at,
            ]
            activity_time = next(
                (
                    parsed
                    for value in candidates
                    if (parsed := SourcingAgent._parse_datetime(value)) is not None
                ),
                None,
            )
            if activity_time is not None and activity_time < cutoff:
                dropped += 1
                continue
            kept.append(project)

        logger.info(
            "时间窗口过滤: 最近 %s 天，保留 %s/%s（过期 %s）",
            config.SOURCING_LOOKBACK_DAYS,
            len(kept),
            len(projects),
            dropped,
        )
        return kept

    @staticmethod
    def _parse_datetime(value: object) -> datetime | None:
        if value is None or value == "":
            return None
        try:
            if isinstance(value, (int, float)) or str(value).isdigit():
                return datetime.fromtimestamp(float(value), tz=timezone.utc)
            return datetime.fromisoformat(
                str(value).strip().replace("Z", "+00:00")
            ).astimezone(timezone.utc)
        except (TypeError, ValueError, OSError):
            return None

    @staticmethod
    def _deduplicate(projects: list[RawProject]) -> list[RawProject]:
        """按 URL 去重，保留信息更丰富的一条"""
        by_url: dict[str, RawProject] = {}
        for p in projects:
            key = p.url.lower().rstrip("/")
            if key not in by_url:
                by_url[key] = p
                continue

            existing = by_url[key]
            if SourcingAgent._richness(p) > SourcingAgent._richness(existing):
                by_url[key] = p

        return list(by_url.values())

    @staticmethod
    def _richness(p: RawProject) -> int:
        """估算记录信息密度：越高越优先保留"""
        score = 0
        if p.description:
            score += min(len(p.description), 400) // 20
        if p.readme_summary:
            score += min(len(p.readme_summary), 1200) // 40
        if p.topics:
            score += min(len(p.topics), 10) * 2
        if p.author:
            score += 3
        if p.language:
            score += 3
        if p.extra:
            score += min(len(p.extra), 8) * 2
        if p.stars > 0:
            score += 2
        return score
