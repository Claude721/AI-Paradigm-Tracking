"""
Triage Agent（子 Agent） — AgentSwarm 初筛节点

职责：用轻量 Prompt + 快速模型，对所有原始项目做 1-10 快速打分。
只保留 score >= TRIAGE_THRESHOLD 的项目传递给 DeepAnalysisAgent。

对应 Prompt 文件：prompts/triage_scoring.md
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass

import config
from agents.llm_utils import build_client, parse_json_response
from skills.loader import SkillLoader
from sources.base import RawProject
from tenacity import retry, wait_exponential, stop_after_attempt, retry_if_exception_type
import openai

logger = logging.getLogger(__name__)


@dataclass
class TriagedProject:
    """初筛结果：原始项目 + 快速评分"""

    raw: RawProject
    triage_score: int
    one_liner: str


class TriageAgent:
    """子 Agent：快速初筛，大量并发，压缩 token 开销"""

    def __init__(self, concurrency: int = 8):
        self.concurrency = concurrency
        self.client, self.model = build_client("sub")
        self.skill_loader = SkillLoader()
        self._progress = {"done": 0, "total": 0, "ok": 0, "fail": 0}

    async def run(self, projects: list[RawProject]) -> list[TriagedProject]:
        if not projects:
            return []

        total = len(projects)
        self._progress = {"done": 0, "total": total, "ok": 0, "fail": 0}
        logger.info(
            f"TriageAgent 启动，待初筛 {total} 个项目"
            f"（并发={self.concurrency}, model={self.model}）"
        )

        sem = asyncio.Semaphore(self.concurrency)
        tasks = [self._score_one(sem, p) for p in projects]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        triaged: list[TriagedProject] = []
        for r in results:
            if isinstance(r, TriagedProject):
                triaged.append(r)
            elif isinstance(r, Exception):
                logger.debug(f"初筛单项失败: {r}")

        passed = [
            t for t in triaged if t.triage_score >= config.TRIAGE_THRESHOLD
        ]
        triaged.sort(key=lambda x: x.triage_score, reverse=True)

        logger.info(
            f"TriageAgent 完成: {self._progress['ok']}/{total} 成功, "
            f"{len(passed)} 个通过阈值(>={config.TRIAGE_THRESHOLD})"
        )
        return passed

    @retry(
        wait=wait_exponential(multiplier=1, min=2, max=10),
        stop=stop_after_attempt(3),
        retry=retry_if_exception_type((openai.RateLimitError, openai.APIConnectionError, openai.InternalServerError)),
        before_sleep=lambda retry_state: logger.warning(
            f"初筛 API 失败，退避重试中... (异常: {retry_state.outcome.exception()})"
        )
    )
    async def _call_llm(self, prompt: str) -> dict:
        resp = await self.client.chat.completions.create(
            model=self.model,
            messages=[{"role": "user", "content": prompt}],
            temperature=0.2,
            max_tokens=200,
        )
        content = resp.choices[0].message.content or ""
        return parse_json_response(content)

    async def _score_one(
        self, sem: asyncio.Semaphore, project: RawProject
    ) -> TriagedProject:
        async with sem:
            org = project.extra.get("organization", "")
            author_info = f"{project.author} ({org})" if org else project.author

            project_type = project.extra.get("project_type", "tech")
            skill_name = f"triage_scoring_{project_type}"
            prompt = self.skill_loader.render(
                skill_name,
                name=project.name,
                source=project.source,
                description=project.description[:500],
                topics=", ".join(project.topics[:8]),
                stars=project.stars,
                author=author_info,
            )

            try:
                data = await self._call_llm(prompt)
                
                result = TriagedProject(
                    raw=project,
                    triage_score=int(data["score"]),
                    one_liner=str(data.get("one_liner", "")),
                )

                self._progress["done"] += 1
                self._progress["ok"] += 1
                done = self._progress["done"]
                total = self._progress["total"]
                logger.info(
                    f"  [初筛 {done}/{total}] "
                    f"{'✓' if result.triage_score >= config.TRIAGE_THRESHOLD else '·'} "
                    f"{result.triage_score}/10 | {project.display_name}"
                )
                return result

            except Exception as e:
                self._progress["done"] += 1
                self._progress["fail"] += 1
                done = self._progress["done"]
                total = self._progress["total"]
                logger.warning(
                    f"  [初筛 {done}/{total}] ✗ 失败 | {project.display_name}: {e}"
                )
                raise RuntimeError(f"初筛失败 [{project.name}]: {e}")
