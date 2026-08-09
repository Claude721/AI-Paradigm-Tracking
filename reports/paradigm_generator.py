"""由研究总编辑 Agent 把结构化候选写成连贯的技术路线 memo。"""

from __future__ import annotations

import json
import logging
import re
from collections import OrderedDict
from datetime import datetime
from pathlib import Path

import config
from agents.llm_utils import build_client
from paradigms.models import (
    EvidenceType,
    ParadigmCandidate,
    ResearcherProfile,
    TechnicalEvidence,
    safe_public_contact_target,
)
from run_audit import run_audit
from skills.loader import SkillLoader

logger = logging.getLogger(__name__)

MOMENTUM_BRIEF_LABEL = "讨论势能判断"


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
        self,
        candidates: list[ParadigmCandidate],
        pipeline_stats: dict | None = None,
        *,
        report_date: str = "",
    ) -> Path:
        date = report_date or datetime.now().astimezone().strftime("%Y-%m-%d")
        path = self.output_dir / f"paradigm_radar_{date}.md"
        stats = pipeline_stats or {}
        ordered = sorted(candidates, key=lambda item: item.total_score, reverse=True)
        if not ordered:
            content = self._empty_report(date, stats)
        elif self.client is False:
            raise RuntimeError("无网络测试客户端不能生成正式研究报告")
        else:
            preflight = _report_input_violations(ordered)
            if preflight:
                run_audit.event(
                    "weekly_memo_quality",
                    "input_incomplete",
                    "；".join(preflight),
                )
                raise RuntimeError(
                    "报告输入未满足人物/原文交付契约，候选应留在待补全状态："
                    + "；".join(preflight)
                )
            try:
                content = await self._editorial_report(date, ordered, stats)
                revision_requests = _editorial_violations(
                    content,
                    ordered,
                    require_primary_sources=False,
                    require_researcher_index=False,
                )
                draft_advisories = _editorial_advisories(content)
                run_audit.event(
                    "weekly_memo_quality",
                    (
                        "revision_requested"
                        if revision_requests
                        else "draft_passed_with_advisory"
                        if draft_advisories
                        else "draft_passed"
                    ),
                    _quality_event_detail(
                        content, ordered, [*revision_requests, *draft_advisories]
                    ),
                )
                if revision_requests:
                    content = await self._revise_editorial_report(
                        date, ordered, content, revision_requests
                    )
                # 原文 URL 来自已经核验并持久化的证据对象，不再依赖模型抄写。
                # 总编辑仍应在正文自然链接论文；此处的确定性索引确保即使模型
                # 漏写，最终邮件里的每条路线也一定能追溯到一手材料。
                content = _attach_coverage_boundary(content, stats)
                content = _attach_researcher_index(content, ordered)
                content = _attach_primary_source_index(content, ordered)
                violations = _editorial_violations(content, ordered)
                if violations:
                    run_audit.event(
                        "weekly_memo_quality",
                        "failed",
                        _quality_event_detail(content, ordered, violations),
                    )
                    raise ValueError("；".join(violations))
                advisories = _editorial_advisories(content)
                run_audit.event(
                    "weekly_memo_quality",
                    "passed_with_advisory" if advisories else "passed",
                    _quality_event_detail(content, ordered, advisories),
                )
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
                f"待刷新 {stats.get('refresh_deferred_count', 0)} 条，"
                f"待补全人物交付信息 {stats.get('delivery_profile_deferred_count', 0)} 条，"
                f"待补全一手链接 {stats.get('delivery_source_deferred_count', 0)} 条。"
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
    primary_sources = _primary_sources(item)
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
        # 与完整 evidence 分开提供经过准入语义过滤的扩散证据，避免总编辑
        # 把作者自发帖、搜索引擎索引命中或论文聚合页误写成社区势能。
        "momentum_evidence": [
            _evidence_dossier(value) for value in _momentum_evidence(item)
        ],
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
        "primary_sources": [_evidence_dossier(value) for value in primary_sources],
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
        "delivery_profile_deferred_count",
        "delivery_source_deferred_count",
        "run_incomplete",
        "candidate_extractions",
        "new_paradigms",
        "updated_paradigms",
    }
    return {key: value for key, value in stats.items() if key in keys}


def _report_input_violations(
    candidates: list[ParadigmCandidate],
) -> list[str]:
    violations = _researcher_profile_violations(candidates)
    for candidate in candidates:
        if not _primary_sources(candidate):
            route = candidate.route_family or candidate.lineage_parent or candidate.name
            violations.append(f"路线「{route}」没有安全的一手材料 URL")
    return violations


