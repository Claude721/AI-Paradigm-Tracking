"""由研究总编辑 Agent 把结构化候选写成连贯的技术路线 memo。"""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime
from pathlib import Path

import config
from agents.llm_utils import build_client
from paradigms.models import ParadigmCandidate, ResearcherProfile, TechnicalEvidence
from run_audit import run_audit
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
            raise RuntimeError("无网络测试客户端不能生成正式研究报告")
        else:
            try:
                content = await self._editorial_report(date, ordered, stats)
                violations = _editorial_violations(content, ordered)
                if violations:
                    content = await self._revise_editorial_report(
                        date, ordered, content, violations
                    )
                    violations = _editorial_violations(content, ordered)
                if violations:
                    raise ValueError("；".join(violations))
            except Exception as exc:
                logger.exception("研究总编辑生成失败；拒绝发送字段拼装降级报告")
                raise RuntimeError("报告未通过编辑质量门槛，任务已停止并可安全重试") from exc
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
            mental_model_method=self.skill_loader.load("technical-mental-model"),
        )
        client, model = self._get_client()
        response = None
        try:
            response = await client.chat.completions.create(
                model=model,
                messages=[{"role": "user", "content": prompt}],
                temperature=0.3,
                max_tokens=7600,
            )
            run_audit.record_llm(
                stage="weekly_memo",
                role="main",
                model=model,
                subject=f"{date} / {len(candidates)} routes",
                response=response,
            )
            return _strip_code_fence(response.choices[0].message.content or "")
        except Exception as exc:
            run_audit.record_llm(
                stage="weekly_memo",
                role="main",
                model=model,
                subject=f"{date} / {len(candidates)} routes",
                response=response,
                error=exc,
            )
            raise

    async def _revise_editorial_report(
        self,
        date: str,
        candidates: list[ParadigmCandidate],
        previous_draft: str,
        violations: list[str],
    ) -> str:
        prompt = self.skill_loader.render(
            "weekly_memo_revision",
            date=date,
            lookback_days=config.SOURCING_LOOKBACK_DAYS,
            violations="；".join(violations),
            candidate_dossiers=json.dumps(
                [_candidate_dossier(candidate) for candidate in candidates],
                ensure_ascii=False,
            ),
            previous_draft=previous_draft[:16_000],
            mental_model_method=self.skill_loader.load("technical-mental-model"),
        )
        client, model = self._get_client()
        response = None
        try:
            response = await client.chat.completions.create(
                model=model,
                messages=[{"role": "user", "content": prompt}],
                temperature=0.2,
                max_tokens=7600,
            )
            run_audit.record_llm(
                stage="weekly_memo_revision",
                role="main",
                model=model,
                subject=f"{date} / {'; '.join(violations)}",
                response=response,
            )
            return _strip_code_fence(response.choices[0].message.content or "")
        except Exception as exc:
            run_audit.record_llm(
                stage="weekly_memo_revision",
                role="main",
                model=model,
                subject=f"{date} / {'; '.join(violations)}",
                response=response,
                error=exc,
            )
            raise

    @staticmethod
    def _empty_report(date: str, stats: dict) -> str:
        coverage = stats.get("frontier_coverage") or {}
        incomplete = [
            value.get("label", domain_id)
            for domain_id, value in (coverage.get("domains") or {}).items()
            if value.get("status") in {"query_failed", "not_executed"}
        ]
        failed_lanes = [
            name
            for name, value in (coverage.get("recall_lanes") or {}).items()
            if value.get("status") == "query_failed"
            or str(value.get("status", "")).startswith("not_executed_")
        ]
        academic_incomplete = [
            (
                f"{name}={value.get('status')}"
                f"(queries {value.get('completed_queries', 0)}/"
                f"{value.get('planned_queries', value.get('queries', 0))}, 429 "
                f"{value.get('rate_limited_requests', 0)})"
            )
            for name, value in (coverage.get("academic_indexes") or {}).items()
            if value.get("status")
            not in {"completed", "completed_after_retry"}
        ]
        official = coverage.get("official_pages") or {}
        official_incomplete = (
            int(official.get("checked_pages", 0) or 0)
            < int(official.get("total_pages", 0) or 0)
            or bool(official.get("request_failed"))
            or bool(official.get("parse_zero_links"))
            or bool(official.get("detail_failures"))
        )
        incomplete_parts = []
        if incomplete:
            incomplete_parts.append("领域：" + "、".join(incomplete))
        if failed_lanes:
            incomplete_parts.append("召回车道：" + "、".join(failed_lanes))
        if academic_incomplete:
            incomplete_parts.append("学术索引：" + "、".join(academic_incomplete))
        if official_incomplete:
            incomplete_parts.append(
                "官方入口："
                f"请求失败 {official.get('request_failed', 0)}、"
                f"解析零链接 {official.get('parse_zero_links', 0)}、"
                f"详情失败 {official.get('detail_failures', 0)}"
            )
        coverage_note = (
            "\n\n但本轮存在**召回覆盖未闭合**："
            + "；".join(incomplete_parts)
            + "。因此这是一份运行不完整的空报告，"
            "不能解释为这些领域没有创新；请结合随信附带的运行审计重试。"
            if incomplete_parts
            else ""
        )
        pending_work = int(stats.get("pending_work_count", 0) or 0)
        if pending_work:
            progress_note = (
                "\n\n本轮还存在**尚未完成研究判断的执行积压**："
                f"机制抽取完成 {stats.get('analysis_completed_count', stats.get('analysis_count', 0))}/"
                f"{stats.get('planned_analysis_count', 0)} 条；"
                f"待抽取 {stats.get('analysis_deferred_count', 0)} 条，"
                f"待深挖 {stats.get('candidate_deferred_count', 0)} 条，"
                f"待刷新 {stats.get('refresh_deferred_count', 0)} 条。"
                "这些材料只是因软时间预算或显式 safety limit 延后，"
                "并未被 Rubric 淘汰；因此本期空白不能解释为近期没有新范式。"
            )
        else:
            progress_note = ""
        return f"""# AI 技术范式雷达

> {date} · 发现窗口 {stats.get('discovery_lookback_days', config.SOURCING_LOOKBACK_DAYS)} 天

## 本期研究 Memo

本期共扫描 {stats.get('origin_count', 0)} 篇论文、Technical Report 与官方技术博客，但在本轮已经完成研究判断的材料中，没有内容同时跨过**技术外延、发布者可信度和外部承接**三道门槛。技术范式不会按周出现，这一期不为了维持篇幅把局部 benchmark 改进或作者的宏大叙事包装成趋势。{coverage_note}{progress_note}

## 接下来真正值得盯的信号

继续观察新的原始机制是否出现独立复现、跨团队承接或有内容的二次讨论。只有当讨论开始围绕设计思想、适用边界和新能力展开，而不只是转发论文标题时，扩散信号才真正成立。
"""

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
        "mental_model": item.mental_model,
        "application_value": item.application_value,
        "why_now": item.why_now,
        "lineage_path": item.lineage_path,
        "evidence_assessment": item.evidence_assessment,
        "objective_momentum_signals": item.objective_momentum_signals,
        "community_coverage": item.community_coverage,
        "secondary_discussion_summary": item.secondary_discussion_summary,
        "trend_interpretation": item.trend_interpretation,
        "open_questions": item.open_questions,
        "publisher_tier": item.publisher_tier,
        "publisher_evidence": item.publisher_evidence,
        "admission_reason": item.admission_reason,
        "is_formal_technical_report": item.is_formal_technical_report,
        "marketing_overclaim_risk": item.marketing_overclaim_risk,
        "frontier_domains": sorted(
            {
                str(domain)
                for evidence in item.evidence
                for domain in (evidence.raw.get("frontier_domains") or [])
                if domain
            }
        ),
        "evidence": [_evidence_dossier(value) for value in item.evidence[:20]],
        "researchers": [
            _researcher_dossier(value)
            for value in item.researchers[
                : config.PARADIGM_RESEARCHER_PROFILE_LIMIT
            ]
        ],
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
        "metric_delta": item.raw.get("metric_delta", {}),
    }


