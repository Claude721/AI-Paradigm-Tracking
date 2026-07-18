"""
GitHub 信源 - 通过 GitHub Search API 搜索近期高星 AI 项目

注意：GitHub Search API 不支持 topic: 配合 OR 的组合查询，
因此按每个 topic 分别查询后合并去重。
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone

import httpx

import config
from .base import BaseSource, RawProject
from tenacity import retry, wait_exponential, stop_after_attempt, retry_if_exception_type

logger = logging.getLogger(__name__)

GITHUB_API = "https://api.github.com"
SEARCH_ENDPOINT = f"{GITHUB_API}/search/repositories"

README_CONCURRENCY = 10
SEARCH_DELAY = 2.0

# 每个 topic 只取第 1 页（30 条），避免搜索耗时过长
MAX_PAGES_PER_TOPIC = 1
MAX_README_FETCH = 60
MAX_REPO_AGE_DAYS = 90


class GitHubSource(BaseSource):
    source_name = "github"

    def __init__(self, lookback_days: int = 7):
        self.lookback_days = lookback_days
        self.headers: dict[str, str] = {"Accept": "application/vnd.github+json"}
        if config.GITHUB_TOKEN:
            self.headers["Authorization"] = f"Bearer {config.GITHUB_TOKEN}"

    async def fetch(self) -> list[RawProject]:
        since = (
            datetime.now(timezone.utc) - timedelta(days=self.lookback_days)
        ).strftime("%Y-%m-%d")
        created_since = (
            datetime.now(timezone.utc) - timedelta(days=MAX_REPO_AGE_DAYS)
        ).strftime("%Y-%m-%d")

        all_items: dict[str, dict] = {}
        async with httpx.AsyncClient(timeout=30) as client:
            for topic in config.AI_TOPICS:
                query = (
                    f"topic:{topic} stars:>{config.STAR_THRESHOLD} "
                    f"pushed:>{since} created:>{created_since} "
                    "archived:false fork:false mirror:false"
                )
                items = await self._search_one(client, query, topic)
                for item in items:
                    fn = item["full_name"]
                    if fn not in all_items:
                        all_items[fn] = item

                await asyncio.sleep(SEARCH_DELAY)

            logger.info(f"GitHub Search 合计去重后 {len(all_items)} 个仓库")

            # 只为 star 数最高的项目获取 README（深度分析阶段需要）
            top_names = sorted(
                all_items.keys(),
                key=lambda n: all_items[n].get("stargazers_count", 0),
                reverse=True,
            )[:MAX_README_FETCH]
            logger.info(f"获取 Top-{len(top_names)} 项目的 README...")
            readmes = await self._fetch_readmes_batch(client, top_names)

        projects: list[RawProject] = []
        for full_name, item in all_items.items():
            projects.append(
                RawProject(
                    source=self.source_name,
                    name=full_name,
                    url=item["html_url"],
                    description=item.get("description") or "",
                    readme_summary=readmes.get(full_name, "")[:3000],
                    stars=item.get("stargazers_count", 0),
                    language=item.get("language") or "",
                    topics=item.get("topics", []),
                    author=item.get("owner", {}).get("login", ""),
                    created_at=item.get("created_at", ""),
                    extra={
                        "pushed_at": item.get("pushed_at", ""),
                        "updated_at": item.get("updated_at", ""),
                        "type": "repo_search",
                        "fetch_full_readme_func": lambda n=full_name: self.fetch_full_readme(n)
                    },
                )
            )

        return projects

    @retry(
        wait=wait_exponential(multiplier=1, min=4, max=10),
        stop=stop_after_attempt(3),
        retry=retry_if_exception_type(httpx.HTTPError),
        before_sleep=lambda retry_state: logger.warning(
            f"GitHub Search API 失败，退避重试中... (异常: {retry_state.outcome.exception()})"
        )
    )
    async def _search_one(
        self, client: httpx.AsyncClient, query: str, topic: str
    ) -> list[dict]:
        """对单个 topic 执行搜索"""
        items: list[dict] = []
        for page in range(1, MAX_PAGES_PER_TOPIC + 1):
            try:
                resp = await client.get(
                    SEARCH_ENDPOINT,
                    params={
                        "q": query,
                        "sort": "stars",
                        "order": "desc",
                        "per_page": 30,
                        "page": page,
                    },
                    headers=self.headers,
                )
                resp.raise_for_status()
            except httpx.HTTPStatusError as e:
                if e.response.status_code == 403:
                    logger.warning("GitHub Search API 限额已用完，停止搜索")
                    return items
                if e.response.status_code == 422:
                    logger.debug(f"topic:{topic} 查询被 GitHub 拒绝，跳过")
                    return items
                raise

            data = resp.json()
            total = data.get("total_count", 0)
            page_items = data.get("items", [])
            items.extend(page_items)

            if page == 1:
                logger.info(f"  topic:{topic:25s} → {total} 个匹配")

            if len(page_items) < 30:
                break
            await asyncio.sleep(SEARCH_DELAY)

        return items

    async def _fetch_readmes_batch(
        self, client: httpx.AsyncClient, full_names: list[str]
    ) -> dict[str, str]:
        """并发批量获取 README，带并发限制"""
        sem = asyncio.Semaphore(README_CONCURRENCY)
        results: dict[str, str] = {}

        async def _get(name: str) -> None:
            async with sem:
                results[name] = await self._fetch_readme(client, name)

        await asyncio.gather(*[_get(n) for n in full_names], return_exceptions=True)
        return results

    async def fetch_full_readme(self, full_name: str) -> str:
        """On-Demand 获取完整未截断的 README (用于深度分析)"""
        async with httpx.AsyncClient(timeout=30) as client:
            return await self._fetch_readme(client, full_name, truncate=False)

    @retry(
        wait=wait_exponential(multiplier=1, min=2, max=5),
        stop=stop_after_attempt(3),
        retry=retry_if_exception_type(httpx.HTTPError),
        before_sleep=lambda retry_state: logger.warning(
            f"获取 README 失败，重试... (异常: {retry_state.outcome.exception()})"
        )
    )
    async def _fetch_readme(self, client: httpx.AsyncClient, full_name: str, truncate: bool = True) -> str:
        try:
            resp = await client.get(
                f"{GITHUB_API}/repos/{full_name}/readme",
                headers={**self.headers, "Accept": "application/vnd.github.raw+json"},
            )
            if resp.status_code == 200:
                return resp.text[:3000] if truncate else resp.text
        except Exception:
            logger.debug(f"无法获取 {full_name} 的 README")
        return ""
