"""
GitHub Trending 信源 - 解析 github.com/trending 页面
等价于 mcp-github-trending MCP Server 的内部实现，直接调用相同数据源。
支持按语言和时间段（daily/weekly/monthly）过滤。
"""

from __future__ import annotations

import logging
import re

import httpx
from config import GITHUB_TOKEN
import logging
from .base import BaseSource, RawProject

from tenacity import retry, wait_exponential, stop_after_attempt, retry_if_exception_type

logger = logging.getLogger(__name__)

GITHUB_API = "https://api.github.com"
TRENDING_URL = "https://github.com/trending"

TRENDING_LANGUAGES = ["python", "typescript", "rust", ""]


class GitHubTrendingSource(BaseSource):
    source_name = "github-trending"

    def __init__(self, since: str = "daily", languages: list[str] | None = None):
        self.since = since
        self.languages = languages or TRENDING_LANGUAGES
        self._api_headers: dict[str, str] = {"Accept": "application/vnd.github+json"}
        if GITHUB_TOKEN:
            self._api_headers["Authorization"] = f"Bearer {GITHUB_TOKEN}"

    async def fetch(self) -> list[RawProject]:
        projects: list[RawProject] = []
        async with httpx.AsyncClient(timeout=30, follow_redirects=True) as client:
            for lang in self.languages:
                page_projects = await self._fetch_trending_page(client, lang)
                projects.extend(page_projects)
        return self._deduplicate(projects)

    @retry(
        wait=wait_exponential(multiplier=1, min=4, max=10),
        stop=stop_after_attempt(3),
        retry=retry_if_exception_type(httpx.HTTPError),
        before_sleep=lambda retry_state: logger.warning(
            f"GitHub Trending 请求失败，准备重试... (异常: {retry_state.outcome.exception()})"
        )
    )
    async def _fetch_trending_page(
        self, client: httpx.AsyncClient, language: str
    ) -> list[RawProject]:
        url = f"{TRENDING_URL}/{language}" if language else TRENDING_URL
        try:
            resp = await client.get(
                url,
                params={"since": self.since},
                headers={"Accept": "text/html"},
            )
            resp.raise_for_status()
        except httpx.HTTPError as e:
            logger.warning(f"GitHub Trending 页面请求失败 ({language}): {e}")
            return []

        return self._parse_trending_html(resp.text)

    def _parse_trending_html(self, html: str) -> list[RawProject]:
        results: list[RawProject] = []
        articles = re.split(r"<article\s", html)

        for article in articles[1:]:
            repo_match = re.search(
                r'<h2[^>]*>\s*<a[^>]*href="(/[^"]+)"', article, re.DOTALL
            )
            if not repo_match:
                continue
            repo_path = repo_match.group(1).strip().strip("/")
            parts = repo_path.split("/")
            if len(parts) != 2:
                continue
            owner, repo_name = parts

            desc_match = re.search(
                r'<p[^>]*class="[^"]*col-9[^"]*"[^>]*>(.*?)</p>',
                article,
                re.DOTALL,
            )
            description = ""
            if desc_match:
                description = re.sub(r"<[^>]+>", "", desc_match.group(1)).strip()

            lang_match = re.search(
                r'itemprop="programmingLanguage"[^>]*>(.*?)</span>', article
            )
            language = lang_match.group(1).strip() if lang_match else ""

            stars_match = re.search(
                r'href="/' + re.escape(repo_path) + r'/stargazers"[^>]*>\s*'
                r'([\d,]+)\s*</a>',
                article,
                re.DOTALL,
            )
            total_stars = (
                int(stars_match.group(1).replace(",", "")) if stars_match else 0
            )

            today_match = re.search(
                r"([\d,]+)\s+stars?\s+(today|this week|this month)", article
            )
            stars_period = (
                int(today_match.group(1).replace(",", "")) if today_match else 0
            )

            # VC 视角更关心“近期势能”，优先使用 period 增长；没有时再回退总星数
            momentum = stars_period if stars_period > 0 else total_stars

            results.append(
                RawProject(
                    source=self.source_name,
                    name=repo_path,
                    url=f"https://github.com/{repo_path}",
                    description=description,
                    stars=momentum,
                    language=language,
                    author=owner,
                    extra={
                        "total_stars": total_stars,
                        "stars_period": stars_period,
                        "trending_since": self.since,
                        "type": "trending",
                    },
                )
            )

        return results

    @staticmethod
    def _deduplicate(projects: list[RawProject]) -> list[RawProject]:
        seen: set[str] = set()
        unique: list[RawProject] = []
        for p in projects:
            key = p.url.lower().rstrip("/")
            if key not in seen:
                seen.add(key)
                unique.append(p)
        return unique