def _researcher_dossier(profile: ResearcherProfile) -> dict:
    return {
        "name": profile.name,
        "role": profile.role,
        "current_affiliation": profile.current_affiliation,
        "background_summary": profile.background_summary,
        "public_bio_excerpt": profile.public_bio_excerpt,
        "prior_affiliations": profile.prior_affiliations,
        "research_trajectory": profile.research_trajectory,
        "key_person_reason": profile.key_person_reason,
        "representative_works": profile.representative_works[:6],
        "public_contacts": profile.public_contacts,
        "contact_search_notes": profile.contact_search_notes,
    }


def _public_stats(stats: dict) -> dict:
    keys = {
        "origin_count",
        "planned_analysis_count",
        "analysis_count",
        "analysis_completed_count",
        "analysis_deferred_count",
        "candidate_deferred_count",
        "refresh_deferred_count",
        "run_incomplete",
        "candidate_extractions",
        "new_paradigms",
        "updated_paradigms",
    }
    return {key: value for key, value in stats.items() if key in keys}


def _valid_editorial_report(
    content: str, candidates: list[ParadigmCandidate] | None = None
) -> bool:
    return not _editorial_violations(content, candidates or [])


def _editorial_violations(
    content: str, candidates: list[ParadigmCandidate] | None = None
) -> list[str]:
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
    violations = []
    if len(content.strip()) < 800:
        violations.append("正文过短")
    if "## 本期研究 Memo" not in content:
        violations.append("缺少本期研究 Memo")
    if "## 接下来真正值得盯的信号" not in content:
        violations.append("缺少后续观察信号")
    if not 350 <= memo_chinese_characters <= 850:
        violations.append("开篇 Memo 中文长度不在合理范围")
    if inline_emphasis < 2:
        violations.append("缺少行内重点强调")
    if not researcher_coverage:
        violations.append("关键人物覆盖不足")
    if contains_table:
        violations.append("出现表格")
    if has_numeric_score or any(value in content for value in forbidden):
        violations.append("出现内部评分")
    if _has_long_english_excerpt(content):
        violations.append("出现英文原文长句或成段摘录")
    return violations


def _has_long_english_excerpt(content: str) -> bool:
    without_links = re.sub(r"\[[^\]]+\]\([^)]+\)", "", content)
    without_urls = re.sub(r"https?://\S+", "", without_links)
    for paragraph in re.split(r"\n\s*\n", without_urls):
        english_words = re.findall(r"\b[A-Za-z][A-Za-z0-9'-]*\b", paragraph)
        chinese_characters = re.findall(r"[\u4e00-\u9fff]", paragraph)
        if len(english_words) >= 18 and len(english_words) > len(chinese_characters) / 2:
            return True
    return False


def _covers_researchers(
    content: str, candidates: list[ParadigmCandidate]
) -> bool:
    """验证关键人物没有整体漏写，同时保留总编辑重组路线的自由。"""
    by_route: dict[str, set[str]] = {}
    for candidate in candidates:
        route = candidate.route_family or candidate.lineage_parent or candidate.name
        by_route.setdefault(route, set()).update(
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
