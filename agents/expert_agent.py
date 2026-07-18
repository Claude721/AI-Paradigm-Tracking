"""
Expert Agent（专家 Agent） — 通用专家分析节点

职责：用不同的 Skill 人设（专家视角），对通过初筛的项目做专项分析。
每个 ExpertAgent 实例绑定一个 skill_name，代表一种专家视角。

典型专家：
  - expert_tech_landing  : 技术落地价值专家（技术类项目）
  - expert_app_landing   : 应用落地可行性专家（应用类项目）
  - expert_app_user      : 用户与市场专家（应用类项目）

模型选择可配置为 "sub" 或 "main"，灵活控制成本。
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field

from agents.llm_utils import build_client
from agents.triage_agent import TriagedProject
from skills.loader import SkillLoader
from tenacity import retry, wait_exponential, stop_after_attempt, retry_if_exception_type
import openai

logger = logging.getLogger(__name__)


@dataclass
class ExpertOpinion:
    """某个专家对某个项目的分析意见"""
    expert_name: str
    analysis: str


@dataclass
class ExpertBundle:
    """一个项目所收集到的全部专家意见"""
    triaged: TriagedProject
    opinions: list[ExpertOpinion] = field(default_factory=list)


class ExpertAgent:
    """通用专家 Agent：一个 skill_name 代表一种专家视角"""

    def __init__(
        self,
        skill_name: str,
        role: str = "sub",
        concurrency: int = 5,
    ):
        self.skill_name = skill_name
        self.concurrency = concurrency
        self.client, self.model = build_client(role)
        self.skill_loader = SkillLoader()
        self._progress = {"done": 0, "total": 0, "ok": 0, "fail": 0}

    async def run(
        self, triaged_list: list[TriagedProject]
    ) -> dict[str, ExpertOpinion]:
        """
        对一批项目进行专家分析。
        返回 {project_url: ExpertOpinion} 映射。
        """
        if not triaged_list:
            return {}

        total = len(triaged_list)
        self._progress = {"done": 0, "total": total, "ok": 0, "fail": 0}
        logger.info(
            f"ExpertAgent[{self.skill_name}] 启动，待分析 {total} 个项目"
            f"（并发={self.concurrency}, model={self.model}）"
        )

        sem = asyncio.Semaphore(self.concurrency)
        tasks = [self._analyze_one(sem, t) for t in triaged_list]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        opinions: dict[str, ExpertOpinion] = {}
        for t, r in zip(triaged_list, results):
            if isinstance(r, ExpertOpinion):
                url_key = (t.raw.url or "").strip().lower().rstrip("/")
                opinions[url_key] = r
            elif isinstance(r, Exception):
                logger.debug(f"专家分析失败[{self.skill_name}]: {r}")

        logger.info(
            f"ExpertAgent[{self.skill_name}] 完成: "
            f"{self._progress['ok']}/{total} 成功, "
            f"{self._progress['fail']} 失败"
        )
        return opinions

    @retry(
        wait=wait_exponential(multiplier=1, min=2, max=10),
        stop=stop_after_attempt(3),
        retry=retry_if_exception_type(
            (openai.RateLimitError, openai.APIConnectionError, openai.InternalServerError)
        ),
        before_sleep=lambda retry_state: logger.warning(
            f"专家 API 失败，退避重试中... (异常: {retry_state.outcome.exception()})"
        ),
    )
    async def _call_llm(self, prompt: str) -> str:
        resp = await self.client.chat.completions.create(
            model=self.model,
            messages=[{"role": "user", "content": prompt}],
            temperature=0.3,
            max_tokens=400,
        )
        return resp.choices[0].message.content or ""

    async def _analyze_one(
        self, sem: asyncio.Semaphore, triaged: TriagedProject
    ) -> ExpertOpinion:
        async with sem:
            project = triaged.raw
            org = project.extra.get("organization", "")
            author_info = f"{project.author} ({org})" if org else project.author

            prompt = self.skill_loader.render(
                self.skill_name,
                name=project.name,
                source=project.source,
                description=project.description[:800],
                readme_summary=project.readme_summary[:4000],
                topics=", ".join(project.topics[:10]),
                stars=project.stars,
                author=author_info,
            )

            try:
                analysis = await self._call_llm(prompt)
                self._progress["done"] += 1
                self._progress["ok"] += 1
                done = self._progress["done"]
                total = self._progress["total"]
                logger.info(
                    f"  [专家:{self.skill_name} {done}/{total}] ✓ {project.display_name}"
                )
                return ExpertOpinion(
                    expert_name=self.skill_name,
                    analysis=analysis.strip(),
                )

            except Exception as e:
                self._progress["done"] += 1
                self._progress["fail"] += 1
                done = self._progress["done"]
                total = self._progress["total"]
                logger.warning(
                    f"  [专家:{self.skill_name} {done}/{total}] ✗ {project.display_name}: {e}"
                )
                raise RuntimeError(
                    f"专家分析失败[{self.skill_name}][{project.name}]: {e}"
                )
