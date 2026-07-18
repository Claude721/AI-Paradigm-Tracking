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
from paradigms.clustering import cluster_extractions
from paradigms.discovery import ParadigmDiscovery
from paradigms.enrichment import EvidenceEnricher
from paradigms.scoring import is_reportable, score_candidate
from reports.paradigm_generator import ParadigmReportGenerator

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
        origins, incremental = self.store.plan_origins(batch.origins)
        stats.update({f"origin_{key}": value for key, value in incremental.items()})
        stats["analysis_count"] = len(origins)

        if origins:
            extractions = await self.analyzer.run(origins)
            successful_origins = [
                item.evidence
                for item in extractions
                if not item.rejection_reason.startswith("抽取失败:")
            ]
            self.store.mark_evidence(successful_origins, analyzed=True)
            stats["candidate_extractions"] = sum(
                item.is_candidate for item in extractions
            )
            candidates = cluster_extractions(extractions)
            if candidates:
                self.store.attach_history(candidates)
                candidates = await self.enricher.run(candidates, batch.supporting)
                candidates = await self.synthesizer.run(candidates)
                candidates = await self.trajectory.run(candidates)
            for candidate in candidates:
                score_candidate(candidate)
        else:
            candidates = []
            stats["candidate_extractions"] = 0

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

        self.store.save_candidates(candidates)
        report_path = await self.report_gen.generate(reportable, stats)
        self.pending_delivery = reportable
        stats["report_path"] = str(report_path)
        stats["saved_count"] = len(candidates)
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
