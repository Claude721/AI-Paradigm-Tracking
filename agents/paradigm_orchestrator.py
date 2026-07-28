"""技术范式雷达 v2 流水线编排。"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

import config
from database.paradigm_store import ParadigmStore
from paradigms.analyzer import (
    ParadigmAnalyzer,
    ParadigmSynthesizer,
    ResearcherTrajectoryAnalyzer,
)
from paradigms.clustering import (
    cluster_extractions,
    initial_gate_reason,
    is_priority_review,
)
from paradigms.discovery import ParadigmDiscovery
from paradigms.enrichment import EvidenceEnricher
from paradigms.scoring import is_reportable, score_candidate
from reports.paradigm_generator import ParadigmReportGenerator
from run_audit import run_audit

logger = logging.getLogger(__name__)


class ParadigmOrchestrator:
    def __init__(self):
        self.store = ParadigmStore()
        self.bootstrap_mode = self.store.is_bootstrap_required()
        self.discovery_lookback_days = (
            config.PARADIGM_BOOTSTRAP_LOOKBACK_DAYS
            if self.bootstrap_mode
            else config.PARADIGM_RECALL_OVERLAP_DAYS
        )
        self.discovery = ParadigmDiscovery(
            lookback_days=self.discovery_lookback_days
        )
        self.analyzer = ParadigmAnalyzer()
        self.enricher = EvidenceEnricher()
        self.synthesizer = ParadigmSynthesizer()
        self.trajectory = ResearcherTrajectoryAnalyzer()
        self.report_gen = ParadigmReportGenerator()
        # 由统一入口在邮件成功后再登记交付，避免“数据库显示已交付但邮件失败”。
        self.pending_delivery: list = []

    async def run(self) -> dict:
        started = datetime.now(timezone.utc)
        stats: dict = {"pipeline_mode": "paradigm"}

        batch = await self.discovery.run()
        stats["bootstrap_mode"] = self.bootstrap_mode
        stats["discovery_lookback_days"] = self.discovery_lookback_days
        stats["origin_count"] = len(batch.origins)
        stats["supporting_count"] = len(batch.supporting)
        stats["source_counts"] = batch.source_counts
        stats["frontier_coverage"] = batch.coverage
        run_audit.event(
            "frontier_coverage",
            (
                "warning"
                if batch.coverage.get("query_failures")
                or batch.coverage.get("covered_domains", 0)
                < batch.coverage.get("total_domains", 0)
                else "passed"
            ),
            (
                f"地图 {batch.coverage.get('landscape_version')}；"
                f"命中 {batch.coverage.get('covered_domains', 0)}/"
                f"{batch.coverage.get('total_domains', 0)} 个领域；"
                f"查询失败 {batch.coverage.get('query_failures', [])}"
            ),
        )
        recall_lanes = batch.coverage.get("recall_lanes") or {}
        failed_lanes = [
            name
            for name, value in recall_lanes.items()
            if value.get("status") == "query_failed"
        ]
        zero_lanes = [
            name
            for name, value in recall_lanes.items()
            if value.get("status") == "searched_zero_hits"
        ]
        run_audit.event(
            "recall_lanes",
            "warning" if failed_lanes else "passed",
            (
                f"独立召回车道 {len(recall_lanes)} 条；"
                f"失败 {failed_lanes}；成功但零命中 {zero_lanes}"
            ),
        )
        official_coverage = batch.coverage.get("official_pages") or {}
        official_warning = bool(
            int(official_coverage.get("checked_pages", 0) or 0)
            < int(official_coverage.get("total_pages", 0) or 0)
            or
            official_coverage.get("request_failed")
            or official_coverage.get("parse_zero_links")
            or official_coverage.get("detail_failures")
        )
        run_audit.event(
            "official_page_coverage",
            "warning" if official_warning else "passed",
            (
                f"官方入口 {official_coverage.get('checked_pages', 0)}/"
                f"{official_coverage.get('total_pages', 0)}；"
                f"请求失败 {official_coverage.get('request_failed', 0)}；"
                f"解析零链接 {official_coverage.get('parse_zero_links', 0)}；"
                f"详情失败 {official_coverage.get('detail_failures', 0)}；"
                f"形成原点 {official_coverage.get('evidence', 0)}"
            ),
        )
        origins, incremental = self.store.plan_origins(batch.origins)
        stats.update({f"origin_{key}": value for key, value in incremental.items()})
        pending_origins = self.store.load_pending_origins(
            exclude_fingerprints={item.fingerprint for item in origins}
        )
        origins = [*pending_origins, *origins]
        stats["pending_origin_backlog_loaded"] = len(pending_origins)
        planned_count = len(origins)
        origins, deferred_origins = _apply_safety_limit(
            origins, config.PARADIGM_ANALYSIS_SAFETY_LIMIT
        )
        if deferred_origins:
            # safety limit 只在用户显式配置时生效，不参与研究筛选。被熔断的
            # 材料保持 last_analyzed_at=NULL，并在审计中明确标为未完成。
            self.store.mark_evidence(deferred_origins, analyzed=False)
        stats["planned_analysis_count"] = planned_count
        stats["analysis_deferred_count"] = len(deferred_origins)
        stats["analysis_count"] = len(origins)

        if origins:
            hydration_stats = await self.enricher.hydrate_priority_origins(origins)
            stats.update(hydration_stats)
            if hydration_stats["priority_origin_hydration_failed"]:
                run_audit.event(
                    "priority_origin_hydration",
                    "warning",
                    (
                        f"高优先级原点 {hydration_stats['priority_origin_targets']} 条；"
                        f"正文补水成功 {hydration_stats['priority_origin_hydrated']} 条；"
                        f"失败 {hydration_stats['priority_origin_hydration_failed']} 条"
                    ),
                )
            extractions = await self.analyzer.run(origins)
            for extraction in extractions:
                gate_reason = initial_gate_reason(extraction)
                run_audit.record_origin(
                    {
                        "title": extraction.evidence.title,
                        "source": extraction.evidence.source,
                        "origin_kind": extraction.evidence.raw.get(
                            "origin_kind", "research_paper"
                        ),
                        "publisher_tier": extraction.evidence.raw.get(
                            "publisher_tier", "unknown"
                        ),
                        "is_candidate": extraction.is_candidate,
                        "initial_gate_passed": not gate_reason,
                        "priority_review": is_priority_review(extraction),
                        "gate_reason": gate_reason,
                        "rejection_reason": extraction.rejection_reason,
                        "novelty_score": extraction.novelty_score,
                        "solidity_score": extraction.solidity_score,
                        "scope_score": extraction.scope_score,
                        "incremental_penalty": extraction.incremental_penalty,
                        "rubric_version": extraction.rubric_assessment.get(
                            "version", ""
                        ),
                        "rubric_score": extraction.rubric_assessment.get(
                            "score", 0
                        ),
                        "rubric_decision": extraction.rubric_assessment.get(
                            "decision", ""
                        ),
                        "rubric_decision_reason": extraction.rubric_assessment.get(
                            "decision_reason", ""
                        ),
                        "rubric_dimension_scores": extraction.rubric_assessment.get(
                            "dimension_scores", {}
                        ),
                        "rubric_answers": extraction.rubric_assessment.get(
                            "answers", []
                        ),
                    }
                )
            failed_origin_fingerprints = {
                item.evidence.fingerprint
                for item in extractions
                if not item.canonical_name
                and not item.rubric_assessment
                and bool(item.rejection_reason)
            }
            successful_origins = list(
                {
                    item.evidence.fingerprint: item.evidence
                    for item in extractions
                    if item.evidence.fingerprint not in failed_origin_fingerprints
                }.values()
            )
            self.store.mark_evidence(successful_origins, analyzed=True)
            stats["candidate_extractions"] = sum(
                item.is_candidate for item in extractions
            )
            new_candidates = cluster_extractions(extractions)
            new_candidates = self.store.attach_history(new_candidates)
        else:
            new_candidates = []
            stats["candidate_extractions"] = 0

        pending_candidates = self.store.load_pending_deep_candidates(
            exclude_keys={candidate.key for candidate in new_candidates}
        )
        stats["pending_deep_backlog_loaded"] = len(pending_candidates)
        ordered_new = sorted(
            new_candidates,
            key=_deep_analysis_priority,
            reverse=True,
        )
        # 旧积压 FIFO 优先，防止每周的新论文长期挤掉已抽取的候选。
        deep_pool = [*pending_candidates, *ordered_new]
        deep_candidates, deferred_candidates = _apply_safety_limit(
            deep_pool, config.PARADIGM_DEEP_SAFETY_LIMIT
        )
        for candidate in deferred_candidates:
            candidate.status = "pending_deep"
            run_audit.record_candidate(
                {
                    "name": candidate.name,
                    "route_family": candidate.route_family,
                    "reportable": False,
                    "status": "pending_deep",
                    "publisher_tier": candidate.publisher_tier,
                    "evidence_count": len(candidate.evidence),
                    "researcher_count": len(candidate.researchers),
                    "admission_reason": "",
                    "rejection_reason": (
                        "触发用户显式配置的深挖 safety limit；尚未完成研究判断，"
                        "已持久化并在下轮 FIFO 优先处理"
                    ),
                    "rubric_score": candidate.screening_rubric.get("score", 0),
                    "rubric_decision": "not_executed",
                }
            )
        stats["deep_candidate_count"] = len(deep_candidates)
        stats["candidate_deferred_count"] = len(deferred_candidates)
        if deep_candidates:
            deep_candidates = await self.enricher.run(
                deep_candidates, batch.supporting
            )
            deep_candidates = await self.synthesizer.run(deep_candidates)
            deep_candidates = await self.trajectory.run(deep_candidates)
        new_candidates = deep_candidates

        historical = self.store.load_refresh_candidates(
            exclude_keys={
                candidate.key
                for candidate in [*new_candidates, *deferred_candidates]
            }
        )
        refreshed = await self.enricher.refresh(historical, batch.supporting)
        if refreshed:
            refreshed = await self.synthesizer.run(refreshed)
        stats["refreshed_paradigms"] = len(refreshed)
        candidates = [*new_candidates, *refreshed]
        # Tavily/Reddit 的用户正文只供本轮综合与人物核验，之后即清除；
        # 数据库和邮件只保留链接、指标、覆盖状态和已提炼的分析。
        candidates = self.enricher.finalize(candidates)
        for candidate in candidates:
            score_candidate(candidate)
            reportable_result = is_reportable(candidate)
            run_audit.record_candidate(
                {
                    "name": candidate.name,
                    "route_family": candidate.route_family,
                    "reportable": reportable_result,
                    "status": candidate.status,
                    "publisher_tier": candidate.publisher_tier,
                    "evidence_count": len(candidate.evidence),
                    "researcher_count": len(candidate.researchers),
                    "mental_model_components": sorted(candidate.mental_model),
                    "admission_reason": candidate.admission_reason,
                    "rejection_reason": candidate.rejection_reason,
                    "community_coverage": candidate.community_coverage,
                    "rubric_version": candidate.rubric_assessment.get(
                        "version", ""
                    ),
                    "rubric_score": candidate.rubric_assessment.get("score", 0),
                    "rubric_decision": candidate.rubric_assessment.get(
                        "decision", ""
                    ),
                    "rubric_decision_reason": candidate.rubric_assessment.get(
                        "decision_reason", ""
                    ),
                    "rubric_dimension_scores": candidate.rubric_assessment.get(
                        "dimension_scores", {}
                    ),
                    "rubric_answers": candidate.rubric_assessment.get(
                        "answers", []
                    ),
                }
            )

        # 支持证据单独去重入库，但绝不独立生成范式。
        self.store.mark_evidence(batch.supporting, analyzed=False)
        self.store.mark_evidence(
            [evidence for candidate in candidates for evidence in candidate.evidence],
            analyzed=False,
        )
        reportable = [candidate for candidate in candidates if is_reportable(candidate)]
        stats["reportable_count"] = len(reportable)
        reportable = self.store.prepare_report(reportable)
        reportable = sorted(
            reportable, key=lambda item: item.total_score, reverse=True
        )
        reportable, report_deferred = _apply_safety_limit(
            reportable, config.PARADIGM_REPORT_SAFETY_LIMIT
        )
        stats["report_safety_deferred_count"] = len(report_deferred)
        stats["high_value_count"] = len(reportable)
        stats["new_paradigms"] = sum(item.report_kind == "new" for item in reportable)
        stats["updated_paradigms"] = sum(
            item.report_kind == "update" for item in reportable
        )

        self.store.save_candidates([*candidates, *deferred_candidates])
        report_path = await self.report_gen.generate(reportable, stats)
        # 报告成功生成后才登记本轮覆盖地图基线；后续邮件失败会由统一入口
        # 回滚整个数据库，因此失败运行不会误以为 60 天补扫已经完成。
        self.store.mark_landscape_version()
        self.pending_delivery = reportable
        stats["report_path"] = str(report_path)
        stats["saved_count"] = len(candidates) + len(deferred_candidates)
        stats["elapsed_seconds"] = (
            datetime.now(timezone.utc) - started
        ).total_seconds()
        logger.info(
            "范式雷达完成：原始材料=%s，范式候选=%s，交付=%s，报告=%s",
            stats["origin_count"],
            len(candidates),
            len(reportable),
            report_path,
        )
        return stats


def _deep_analysis_priority(candidate) -> tuple[float, float, float, int]:
    """顺序只影响执行先后；Rubric 决定是否深挖，数量不由排序截断。"""
    origin_priority = max(
        (int(item.raw.get("origin_priority", 0) or 0) for item in candidate.evidence),
        default=0,
    )
    return (
        float(origin_priority),
        float(candidate.screening_rubric.get("score", 0.0)),
        float(candidate.screening_rubric.get("answer_coverage", 0.0)),
        len(candidate.evidence),
    )


def _apply_safety_limit(items: list, limit: int) -> tuple[list, list]:
    """0 表示不限制；非零值是执行熔断，不是研究筛选或 Top-K。"""
    if limit <= 0 or len(items) <= limit:
        return items, []
    return items[:limit], items[limit:]
