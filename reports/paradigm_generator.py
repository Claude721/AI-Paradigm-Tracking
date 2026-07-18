"""由研究总编辑 Agent 把结构化候选写成连贯的技术路线 memo。"""

from __future__ import annotations

import json
import logging
import re
from collections import defaultdict
from datetime import datetime
from pathlib import Path

import config
from agents.llm_utils import build_client
from paradigms.models import ParadigmCandidate, ResearcherProfile, TechnicalEvidence
from skills.loader import SkillLoader

logger = logging.getLogger(__name__)


class ParadigmReportGenerator:
    def __init__(
        self,
        output_dir: Path | str | None = None,
        client=None,
        model: str = "",
    ):
        self.output_dir = Path(output_dir) if output_dir else config.REPORTS_DIR
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.client = client
        self.model = model
        self.skill_loader = SkillLoader()

    def _get_client(self):
        if self.client is None:
            self.client, self.model = build_client("main")
        return self.client, self.model

    async def generate(
        self, candidates: list[ParadigmCandidate], pipeline_stats: dict | None = None
    ) -> Path:
        date = datetime.now().astimezone().strftime("%Y-%m-%d")
        path = self.output_dir / f"paradigm_radar_{date}.md"
        stats = pipeline_stats or {}
        ordered = sorted(candidates, key=lambda item: item.total_score, reverse=True)
        if not ordered:
            content = self._empty_report(date, stats)
        elif self.client is False:
            content = self._fallback_report(date, ordered, stats)
        else:
            try:
                content = await self._editorial_report(date, ordered, stats)
                if not _valid_editorial_report(content, ordered):
                    raise ValueError("总编辑输出未通过结构与反模板检查")
            except Exception:
                logger.exception("研究总编辑生成失败，使用无评分的路线式降级报告")
                content = self._fallback_report(date, ordered, stats)
        path.write_text(content.strip() + "\n", encoding="utf-8")
        return path

    async def _editorial_report(
        self, date: str, candidates: list[ParadigmCandidate], stats: dict
    ) -> str:
        dossiers = [_candidate_dossier(candidate) for candidate in candidates]
        prompt = self.skill_loader.render(
            "weekly_research_memo",
            date=date,
            lookback_days=config.SOURCING_LOOKBACK_DAYS,
            stats=json.dumps(_public_stats(stats), ensure_ascii=False),
            candidate_dossiers=json.dumps(dossiers, ensure_ascii=False),
        )
        client, model = self._get_client()
        response = await client.chat.completions.create(
            model=model,
            messages=[{"role": "user", "content": prompt}],
            temperature=0.3,
            max_tokens=7600,
        )
        return _strip_code_fence(response.choices[0].message.content or "")

    @staticmethod
    def _empty_report(date: str, stats: dict) -> str:
        return f"""# AI 技术范式雷达

> {date} · 最近 {config.SOURCING_LOOKBACK_DAYS} 天

## 本期研究 Memo

本期共扫描 {stats.get('origin_count', 0)} 篇论文与技术博客，但没有材料同时跨过**问题边界变化、机制可解释性和路线外延**三道门槛。这里不为了维持周报篇幅把局部 benchmark 改进包装成新范式。

## 接下来真正值得盯的信号

继续观察新的原始机制是否出现独立复现、跨团队承接或有内容的二次讨论。只有当讨论开始围绕设计思想、适用边界和新能力展开，而不只是转发论文标题时，扩散信号才真正成立。
"""

    def _fallback_report(
        self, date: str, candidates: list[ParadigmCandidate], stats: dict
    ) -> str:
        """LLM 不可用时仍输出路线式叙事，不回退到评分表和字段表。"""
        grouped: dict[str, list[ParadigmCandidate]] = defaultdict(list)
        for item in candidates:
            grouped[item.route_family or item.lineage_parent or item.name].append(item)
        route_names = list(grouped)
        opening = (
            f"本期扫描了 {stats.get('origin_count', 0)} 篇论文与技术博客，"
            f"最后能组织成 {len(route_names)} 条值得继续验证的技术路线。"
            + "；".join(
                f"**{name}**关注{items[0].background or items[0].problem_shift}"
                for name, items in list(grouped.items())[:4]
            )
            + "。这些材料目前大多仍是原始团队的单点提出，是否成为趋势，要看后续是否出现真正相关的复现、承接和二次解释。"
        )
        sections = [
            "# AI 技术范式雷达",
            "",
            f"> {date} · 最近 {config.SOURCING_LOOKBACK_DAYS} 天",
            "",
            "## 本期研究 Memo",
            "",
            opening,
        ]
        for route, items in grouped.items():
            sections.extend(["", f"## {route}", ""])
            lead = items[0]
            sections.append(
                f"这条路线面对的是{lead.background or lead.problem_shift}。"
                f"它背后的**设计思想**是{lead.design_philosophy or lead.thesis}。"
            )
            for item in items:
                papers = [
                    f"[{evidence.title}]({evidence.url})"
                    for evidence in item.evidence
                    if evidence.evidence_type.value in {"primary_paper", "technical_blog"}
                ][:3]
                sections.append(
                    f"{'、'.join(papers) or item.name}给出的具体解法是{item.technical_explanation or item.mechanism}。"
                    f"如果核心主张成立，直接价值在于{item.application_value or '把旧问题的能力边界向外推进'}。"
                )
                if item.objective_momentum_signals:
                    sections.append(
                        "目前可以确认的二次信号包括"
                        + "；".join(item.objective_momentum_signals)
                        + f"。{item.secondary_discussion_summary}"
                    )
            sections.extend(["", "### 谁在推动这条路线", ""])
            profiles = _unique_profiles(items)[:3]
            if not profiles:
                sections.append("本期资料只能确认论文署名，尚未形成可交叉核验的人物档案。")
            for profile in profiles:
                contacts = _contact_links(profile)
                search_note = "；".join(profile.contact_search_notes)
                sections.append(
                    f"**{profile.name}**，{profile.background_summary or profile.current_affiliation or '当前机构未公开'}。"
                    f"{profile.research_trajectory or profile.key_person_reason or '研究连续性仍待更多代表作验证。'}"
                    f"公开入口：{contacts or '未发现公开主页或邮箱'}。"
                    f"{('身份检索记录：' + search_note + '。') if search_note else ''}"
                )
        sections.extend(
            [
                "",
                "## 接下来真正值得盯的信号",
                "",
                "接下来应优先观察**独立复现、异构任务迁移和有内容的二次讨论**。如果新工作只是复述论文名或收录摘要，不算扩散；如果不同团队开始复用同一设计思想，并围绕它的边界和用途形成讨论，才说明路线正在从单点工作变成可持续的技术方向。",
            ]
        )
        return "\n".join(sections)


