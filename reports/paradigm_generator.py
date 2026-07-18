"""按“技术范式”而非项目生成证据化周报。"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

import config
from paradigms.models import EvidenceType, ParadigmCandidate, TechnicalEvidence


TYPE_LABELS = {
    EvidenceType.PRIMARY_PAPER: "原始论文",
    EvidenceType.TECHNICAL_BLOG: "技术博客",
    EvidenceType.PEER_REVIEW: "同行评议",
    EvidenceType.INDEPENDENT_REPLICATION: "独立复现",
    EvidenceType.IMPLEMENTATION: "代码实现",
    EvidenceType.CITATION: "引用网络",
    EvidenceType.COMMUNITY_DISCUSSION: "社区讨论",
    EvidenceType.SECONDARY_INTERPRETATION: "二次解读",
    EvidenceType.PRODUCT_ADOPTION: "产品采用",
}


class ParadigmReportGenerator:
    def __init__(self, output_dir: Path | str | None = None):
        self.output_dir = Path(output_dir) if output_dir else config.REPORTS_DIR
        self.output_dir.mkdir(parents=True, exist_ok=True)

    async def generate(
        self, candidates: list[ParadigmCandidate], pipeline_stats: dict | None = None
    ) -> Path:
        date = datetime.now().astimezone().strftime("%Y-%m-%d")
        path = self.output_dir / f"paradigm_radar_{date}.md"
        ordered = sorted(candidates, key=lambda item: item.total_score, reverse=True)
        sections = [self._header(date, ordered, pipeline_stats or {})]
        if not ordered:
            sections.append(
                "\n> 本期没有候选同时通过“新机制、范式外延、技术扎实度”三道门。"
                "观察池内容不会为凑数进入邮件。\n"
            )
        else:
            sections.append(self._radar_table(ordered))
            for index, candidate in enumerate(ordered, 1):
                sections.append(self._candidate_section(index, candidate))
        sections.append(
            "\n---\n\n本报告中的联系方式仅来自公开专业页面；空缺表示未能验证，"
            "不会使用猜测邮箱。声量是验证信号，不是入选门槛。\n"
        )
        path.write_text("\n".join(sections), encoding="utf-8")
        return path

    @staticmethod
    def _header(
        date: str, candidates: list[ParadigmCandidate], stats: dict
    ) -> str:
        new_count = sum(item.report_kind == "new" for item in candidates)
        update_count = sum(item.report_kind == "update" for item in candidates)
        return f"""# AI 技术范式雷达

> 日期：{date} ｜ 回看窗口：最近 {config.SOURCING_LOOKBACK_DAYS} 天 ｜ 新范式：{new_count} ｜ 进展更新：{update_count}

本期扫描 {stats.get('origin_count', 0)} 篇论文/技术博客。报告只保留可能改变能力边界、学习方式、架构、数据范式或行动闭环的候选；绝对热度不作为准入条件。