def _valid_editorial_report(
    content: str, candidates: list[ParadigmCandidate] | None = None
) -> bool:
    return not _editorial_violations(content, candidates or [])


def _editorial_violations(
    content: str,
    candidates: list[ParadigmCandidate] | None = None,
    *,
    require_primary_sources: bool = True,
    require_researcher_index: bool = True,
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
        r"(?ms)^## 本期研究 Memo\s*$\s*(.*?)(?=^#{2,6}\s|\Z)", content
    )
    memo = memo_match.group(1) if memo_match else ""
    memo_chinese_characters = len(re.findall(r"[\u4e00-\u9fff]", memo))
    contains_table = bool(re.search(r"(?m)^\s*\|.+\|\s*$", content))
    has_numeric_score = bool(
        re.search(r"(?:总分|新颖性得分|趋势得分|声量得分)\s*[:：]?\s*\d", content)
    )
    researcher_coverage = _covers_researchers(content, candidates or [])
    violations = []
    if len(content.strip()) < 800:
        violations.append("正文过短")
    if "## 本期研究 Memo" not in content:
        violations.append("缺少本期研究 Memo")
    if "## 接下来真正值得盯的信号" not in content:
        violations.append("缺少后续观察信号")
    # 450–650 字是编辑目标；这里保留更宽但仍有意义的交付边界。
    # 轻微偏离只写入审计，不消耗全文重写，也不会销毁整轮研究结果。
    if not 250 <= memo_chinese_characters <= 1000:
        violations.append(
            f"开篇 Memo 中文长度为 {memo_chinese_characters}，交付范围为 250–1000"
        )
    if not researcher_coverage:
        violations.append("关键人物覆盖不足")
    if contains_table:
        violations.append("出现表格")
    if has_numeric_score or any(value in content for value in forbidden):
        violations.append("出现内部评分")
    if _has_long_english_excerpt(content):
        violations.append("出现英文原文长句或成段摘录")
    violations.extend(_momentum_brief_violations(content, candidates or []))
    violations.extend(_researcher_profile_violations(candidates or []))
    if require_researcher_index and candidates:
        if "## 关键人物与公开联系入口" not in content:
            violations.append("缺少确定性的关键人物与公开联系入口")
    if require_primary_sources:
        violations.extend(_primary_source_violations(content, candidates or []))
    return violations


def _editorial_advisories(content: str) -> list[str]:
    """记录纯排版偏离；不为它消耗一次全文重写或销毁研究结果。"""
    memo_match = re.search(
        r"(?ms)^## 本期研究 Memo\s*$\s*(.*?)(?=^#{2,6}\s|\Z)", content
    )
    memo = memo_match.group(1) if memo_match else ""
    memo_chinese_characters = len(re.findall(r"[\u4e00-\u9fff]", memo))
    editorial_body = _editorial_body(content)
    inline_emphasis = len(re.findall(r"\*\*[^*\n]+\*\*", editorial_body))
    advisories = []
    if memo_match and not 450 <= memo_chinese_characters <= 650:
        advisories.append(
            f"开篇 Memo 中文长度为 {memo_chinese_characters}，编辑目标为 450–650"
        )
    if inline_emphasis < 2:
        advisories.append(
            f"行内重点强调为 {inline_emphasis} 处，编辑目标为至少 2 处"
        )
    return advisories


def _primary_sources(candidate: ParadigmCandidate) -> list[TechnicalEvidence]:
    """返回本期路线可直接追溯的一手材料，优先本周新增而非历史证据。"""
    primary_types = {EvidenceType.PRIMARY_PAPER, EvidenceType.TECHNICAL_BLOG}
    sources = [
        value
        for value in candidate.evidence
        if value.evidence_type in primary_types
        and safe_public_contact_target("source", value.url)
    ]
    current = [value for value in sources if not value.raw.get("historical")]
    historical = [value for value in sources if value.raw.get("historical")]
    # 新报告列出本轮所有直接材料；进展更新再补最多三个路线起点，既能
    # 回到原始工作，也避免跨周证据积累把索引膨胀成论文清单。
    selected = [*current, *historical[:3]]
    deduplicated: list[TechnicalEvidence] = []
    seen: set[str] = set()
    for value in selected:
        normalized = value.url.strip().rstrip("/").casefold()
        if normalized in seen:
            continue
        seen.add(normalized)
        deduplicated.append(value)
    return deduplicated


