"""
Critique Agent — 靶向追问与信息蒸馏

参考 MARS 论文的 Author-Reviewer-MetaReviewer 模式做了简化：
  - Senior Partner（主 Agent）审阅 Analyst 分析，提出 1-2 个针对性追问
  - Research Analyst（子 Agent）基于原始信息回答追问
  - 最终合并为 EnrichedProject

仅对高分项目触发（score >= CRITIQUE_MIN_SCORE），避免 token 浪费。
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass, field

import config
from agents.deep_agent import ScoredProject
from agents.llm_utils import build_client, parse_json_response
from skills.loader import SkillLoader
from tenacity import retry, wait_exponential, stop_after_attempt, retry_if_exception_type
import openai

logger = logging.getLogger(__name__)


@dataclass
class EnrichedProject:
    """经靶向追问后的增强项目数据"""

    scored: ScoredProject
    critique_qa: list[dict] = field(default_factory=list)
    mini_memo: str = ""

    @property
    def score(self) -> int:
        return self.scored.score


class CritiqueAgent:
    """靶向追问 Agent：Senior Partner 审阅 + Analyst 回答"""

    def __init__(self, concurrency: int = 3):
        self.concurrency = concurrency
        self.client, self.model = build_client("main")
        self.skill_loader = SkillLoader()
        self.min_score = config.CRITIQUE_MIN_SCORE
        self.enabled = config.CRITIQUE_ENABLED

    async def run(self, scored: list[ScoredProject]) -> list[EnrichedProject]:
        enriched: list[EnrichedProject] = []

        if not self.enabled:
            logger.info("CritiqueAgent 已禁用，跳过靶向追问")
            return [EnrichedProject(scored=sp) for sp in scored]

        top = [sp for sp in scored if sp.score >= self.min_score]
        rest = [sp for sp in scored if sp.score < self.min_score]

        if not top:
            logger.info(
                f"无项目达到追问阈值 (>={self.min_score})，跳过靶向追问"
            )
            return [EnrichedProject(scored=sp) for sp in scored]

        logger.info(
            f"CritiqueAgent 启动: {len(top)} 个高分项目进入靶向追问"
            f"（阈值>={self.min_score}, model={self.model}）"
        )

        sem = asyncio.Semaphore(self.concurrency)
        tasks = [self._critique_one(sem, sp) for sp in top]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        ok = 0
        for r in results:
            if isinstance(r, EnrichedProject):
                enriched.append(r)
                ok += 1
            elif isinstance(r, Exception):
                logger.warning(f"靶向追问失败: {r}")

        enriched.extend(EnrichedProject(scored=sp) for sp in rest)
        enriched.sort(key=lambda e: e.score, reverse=True)

        logger.info(
            f"CritiqueAgent 完成: {ok}/{len(top)} 成功追问"
        )
        return enriched

    async def _critique_one(
        self, sem: asyncio.Semaphore, sp: ScoredProject
    ) -> EnrichedProject:
        async with sem:
            questions = await self._generate_questions(sp)

            if not questions:
                logger.info(
                    f"  [追问] {sp.raw.name[:40]} — 分析已充分，无需追问"
                )
                return EnrichedProject(scored=sp)

            answers = await self._answer_questions(sp, questions)
            qa_pairs = [
                {"question": q, "answer": a}
                for q, a in zip(questions, answers)
            ]

            logger.info(
                f"  [追问] {sp.raw.name[:40]} — {len(qa_pairs)} 个追问已回答"
            )
            return EnrichedProject(scored=sp, critique_qa=qa_pairs)

    @retry(
        wait=wait_exponential(multiplier=1, min=2, max=10),
        stop=stop_after_attempt(3),
        retry=retry_if_exception_type((openai.RateLimitError, openai.APIConnectionError, openai.InternalServerError)),
        before_sleep=lambda retry_state: logger.warning(
            f"追问生成 API 失败，退避重试中... (异常: {retry_state.outcome.exception()})"
        )
    )
    async def _call_llm_generate(self, prompt: str) -> list[str]:
        resp = await self.client.chat.completions.create(
            model=self.model,
            messages=[{"role": "user", "content": prompt}],
            temperature=0.4,
            max_tokens=300,
        )
        content = resp.choices[0].message.content or ""
        start = content.find("{")
        end = content.rfind("}")
        if start == -1 or end == -1 or end <= start:
            return []
        data = json.loads(content[start:end + 1])
        questions = data.get("questions", [])
        if not isinstance(questions, list):
            return []
        return [str(q) for q in questions if q and str(q).strip()][:2]

    async def _generate_questions(self, sp: ScoredProject) -> list[str]:
        analysis_json = json.dumps(
            {
                "one_liner": sp.one_liner,
                "innovation": sp.innovation,
                "key_design": sp.key_design,
                "risks": sp.risks,
                "ai_integration": sp.ai_integration,
                "founder_guess": sp.founder_guess,
                "category": sp.category,
                "reasoning": sp.reasoning,
            },
            ensure_ascii=False,
            indent=2,
        )

        prompt = self.skill_loader.render(
            "critique_drill",
            name=sp.raw.name,
            score=sp.score,
            analysis_json=analysis_json,
        )

        try:
            return await self._call_llm_generate(prompt)
        except Exception as e:
            logger.warning(f"追问生成失败 [{sp.raw.name[:30]}]: {e}")
            return []

    @retry(
        wait=wait_exponential(multiplier=1, min=2, max=10),
        stop=stop_after_attempt(3),
        retry=retry_if_exception_type((openai.RateLimitError, openai.APIConnectionError, openai.InternalServerError)),
        before_sleep=lambda retry_state: logger.warning(
            f"追问回答 API 失败，退避重试中... (异常: {retry_state.outcome.exception()})"
        )
    )
    async def _call_llm_answer(self, prompt: str, q_len: int) -> list[str]:
        resp = await self.client.chat.completions.create(
            model=self.model,
            messages=[{"role": "user", "content": prompt}],
            temperature=0.3,
            max_tokens=400,
        )
        content = resp.choices[0].message.content or ""
        parts = [p.strip() for p in content.split("\n\n") if p.strip()]
        while len(parts) < q_len:
            parts.append("")
        return parts[:q_len]

    async def _answer_questions(
        self, sp: ScoredProject, questions: list[str]
    ) -> list[str]:
        questions_text = "\n".join(
            f"{i+1}. {q}" for i, q in enumerate(questions)
        )
        prompt = self.skill_loader.render(
            "critique_answer",
            name=sp.raw.name,
            description=sp.raw.description[:600],
            readme_summary=sp.raw.readme_summary[:1500],
            questions=questions_text,
        )

        try:
            return await self._call_llm_answer(prompt, len(questions))
        except Exception as e:
            logger.warning(f"追问回答失败 [{sp.raw.name[:30]}]: {e}")
            return [""] * len(questions)