---
"""

    @staticmethod
    def _radar_table(candidates: list[ParadigmCandidate]) -> str:
        lines = [
            "## 本期雷达",
            "",
            "| 状态 | 类型 | 技术范式 | 总分 | 跨平台证据 |",
            "|---|---|---|---:|---:|",
        ]
        for item in candidates:
            kind = "首次捕捉" if item.report_kind == "new" else "进展更新"
            lines.append(
                f"| {item.status} | {kind} | {item.name} | {item.total_score:.1f} | {len(item.evidence_sources)} |"
            )
        return "\n".join(lines)

    def _candidate_section(self, index: int, item: ParadigmCandidate) -> str:
        lineage = " → ".join(item.lineage_path) or "尚未形成清晰谱系"
        lines = [
            f"## {index}. {item.name}",
            "",
            f"> **{item.thesis}**",
            "",
            f"- **技术谱系**：{lineage}",
            f"- **问题边界的变化**：{item.problem_shift}",
            f"- **核心机制**：{item.mechanism}",
            f"- **为什么是现在**：{item.why_now or '现有证据不足，需继续追踪。'}",
            f"- **创新类型**：{item.novelty_type or '未归类'}",
            "",
            "### 趋势与扎实度证据",
            "",
            self._evidence_table(item.evidence),
            "",
            f"**证据判断**：{item.evidence_assessment or '当前由原始材料与平台指标自动聚合，仍需人工复核独立性。'}",
            "",
            self._momentum_summary(item),
            "",
            self._open_questions(item),
            "",
            "### 关键人物",
            "",
            self._people(item),
            "",
            "### 评分拆解",
            "",
            "| 新颖性 | 技术扎实度 | 范式外延 | 扩散势能 | 人物连续性 | 增量惩罚 | 总分 |",
            "|---:|---:|---:|---:|---:|---:|---:|",
            f"| {item.novelty_score:.1f} | {item.effective_solidity_score:.1f} | {item.scope_score:.1f} | {item.momentum_score:.1f} | {item.researcher_score:.1f} | -{item.incremental_penalty:.1f} | **{item.total_score:.1f}/100** |",
        ]
        return "\n".join(lines)

    @staticmethod
    def _evidence_table(evidence: list[TechnicalEvidence]) -> str:
        lines = [
            "| 批次 | 证据类型 | 平台 | 数据/现象 | 时间 | 链接 |",
            "|---|---|---|---|---|---|",
        ]
        ordered = sorted(evidence, key=lambda item: bool(item.raw.get("historical")))
        for item in ordered[:15]:
            metrics = ", ".join(
                f"{key}={value}" for key, value in item.metrics.items() if value not in (None, "", 0)
            ) or "已发现"
            title = item.title.replace("|", "\\|")[:90]
            date = (item.published_at or "未知")[:10]
            batch = "历史" if item.raw.get("historical") else "本期"
            lines.append(
                f"| {batch} | {TYPE_LABELS.get(item.evidence_type, item.evidence_type.value)} | "
                f"{item.source} | {metrics} | {date} | [{title}]({item.url}) |"
            )
        return "\n".join(lines)

    @staticmethod
    def _momentum_summary(item: ParadigmCandidate) -> str:
        sources = "、".join(sorted(item.evidence_sources))
        replications = sum(
            evidence.evidence_type
            in {EvidenceType.IMPLEMENTATION, EvidenceType.INDEPENDENT_REPLICATION}
            for evidence in item.evidence
        )
        discussions = sum(
            evidence.evidence_type == EvidenceType.COMMUNITY_DISCUSSION
            for evidence in item.evidence
        )
        computed = (
            f"**趋势判断**：目前在 {sources or '单一来源'} 出现信号；"
            f"可见实现/复现 {replications} 条，社区讨论 {discussions} 条。"
            f"扩散势能评分 {item.momentum_score:.1f}/10。"
        )
        if item.trend_interpretation:
            return f"{computed}\n\n{item.trend_interpretation}"
        return computed

    @staticmethod
    def _open_questions(item: ParadigmCandidate) -> str:
        if not item.open_questions:
            return "**待验证问题**：尚未形成独立复现或跨任务泛化证据时，应继续观察。"
        return "**待验证问题**：\n" + "\n".join(
            f"- {question}" for question in item.open_questions
        )

    @staticmethod
    def _people(item: ParadigmCandidate) -> str:
        if not item.researchers:
            authors = list(dict.fromkeys(
                author for evidence in item.evidence for author in evidence.authors if author
            ))[:5]
            if authors:
                return "已识别作者：" + "、".join(authors) + "。公开履历尚未完成验证。"
            return "尚未获得可验证的作者履历。"
        lines = []
        for profile in item.researchers[:3]:
            lines.extend(
                [
                    f"#### {profile.name}（{profile.role or '作者'}）",
                    "",
                    f"- **当前机构**：{profile.current_affiliation or '未验证'}",
                    f"- **研究连续性**：{profile.research_trajectory or '资料不足，待人工复核。'}",
                    f"- **连续性评分**：{profile.trajectory_consistency:.1f}/10",
                ]
            )
            if profile.representative_works:
                lines.append("- **此前代表作**：")
                for work in profile.representative_works[:4]:
                    lines.append(
                        f"  - [{work.get('title', '未命名')}]({work.get('url', '')}) "
                        f"({work.get('year', '未知')})"
                    )
            contacts = profile.public_contacts
            if contacts:
                links = " ｜ ".join(f"[{key}]({url})" for key, url in contacts.items())
                lines.append(f"- **公开专业联系方式/主页**：{links}")
            else:
                lines.append("- **公开专业联系方式/主页**：未验证")
            lines.append("")
        return "\n".join(lines)