def _momentum_evidence(candidate: ParadigmCandidate) -> list[TechnicalEvidence]:
    """只向总编辑暴露能参与扩散判断的证据，保留低声量和反证。"""

    evidence_types = {
        EvidenceType.PEER_REVIEW,
        EvidenceType.INDEPENDENT_REPLICATION,
        EvidenceType.IMPLEMENTATION,
        EvidenceType.CITATION,
        EvidenceType.COMMUNITY_DISCUSSION,
        EvidenceType.SECONDARY_INTERPRETATION,
        EvidenceType.PRODUCT_ADOPTION,
    }
    selected = []
    for value in candidate.evidence:
        if value.evidence_type not in evidence_types:
            continue
        if value.raw.get("indexed_discovery_only"):
            continue
        if value.raw.get("relationship") == "author_self_release":
            continue
        selected.append(value)
    return selected[:12]


def _momentum_brief_violations(
    content: str, candidates: list[ParadigmCandidate]
) -> list[str]:
    """每条交付路线都要给出简短、可审计的势能结论，而非内部评分。"""

    if not candidates:
        return []
    routes = {
        (candidate.route_family or candidate.lineage_parent or candidate.name).strip()
        for candidate in candidates
    }
    expected = len({route for route in routes if route})
    body = _editorial_body(content)
    observed = len(
        re.findall(
            rf"\*\*(?:当前)?{re.escape(MOMENTUM_BRIEF_LABEL)}\s*[：:]?\*\*",
            body,
        )
    )
    if observed < expected:
        return [
            f"讨论势能判断覆盖不足：需要 {expected} 条，实际 {observed} 条"
        ]
    return []


def _primary_source_violations(
    content: str, candidates: list[ParadigmCandidate]
) -> list[str]:
    if not candidates:
        return []
    violations = []
    linked_urls = _markdown_link_targets(content)
    if "## 原文与一手资料" not in content:
        violations.append("缺少确定性的原文与一手资料索引")
    for candidate in candidates:
        sources = _primary_sources(candidate)
        route = candidate.route_family or candidate.lineage_parent or candidate.name
        if not sources:
            violations.append(f"路线「{route}」没有可验证的一手材料 URL")
            continue
        if not any(_normalized_url(value.url) in linked_urls for value in sources):
            violations.append(f"路线「{route}」没有附上可点击的一手材料链接")
    return violations


def _attach_primary_source_index(
    content: str, candidates: list[ParadigmCandidate]
) -> str:
    """在结尾观察信号之前插入由证据对象确定性渲染的原文索引。"""
    if not candidates:
        return content
    # 即使模型自行生成了同名章节，也以证据对象重建，避免 URL 被改写或猜测。
    value = _without_primary_source_index(content).strip()
    grouped: OrderedDict[str, list[TechnicalEvidence]] = OrderedDict()
    for candidate in candidates:
        route = candidate.route_family or candidate.lineage_parent or candidate.name
        bucket = grouped.setdefault(route, [])
        existing = {item.url.strip().rstrip("/").casefold() for item in bucket}
        for source in _primary_sources(candidate):
            normalized = source.url.strip().rstrip("/").casefold()
            if normalized not in existing:
                bucket.append(source)
                existing.add(normalized)

    lines = [
        "## 原文与一手资料",
        "",
        "以下链接由系统直接从入选路线的一手证据生成，便于回到论文或官方技术材料核验。",
        "",
    ]
    for route, sources in grouped.items():
        safe_route = _markdown_label(route)
        links = "；".join(
            f"[{_markdown_label(source.title or source.url)}](<{source.url.strip()}>)"
            for source in sources
        )
        lines.append(f"- **{safe_route}**：{links or '缺少可验证的一手材料链接'}")
    source_index = "\n".join(lines)
    closing = re.search(r"(?m)^## 接下来真正值得盯的信号\s*$", value)
    if closing:
        return (
            value[: closing.start()].rstrip()
            + "\n\n"
            + source_index
            + "\n\n"
            + value[closing.start() :].lstrip()
        )
    return value + "\n\n" + source_index


def _markdown_label(value: str) -> str:
    return (
        re.sub(r"[\[\]\r\n<>`*_]+", " ", str(value)).strip()
        or "未命名材料"
    )


def _without_primary_source_index(content: str) -> str:
    return re.sub(
        r"(?ms)\n*^## 原文与一手资料\s*$.*?(?=^##\s|\Z)",
        "\n",
        content,
    )