def _candidate_dossier(item: ParadigmCandidate) -> dict:
    return {
        "name": item.name,
        "route_family": item.route_family,
        "report_kind": item.report_kind,
        "thesis": item.thesis,
        "background": item.background,
        "problem_shift": item.problem_shift,
        "design_philosophy": item.design_philosophy,
        "mechanism": item.mechanism,
        "technical_explanation": item.technical_explanation,
        "application_value": item.application_value,
        "why_now": item.why_now,
        "lineage_path": item.lineage_path,
        "evidence_assessment": item.evidence_assessment,
        "objective_momentum_signals": item.objective_momentum_signals,
        "secondary_discussion_summary": item.secondary_discussion_summary,
        "trend_interpretation": item.trend_interpretation,
        "open_questions": item.open_questions,
        "evidence": [_evidence_dossier(value) for value in item.evidence[:20]],
        "researchers": [_researcher_dossier(value) for value in item.researchers[:3]],
    }


def _evidence_dossier(item: TechnicalEvidence) -> dict:
    return {
        "type": item.evidence_type.value,
        "source": item.source,
        "title": item.title,
        "url": item.url,
        "summary": item.summary[:1000],
        "published_at": item.published_at,
        "authors": item.authors,
        "organization": item.organization,
        "metrics": item.metrics,
        "historical": bool(item.raw.get("historical")),
        "relationship": item.raw.get("relationship", ""),
    }


