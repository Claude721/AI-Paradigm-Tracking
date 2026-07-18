"""
Markdown 报告生成模块 — "总-分-总" 结构
  总（开篇）：LLM 生成 Executive Summary
  分（项目）：高分项目 LLM Mini Memo + 属性表；普通项目精简模板
  总（结语）：LLM 生成跨项目趋势与风险
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path

import config
from agents.llm_utils import build_client
from skills.loader import SkillLoader

logger = logging.getLogger(__name__)


class ReportGenerator:
    """生成专业的 Markdown 格式每日报告（LLM 叙事 + 模板混合）"""

    def __init__(self, output_dir: Path | str | None = None):
        self.output_dir = Path(output_dir) if output_dir else config.REPORTS_DIR
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.llm_enabled = config.REPORT_LLM_ENABLED
        self.memo_min_score = config.REPORT_MEMO_MIN_SCORE
        self.skill_loader = SkillLoader()
        self._client = None
        self._model = None

    def _get_client(self):
        if self._client is None:
            self._client, self._model = build_client("main")
        return self._client, self._model

    async def generate(
        self, projects: list[dict], stats: dict | None = None,
        pipeline_stats: dict | None = None,
    ) -> Path:
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        filename = f"deal_flow_{today}.md"
        filepath = self.output_dir / filename

        sections: list[str] = []
        sections.append(self._header(today, len(projects)))

        if not projects:
            sections.append("\n> 今日暂无高分项目。\n")
            filepath.write_text("\n".join(sections), encoding="utf-8")
            return filepath

        # -- 总（开篇）: LLM Executive Summary --
        opening = await self._llm_opening(today, projects, pipeline_stats)
        sections.append(opening)

        # -- 分（项目详情）: 按分类分组 --
        by_category: dict[str, list[dict]] = {}
        for p in projects:
            cat = p.get("category", "其他")
            by_category.setdefault(cat, []).append(p)

        for category, items in sorted(
            by_category.items(),
            key=lambda x: -max(i.get("score", 0) for i in x[1]),
        ):
            sections.append(await self._category_section(category, items))

        # -- Stats --
        if stats:
            sections.append(self._stats_section(stats))

        # -- 总（结语）: LLM Closing --
        closing = await self._llm_closing(today, projects)
        sections.append(closing)

        sections.append(self._footer())

        content = "\n".join(sections)
        filepath.write_text(content, encoding="utf-8")
        logger.info(f"报告已生成: {filepath}")
        return filepath

    # ── LLM 叙事 ──────────────────────────────────────────

    async def _llm_opening(
        self, date: str, projects: list[dict],
        pipeline_stats: dict | None,
    ) -> str:
        if not self.llm_enabled:
            return self._template_executive_summary(projects)

        project_list = "\n".join(
            f"- [{p['score']}/10] [{p.get('category','其他')}] "
            f"{p['name']}: {p.get('one_liner','N/A')}\n  创新点: {p.get('innovation','')}\n  局限/风险: {p.get('risks','')}"
            for p in projects[:30]
        )
        
        by_cat: dict[str, int] = {}
        for p in projects:
            cat = p.get("category", "其他")
            by_cat[cat] = by_cat.get(cat, 0) + 1
        category_summary = "\n".join(
            f"- {cat}: {cnt} 个项目" for cat, cnt in sorted(by_cat.items())
        )
        
        ps = pipeline_stats or {}
        try:
            prompt = self.skill_loader.render(
                "report_opening",
                date=date,
                total_sourced=ps.get("raw_count", "N/A"),
                total_analyzed=ps.get("scored_count", "N/A"),
                high_value_count=len(projects),
                category_summary=category_summary,
                project_list=project_list,
            )
            client, model = self._get_client()
            resp = await client.chat.completions.create(
                model=model,
                messages=[{"role": "user", "content": prompt}],
                temperature=0.4,
                max_tokens=2000,
            )
            text = resp.choices[0].message.content or ""
            return f"## Deep Research: Macro Analysis\n\n{text.strip()}\n"
        except Exception as e:
            logger.warning(f"LLM 开篇生成失败，回退模板: {e}")
            return self._template_executive_summary(projects)

    async def _llm_closing(self, date: str, projects: list[dict]) -> str:
        if not self.llm_enabled:
            return ""

        by_cat: dict[str, int] = {}
        for p in projects:
            cat = p.get("category", "其他")
            by_cat[cat] = by_cat.get(cat, 0) + 1
        category_summary = "\n".join(
            f"- {cat}: {cnt} 个项目" for cat, cnt in sorted(by_cat.items())
        )
        top_projects = "\n".join(
            f"- [{p['score']}/10] {p['name']}: {p.get('one_liner','')}"
            for p in projects[:10]
        )

        try:
            prompt = self.skill_loader.render(
                "report_closing",
                date=date,
                high_value_count=len(projects),
                category_summary=category_summary,
                top_projects=top_projects,
            )
            client, model = self._get_client()
            resp = await client.chat.completions.create(
                model=model,
                messages=[{"role": "user", "content": prompt}],
                temperature=0.5,
                max_tokens=600,
            )
            text = resp.choices[0].message.content or ""
            return f"\n## 结语与趋势\n\n{text.strip()}\n"
        except Exception as e:
            logger.warning(f"LLM 结语生成失败: {e}")
            return ""

    async def _llm_mini_memo(self, p: dict) -> str:
        if not self.llm_enabled:
            return ""

        qa_text = ""
        for qa in p.get("critique_qa", []):
            qa_text += f"Q: {qa.get('question','')}\nA: {qa.get('answer','')}\n\n"
        if not qa_text:
            qa_text = "无追问记录"

            try:
                prompt = self.skill_loader.render(
                    "project_memo",
                    name=p["name"],
                    score=p["score"],
                    one_liner=p.get("one_liner", ""),
                    key_design=p.get("key_design", ""),
                    risks=p.get("risks", ""),
                    innovation=p.get("innovation", ""),
                    ai_integration=p.get("ai_integration", ""),
                    reasoning=p.get("reasoning", ""),
                    critique_qa=qa_text,
                )
                client, model = self._get_client()
                
                # 重试逻辑，直接在这里写，避免循环依赖
                for attempt in range(3):
                    try:
                        resp = await client.chat.completions.create(
                            model=model,
                            messages=[{"role": "user", "content": prompt}],
                            temperature=0.4,
                            max_tokens=400,
                        )
                        return resp.choices[0].message.content or ""
                    except Exception as llm_err:
                        import asyncio
                        if attempt < 2:
                            await asyncio.sleep(2 ** attempt)
                        else:
                            raise llm_err
            except Exception as e:
                logger.warning(f"Mini Memo 生成失败 [{p['name'][:30]}]: {e}")
                return ""

    # ── 项目区块 ──────────────────────────────────────────

    async def _category_section(
        self, category: str, items: list[dict]
    ) -> str:
        sorted_items = sorted(items, key=lambda x: -x.get("score", 0))
        top_items = [p for p in sorted_items if p.get("score", 0) >= self.memo_min_score]

        import asyncio
        memo_tasks = {id(p): self._llm_mini_memo(p) for p in top_items}
        memo_results: dict = {}
        if memo_tasks:
            results = await asyncio.gather(*memo_tasks.values(), return_exceptions=True)
            for pid, result in zip(memo_tasks.keys(), results):
                memo_results[pid] = result if isinstance(result, str) else ""

        lines = [f"\n## {category}\n"]
        for p in sorted_items:
            score = max(0, min(10, p.get("score", 0)))
            is_top = score >= self.memo_min_score
            lines.append(self._project_header(p))

            if is_top:
                memo = memo_results.get(id(p), "")
                if memo:
                    lines.append(f"\n{memo.strip()}\n")

            lines.append(self._project_details(p, is_top))
            lines.append("\n---\n")

        return "\n".join(lines)

    @staticmethod
    def _project_header(p: dict) -> str:
        score = max(0, min(10, p.get("score", 0)))
        score_bar = "★" * score + "☆" * (10 - score)
        lines = [f"### {p['name']}"]
        lines.append("")
        lines.append("| 属性 | 值 |")
        lines.append("|---|---|")
        lines.append(f"| **评分** | {score_bar} ({score}/10) |")
        lines.append(f"| **来源** | {p.get('source', 'N/A')} |")
        lines.append(f"| **链接** | [{p['url']}]({p['url']}) |")

        stars = p.get("stars", 0)
        if stars and stars > 0:
            source = p.get("source", "")
            if "paper" in source or "arxiv" in source:
                label = "Upvotes"
            elif "hackernews" in source:
                label = "HN Score"
            elif "producthunt" in source:
                label = "Votes"
            else:
                label = "Stars"
            lines.append(f"| **{label}** | {stars} |")

        if p.get("author"):
            lines.append(f"| **作者** | {p['author']} |")
        if p.get("language"):
            lines.append(f"| **语言** | {p['language']} |")

        return "\n".join(lines)

    @staticmethod
    def _project_details(p: dict, is_top: bool) -> str:
        lines = [""]
        lines.append(f"**一句话总结**: {p.get('one_liner', 'N/A')}")

        if is_top:
            lines.append("")
            lines.append(f"**创新点**: {p.get('innovation', 'N/A')}")

            key_design = p.get("key_design", "")
            if key_design:
                lines.append("")
                lines.append(f"**关键设计**: {key_design}")

            risks = p.get("risks", "")
            if risks:
                lines.append("")
                lines.append(f"**风险与挑战**: {risks}")

            ai_integration = p.get("ai_integration", "")
            if ai_integration and "纯技术项目" not in ai_integration:
                lines.append("")
                lines.append(f"**AI 场景整合分析**: {ai_integration}")

            lines.append("")
            lines.append(f"**创始人/机构**: {p.get('founder_guess', '未知')}")

            qa = p.get("critique_qa", [])
            if qa:
                lines.append("")
                lines.append("**追问与补充分析**:")
                for item in qa:
                    lines.append(f"> **Q**: {item.get('question', '')}")
                    lines.append(f"> **A**: {item.get('answer', '')}")
                    lines.append(">")

        lines.append("")
        lines.append(f"**评分理由**: {p.get('reasoning', 'N/A')}")

        return "\n".join(lines)

    # ── 模板 fallback ─────────────────────────────────────

    @staticmethod
    def _template_executive_summary(projects: list[dict]) -> str:
        top3 = projects[:3]
        lines = ["## Executive Summary\n"]
        for i, p in enumerate(top3, 1):
            lines.append(
                f"{i}. **{p['name']}** (Score: {p['score']}/10) — "
                f"{p.get('one_liner', 'N/A')}"
            )

        source_counts: dict[str, int] = {}
        for p in projects:
            src = p.get("source", "unknown")
            source_counts[src] = source_counts.get(src, 0) + 1
        source_str = " | ".join(f"{k}: {v}" for k, v in source_counts.items())
        lines.append(f"\n> 信源分布: {source_str}\n")
        return "\n".join(lines)

    @staticmethod
    def _header(date: str, count: int) -> str:
        return f"""\
# AI Deal Sourcing Report

> **Date**: {date} | **Lookback**: {config.SOURCING_LOOKBACK_DAYS} days | **High-Value Projects**: {count} | **Generated by**: AI Deal Sourcing Agent

---
"""

    @staticmethod
    def _stats_section(stats: dict) -> str:
        lines = ["\n## Database Statistics\n"]
        lines.append(f"- 累计收录项目: **{stats.get('total', 0)}**")
        lines.append(f"- 平均评分: **{stats.get('avg_score', 0)}**")
        by_source = stats.get("by_source", {})
        if by_source:
            lines.append("- 各信源收录:")
            for src, count in by_source.items():
                lines.append(f"  - {src}: {count}")
        return "\n".join(lines)

    @staticmethod
    def _footer() -> str:
        ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
        return f"""
---

*Report generated at {ts} by AI Deal Sourcing Agent*
*Disclaimer: Scores are AI-generated estimates and should not be the sole basis for investment decisions.*
"""
