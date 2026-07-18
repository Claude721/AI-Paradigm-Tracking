"""
Analysis Agent - 分析 Agent
负责调用 LLM 对项目进行 VC 投资潜力打分和信息提取
支持: DashScope (阿里百炼) / 火山方舟 / Ollama 本地模型
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from dataclasses import dataclass

from openai import AsyncOpenAI

import config
from sources.base import RawProject

logger = logging.getLogger(__name__)

# ── 打分 Prompt（可在此处直接修改评分标准和排除规则） ─────
SCORING_PROMPT = """\
你是一位顶级 VC（风险投资）的 AI 赛道分析师。你的核心任务是从海量项目和论文中，筛选出具备**商业落地潜力**的早期投资标的。

## 核心评分原则

**你最看重的是"离钱近"的项目**——即有明确消费场景、能产生收入的 AI 应用。纯学术改进不是你的关注点。

## 评分标准（1-10 分）
- **9-10**: 在高价值垂直场景（自动驾驶、医疗、金融、教育、电商、内容创作、企业服务等）中深度整合 AI 的创新产品，或具备平台级潜力的底层基础设施创新
- **7-8**: 硬核应用创新，在特定领域做出显著差异化，有清晰的用户场景和商业模式（如垂直 Agent、行业 SaaS + AI）
- **5-6**: 有一定技术含量但缺乏明确落地场景，或商业模式不清晰
- **3-4**: 套壳项目、教程集合、API wrapper、或仅停留在学术 benchmark 层面
- **1-2**: 无商业价值、已有成熟替代品、或纯理论研究

## 机构与作者判定规则
- **加分**：来自业界知名公司或研究机构（如 Google、Meta、OpenAI、Anthropic、NVIDIA、Microsoft、阿里、字节、百度、Mistral 等）的项目或论文，这些机构的产出往往更接近产业化
- **减分**：来自高校或个人研究者的纯学术论文，除非该论文已获得极高认可度（如 upvotes > 30 或 GitHub Stars > 200）
- 对于作者背景不确定的开源项目，根据项目本身质量判断

## 对"AI 优化类"论文的特殊处理
以下类型的论文请保持**冷静旁观态度**，默认给 3-4 分（除非认可度极高）：
- "让模型推理更快/更稳定/更准确"的优化技术
- "提升训练效率/降低显存占用"的工程论文
- "新的 attention 变体/新的 loss 函数"等架构微调
- "在 benchmark X 上提升了 Y%"的对比实验
- 这些论文虽然有技术价值，但离最终价值实现太远，且大多片面，除非社区认可度非常高（upvotes > 30 或 Stars > 200），否则不值得投资者关注

## 产业落地场景分析（重点）
对于涉及具体消费/行业场景的项目，请重点分析：
- 它解决了什么具体的用户痛点？
- 它是如何将 AI 整合到该场景中的？（是核心引擎还是辅助功能？）
- 这种整合方式是否创造了新的产品形态，还是仅仅是给老产品加了个 AI 接口？

## 项目信息
- **名称**: {name}
- **来源**: {source}
- **描述**: {description}
- **README 摘要**: {readme_summary}
- **标签**: {topics}
- **热度指标**: {stars}（Star/Upvote/Score）
- **作者**: {author}

