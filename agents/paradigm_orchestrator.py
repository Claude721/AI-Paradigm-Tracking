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
from paradigms.clustering import cluster_extractions, initial_gate_reason
from paradigms.discovery import ParadigmDiscovery
from paradigms.enrichment import EvidenceEnricher
from paradigms.scoring import is_reportable, score_candidate
from reports.paradigm_generator import ParadigmReportGenerator
from run_audit import run_audit

logger = logging.getLogger(__name__)


class ParadigmOrchestrator:
    def __init__(self):
        self.discovery = ParadigmDiscovery()
        self.store = ParadigmStore()
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
        stats["origin_count"] = len(batch.origins)
        stats["supporting_count"] = len(batch.supporting)
        stats["source_counts"] = batch.source_counts
        origins, incremental = self.store.plan_origins(batch.origins)
        stats.update({f"origin_{key}": value for key, value in incremental.items()})
        pending_origins = self.store.load_pending_origins(
            exclude_fingerprints={item.fingerprint for item in origins}
        )
        origins = [*pending_origins, *origins]
        stats["pending_origin_backlog_loaded"] = len(pending_origins)
        planned_count = len(origins)
        deferred_origins = origins[config.PARADIGM_MAX_ANALYSIS_ITEMS :]
        origins = origins[: config.PARADIGM_MAX_ANALYSIS_ITEMS]
        if deferred_origins:
            # 只登记“已发现”，保留 last_analyzed_at=NULL；下次运行会继续轮转，
            # 不会因为本轮预算上限而永久漏掉。
            self.store.mark_evidence(deferred_origins, analyzed=False)
        stats["planned_analysis_count"] = planned_count
        stats["analysis_deferred_count"] = len(deferred_origins)
        stats["analysis_count"] = len(origins)

        if origins:
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
                        "gate_reason": gate_reason,
                        "rejection_reason": extraction.rejection_reason,
                        "novelty_score": extraction.novelty_score,
                        "solidity_score": extraction.solidity_score,
                        "scope_score": extraction.scope_score,
                        "incremental_penalty": extraction.incremental_penalty,
                    }
                )
            successful_origins = list(
                {
                    item.evidence.fingerprint: item.evidence
                    for item in extractions
                    if not item.rejection_reason.startswith("抽取失败:")
                }.values()
            )
            self.store.mark_evidence(successful_origins, analyzed=True)
            stats["candidate_extractions"] = sum(
                item.is_candidate for item in extractions
            )
            new_candidates = cluster_extractions(extractions)
            self.store.attach_history(new_candidates)
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
        deep_candidates = deep_pool[: config.PARADIGM_MAX_DEEP_CANDIDATES]
        deferred_candidates = deep_pool[config.PARADIGM_MAX_DEEP_CANDIDATES :]
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
                        "达到本轮外部证据深挖预算上限，已持久化并在下轮 FIFO 优先处理"
                    ),
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
                    "admission_reason": candidate.admission_reason,
                    "rejection_reason": candidate.rejection_reason,
                    "community_coverage": candidate.community_coverage,
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
        )[: config.PARADIGM_MAX_REPORT_ITEMS]
        stats["high_value_count"] = len(reportable)
        stats["new_paradigms"] = sum(item.report_kind == "new" for item in reportable)
        stats["updated_paradigms"] = sum(
            item.report_kind == "update" for item in reportable
        )

        self.store.save_candidates([*candidates, *deferred_candidates])
        report_path = await self.report_gen.generate(reportable, stats)
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
    """先深挖正式报告、官方发布及技术分更强的路线。"""
    origin_priority = max(
        (int(item.raw.get("origin_priority", 0) or 0) for item in candidate.evidence),
        default=0,
    )
    return (
        float(origin_priority),
        candidate.novelty_score + candidate.scope_score,
        candidate.solidity_score - candidate.incremental_penalty,
        len(candidate.evidence),
    )
