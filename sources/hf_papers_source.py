"""
HuggingFace Daily Papers 信源 - 通过 HF REST API 拉取每日精选论文
等价于 huggingface-daily-paper-mcp MCP Server 的内部实现。
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

import httpx

from .base import BaseSource, RawProject

logger = logging.getLogger(__name__)

HF_PAPERS_API = "https://huggingface.co/api/daily_papers"


class HuggingFacePapersSource(BaseSource):
    source_name = "huggingface-papers"

    def __init__(self, lookback_days: int = 7):
        self.lookback_days = max(lookback_days, 1)

    async def fetch(self) -> list[RawProject]:
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.get(HF_PAPERS_API)
            resp.raise_for_status()
            data = resp.json()

        results: list[RawProject] = []
        for item in data:
            paper = item.get("paper", {})
            paper_id = paper.get("id", "")
            title = paper.get("title", "")
            summary = paper.get("summary", "")

            authors = paper.get("authors", [])
            author_names = [a.get("name", "") for a in authors if a.get("name")]
            first_author = author_names[0] if author_names else ""

            published = item.get("publishedAt", "")
            if published:
                try:
                    published_dt = datetime.fromisoformat(
                        published.replace("Z", "+00:00")
                    ).astimezone(timezone.utc)
                    cutoff = datetime.now(timezone.utc) - timedelta(
                        days=self.lookback_days
                    )
                    if published_dt < cutoff:
                        continue
                except ValueError:
                    pass
            upvotes = paper.get("upvotes", 0)
            github_stars = paper.get("githubStars", 0) or 0
            github_repo = paper.get("githubRepo", "")

            org_data = item.get("organization") or paper.get("organization")
            org_name = ""
            if isinstance(org_data, dict):
                org_name = org_data.get("fullname", org_data.get("name", ""))

            popularity = github_stars if github_stars > 0 else upvotes

            results.append(
                RawProject(
                    source=self.source_name,
                    name=title,
                    url=f"https://huggingface.co/papers/{paper_id}",
                    description=summary[:1500],
                    readme_summary=summary[:3000],
                    stars=popularity,
                    author=first_author,
                    created_at=published,
                    extra={
                        "arxiv_id": paper_id,
                        "all_authors": author_names,
                        "organization": org_name,
                        "upvotes": upvotes,
                        "github_stars": github_stars,
                        "github_repo": github_repo,
                        "num_comments": item.get("numComments", 0),
                        "type": "daily_paper",
                    },
                )
            )

        logger.info(f"获取 HuggingFace Daily Papers: {len(results)} 篇")
        return results