def _researcher_profile_violations(
    candidates: list[ParadigmCandidate],
) -> list[str]:
    """Ensure each reported route has a verified person and a completed lookup."""

    violations = []
    grouped: OrderedDict[str, list[ResearcherProfile]] = OrderedDict()
    for candidate in candidates:
        route = candidate.route_family or candidate.lineage_parent or candidate.name
        grouped.setdefault(route, []).extend(candidate.researchers)
    for route, profiles in grouped.items():
        named = [profile for profile in profiles if profile.name.strip()]
        if not named:
            violations.append(f"路线「{route}」缺少可核验关键人物")
            continue
        missing_background = [
            profile.name
            for profile in named
            if not (
                profile.current_affiliation
                or profile.background_summary
                or profile.prior_affiliations
                or profile.research_trajectory
                or profile.key_person_reason
            )
        ]
        if missing_background:
            violations.append(
                f"路线「{route}」关键人物缺少背景或机构信息："
                + "、".join(missing_background)
            )
        missing_lookup = [
            profile.name
            for profile in named
            if not profile.contact_lookup_completed
        ]
        if missing_lookup:
            violations.append(
                f"路线「{route}」未完成公开联系方式检索："
                + "、".join(missing_lookup)
            )
    return violations


def _attach_researcher_index(
    content: str, candidates: list[ParadigmCandidate]
) -> str:
    """Deterministically render verified people and public professional contacts."""

    if not candidates:
        return content
    value = _without_researcher_index(content).strip()
    grouped: OrderedDict[str, list[ResearcherProfile]] = OrderedDict()
    for candidate in candidates:
        route = candidate.route_family or candidate.lineage_parent or candidate.name
        bucket = grouped.setdefault(route, [])
        seen = {profile.name.casefold() for profile in bucket if profile.name}
        for profile in candidate.researchers:
            if profile.name and profile.name.casefold() not in seen:
                bucket.append(profile)
                seen.add(profile.name.casefold())

    lines = [
        "## 关键人物与公开联系入口",
        "",
        "以下信息由已核验人物档案确定性生成；只列公开职业信息，不猜测邮箱。",
        "",
    ]
    for route, profiles in grouped.items():
        lines.append(f"### {_markdown_label(route)}")
        lines.append("")
        if not profiles:
            lines.append("- 缺少可核验关键人物。")
            lines.append("")
            continue
        for profile in profiles[: config.PARADIGM_RESEARCHER_PROFILE_LIMIT]:
            affiliation = profile.current_affiliation or "机构待进一步核验"
            background = (
                profile.background_summary
                or profile.research_trajectory
                or profile.key_person_reason
                or "背景资料待进一步核验"
            )
            contacts = []
            for label, target in profile.public_contacts.items():
                target = safe_public_contact_target(str(label), str(target))
                if label == "email" and target:
                    contacts.append(f"[邮箱](mailto:{target})")
                elif target:
                    contacts.append(
                        f"[{_markdown_label(label)}](<{target}>)"
                    )
            contact_text = "、".join(contacts)
            lookup_text = ""
            if not contact_text:
                contact_text = "未找到可核验的公开联系入口"
                notes = [
                    _markdown_label(note)
                    for note in profile.contact_search_notes[:3]
                    if note
                ]
                if notes:
                    lookup_text = "；检索记录：" + "、".join(notes)
            lines.append(
                f"- **{_markdown_label(profile.name)}**（{_markdown_label(affiliation)}）："
                f"{_markdown_label(background)}；公开联系：{contact_text}"
                f"{lookup_text}。"
            )
        lines.append("")

    researcher_index = "\n".join(lines).rstrip()
    source_heading = re.search(r"(?m)^## 原文与一手资料\s*$", value)
    closing = re.search(r"(?m)^## 接下来真正值得盯的信号\s*$", value)
    insertion = source_heading or closing
    if insertion:
        return (
            value[: insertion.start()].rstrip()
            + "\n\n"
            + researcher_index
            + "\n\n"
            + value[insertion.start() :].lstrip()
        )
    return value + "\n\n" + researcher_index


def _without_researcher_index(content: str) -> str:
    return re.sub(
        r"(?ms)\n*^## 关键人物与公开联系入口\s*$.*?(?=^##\s|\Z)",
        "\n",
        content,
    )


