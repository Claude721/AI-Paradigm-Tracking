"""由研究总编辑 Agent 把结构化候选写成连贯的技术路线 memo。"""

from __future__ import annotations

import asyncio
import hashlib
import inspect
import json
import logging
import re
from collections import OrderedDict
from pathlib import Path

import config
from agents.llm_utils import build_client
from paradigms.models import (
    EvidenceType,
    ParadigmCandidate,
    ResearcherProfile,
    TechnicalEvidence,
    key_researcher_profiles,
    primary_material_url,
    safe_public_contact_target,
    verified_organization_attribution,
)
from run_audit import run_audit
from runtime_clock import scheduled_date
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
            self.client, self.model = build_client(
                "main",
                timeout_seconds=config.PARADIGM_REPORT_REQUEST_TIMEOUT_SECONDS,
            )
        return self.client, self.model

    async def generate(
        self,
        candidates: list[ParadigmCandidate],
        pipeline_stats: dict | None = None,
        *,
        report_date: str = "",
        route_fragments: dict[str, str] | None = None,
        save_route_fragment=None,
    ) -> Path:
        date = report_date or scheduled_date()
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
                route_drafts = await self._draft_routes(
                    date,
                    ordered,
                    cached=route_fragments or {},
                    save_fragment=save_route_fragment,
                )
                content = await self._editorial_frame(
                    date,
                    ordered,
                    route_drafts,
                    stats,
                )
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
                    content = await self._editorial_frame(
                        date,
                        ordered,
                        route_drafts,
                        stats,
                        repair_violations=revision_requests,
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

    async def _editorial_frame(
        self,
        date: str,
        candidates: list[ParadigmCandidate],
        route_drafts: list[str],
        stats: dict,
        *,
        repair_violations: list[str] | None = None,
    ) -> str:
        """Write only the bounded memo frame, then attach every checked route."""

        frame_payload = _editorial_frame_payload(candidates)
        prompt = self.skill_loader.render(
            "weekly_memo_frame",
            date=date,
            lookback_days=config.SOURCING_LOOKBACK_DAYS,
            stats=json.dumps(_public_stats(stats), ensure_ascii=False),
            route_summaries=json.dumps(
                frame_payload,
                ensure_ascii=False,
            ),
            repair_violations="；".join(repair_violations or []) or "无",
        )
        run_audit.event(
            "weekly_memo_frame",
            "request_bounded",
            f"输入 {len(prompt)} 字符；展开路线 "
            f"{len(frame_payload['routes'])}/{len(candidates)}；"
            f"摘要折叠 {frame_payload['overflow_route_count']} 条",
        )
        frame = await self._request_markdown(
            prompt,
            stage="weekly_memo_frame",
            subject=f"{date} / {len(candidates)} routes",
            temperature=0.2,
            max_tokens=2200,
        )
        violations = _editorial_frame_violations(frame)
        if violations:
            repair_prompt = prompt + (
                "\n\n上一版框架未通过检查："
                + "；".join(violations)
                + "。只重写开篇框架，不得输出路线正文。"
            )
            frame = await self._request_markdown(
                repair_prompt,
                stage="weekly_memo_frame_revision",
                subject=f"{date} / {'; '.join(violations)}",
                temperature=0.1,
                max_tokens=2200,
            )
            violations = _editorial_frame_violations(frame)
        if violations:
            raise ValueError("周报开篇框架未通过质量闸门：" + "；".join(violations))
        return _assemble_editorial_frame(frame, route_drafts)

    async def _request_markdown(
        self,
        prompt: str,
        *,
        stage: str,
        subject: str,
        temperature: float,
        max_tokens: int,
    ) -> str:
        """对瞬时连接/读超时做一次显式、可审计的业务重试。"""

        client, model = self._get_client()
        last_error: Exception | None = None
        for attempt in range(2):
            response = None
            try:
                retry_note = (
                    ""
                    if attempt == 0
                    else "\n\n上一次请求未完成。请保持事实密度，压缩重复表达并直接输出完整 Markdown。"
                )
                response = await client.chat.completions.create(
                    model=model,
                    messages=[
                        {"role": "user", "content": prompt + retry_note}
                    ],
                    temperature=temperature,
                    max_tokens=max_tokens,
                )
                run_audit.record_llm(
                    stage=stage,
                    role="main",
                    model=model,
                    subject=f"{subject} / attempt-{attempt + 1}",
                    response=response,
                )
                return _strip_code_fence(
                    response.choices[0].message.content or ""
                )
            except Exception as exc:
                last_error = exc
                run_audit.record_llm(
                    stage=stage,
                    role="main",
                    model=model,
                    subject=f"{subject} / attempt-{attempt + 1}",
                    response=response,
                    error=exc,
                )
                logger.warning(
                    "%s 第 %s 次请求失败 [%s]: %s",
                    stage,
                    attempt + 1,
                    subject[:80],
                    exc,
                )
        assert last_error is not None
        raise last_error

    async def _draft_routes(
        self,
        date: str,
        candidates: list[ParadigmCandidate],
        *,
        cached: dict[str, str],
        save_fragment,
    ) -> list[str]:
        """把动态路线数量拆成有界、可并发、可续跑的写作事务。"""

        semaphore = asyncio.Semaphore(config.PARADIGM_REPORT_ROUTE_CONCURRENCY)
        drafts: dict[str, str] = {}

        async def draft_one(index: int, candidate: ParadigmCandidate) -> None:
            fragment_key = _route_fragment_key(candidate)
            cached_content = cached.get(fragment_key, "")
            if cached_content and not _route_draft_violations(
                cached_content,
                candidate,
            ):
                drafts[fragment_key] = cached_content
                run_audit.event(
                    "weekly_route_draft",
                    "reused",
                    f"复用路线草稿 {index}/{len(candidates)}: {candidate.name[:80]}",
                )
                return
            async with semaphore:
                content = await self._draft_one_route(
                    date,
                    candidate,
                    index=index,
                    total=len(candidates),
                )
                drafts[fragment_key] = content
                if save_fragment is not None:
                    result = save_fragment(fragment_key, content)
                    if inspect.isawaitable(result):
                        await result

        results = await asyncio.gather(
            *(
                draft_one(index, candidate)
                for index, candidate in enumerate(candidates, 1)
            ),
            return_exceptions=True,
        )
        failures = [value for value in results if isinstance(value, BaseException)]
        if failures:
            run_audit.event(
                "weekly_route_draft",
                "partial_failure",
                f"{len(failures)} 条路线失败，"
                f"{len(drafts)} 条成功草稿已保存并可在下次复用",
            )
            details = "；".join(
                f"{type(value).__name__}: {str(value)[:180]}"
                for value in failures[:3]
            )
            raise RuntimeError(
                f"{len(failures)} 条路线草稿未完成；已保存 {len(drafts)} 条："
                + details
            ) from failures[0]
        return [drafts[_route_fragment_key(candidate)] for candidate in candidates]

    async def _draft_one_route(
        self,
        date: str,
        candidate: ParadigmCandidate,
        *,
        index: int,
        total: int,
    ) -> str:
        dossier = _compact_route_dossier(candidate)
        prompt = self.skill_loader.render(
            "weekly_route_draft",
            date=date,
            route_index=index,
            route_total=total,
            route_dossier=json.dumps(dossier, ensure_ascii=False),
            mental_model_method=self.skill_loader.load("technical-mental-model"),
        )
        last_violations: list[str] = []
        for attempt in range(2):
            repair_note = (
                ""
                if attempt == 0
                else "\n\n上一轮路线草稿未通过检查："
                + "；".join(last_violations)
                + "。请直接重写这一条路线，不要输出整份周报。"
            )
            content = await self._request_markdown(
                prompt + repair_note,
                stage="weekly_route_draft",
                subject=f"{date} / route-{index} / {candidate.name}",
                temperature=0.2,
                max_tokens=2800,
            )
            last_violations = _route_draft_violations(content, candidate)
            if not last_violations:
                run_audit.event(
                    "weekly_route_draft",
                    "passed",
                    f"路线草稿 {index}/{total} 通过；输入 {len(prompt)} 字符，输出 {len(content)} 字符",
                )
                return content
        raise ValueError(
            f"路线草稿未通过质量闸门 [{candidate.name}]: "
            + "；".join(last_violations)
        )

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
        failed_sources = [
            f"{name}={value.get('status')}"
            for name, value in (coverage.get("source_health") or {}).items()
            if value.get("status") in {"partial", "query_failed", "timed_out"}
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
        if failed_sources:
            incomplete_parts.append("发现源：" + "、".join(failed_sources))
        if official_incomplete:
            incomplete_parts.append(
                "官方入口："
                f"请求失败 {official.get('request_failed', 0)}、"
                f"解析零链接 {official.get('parse_zero_links', 0)}、"
                f"详情失败 {official.get('detail_failures', 0)}"
            )
        pending_work = int(stats.get("pending_work_count", 0) or 0)
        run_incomplete = bool(
            stats.get("run_incomplete") or pending_work or incomplete_parts
        )
        coverage_note = (
            "\n\n本轮存在**召回覆盖未闭合**："
            + "；".join(incomplete_parts)
            + "。这会降低结论置信度，具体失败车道与重试线索见随信审计。"
            if incomplete_parts
            else ""
        )
        if pending_work:
            progress_note = (
                "\n\n本轮还存在**尚未完成研究判断的执行积压**："
                f"机制抽取完成 {stats.get('analysis_completed_count', stats.get('analysis_count', 0))}/"
                f"{stats.get('planned_analysis_count', 0)} 条；"
                f"待抽取 {stats.get('analysis_deferred_count', 0)} 条，"
                f"待深挖 {stats.get('candidate_deferred_count', 0)} 条，"
                f"待刷新 {stats.get('refresh_deferred_count', 0)} 条，"
                f"待补全人物交付信息 {stats.get('delivery_profile_deferred_count', 0)} 条，"
                f"待补全一手链接 {stats.get('delivery_source_deferred_count', 0)} 条，"
                f"因显式报告 safety limit 延后 {stats.get('report_safety_deferred_count', 0)} 条。"
                "这些材料只是因软时间预算或显式 safety limit 延后，"
                "并未被 Rubric 淘汰；因此本期空白不能解释为近期没有新范式。"
            )
        else:
            progress_note = ""
        if run_incomplete:
            memo_heading = "## 本期运行状态 Memo"
            memo = (
                f"本轮已发现 {stats.get('origin_count', 0)} 篇论文、Technical Report "
                "与官方技术博客，但研究链路**尚未完成**，因此当前 0 条交付"
                "不是技术判断，也不能解释为本周没有值得关注的新工作。系统"
                "不会用未完成样本冒充完整周报；已发现材料与已完成研究检查点"
                "均已保留，后续运行会从 backlog 继续。"
            )
            closing = (
                "优先恢复未闭合的召回车道、机制抽取、深挖与人物/原文交付"
                "契约；在这些步骤完成前，不对近期技术演变做负面结论。"
            )
        else:
            memo_heading = "## 本期研究 Memo"
            memo = (
                f"本期共扫描 {stats.get('origin_count', 0)} 篇论文、Technical Report "
                "与官方技术博客；在覆盖完整且已经完成研究判断的材料中，没有"
                "内容同时跨过**技术外延、发布者可信度和外部承接**三道门槛。"
                "技术范式不会按周出现，这一期不为了维持篇幅把局部 benchmark "
                "改进或作者的宏大叙事包装成趋势。"
            )
            closing = (
                "继续观察新的原始机制是否出现独立复现、跨团队承接或有内容的"
                "二次讨论。只有当讨论开始围绕设计思想、适用边界和新能力展开，"
                "而不只是转发论文标题时，扩散信号才真正成立。"
            )
        ordinary_window = stats.get(
            "ordinary_discovery_lookback_days",
            config.SOURCING_LOOKBACK_DAYS,
        )
        high_signal_window = stats.get(
            "high_signal_discovery_lookback_days",
            stats.get("discovery_lookback_days", ordinary_window),
        )
        return f"""# AI 技术范式雷达

> {date} · 普通发现 {ordinary_window} 天 · 高信号回补 {high_signal_window} 天

{memo_heading}

{memo}{coverage_note}{progress_note}

## 接下来真正值得盯的信号

{closing}
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
        "verified_organization_attribution": verified_organization_attribution(item),
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
            for value in key_researcher_profiles(
                item.researchers,
                config.PARADIGM_KEY_RESEARCHER_LIMIT,
            )
        ],
    }


def _route_fragment_key(item: ParadigmCandidate) -> str:
    """Stable key for one evidence snapshot, not merely for a route name."""

    contract = SkillLoader()
    contract_material = (
        contract.load("weekly_route_draft")
        + "\n"
        + contract.load("technical-mental-model")
    )
    contract_signature = hashlib.sha256(
        contract_material.encode("utf-8")
    ).hexdigest()[:12]
    input_signature = hashlib.sha256(
        json.dumps(
            {
                "report_signature": item.report_signature,
                "dossier": _compact_route_dossier(item),
            },
            ensure_ascii=False,
            sort_keys=True,
        ).encode("utf-8")
    ).hexdigest()[:20]
    return f"route:{contract_signature}:{item.key}:{input_signature}"


def _compact_route_dossier(item: ParadigmCandidate) -> dict:
    """Bound one route-writing request without weakening its evidence contract.

    The full candidate can accumulate dozens of historical/community records.
    A route writer needs the already-synthesized causal model plus primary and
    momentum evidence, not a second copy of every raw discovery document.
    """

    primary, primary_overflow = _bounded_route_evidence(
        _primary_sources(item),
        char_budget=14_000,
    )
    momentum, momentum_overflow = _bounded_route_evidence(
        _momentum_evidence(item),
        char_budget=12_000,
    )

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
        "objective_momentum_signals": item.objective_momentum_signals[:12],
        "community_coverage": item.community_coverage,
        "secondary_discussion_summary": item.secondary_discussion_summary,
        "trend_interpretation": item.trend_interpretation,
        "open_questions": item.open_questions[:8],
        "publisher_tier": item.publisher_tier,
        "publisher_evidence": item.publisher_evidence[:8],
        "verified_organization_attribution": verified_organization_attribution(item),
        "is_formal_technical_report": item.is_formal_technical_report,
        "marketing_overclaim_risk": item.marketing_overclaim_risk,
        "primary_sources": primary,
        "momentum_evidence": momentum,
        "evidence_overflow": {
            "primary_sources": primary_overflow,
            "momentum_evidence": momentum_overflow,
            "meaning": (
                "超出路线写作上下文的证据仍会进入确定性原文索引和持久化"
                "候选，不代表被淘汰或不存在。"
            ),
        },
        "researchers": [
            _researcher_dossier(value)
            for value in key_researcher_profiles(
                item.researchers,
                config.PARADIGM_KEY_RESEARCHER_LIMIT,
            )
        ],
    }


def _bounded_route_evidence(
    evidence: list[TechnicalEvidence],
    *,
    char_budget: int,
) -> tuple[list[dict], dict[str, object]]:
    records = []
    used = 0
    overflow: list[TechnicalEvidence] = []
    for value in evidence:
        record = _evidence_dossier(value)
        record["source"] = str(record.get("source", ""))[:120]
        record["title"] = str(record.get("title", ""))[:500]
        record["url"] = str(record.get("url", ""))[:1500]
        record["organization"] = str(record.get("organization", ""))[:300]
        record["summary"] = str(record.get("summary", ""))[:650]
        metrics = record.get("metrics")
        record["metrics"] = (
            {
                str(key)[:80]: (
                    metric
                    if isinstance(metric, (int, float, bool)) or metric is None
                    else str(metric)[:200]
                )
                for key, metric in list(metrics.items())[:20]
            }
            if isinstance(metrics, dict)
            else {}
        )
        authors = record.get("authors") or []
        if isinstance(authors, list) and len(authors) > 8:
            record["authors"] = [
                *authors[:8],
                f"另有 {len(authors) - 8} 位作者",
            ]
        length = len(json.dumps(record, ensure_ascii=False))
        if used + length <= char_budget:
            records.append(record)
            used += length
        else:
            overflow.append(value)
    type_counts: dict[str, int] = {}
    for value in overflow:
        evidence_type = value.evidence_type.value
        type_counts[evidence_type] = type_counts.get(evidence_type, 0) + 1
    return records, {
        "count": len(overflow),
        "type_counts": type_counts,
    }


def _editorial_frame_payload(
    candidates: list[ParadigmCandidate],
    *,
    char_budget: int = 28_000,
) -> dict:
    """Bound the overview request while accounting for every reportable route."""

    records = []
    overflow_count = 0
    overflow_kinds: dict[str, int] = {}
    used = 0
    for candidate in candidates:
        record = {
            "route_family": (
                candidate.route_family
                or candidate.lineage_parent
                or candidate.name
            )[:300],
            "report_kind": candidate.report_kind,
            "thesis": candidate.thesis[:700],
            "problem_shift": candidate.problem_shift[:500],
            "why_now": candidate.why_now[:400],
            "trend_interpretation": candidate.trend_interpretation[:500],
            "primary_source_urls": [
                source.url for source in _primary_sources(candidate)[:4]
            ],
            "key_people": [
                profile.name
                for profile in key_researcher_profiles(
                    candidate.researchers,
                    config.PARADIGM_KEY_RESEARCHER_LIMIT,
                )
            ],
        }
        length = len(json.dumps(record, ensure_ascii=False))
        if used + length <= char_budget:
            records.append(record)
            used += length
        else:
            overflow_count += 1
            kind = str(record["report_kind"] or "unknown")
            overflow_kinds[kind] = overflow_kinds.get(kind, 0) + 1
    return {
        "routes": records,
        "overflow_route_count": overflow_count,
        "overflow_report_kinds": overflow_kinds,
        "total_route_count": len(candidates),
        "note": (
            "overflow_routes 仍会由程序完整附入正文；这里只因总编上下文预算"
            "省略其详细摘要，不代表它们被筛掉。"
        ),
    }


def _editorial_frame_violations(content: str) -> list[str]:
    value = content.strip()
    memo_match = re.search(
        r"(?ms)^## 本期研究 Memo\s*$\s*(.*?)(?=^##\s|\Z)", value
    )
    memo = memo_match.group(1) if memo_match else ""
    memo_chinese = len(re.findall(r"[\u4e00-\u9fff]", memo))
    violations = []
    if "## 本期研究 Memo" not in value:
        violations.append("缺少本期研究 Memo")
    if "## 接下来真正值得盯的信号" not in value:
        violations.append("缺少后续观察信号")
    if not 250 <= memo_chinese <= 1000:
        violations.append(
            f"开篇 Memo 中文长度为 {memo_chinese}，交付范围为 250–1000"
        )
    if re.search(r"(?m)^###\s+", value):
        violations.append("开篇框架越权生成路线正文")
    if re.search(r"(?m)^\s*\|.+\|\s*$", value):
        violations.append("开篇框架出现表格")
    if re.search(r"(?:总分|新颖性得分|趋势得分|声量得分)\s*[:：]?\s*\d", value):
        violations.append("开篇框架出现内部评分")
    if _has_long_english_excerpt(value):
        violations.append("开篇框架出现英文原文长句或成段摘录")
    return violations


def _assemble_editorial_frame(frame: str, route_drafts: list[str]) -> str:
    """Deterministically retain every route even if the overview model is slow."""

    value = frame.strip()
    closing = re.search(r"(?m)^## 接下来真正值得盯的信号\s*$", value)
    routes = "\n\n".join(draft.strip() for draft in route_drafts)
    if not closing:
        return value + "\n\n" + routes
    return (
        value[: closing.start()].rstrip()
        + "\n\n"
        + routes
        + "\n\n"
        + value[closing.start() :].lstrip()
    )


def _route_draft_violations(
    content: str, candidate: ParadigmCandidate
) -> list[str]:
    """Route-local gate so persisted fragments are safe to reuse verbatim."""

    value = content.strip()
    chinese_characters = len(re.findall(r"[\u4e00-\u9fff]", value))
    violations = []
    if not 300 <= chinese_characters <= 1800:
        violations.append(
            f"路线正文中文长度为 {chinese_characters}，交付范围为 300–1800"
        )
    if not re.search(r"(?m)^###\s+\S", value):
        violations.append("缺少三级路线标题")
    if re.search(r"(?m)^##\s+", value):
        violations.append("路线草稿越权生成整份周报章节")
    if len(
        re.findall(
            rf"\*\*(?:当前)?{re.escape(MOMENTUM_BRIEF_LABEL)}\s*[：:]?\*\*",
            value,
        )
    ) != 1:
        violations.append("必须且只能包含一段讨论势能判断")
    if re.search(r"(?m)^\s*\|.+\|\s*$", value):
        violations.append("出现表格")
    if re.search(r"(?:总分|新颖性得分|趋势得分|声量得分)\s*[:：]?\s*\d", value):
        violations.append("出现内部评分")
    if _has_long_english_excerpt(value):
        violations.append("出现英文原文长句或成段摘录")
    linked_urls = _markdown_link_targets(value)
    if not any(
        _normalized_url(source.url) in linked_urls
        for source in _primary_sources(candidate)
    ):
        violations.append("路线正文没有原样附上一手材料 Markdown 链接")
    people = [
        profile.name
        for profile in key_researcher_profiles(
            candidate.researchers,
            config.PARADIGM_KEY_RESEARCHER_LIMIT,
        )
        if profile.name.strip()
    ]
    organization = verified_organization_attribution(candidate)
    attributable = [*people]
    if organization:
        attributable.append(organization["name"])
    if attributable and not any(value in content for value in attributable):
        violations.append("路线正文没有交代已核验的关键推动者或发布组织")
    return violations


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
        "ordinary_discovery_lookback_days",
        "high_signal_discovery_lookback_days",
        "source_counts",
        "planned_analysis_count",
        "analysis_count",
        "analysis_completed_count",
        "analysis_deferred_count",
        "candidate_deferred_count",
        "refresh_deferred_count",
        "delivery_profile_deferred_count",
        "delivery_source_deferred_count",
        "report_safety_deferred_count",
        "recall_coverage_incomplete",
        "run_incomplete",
        "result_kind",
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
        and primary_material_url(value)
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
    # 不在语义过滤之后再做静默 Top-K。路线写作的上下文上限由
    # `_bounded_route_evidence` 负责，并把未展开部分写进 overflow 账本；
    # 这样既控制请求体，也不会把“没有进入 prompt”伪装成“没有证据”。
    return selected


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
    organizations: dict[str, list[dict[str, str]]] = {}
    for candidate in candidates:
        route = candidate.route_family or candidate.lineage_parent or candidate.name
        grouped.setdefault(route, []).extend(
            key_researcher_profiles(
                candidate.researchers,
                config.PARADIGM_KEY_RESEARCHER_LIMIT,
            )
        )
        organization = verified_organization_attribution(candidate)
        if organization:
            organizations.setdefault(route, []).append(organization)
    for route, profiles in grouped.items():
        named = [profile for profile in profiles if profile.name.strip()]
        if not named:
            if organizations.get(route):
                continue
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
    organizations: dict[str, list[dict[str, str]]] = {}
    for candidate in candidates:
        route = candidate.route_family or candidate.lineage_parent or candidate.name
        bucket = grouped.setdefault(route, [])
        seen = {profile.name.casefold() for profile in bucket if profile.name}
        for profile in key_researcher_profiles(
            candidate.researchers,
            config.PARADIGM_KEY_RESEARCHER_LIMIT,
        ):
            if profile.name and profile.name.casefold() not in seen:
                bucket.append(profile)
                seen.add(profile.name.casefold())
        organization = verified_organization_attribution(candidate)
        if organization:
            organization_bucket = organizations.setdefault(route, [])
            if all(
                item["name"].casefold() != organization["name"].casefold()
                for item in organization_bucket
            ):
                organization_bucket.append(organization)

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
            organization_entries = organizations.get(route, [])
            if organization_entries:
                for organization in organization_entries:
                    lines.append(
                        f"- **{_markdown_label(organization['name'])}**（组织发布）："
                        "一手材料未披露可可靠归因的自然人贡献角色，因此不猜测"
                        "负责人；"
                        f"[核验发布入口](<{organization['source_url']}>）。"
                    )
            else:
                lines.append("- 缺少可核验关键人物或发布组织。")
            lines.append("")
            continue
        for profile in profiles[: config.PARADIGM_KEY_RESEARCHER_LIMIT]:
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
    failed_sources = [
        f"{name}={value.get('status')}"
        for name, value in (coverage.get("source_health") or {}).items()
        if value.get("status") in {"partial", "query_failed", "timed_out"}
    ]
    if failed_sources:
        issues.append("发现源异常：" + "、".join(failed_sources))
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
            profile.name
            for profile in key_researcher_profiles(
                candidate.researchers,
                config.PARADIGM_KEY_RESEARCHER_LIMIT,
            )
            if profile.name
        )
        organization = verified_organization_attribution(candidate)
        if not by_route[route] and organization:
            by_route[route].add(organization["name"])
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