def _researcher_dossier(profile: ResearcherProfile) -> dict:
    return {
        "name": profile.name,
        "role": profile.role,
        "current_affiliation": profile.current_affiliation,
        "background_summary": profile.background_summary,
        "research_trajectory": profile.research_trajectory,
        "key_person_reason": profile.key_person_reason,
        "representative_works": profile.representative_works[:6],
        "public_contacts": profile.public_contacts,
        "contact_search_notes": profile.contact_search_notes,
    }


def _public_stats(stats: dict) -> dict:
    keys = {
        "origin_count",
        "analysis_count",
        "candidate_extractions",
        "new_paradigms",
        "updated_paradigms",
    }
    return {key: value for key, value in stats.items() if key in keys}


def _valid_editorial_report(
    content: str, candidates: list[ParadigmCandidate] | None = None
) -> bool:
    forbidden = (
        "评分拆解",
        "扩散势能评分",
        "| 新颖性 |",
        "| 总分 |",
        "total_score",
        "novelty_score",
        "momentum_score",
    )
    memo_match = re.search(
        r"## 本期研究 Memo\s*(.*?)(?=\n##\s)", content, re.DOTALL
    )
    memo = memo_match.group(1) if memo_match else ""
    memo_chinese_characters = len(re.findall(r"[\u4e00-\u9fff]", memo))
    contains_table = bool(re.search(r"(?m)^\s*\|.+\|\s*$", content))
    has_numeric_score = bool(
        re.search(r"(?:总分|新颖性得分|趋势得分|声量得分)\s*[:：]?\s*\d", content)
    )
    inline_emphasis = len(re.findall(r"\*\*[^*\n]+\*\*", content))
    researcher_coverage = _covers_researchers(content, candidates or [])
    return (
        len(content.strip()) >= 800
        and "## 本期研究 Memo" in content
        and "## 接下来真正值得盯的信号" in content
        and 350 <= memo_chinese_characters <= 850
        and inline_emphasis >= 2
        and researcher_coverage
        and not contains_table
        and not has_numeric_score
        and not any(value in content for value in forbidden)
    )


def _covers_researchers(
    content: str, candidates: list[ParadigmCandidate]
) -> bool:
    """验证关键人物没有整体漏写，同时保留总编辑重组路线的自由。"""
    by_route: dict[str, set[str]] = defaultdict(set)
    for candidate in candidates:
        route = candidate.route_family or candidate.lineage_parent or candidate.name
        by_route[route].update(
            profile.name for profile in candidate.researchers if profile.name
        )
    searchable_routes = [names for names in by_route.values() if names]
    if not searchable_routes:
        return True
    covered = sum(any(name in content for name in names) for names in searchable_routes)
    return covered >= min(2, len(searchable_routes))


def _strip_code_fence(content: str) -> str:
    value = content.strip()
    match = re.fullmatch(r"```(?:markdown|md)?\s*(.*?)\s*```", value, re.DOTALL)
    return match.group(1).strip() if match else value


def _unique_profiles(items: list[ParadigmCandidate]) -> list[ResearcherProfile]:
    result: dict[str, ResearcherProfile] = {}
    for item in items:
        for profile in item.researchers:
            result.setdefault(profile.name.casefold(), profile)
    return list(result.values())


def _contact_links(profile: ResearcherProfile) -> str:
    links = []
    for label, url in profile.public_contacts.items():
        if label == "email":
            links.append(f"[{profile.public_email}](mailto:{profile.public_email})")
        else:
            links.append(f"[{label}]({url})")
    return "、".join(links)