def _attach_coverage_boundary(content: str, stats: dict) -> str:
    """Deterministically disclose degraded recall and unfinished backlog."""

    coverage = stats.get("frontier_coverage") or {}
    issues = []
    failed_lanes = [
        name
        for name, value in (coverage.get("recall_lanes") or {}).items()
        if value.get("status") == "query_failed"
        or str(value.get("status", "")).startswith("not_executed_")
    ]
    if failed_lanes:
        issues.append("未闭合召回车道：" + "、".join(failed_lanes))
    degraded_indexes = [
        f"{name}={value.get('status')}"
        for name, value in (coverage.get("academic_indexes") or {}).items()
        if value.get("status") not in {"completed", "completed_after_retry"}
    ]
    if degraded_indexes:
        issues.append("学术索引退化：" + "、".join(degraded_indexes))
    official = coverage.get("official_pages") or {}
    if (
        official.get("request_failed")
        or official.get("parse_zero_links")
        or official.get("detail_failures")
    ):
        issues.append(
            "官方入口异常：请求失败 "
            f"{official.get('request_failed', 0)}、解析零链接 "
            f"{official.get('parse_zero_links', 0)}、详情失败 "
            f"{official.get('detail_failures', 0)}"
        )
    pending = int(stats.get("pending_work_count", 0) or 0)
    if pending:
        issues.append(f"仍有 {pending} 项研究积压，将在后续运行续跑")
    if not issues:
        return content

    value = re.sub(
        r"(?ms)\n*^## 本轮覆盖边界\s*$.*?(?=^##\s|\Z)",
        "\n",
        content,
    ).strip()
    section = (
        "## 本轮覆盖边界\n\n"
        "本期结论只覆盖已经完成检索与研究判断的材料。"
        + "；".join(issues)
        + "。这些缺口不能被解释为对应领域没有新进展。"
    )
    insertion = re.search(
        r"(?m)^## (?:关键人物与公开联系入口|原文与一手资料|接下来真正值得盯的信号)\s*$",
        value,
    )
    if insertion:
        return (
            value[: insertion.start()].rstrip()
            + "\n\n"
            + section
            + "\n\n"
            + value[insertion.start() :].lstrip()
        )
    return value + "\n\n" + section


def _editorial_body(content: str) -> str:
    return _without_researcher_index(_without_primary_source_index(content))


def _normalized_url(value: str) -> str:
    return value.strip().rstrip("/").casefold()


def _markdown_link_targets(content: str) -> set[str]:
    """提取 Markdown 链接目标；裸 URL 不算作已满足可点击交付契约。"""
    targets = set()
    pattern = re.compile(
        r"\[[^\]]+\]\(\s*(?:<(?P<angle>https?://[^>]+)>|"
        r"(?P<plain>https?://[^)\s]+))\s*\)",
        re.IGNORECASE,
    )
    for match in pattern.finditer(content):
        target = match.group("angle") or match.group("plain") or ""
        targets.add(_normalized_url(target))
    return targets


def _quality_event_detail(
    content: str,
    candidates: list[ParadigmCandidate],
    issues: list[str],
) -> str:
    memo_match = re.search(
        r"(?ms)^## 本期研究 Memo\s*$\s*(.*?)(?=^#{2,6}\s|\Z)", content
    )
    memo = memo_match.group(1) if memo_match else ""
    memo_characters = len(re.findall(r"[\u4e00-\u9fff]", memo))
    editorial_body = _editorial_body(content)
    emphasis_count = len(re.findall(r"\*\*[^*\n]+\*\*", editorial_body))
    linked_urls = _markdown_link_targets(content)
    source_routes = sum(bool(_primary_sources(value)) for value in candidates)
    linked_routes = sum(
        bool(_primary_sources(value))
        and any(
            _normalized_url(source.url) in linked_urls
            for source in _primary_sources(value)
        )
        for value in candidates
    )
    issue_text = "；".join(issues) if issues else "无"
    momentum_briefs = len(
        re.findall(
            rf"\*\*(?:当前)?{re.escape(MOMENTUM_BRIEF_LABEL)}\s*[：:]?\*\*",
            editorial_body,
        )
    )
    return (
        f"Memo中文字={memo_characters}；行内强调={emphasis_count}；"
        f"原文链接覆盖={linked_routes}/{source_routes} 条候选路线；"
        f"势能判断段={momentum_briefs}；问题={issue_text}"
    )


def _has_long_english_excerpt(content: str) -> bool:
    editorial_body = _editorial_body(content)
    without_links = re.sub(r"\[[^\]]+\]\([^)]+\)", "", editorial_body)
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
    if not candidates:
        return True
    if not by_route or any(not names for names in by_route.values()):
        return False
    editorial_body = _editorial_body(content)
    return all(
        any(name in editorial_body for name in names)
        for names in by_route.values()
    )


def _strip_code_fence(content: str) -> str:
    value = content.strip()
    match = re.fullmatch(r"```(?:markdown|md)?\s*(.*?)\s*```", value, re.DOTALL)
    return match.group(1).strip() if match else value
