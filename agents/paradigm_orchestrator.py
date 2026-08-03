"""技术范式雷达 v2 流水线编排。"""

from __future__ import annotations

import asyncio
import copy
import logging
import time
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
        started_monotonic = time.monotonic()
        run_deadline, origin_deadline, deep_deadline, effective_reserve = (
            _execution_deadlines(started_monotonic)
        )
        stats: dict = {
            "pipeline_mode": "paradigm",
            "run_budget_seconds": config.PARADIGM_RUN_BUDGET_SECONDS,
            "stage_reserve_seconds": effective_reserve,
        }

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
        academic_indexes = batch.coverage.get("academic_indexes") or {}
        degraded_indexes = [
            name
            for name, value in academic_indexes.items()
            if value.get("status") != "completed"
        ]
        run_audit.event(
            "academic_index_coverage",
            "warning" if degraded_indexes else "passed",
            "；".join(
                (
                    f"{name}={value.get('status')}，"
                    f"queries {value.get('completed_queries', 0)}/"
                    f"{value.get('queries', 0)}，requests "
                    f"{value.get('requests', 0)}，results "
                    f"{value.get('results', 0)}，429 "
                    f"{value.get('rate_limited_requests', 0)}"
                )
                for name, value in academic_indexes.items()
            )
            or "没有记录学术索引运行状态",
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
        # 发现和分析必须是两个独立检查点。先把本轮所有新原点写成 pending，
        # 即使后续只处理其中一部分，也不会把运行预算误写成研究淘汰。
        self.store.mark_evidence(origins, analyzed=False)
        pending_origins = self.store.load_pending_origins(
            exclude_fingerprints={item.fingerprint for item in origins}
        )
        origins = _origin_execution_order(pending_origins, origins)
        stats["pending_origin_backlog_loaded"] = len(pending_origins)
        planned_count = len(origins)
        origins, safety_deferred_origins = _apply_safety_limit(
            origins, config.PARADIGM_ANALYSIS_SAFETY_LIMIT
        )
        stats["planned_analysis_count"] = planned_count
        stats["analysis_safety_deferred_count"] = len(safety_deferred_origins)

        if origins:
            (
                extractions,
                analyzed_origin_count,
                failed_origin_count,
                budget_deferred_origins,
                hydration_stats,
            ) = await self._analyze_origins_in_batches(origins, origin_deadline)
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
            stats["candidate_extractions"] = sum(
                item.is_candidate for item in extractions
            )
            new_candidates = cluster_extractions(extractions)
            new_candidates = self.store.attach_history(new_candidates)
        else:
            new_candidates = []
            analyzed_origin_count = 0
            failed_origin_count = 0
            budget_deferred_origins = []
            stats["candidate_extractions"] = 0
            stats.update(_empty_hydration_stats())

        stats["analysis_count"] = analyzed_origin_count
        stats["analysis_failed_count"] = failed_origin_count
        stats["analysis_completed_count"] = (
            analyzed_origin_count - failed_origin_count
        )
        stats["analysis_budget_deferred_count"] = len(budget_deferred_origins)
        stats["analysis_deferred_count"] = (
            len(safety_deferred_origins)
            + len(budget_deferred_origins)
            + failed_origin_count
        )
        stats["pending_origin_backlog_remaining"] = stats[
            "analysis_deferred_count"
        ]
        if budget_deferred_origins:
            run_audit.event(
                "origin_analysis_budget",
                "warning",
                (
                    f"软时间预算到达；本轮完成 {analyzed_origin_count}/"
                    f"{planned_count} 条机制抽取，剩余 "
                    f"{len(budget_deferred_origins)} 条保留在 backlog"
                ),
            )

        pending_candidates = self.store.load_pending_deep_candidates(
            exclude_keys={candidate.key for candidate in new_candidates}
        )
        stats["pending_deep_backlog_loaded"] = len(pending_candidates)
        # 优先级只决定本轮先做谁；同优先级下 pending 在 new 之前，保持 FIFO。
        deep_pool = sorted(
            [*pending_candidates, *new_candidates],
            key=_deep_analysis_priority,
            reverse=True,
        )
        deep_candidates, safety_deferred_candidates = _apply_safety_limit(
            deep_pool, config.PARADIGM_DEEP_SAFETY_LIMIT
        )
        stats["planned_deep_candidate_count"] = len(deep_pool)
        if deep_candidates:
            deep_candidates, budget_deferred_candidates = (
                await self._deep_analyze_in_batches(
                    deep_candidates,
                    batch.supporting,
                    deep_deadline,
                )
            )
        else:
            budget_deferred_candidates = []
        deferred_candidates = [
            *safety_deferred_candidates,
            *budget_deferred_candidates,
        ]
        for candidate in safety_deferred_candidates:
            _record_deferred_candidate(
                candidate,
                "触发用户显式配置的深挖 safety limit；尚未完成研究判断，"
                "已持久化并在下轮继续处理",
            )
        for candidate in budget_deferred_candidates:
            _record_deferred_candidate(
                candidate,
                "到达本轮软时间预算；尚未完成研究判断，已持久化并在下轮继续处理",
            )
        stats["deep_candidate_count"] = len(deep_candidates)
        stats["candidate_safety_deferred_count"] = len(
            safety_deferred_candidates
        )
        stats["candidate_budget_deferred_count"] = len(
            budget_deferred_candidates
        )
        stats["candidate_deferred_count"] = len(deferred_candidates)
        if budget_deferred_candidates:
            run_audit.event(
                "deep_analysis_budget",
                "warning",
                (
                    f"软时间预算到达；本轮完成 {len(deep_candidates)}/"
                    f"{len(deep_pool)} 条深挖，剩余 "
                    f"{len(budget_deferred_candidates)} 条保留在 backlog"
                ),
            )
        new_candidates = deep_candidates

        historical = self.store.load_refresh_candidates(
            exclude_keys={
                candidate.key
                for candidate in [*new_candidates, *deferred_candidates]
            },
            limit=0,
        )
        historical, refresh_safety_deferred = _apply_safety_limit(
            historical, config.PARADIGM_REFRESH_SAFETY_LIMIT
        )
        refreshed, refresh_budget_deferred, refresh_attempted = (
            await self._refresh_in_batches(
                historical,
                batch.supporting,
                deep_deadline,
            )
        )
        stats["refresh_analysis_count"] = refresh_attempted
        stats["refresh_safety_deferred_count"] = len(refresh_safety_deferred)
        stats["refresh_budget_deferred_count"] = len(refresh_budget_deferred)
        stats["refresh_deferred_count"] = (
            len(refresh_safety_deferred) + len(refresh_budget_deferred)
        )
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

        stats["run_incomplete"] = bool(
            stats["analysis_deferred_count"]
            or stats["candidate_deferred_count"]
            or stats["refresh_deferred_count"]
        )
        stats["pending_work_count"] = (
            stats["analysis_deferred_count"]
            + stats["candidate_deferred_count"]
            + stats["refresh_deferred_count"]
        )
        stats["run_budget_exhausted"] = bool(
            stats["analysis_budget_deferred_count"]
            or stats["candidate_budget_deferred_count"]
            or stats["refresh_budget_deferred_count"]
        )

        self.store.save_candidates([*candidates, *deferred_candidates])
        report_remaining = _remaining_seconds(run_deadline)
        if report_remaining <= 0:
            raise TimeoutError("流水线软时间预算已用尽，未留下报告生成时间")
        try:
            report_path = await asyncio.wait_for(
                self.report_gen.generate(reportable, stats),
                timeout=report_remaining,
            )
        except TimeoutError as exc:
            raise TimeoutError("报告生成超过流水线软时间预算") from exc
        # 全部发现结果已先持久化为 pending，因此这里登记的是“覆盖地图已完成
        # 发现基线”，不是声称 backlog 已全部完成研究判断。
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

    async def _analyze_origins_in_batches(
        self,
        origins: list,
        deadline: float,
    ) -> tuple[list, int, int, list, dict[str, int]]:
        """逐批抽取并提交检查点；超时项保留 pending。"""
        extractions = []
        analyzed_count = 0
        failed_count = 0
        hydration_totals = _empty_hydration_stats()
        batch_size = config.PARADIGM_ANALYSIS_BATCH_SIZE
        for offset in range(0, len(origins), batch_size):
            remaining = _remaining_seconds(deadline)
            if remaining <= 0:
                return (
                    extractions,
                    analyzed_count,
                    failed_count,
                    origins[offset:],
                    hydration_totals,
                )
            origin_batch = origins[offset : offset + batch_size]

            async def process_batch():
                hydration = await self.enricher.hydrate_priority_origins(
                    origin_batch
                )
                extracted = await self.analyzer.run(origin_batch)
                return hydration, extracted

            try:
                hydration, batch_extractions = await asyncio.wait_for(
                    process_batch(), timeout=remaining
                )
            except TimeoutError:
                return (
                    extractions,
                    analyzed_count,
                    failed_count,
                    origins[offset:],
                    hydration_totals,
                )

            for key, value in hydration.items():
                hydration_totals[key] += int(value or 0)
            failed_fingerprints = {
                item.evidence.fingerprint
                for item in batch_extractions
                if not item.canonical_name
                and not item.rubric_assessment
                and bool(item.rejection_reason)
            }
            successful_origins = [
                item
                for item in origin_batch
                if item.fingerprint not in failed_fingerprints
            ]
            failed_origins = [
                item
                for item in origin_batch
                if item.fingerprint in failed_fingerprints
            ]
            for item in failed_origins:
                item.raw["analysis_failure_count"] = (
                    int(item.raw.get("analysis_failure_count", 0) or 0) + 1
                )
                item.raw["last_analysis_failure_at"] = datetime.now(
                    timezone.utc
                ).isoformat()
            self.store.mark_evidence(successful_origins, analyzed=True)
            if failed_origins:
                self.store.mark_evidence(failed_origins, analyzed=False)
            extractions.extend(batch_extractions)
            analyzed_count += len(origin_batch)
            failed_count += len(failed_fingerprints)
        return extractions, analyzed_count, failed_count, [], hydration_totals

    async def _deep_analyze_in_batches(
        self,
        candidates: list,
        supporting: list,
        deadline: float,
    ) -> tuple[list, list]:
        completed = []
        batch_size = config.PARADIGM_DEEP_BATCH_SIZE
        for offset in range(0, len(candidates), batch_size):
            remaining = _remaining_seconds(deadline)
            if remaining <= 0:
                return completed, candidates[offset:]
            # External enrichment mutates candidates.  Work on a copy so an
            # interrupted batch never persists half-scrubbed community text.
            candidate_batch = copy.deepcopy(
                candidates[offset : offset + batch_size]
            )

            async def process_batch():
                values = await self.enricher.run(candidate_batch, supporting)
                values = await self.synthesizer.run(values)
                return await self.trajectory.run(values)

            try:
                values = await asyncio.wait_for(
                    process_batch(), timeout=remaining
                )
            except TimeoutError:
                return completed, candidates[offset:]
            completed.extend(values)
        return completed, []

    async def _refresh_in_batches(
        self,
        candidates: list,
        supporting: list,
        deadline: float,
    ) -> tuple[list, list, int]:
        refreshed = []
        attempted = 0
        batch_size = config.PARADIGM_DEEP_BATCH_SIZE
        for offset in range(0, len(candidates), batch_size):
            remaining = _remaining_seconds(deadline)
            if remaining <= 0:
                return refreshed, candidates[offset:], attempted
            candidate_batch = candidates[offset : offset + batch_size]

            async def process_batch():
                values = await self.enricher.refresh(candidate_batch, supporting)
                if values:
                    values = await self.synthesizer.run(values)
                return values

            try:
                values = await asyncio.wait_for(
                    process_batch(), timeout=remaining
                )
            except TimeoutError:
                return refreshed, candidates[offset:], attempted
            refreshed.extend(values)
            attempted += len(candidate_batch)
        return refreshed, [], attempted


def _origin_analysis_priority(evidence) -> tuple[int, int, int, int, int, str]:
    """只安排执行先后，不依据优先级删除任何待评估原点。"""
    publisher_rank = {
        "established": 2,
        "verified": 1,
        "unknown": 0,
    }.get(str(evidence.raw.get("publisher_tier", "unknown")), 0)
    return (
        int(bool(evidence.raw.get("explicit_seed"))),
        int(evidence.raw.get("origin_priority", 0) or 0),
        int(evidence.raw.get("origin_kind") == "technical_report"),
        publisher_rank,
        -int(evidence.raw.get("analysis_failure_count", 0) or 0),
        evidence.published_at or "",
    )


def _origin_execution_order(pending: list, newly_discovered: list) -> list:
    """高势能材料先行；普通材料在新发现与旧 backlog 间公平轮转。"""
    high_pending = sorted(
        (item for item in pending if _is_high_priority_origin(item)),
        key=_origin_analysis_priority,
        reverse=True,
    )
    high_new = sorted(
        (
            item
            for item in newly_discovered
            if _is_high_priority_origin(item)
        ),
        key=_origin_analysis_priority,
        reverse=True,
    )
    high = []
    for index in range(max(len(high_pending), len(high_new))):
        if index < len(high_new):
            high.append(high_new[index])
        if index < len(high_pending):
            high.append(high_pending[index])
    ordinary_pending = [
        item for item in pending if not _is_high_priority_origin(item)
    ]
    ordinary_new = sorted(
        (
            item
            for item in newly_discovered
            if not _is_high_priority_origin(item)
        ),
        key=lambda item: item.published_at or "",
        reverse=True,
    )
    ordinary = []
    for index in range(max(len(ordinary_pending), len(ordinary_new))):
        # 新材料先进入本周视野；同一轮紧接一条 FIFO 旧积压，避免冷启动
        # backlog 被每周新增论文永久饿死。
        if index < len(ordinary_new):
            ordinary.append(ordinary_new[index])
        if index < len(ordinary_pending):
            ordinary.append(ordinary_pending[index])
    return [*high, *ordinary]


def _is_high_priority_origin(evidence) -> bool:
    return bool(
        evidence.raw.get("explicit_seed")
        or int(evidence.raw.get("origin_priority", 0) or 0) >= 2
        or evidence.raw.get("origin_kind") == "technical_report"
    )


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


def _execution_deadlines(started: float) -> tuple[float, float, float, int]:
    """为机制抽取、深挖和最终交付划分同一软时间预算。"""
    budget = config.PARADIGM_RUN_BUDGET_SECONDS
    if budget <= 0:
        return float("inf"), float("inf"), float("inf"), 0
    # 防止极小测试预算被固定 reserve 全部吃掉；正常云端配置仍使用 600 秒。
    reserve = min(
        config.PARADIGM_STAGE_RESERVE_SECONDS,
        max(budget // 4, 1),
    )
    run_deadline = started + budget
    return (
        run_deadline,
        run_deadline - (2 * reserve),
        run_deadline - reserve,
        reserve,
    )


def _remaining_seconds(deadline: float) -> float:
    if deadline == float("inf"):
        # asyncio.wait_for requires a finite number on some event-loop versions.
        return 365 * 24 * 60 * 60
    return max(deadline - time.monotonic(), 0.0)


def _empty_hydration_stats() -> dict[str, int]:
    return {
        "priority_origin_targets": 0,
        "priority_origin_hydrated": 0,
        "priority_origin_hydration_failed": 0,
    }


def _record_deferred_candidate(candidate, reason: str) -> None:
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
            "rejection_reason": reason,
            "rubric_score": candidate.screening_rubric.get("score", 0),
            "rubric_decision": "not_executed",
        }
    )
