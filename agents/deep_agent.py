"""
Deep Analysis Agent（主 Agent） — AgentSwarm 深度分析节点

职责：对通过 Triage 初筛的高潜力项目做完整的 VC 视角深度分析。
使用更长的 Prompt、更高质量的模型，产出完整的分析报告字段。

对应 Prompt 文件：prompts/deep_analysis.md
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass

from agents.llm_utils import build_client, parse_json_response
from agents.triage_agent import TriagedProject
from skills.loader import SkillLoader
from sources.base import RawProject
from tenacity import retry, wait_exponential, stop_after_attempt, retry_if_exception_type
import openai

logger = logging.getLogger(__name__)


@dataclass
class ScoredProject:
    """经主 Agent 深度分析后的项目（完整评分结果）"""

    raw: RawProject
    score: int
    one_liner: str
    innovation: str
    key_design: str
    risks: str
    ai_integration: str
    founder_guess: str
    category: str
    reasoning: str


class DeepAnalysisAgent:
    """主 Agent：深度分析，产出完整投资分析。可接收专家意见作为额外上下文。"""

    def __init__(self, concurrency: int = 3):
        self.concurrency = concurrency
        self.client, self.model = build_client("main")
        self.skill_loader = SkillLoader()
        self._progress = {"done": 0, "total": 0, "ok": 0, "fail": 0}
        self._expert_opinions: dict[str, list[tuple[str, str]]] = {}

    def set_expert_opinions(
        self, opinions: dict[str, list[tuple[str, str]]]
    ) -> None:
        """注入专家意见，格式 {url_key: [(expert_name, analysis_text), ...]}"""
        self._expert_opinions = opinions

    async def run(
        self, triaged: list[TriagedProject]
    ) -> list[ScoredProject]:
        if not triaged:
            return []

        total = len(triaged)
        self._progress = {"done": 0, "total": total, "ok": 0, "fail": 0}
        logger.info(
            f"DeepAnalysisAgent 启动，待深度分析 {total} 个项目"
            f"（并发={self.concurrency}, model={self.model}）"
        )

        sem = asyncio.Semaphore(self.concurrency)
        tasks = [self._analyze_one(sem, t) for t in triaged]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        scored: list[ScoredProject] = []
        for r in results:
            if isinstance(r, ScoredProject):
                scored.append(r)
            elif isinstance(r, Exception):
                logger.debug(f"深度分析单项失败: {r}")

        scored.sort(key=lambda x: x.score, reverse=True)
        logger.info(
            f"DeepAnalysisAgent 完成: {self._progress['ok']}/{total} 成功, "
            f"{self._progress['fail']} 失败"
        )
        return scored

    @retry(
        wait=wait_exponential(multiplier=1, min=2, max=10),
        stop=stop_after_attempt(3),
        retry=retry_if_exception_type((openai.RateLimitError, openai.APIConnectionError, openai.InternalServerError)),
        before_sleep=lambda retry_state: logger.warning(
            f"深度分析 API 失败，退避重试中... (异常: {retry_state.outcome.exception()})"
        )
    )
    async def _call_llm(self, prompt: str) -> dict:
        resp = await self.client.chat.completions.create(
            model=self.model,
            messages=[{"role": "user", "content": prompt}],
            temperature=0.3,
            max_tokens=1500,
        )
        content = resp.choices[0].message.content or ""
        return parse_json_response(content)

    async def _analyze_one(
        self, sem: asyncio.Semaphore, triaged: TriagedProject
    ) -> ScoredProject:
        async with sem:
            project = triaged.raw
            org = project.extra.get("organization", "")
            author_info = f"{project.author} ({org})" if org else project.author

            # Deep Analysis 阶段按需获取未截断的长上下文（如果来源支持）
            readme = project.readme_summary
            fetch_func = project.extra.get("fetch_full_readme_func")
            if fetch_func:
                try:
                    full_readme = await fetch_func()
                    if full_readme:
                        readme = full_readme[:8000] # 保留较长的上下文，避免超出 token 限制
                except Exception as e:
                    logger.debug(f"[{project.name}] 获取长 README 失败，降级使用摘要: {e}")

            project_type = project.extra.get("project_type", "tech")
            skill_name = f"deep_analysis_{project_type}"
            # 组装专家意见
            url_key = (project.url or "").strip().lower().rstrip("/")
            expert_texts = self._expert_opinions.get(url_key, [])
            if expert_texts:
                expert_section = "\n\n".join(
                    f"【{name}】\n{text}" for name, text in expert_texts
                )
            else:
                expert_section = "（无专家预分析意见）"

            prompt = self.skill_loader.render(
                skill_name,
                name=project.name,
                source=project.source,
                description=project.description[:800],
                readme_summary=readme[:8000],
                topics=", ".join(project.topics[:10]),
                stars=project.stars,
                author=author_info,
                expert_opinions=expert_section,
            )

            try:
                data = await self._call_llm(prompt)
                
                result = ScoredProject(
                    raw=project,
                    score=int(data["score"]),
                    one_liner=str(data.get("one_liner", "")),
                    innovation=str(data.get("innovation", "")),
                    key_design=str(data.get("key_design", "")),
                    risks=str(data.get("risks", "")),
                    ai_integration=str(data.get("ai_integration", "")),
                    founder_guess=str(data.get("founder_guess", "未知")),
                    category=str(data.get("category", "其他")),
                    reasoning=str(data.get("reasoning", "")),
                )

                self._progress["done"] += 1
                self._progress["ok"] += 1
                done = self._progress["done"]
                total = self._progress["total"]
                logger.info(
                    f"  [深度 {done}/{total}] ✓ "
                    f"{result.score}/10 | {project.display_name}"
                )
                return result

            except Exception as e:
                self._progress["done"] += 1
                self._progress["fail"] += 1
                done = self._progress["done"]
                total = self._progress["total"]
                logger.warning(
                    f"  [深度 {done}/{total}] ✗ 最终失败 | {project.display_name}: {e}"
                )
                raise RuntimeError(f"深度分析失败 [{project.name}]: {e}")