## 请严格按以下 JSON 格式输出，不要输出任何其他内容：
{{
  "score": <1-10的整数>,
  "one_liner": "<一句话总结该项目的核心价值>",
  "innovation": "<该项目的创新点，2-3句话>",
  "ai_integration": "<如果涉及具体应用场景：分析该项目如何将 AI 整合到场景中，这种整合方式的独特性。如果是纯技术/纯学术项目，写'纯技术项目，无具体应用场景'>",
  "founder_guess": "<根据作者/机构信息推测其背景和可信度>",
  "category": "<分类：基础模型/Agent框架/开发工具/垂直应用/数据基础设施/其他>",
  "reasoning": "<打分理由，2-3句话，务必说明该项目离商业落地有多远>"
}}
"""


@dataclass
class ScoredProject:
    """经 LLM 打分后的项目"""

    raw: RawProject
    score: int
    one_liner: str
    innovation: str
    ai_integration: str
    founder_guess: str
    category: str
    reasoning: str


class AnalysisAgent:
    """分析 Agent：调用 LLM 对项目逐一打分"""

    def __init__(self, concurrency: int = 5):
        self.concurrency = concurrency
        self.client = self._build_client()
        self.model = self._get_model()

    def _build_client(self) -> AsyncOpenAI:
        if config.LLM_PROVIDER == "ollama":
            return AsyncOpenAI(
                base_url=config.OLLAMA_BASE_URL,
                api_key="ollama",
            )
        if not config.LLM_API_KEY:
            logger.warning(
                "LLM_API_KEY 未配置！请在 .env 中设置，或将 LLM_PROVIDER 改为 ollama"
            )
        return AsyncOpenAI(
            base_url=config.LLM_BASE_URL,
            api_key=config.LLM_API_KEY or "placeholder",
        )

    def _get_model(self) -> str:
        if config.LLM_PROVIDER == "ollama":
            return config.OLLAMA_MODEL
        return config.LLM_MODEL

    async def run(self, projects: list[RawProject]) -> list[ScoredProject]:
        if not projects:
            return []

        total = len(projects)
        logger.info(f"AnalysisAgent 启动，待分析 {total} 个项目（并发={self.concurrency}）")

        self._progress = {"done": 0, "total": total, "ok": 0, "fail": 0}
        semaphore = asyncio.Semaphore(self.concurrency)
        tasks = [self._score_with_limit(semaphore, i, p) for i, p in enumerate(projects)]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        scored: list[ScoredProject] = []
        for r in results:
            if isinstance(r, ScoredProject):
                scored.append(r)
            elif isinstance(r, Exception):
                logger.warning(f"打分失败: {r}")

        scored.sort(key=lambda x: x.score, reverse=True)
        logger.info(
            f"AnalysisAgent 完成，成功打分 {len(scored)}/{total} 个项目"
        )
        return scored

    async def _score_with_limit(
        self, sem: asyncio.Semaphore, index: int, project: RawProject
    ) -> ScoredProject:
        async with sem:
            result = await self._score_one(project)
            self._progress["done"] += 1
            self._progress["ok"] += 1
            done = self._progress["done"]
            total = self._progress["total"]
            logger.info(
                f"  [{done}/{total}] ✓ {result.score}/10 | {project.display_name}"
            )
            return result

    async def _score_one(self, project: RawProject) -> ScoredProject:
        org = project.extra.get("organization", "")
        author_info = project.author
        if org:
            author_info = f"{project.author} ({org})"

        prompt = SCORING_PROMPT.format(
            name=project.name,
            source=project.source,
            description=project.description[:800],
            readme_summary=project.readme_summary[:2000],
            topics=", ".join(project.topics[:10]),
            stars=project.stars,
            author=author_info,
        )

        last_error: Exception | None = None
        for attempt in range(3):
            try:
                resp = await self.client.chat.completions.create(
                    model=self.model,
                    messages=[{"role": "user", "content": prompt}],
                    temperature=0.3,
                    max_tokens=800,
                )
                content = resp.choices[0].message.content or ""
                parsed = self._parse_response(content)
                return ScoredProject(raw=project, **parsed)
            except (json.JSONDecodeError, KeyError, ValueError) as e:
                last_error = e
                logger.warning(
                    f"  解析失败 (attempt {attempt + 1}/3) [{project.name[:40]}]: {e}"
                )
            except Exception as e:
                last_error = e
                logger.warning(
                    f"  LLM 调用失败 (attempt {attempt + 1}/3) [{project.name[:40]}]: {e}"
                )
                await asyncio.sleep(2 ** attempt)

        self._progress["done"] += 1
        self._progress["fail"] += 1
        done = self._progress["done"]
        total = self._progress["total"]
        logger.warning(
            f"  [{done}/{total}] ✗ 最终失败 | {project.display_name}"
        )
        raise RuntimeError(f"打分最终失败 [{project.name}]: {last_error}")

    @staticmethod
    def _parse_response(content: str) -> dict:
        """从 LLM 响应中提取 JSON，容忍 markdown 代码块和前后文字"""
        content = content.strip()

        # 尝试提取 ```json ... ``` 或 ``` ... ``` 中的内容
        fence_match = re.search(
            r"```(?:json)?\s*\n?(.*?)\n?\s*```", content, re.DOTALL
        )
        if fence_match:
            content = fence_match.group(1).strip()

        # 尝试直接找到最外层的 { ... }
        brace_match = re.search(r"\{.*\}", content, re.DOTALL)
        if brace_match:
            content = brace_match.group(0)

        data = json.loads(content)

        score = int(data["score"])
        if not 1 <= score <= 10:
            raise ValueError(f"score 超出范围: {score}")

        return {
            "score": score,
            "one_liner": str(data.get("one_liner", "")),
            "innovation": str(data.get("innovation", "")),
            "ai_integration": str(data.get("ai_integration", "")),
            "founder_guess": str(data.get("founder_guess", "未知")),
            "category": str(data.get("category", "其他")),
            "reasoning": str(data.get("reasoning", "")),
        }
