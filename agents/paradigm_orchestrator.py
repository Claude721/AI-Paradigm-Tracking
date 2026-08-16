"""技术范式雷达 v2 流水线编排。"""

from __future__ import annotations

import asyncio
import copy
import logging
import time
from collections import Counter
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
from paradigms.models import (
    ORIGIN_EVIDENCE_TYPES,
    EvidenceType,
    assess_candidate_freshness,
    key_researcher_profiles,
    primary_material_url,
    verified_organization_attribution,
)
from paradigms.scoring import is_reportable, score_candidate
from run_audit import run_audit

logger = logging.getLogger(__name__)


class ParadigmOrchestrator:
    def __init__(self):
        self.store = ParadigmStore()
        self.bootstrap_mode = self.store.is_bootstrap_required()
        self.ordinary_discovery_lookback_days = config.SOURCING_LOOKBACK_DAYS
        self.high_signal_discovery_lookback_days = (
            config.PARADIGM_BOOTSTRAP_LOOKBACK_DAYS
            if self.bootstrap_mode
            else config.PARADIGM_RECALL_OVERLAP_DAYS
        )
        # 旧统计字段保留为“本轮最长窗口”，便于历史审计兼容。
        self.discovery_lookback_days = self.high_signal_discovery_lookback_days
        self.discovery = ParadigmDiscovery(
            broad_lookback_days=self.ordinary_discovery_lookback_days,
            high_signal_lookback_days=self.high_signal_discovery_lookback_days,
        )
        self.analyzer = ParadigmAnalyzer()
        self.enricher = EvidenceEnricher()
        self.synthesizer = ParadigmSynthesizer()
        self.trajectory = ResearcherTrajectoryAnalyzer()
        # 由统一入口在邮件成功后再登记交付，避免“数据库显示已交付但邮件失败”。
        self.pending_delivery: list = []

    async def run(self) -> dict:
        started = datetime.now(timezone.utc)
        started_monotonic = time.monotonic()
        stats: dict = {
            "pipeline_mode": "paradigm",
            "run_budget_seconds": config.PARADIGM_RUN_BUDGET_SECONDS,
        }

        batch = await self.discovery.run()
        # 发现源的耗时不可预知，尤其冷启动会翻阅更长窗口。研究阶段的份额
        # 必须在发现完成后按剩余时间重新划分，否则慢发现会把机制抽取窗口
        # 直接吃完，形成“抓到近两万条、只分析六条”的假运行。
        _, origin_deadline, deep_deadline, effective_reserve = (
            _execution_deadlines(started_monotonic, time.monotonic())
        )
        stats["stage_reserve_seconds"] = effective_reserve
        stats["discovery_elapsed_seconds"] = max(
            time.monotonic() - started_monotonic,
            0.0,
        )
        stats["bootstrap_mode"] = self.bootstrap_mode
        stats["discovery_lookback_days"] = self.discovery_lookback_days
        stats["ordinary_discovery_lookback_days"] = (
            self.ordinary_discovery_lookback_days
        )
        stats["high_signal_discovery_lookback_days"] = (
            self.high_signal_discovery_lookback_days
        )
        stats["origin_count"] = len(batch.origins)
        stats["supporting_count"] = len(batch.supporting)
        stats["source_counts"] = batch.source_counts
        stats["frontier_coverage"] = batch.coverage
        run_audit.checkpoint(stats)
        run_audit.event(
            "recall_windows",
            "passed",
            (
                f"普通发现 {self.ordinary_discovery_lookback_days} 天；"
                f"正式报告/重点研究者/官方入口回补 "
                f"{self.high_signal_discovery_lookback_days} 天；"
                + ", ".join(
                    f"{name}={count}"
                    for name, count in sorted(batch.source_counts.items())
                )
            ),
        )
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
            or str(value.get("status", "")).startswith("not_executed_")
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
            if value.get("status") not in {"completed", "completed_after_retry"}
        ]
        run_audit.event(
            "academic_index_coverage",
            "warning" if degraded_indexes else "passed",
            "；".join(
                (
                    f"{name}={value.get('status')}，"
                    f"queries {value.get('completed_queries', 0)}/"
                    f"{value.get('planned_queries', value.get('queries', 0))}，requests "
                    f"{value.get('requests', 0)}，results "
                    f"{value.get('results', 0)}，429 "
                    f"{value.get('rate_limited_requests', 0)}，短暂故障重试 "
                    f"{value.get('transient_retries', 0)}"
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
        repository_coverage = batch.coverage.get("official_repositories") or {}
        repository_failures = repository_coverage.get("failed_organizations") or []
        repository_page_failures = (
            repository_coverage.get("repository_page_failures") or []
        )
        repository_only = repository_coverage.get("repository_only_releases") or []
        repository_unverified = (
            repository_coverage.get("unverified_primary_releases") or []
        )
        run_audit.event(
            "official_repository_release_coverage",
            (
                "warning"
                if repository_failures
                or repository_page_failures
                or repository_unverified
                else "passed"
            ),
            (
                f"官方 GitHub 组织 {repository_coverage.get('checked_organizations', 0)}/"
                f"{repository_coverage.get('configured_organizations', 0)}；"
                f"窗口内新仓库 {repository_coverage.get('recent_repositories', 0)}；"
                f"链接到独立一手材料 {repository_coverage.get('linked_primary_origins', 0)}；"
                f"外部一手链接核验失败 "
                f"{repository_coverage.get('external_primary_validation_failures', 0)}/"
                f"{repository_coverage.get('external_primary_targets', 0)}；"
                f"仅仓库、未生成范式原点 {repository_only[:10]}；"
                f"外部一手链接未通过 HTTP 核验 {repository_unverified[:10]}；"
                f"组织读取失败 {repository_failures[:10]}；"
                f"仓库分页中断 {repository_page_failures[:10]}"
            ),
        )
        domain_coverage_incomplete = any(
            value.get("status") in {"query_failed", "not_executed"}
            for value in (batch.coverage.get("domains") or {}).values()
        )
        source_health = batch.coverage.get("source_health") or {}
        failed_sources = [
            name
            for name, value in source_health.items()
            if value.get("status") in {"partial", "query_failed", "timed_out"}
        ]
        run_audit.event(
            "discovery_source_health",
            "warning" if failed_sources else "passed",
            "；".join(
                f"{name}={value.get('status')}，{value.get('results', 0)} 条，"
                f"{value.get('elapsed_seconds', 0)}s"
                for name, value in source_health.items()
            )
            or "没有发现源运行记录",
        )
        stats["recall_coverage_incomplete"] = bool(
            domain_coverage_incomplete
            or failed_lanes
            or degraded_indexes
            or official_warning
            or failed_sources
        )
        # 覆盖地图基线只受地图/核心学术召回是否闭合影响。官方网页、Feed
        # 等动态入口的局部失败仍会让本期报告标成 incomplete，但不能因为
        # 一个长期失效页面让整个仓库永远停在 60 天 bootstrap 模式。
        baseline_lane_failures = [
            name for name in failed_lanes if name != "explicit_seeds"
        ]
        stats["landscape_coverage_incomplete"] = bool(
            domain_coverage_incomplete
            or baseline_lane_failures
            or degraded_indexes
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
                completed_origins,
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
            _commit_origin_analysis_checkpoint(
                self.store,
                new_candidates,
                completed_origins,
            )
        else:
            new_candidates = []
            analyzed_origin_count = 0
            failed_origin_count = 0
            budget_deferred_origins = []
            completed_origins = []
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
        run_audit.checkpoint(stats)
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
            (
                deep_candidates,
                budget_deferred_candidates,
                execution_deferred_candidates,
            ) = (
                await self._deep_analyze_in_batches(
                    deep_candidates,
                    batch.supporting,
                    deep_deadline,
                )
            )
        else:
            budget_deferred_candidates = []
            execution_deferred_candidates = []
        deferred_candidates = [
            *safety_deferred_candidates,
            *budget_deferred_candidates,
            *execution_deferred_candidates,
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
        stats["candidate_execution_deferred_count"] = len(
            execution_deferred_candidates
        )
        stats["candidate_deferred_count"] = len(deferred_candidates)
        run_audit.checkpoint(stats)
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
        (
            refreshed,
            refresh_unchanged,
            refresh_budget_deferred,
            refresh_execution_deferred,
            refresh_attempted,
        ) = (
            await self._refresh_in_batches(
                historical,
                batch.supporting,
                deep_deadline,
            )
        )
        stats["refresh_analysis_count"] = refresh_attempted
        stats["refresh_safety_deferred_count"] = len(refresh_safety_deferred)
        stats["refresh_budget_deferred_count"] = len(refresh_budget_deferred)
        stats["refresh_execution_deferred_count"] = len(
            refresh_execution_deferred
        )
        stats["refresh_deferred_count"] = (
            len(refresh_safety_deferred)
            + len(refresh_budget_deferred)
            + len(refresh_execution_deferred)
        )
        stats["refreshed_paradigms"] = len(refreshed)
        stats["refresh_unchanged_count"] = len(refresh_unchanged)
        run_audit.checkpoint(stats)
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

        # 综合器/最终 Rubric 的结构失败不是“技术不值得关注”。这些路线必须
        # 回到完整深挖队列，而且要进入本轮 backlog 统计；否则空报告会把
        # 实际的模型输出故障误写成“本周没有新范式”。
        research_incomplete_candidates = [
            candidate
            for candidate in candidates
            if candidate.status == "pending_deep"
            and candidate.rubric_assessment.get("decision") == "incomplete"
        ]
        stats["candidate_research_incomplete_count"] = len(
            research_incomplete_candidates
        )
        if research_incomplete_candidates:
            run_audit.event(
                "deep_research_contract",
                "deferred",
                f"{len(research_incomplete_candidates)} 条路线的综合/Rubric 输出"
                "未闭合；已保留 pending_deep，下轮重新执行完整深挖，不写成淘汰",
            )

        # 支持证据单独去重入库，但绝不独立生成范式。
        self.store.mark_evidence(batch.supporting, analyzed=False)
        self.store.mark_evidence(
            [evidence for candidate in candidates for evidence in candidate.evidence],
            analyzed=False,
        )
        reportable = [candidate for candidate in candidates if is_reportable(candidate)]
        delivery_profile_deferred = [
            candidate
            for candidate in reportable
            if not _delivery_profile_ready(candidate)
        ]
        if delivery_profile_deferred:
            deferred_keys = {
                candidate.key for candidate in delivery_profile_deferred
            }
            reportable = [
                candidate
                for candidate in reportable
                if candidate.key not in deferred_keys
            ]
            for candidate in delivery_profile_deferred:
                candidate.status = "pending_deep"
            run_audit.event(
                "delivery_profile_readiness",
                "warning",
                f"{len(delivery_profile_deferred)} 条已通过研究准入的路线缺少"
                "可交付人物背景/联系方式检索记录；保留 pending_deep，"
                "不降低 Rubric 结论，也不发送残缺报告",
            )
        delivery_source_deferred = [
            candidate
            for candidate in reportable
            if not _delivery_primary_source_ready(candidate)
        ]
        if delivery_source_deferred:
            deferred_keys = {
                candidate.key for candidate in delivery_source_deferred
            }
            reportable = [
                candidate
                for candidate in reportable
                if candidate.key not in deferred_keys
            ]
            for candidate in delivery_source_deferred:
                candidate.status = "pending_deep"
            run_audit.event(
                "delivery_primary_source_readiness",
                "warning",
                f"{len(delivery_source_deferred)} 条已通过研究准入的路线缺少"
                "安全的一手论文/官方材料 URL；保留 pending_deep，不发送"
                "无法追溯原文的报告",
            )
        stats["delivery_profile_deferred_count"] = len(
            delivery_profile_deferred
        )
        stats["delivery_source_deferred_count"] = len(
            delivery_source_deferred
        )
        freshness_deferred = []
        for candidate in reportable:
            assessment = assess_candidate_freshness(
                candidate,
                window_days=self.high_signal_discovery_lookback_days,
            )
            candidate.freshness_assessment = assessment
            if assessment["decision"] == "defer":
                candidate.status = "observe"
                candidate.rejection_reason = assessment["reason"]
                freshness_deferred.append(candidate)
        if freshness_deferred:
            deferred_keys = {candidate.key for candidate in freshness_deferred}
            reportable = [
                candidate
                for candidate in reportable
                if candidate.key not in deferred_keys
            ]
            run_audit.event(
                "report_freshness",
                "deferred",
                f"{len(freshness_deferred)} 条技术 Rubric 已通过，但一手材料"
                "并非本期发布且没有本期独立承接（或发布日期不可核验）；"
                "保留观察，不把首次入库误写成本周新范式："
                + "、".join(
                    (candidate.route_family or candidate.name)[:60]
                    for candidate in freshness_deferred[:6]
                ),
            )
        else:
            run_audit.event(
                "report_freshness",
                "passed",
                "所有待交付路线均有窗口内一手发布，或有窗口内独立承接/指标增量",
            )
        stats["freshness_deferred_count"] = len(freshness_deferred)
        stats["reportable_count"] = len(reportable)
        reportable = self.store.prepare_report(reportable)
        reportable = sorted(
            reportable, key=lambda item: item.total_score, reverse=True
        )
        reportable, report_deferred = _apply_safety_limit(
            reportable, config.PARADIGM_REPORT_SAFETY_LIMIT
        )
        for candidate in report_deferred:
            candidate.status = "pending_deep"
        stats["report_safety_deferred_count"] = len(report_deferred)
        if report_deferred:
            run_audit.event(
                "report_safety_limit",
                "warning",
                f"{len(report_deferred)} 条已具备交付条件的路线因用户显式"
                " report safety limit 延后；已保留 pending_deep，不能记作淘汰",
            )
        stats["high_value_count"] = len(reportable)
        stats["new_paradigms"] = sum(item.report_kind == "new" for item in reportable)
        stats["updated_paradigms"] = sum(
            item.report_kind == "update" for item in reportable
        )

        stats["run_incomplete"] = bool(
            stats["recall_coverage_incomplete"]
            or stats["analysis_deferred_count"]
            or stats["candidate_deferred_count"]
            or stats["candidate_research_incomplete_count"]
            or stats["refresh_deferred_count"]
            or stats["delivery_profile_deferred_count"]
            or stats["delivery_source_deferred_count"]
            or stats["report_safety_deferred_count"]
        )
        stats["pending_work_count"] = (
            stats["analysis_deferred_count"]
            + stats["candidate_deferred_count"]
            + stats["candidate_research_incomplete_count"]
            + stats["refresh_deferred_count"]
            + stats["delivery_profile_deferred_count"]
            + stats["delivery_source_deferred_count"]
            + stats["report_safety_deferred_count"]
        )
        stats["run_budget_exhausted"] = bool(
            stats["analysis_budget_deferred_count"]
            or stats["candidate_budget_deferred_count"]
            or stats["refresh_budget_deferred_count"]
        )
        stats["result_kind"] = (
            "partial_memo"
            if reportable and stats["run_incomplete"]
            else "research_incomplete"
            if stats["run_incomplete"]
            else "complete_memo"
            if reportable
            else "complete_no_signal"
        )

        # 刷新失败的历史路线不参与本轮写作，但新的失败计数必须随原快照
        # 持久化；否则下轮排序看不到失败历史，同一个外部异常会反复占用
        # 相同执行位置。成功处理后会清零，避免一次偶发故障永久降权。
        self.store.save_candidates(
            [
                *candidates,
                *deferred_candidates,
                *refresh_execution_deferred,
                *refresh_unchanged,
            ]
        )
        # 发现结果与覆盖基线是两个检查点。已抓到的原点即使后续失败也保留
        # 在 backlog；但只要任一召回车道/索引/官方入口没有闭合，就不能把
        # 新地图版本标成已完成，否则下一轮会失去 bootstrap 补扫窗口。
        stats["landscape_checkpoint_advanced"] = (
            _commit_landscape_checkpoint_if_complete(
                self.store,
                landscape_coverage_incomplete=stats[
                    "landscape_coverage_incomplete"
                ],
            )
        )
        if not stats["landscape_checkpoint_advanced"]:
            run_audit.event(
                "landscape_checkpoint",
                "deferred",
                "本轮领域/核心学术召回未闭合；保留旧地图版本，下次继续高信号补扫",
            )
        self.pending_delivery = reportable
        stats["saved_count"] = len(
            {
                candidate.key
                for candidate in [
                    *candidates,
                    *deferred_candidates,
                    *refresh_execution_deferred,
                    *refresh_unchanged,
                ]
            }
        )
        stats["elapsed_seconds"] = (
            datetime.now(timezone.utc) - started
        ).total_seconds()
        run_audit.checkpoint(stats)
        logger.info(
            "范式研究阶段完成：原始材料=%s，范式候选=%s，待交付=%s",
            stats["origin_count"],
            len(candidates),
            len(reportable),
        )
        return stats

    async def _analyze_origins_in_batches(
        self,
        origins: list,
        deadline: float,
    ) -> tuple[list, int, int, list, dict[str, int], list]:
        """逐批抽取；只有候选快照落盘后，原点才能提交为已分析。"""
        extractions = []
        analyzed_count = 0
        failed_count = 0
        hydration_totals = _empty_hydration_stats()
        completed_origins = []
        budget_deferred = []
        batch_size = config.PARADIGM_ANALYSIS_BATCH_SIZE

        def record_failed(items: list, reason: str) -> None:
            nonlocal analyzed_count, failed_count
            now = datetime.now(timezone.utc).isoformat()
            for item in items:
                item.raw["analysis_failure_count"] = (
                    _safe_int(item.raw.get("analysis_failure_count", 0)) + 1
                )
                item.raw["last_analysis_failure_at"] = now
            self.store.mark_evidence(items, analyzed=False)
            analyzed_count += len(items)
            failed_count += len(items)
            run_audit.event(
                "origin_analysis_item",
                "deferred",
                f"{len(items)} 条原点因 {reason} 保留 pending；未写成技术淘汰",
            )

        async def analyze_group(group: list) -> None:
            """Split only unexpected batch failures; accept valid peer outputs."""

            nonlocal analyzed_count, failed_count
            remaining = _remaining_seconds(deadline)
            if remaining <= 0:
                budget_deferred.extend(group)
                return
            try:
                values = await asyncio.wait_for(
                    self.analyzer.run(group),
                    timeout=remaining,
                )
                returned, unknown = _validated_origin_stage_output(group, values)
            except asyncio.TimeoutError:
                budget_deferred.extend(group)
                return
            except Exception as exc:
                if len(group) > 1:
                    midpoint = len(group) // 2
                    run_audit.event(
                        "origin_analysis_batch",
                        "isolating",
                        f"{len(group)} 条原点批次发生 {type(exc).__name__}；"
                        "二分隔离坏样本，健康同批材料继续",
                    )
                    await analyze_group(group[:midpoint])
                    await analyze_group(group[midpoint:])
                    return
                logger.exception("单条机制抽取异常；原点保留 pending")
                record_failed(group, type(exc).__name__)
                return

            allowed = {item.fingerprint for item in group}
            if unknown:
                run_audit.event(
                    "origin_analysis_contract",
                    "warning",
                    f"丢弃 {unknown} 条不属于当前输入批次的抽取输出",
                )
            returned_fingerprints = {
                item.evidence.fingerprint for item in returned
            }
            failed_fingerprints = {
                item.evidence.fingerprint
                for item in returned
                if not item.canonical_name
                and not item.rubric_assessment
                and bool(item.rejection_reason)
            }
            missing_fingerprints = allowed - returned_fingerprints
            if missing_fingerprints:
                run_audit.event(
                    "origin_analysis_contract",
                    "deferred",
                    f"模型/分析器漏回 {len(missing_fingerprints)} 条输入；"
                    "只保留漏项待重试，已返回的同批结果继续提交",
                )
            terminal_failures = failed_fingerprints | missing_fingerprints
            successful = [
                item for item in group if item.fingerprint not in terminal_failures
            ]
            failed = [
                item for item in group if item.fingerprint in terminal_failures
            ]
            if failed:
                now = datetime.now(timezone.utc).isoformat()
                for item in failed:
                    item.raw["analysis_failure_count"] = (
                        _safe_int(item.raw.get("analysis_failure_count", 0)) + 1
                    )
                    item.raw["last_analysis_failure_at"] = now
                self.store.mark_evidence(failed, analyzed=False)
            completed_origins.extend(successful)
            extractions.extend(returned)
            analyzed_count += len(group)
            failed_count += len(failed)

        for offset in range(0, len(origins), batch_size):
            remaining = _remaining_seconds(deadline)
            if remaining <= 0:
                return (
                    extractions,
                    analyzed_count,
                    failed_count,
                    origins[offset:],
                    hydration_totals,
                    completed_origins,
                )
            origin_batch = origins[offset : offset + batch_size]
            try:
                hydration = await asyncio.wait_for(
                    self.enricher.hydrate_priority_origins(origin_batch),
                    timeout=remaining,
                )
                if not isinstance(hydration, dict):
                    raise _StageOutputContractError("正文补水统计不是 dict")
            except asyncio.TimeoutError:
                return (
                    extractions,
                    analyzed_count,
                    failed_count,
                    origins[offset:],
                    hydration_totals,
                    completed_origins,
                )
            except Exception as exc:
                targets = sum(_is_high_priority_origin(item) for item in origin_batch)
                hydration = {
                    "priority_origin_targets": targets,
                    "priority_origin_hydrated": 0,
                    "priority_origin_hydration_failed": targets,
                }
                logger.exception("原点正文补水批次异常；降级使用现有摘要继续抽取")
                run_audit.event(
                    "priority_origin_hydration",
                    "warning",
                    f"{len(origin_batch)} 条批次发生 {type(exc).__name__}；"
                    "正文补水降级，但机制抽取仍使用已持久化摘要继续",
                )
            for key, value in hydration.items():
                if key in hydration_totals:
                    hydration_totals[key] += _safe_int(value)
            await analyze_group(origin_batch)
            if budget_deferred:
                budget_deferred.extend(origins[offset + len(origin_batch) :])
                return (
                    extractions,
                    analyzed_count,
                    failed_count,
                    budget_deferred,
                    hydration_totals,
                    completed_origins,
                )
        return (
            extractions,
            analyzed_count,
            failed_count,
            [],
            hydration_totals,
            completed_origins,
        )

    async def _deep_analyze_in_batches(
        self,
        candidates: list,
        supporting: list,
        deadline: float,
    ) -> tuple[list, list, list]:
        completed: list = []
        budget_deferred: list = []
        execution_deferred: list = []
        batch_size = config.PARADIGM_DEEP_BATCH_SIZE

        async def process_group(group: list) -> None:
            remaining = _remaining_seconds(deadline)
            if remaining <= 0:
                budget_deferred.extend(group)
                return
            candidate_batch = copy.deepcopy(group)

            async def process_batch():
                values = await self.enricher.run(candidate_batch, supporting)
                _validate_candidate_stage_output(
                    candidate_batch, values, "external_enrichment"
                )
                values = await self.synthesizer.run(values)
                _validate_candidate_stage_output(
                    candidate_batch, values, "paradigm_synthesis"
                )
                values = await self.trajectory.run(values)
                _validate_candidate_stage_output(
                    candidate_batch, values, "researcher_trajectory"
                )
                return values

            try:
                values = await asyncio.wait_for(
                    process_batch(), timeout=remaining
                )
            except asyncio.TimeoutError:
                budget_deferred.extend(group)
                return
            except Exception as exc:
                if len(group) > 1:
                    midpoint = len(group) // 2
                    run_audit.event(
                        "deep_analysis_batch",
                        "isolating",
                        f"{len(group)} 条路线批次发生 {type(exc).__name__}；"
                        "二分隔离坏样本，健康路线继续",
                    )
                    await process_group(group[:midpoint])
                    await process_group(group[midpoint:])
                    return
                failed = group[0]
                logger.exception("单条候选深挖异常；路线保留 pending")
                failed.execution_failure_count += 1
                failed.last_execution_failure_at = datetime.now(
                    timezone.utc
                ).isoformat()
                _record_deferred_candidate(
                    failed,
                    "深挖执行异常；保留 pending，下轮重试。"
                    f"错误类型：{type(exc).__name__}",
                )
                execution_deferred.append(failed)
                return
            for candidate in values:
                candidate.execution_failure_count = 0
                candidate.last_execution_failure_at = ""
            completed.extend(values)

        for offset in range(0, len(candidates), batch_size):
            group = candidates[offset : offset + batch_size]
            await process_group(group)
            if budget_deferred:
                budget_deferred.extend(candidates[offset + len(group) :])
                break
        return completed, budget_deferred, execution_deferred

    async def _refresh_in_batches(
        self,
        candidates: list,
        supporting: list,
        deadline: float,
    ) -> tuple[list, list, list, list, int]:
        refreshed: list = []
        unchanged: list = []
        budget_deferred: list = []
        execution_deferred: list = []
        attempted = 0
        batch_size = config.PARADIGM_DEEP_BATCH_SIZE

        async def process_group(group: list) -> None:
            nonlocal attempted
            remaining = _remaining_seconds(deadline)
            if remaining <= 0:
                budget_deferred.extend(group)
                return
            candidate_batch = copy.deepcopy(group)

            async def process_batch():
                values = await self.enricher.refresh(candidate_batch, supporting)
                _validate_candidate_stage_output(
                    candidate_batch,
                    values,
                    "community_refresh",
                    allow_subset=True,
                )
                if values:
                    changed_inputs = [
                        candidate
                        for candidate in candidate_batch
                        if candidate.key in {value.key for value in values}
                    ]
                    values = await self.synthesizer.run(values)
                    _validate_candidate_stage_output(
                        changed_inputs,
                        values,
                        "refresh_synthesis",
                    )
                return values

            try:
                values = await asyncio.wait_for(
                    process_batch(), timeout=remaining
                )
            except asyncio.TimeoutError:
                attempted += len(group)
                _mark_refresh_attempted(self, group)
                budget_deferred.extend(group)
                return
            except Exception as exc:
                if len(group) > 1:
                    midpoint = len(group) // 2
                    run_audit.event(
                        "refresh_analysis_batch",
                        "isolating",
                        f"{len(group)} 条历史路线批次发生 {type(exc).__name__}；"
                        "二分隔离坏样本，健康路线继续",
                    )
                    await process_group(group[:midpoint])
                    await process_group(group[midpoint:])
                    return
                attempted += 1
                failed = group[0]
                failed.execution_failure_count += 1
                failed.last_execution_failure_at = datetime.now(
                    timezone.utc
                ).isoformat()
                execution_deferred.append(failed)
                _mark_refresh_attempted(self, group)
                logger.exception("单条历史路线刷新异常；保留原快照后继续")
                run_audit.event(
                    "refresh_analysis_batch",
                    "deferred",
                    f"路线 {failed.key[:80]} 发生 {type(exc).__name__}；"
                    "保留原快照，下轮重试",
                )
                return

            attempted += len(group)
            _mark_refresh_attempted(self, group)
            changed_keys = {candidate.key for candidate in values}
            for candidate in candidate_batch:
                candidate.execution_failure_count = 0
                candidate.last_execution_failure_at = ""
                if candidate.key not in changed_keys:
                    unchanged.append(candidate)
            refreshed.extend(values)

        for offset in range(0, len(candidates), batch_size):
            group = candidates[offset : offset + batch_size]
            await process_group(group)
            if budget_deferred:
                budget_deferred.extend(candidates[offset + len(group) :])
                break
        return refreshed, unchanged, budget_deferred, execution_deferred, attempted


class _StageOutputContractError(RuntimeError):
    """A stage returned a structurally incomplete or foreign result set."""


def _validated_origin_stage_output(group: list, values) -> tuple[list, int]:
    if not isinstance(values, list):
        raise _StageOutputContractError("机制抽取输出不是 list")
    allowed = {item.fingerprint for item in group}
    returned = []
    unknown = 0
    for extraction in values:
        try:
            fingerprint = extraction.evidence.fingerprint
        except (AttributeError, TypeError) as exc:
            raise _StageOutputContractError(
                "机制抽取元素缺少 evidence"
            ) from exc
        if fingerprint not in allowed:
            unknown += 1
            continue
        returned.append(extraction)
    return returned, unknown


def _validate_candidate_stage_output(
    expected: list,
    values,
    stage: str,
    *,
    allow_subset: bool = False,
) -> None:
    """Prevent silent cardinality loss between mutating async stages."""

    if not isinstance(values, list):
        raise _StageOutputContractError(f"{stage} 输出不是 list")
    expected_keys = Counter(str(candidate.key) for candidate in expected)
    try:
        returned_keys = Counter(str(candidate.key) for candidate in values)
    except AttributeError as exc:
        raise _StageOutputContractError(f"{stage} 输出缺少 candidate.key") from exc
    extra = returned_keys - expected_keys
    missing = expected_keys - returned_keys
    if extra or (missing and not allow_subset):
        raise _StageOutputContractError(
            f"{stage} 输出基数不守恒: missing={dict(missing)}, extra={dict(extra)}"
        )


def _mark_refresh_attempted(orchestrator, candidates: list) -> None:
    store = getattr(orchestrator, "store", None)
    marker = getattr(store, "mark_refresh_attempted", None)
    if marker is not None:
        marker(candidates)


def _origin_analysis_priority(evidence) -> tuple[int, int, int, int, int, str]:
    """只安排执行先后，不依据优先级删除任何待评估原点。"""
    publisher_rank = {
        "established": 2,
        "verified": 1,
        "unknown": 0,
    }.get(str(evidence.raw.get("publisher_tier", "unknown")), 0)
    return (
        int(bool(evidence.raw.get("explicit_seed"))),
        _safe_int(evidence.raw.get("origin_priority", 0)),
        int(evidence.raw.get("origin_kind") == "technical_report"),
        publisher_rank,
        -_safe_int(evidence.raw.get("analysis_failure_count", 0)),
        evidence.published_at or "",
    )


def _commit_landscape_checkpoint_if_complete(
    store,
    *,
    landscape_coverage_incomplete: bool,
) -> bool:
    """Advance the coverage baseline only after every discovery lane closes."""

    if landscape_coverage_incomplete:
        return False
    store.mark_landscape_version()
    return True


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
        or _safe_int(evidence.raw.get("origin_priority", 0)) >= 2
        or evidence.raw.get("origin_kind") == "technical_report"
    )


def _deep_analysis_priority(candidate) -> tuple[float, int, float, float, int]:
    """顺序只影响执行先后；Rubric 决定是否深挖，数量不由排序截断。"""
    origin_priority = max(
        (
            _safe_int(item.raw.get("origin_priority", 0))
            for item in candidate.evidence
        ),
        default=0,
    )
    return (
        float(origin_priority),
        -_safe_int(candidate.execution_failure_count),
        _safe_float(candidate.screening_rubric.get("score", 0.0)),
        _safe_float(candidate.screening_rubric.get("answer_coverage", 0.0)),
        len(candidate.evidence),
    )


def _apply_safety_limit(items: list, limit: int) -> tuple[list, list]:
    """0 表示不限制；非零值是执行熔断，不是研究筛选或 Top-K。"""
    if limit <= 0 or len(items) <= limit:
        return items, []
    return items[:limit], items[limit:]


def _execution_deadlines(
    started: float,
    discovery_completed: float | None = None,
) -> tuple[float, float, float, int]:
    """发现完成后按剩余时间划分机制抽取与深挖预算。

    返回值仍是 ``run/origin/deep/reserve``。deep deadline 就是研究阶段总
    deadline；reserve 表示从剩余预算中留给深挖的时间。没有把任何候选按
    数量截断，时间用尽的材料仍进入持久化 backlog。
    """
    budget = config.PARADIGM_RUN_BUDGET_SECONDS
    if budget <= 0:
        return float("inf"), float("inf"), float("inf"), 0
    discovery_completed = (
        started if discovery_completed is None else max(discovery_completed, started)
    )
    run_deadline = started + budget
    remaining = max(run_deadline - discovery_completed, 0.0)
    # 配置值是深挖目标份额；若发现已经很慢，最多取剩余时间的一半，确保
    # 机制抽取和深挖都还有机会执行，不会出现某一阶段被负 deadline 跳过。
    reserve = min(
        config.PARADIGM_STAGE_RESERVE_SECONDS,
        max(int(remaining // 2), 0),
    )
    return (
        run_deadline,
        run_deadline - reserve,
        run_deadline,
        reserve,
    )


def _remaining_seconds(deadline: float) -> float:
    if deadline == float("inf"):
        # asyncio.wait_for requires a finite number on some event-loop versions.
        return 365 * 24 * 60 * 60
    return max(deadline - time.monotonic(), 0.0)


def _safe_int(value) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _safe_float(value) -> float:
    try:
        return float(value or 0.0)
    except (TypeError, ValueError):
        return 0.0


def _empty_hydration_stats() -> dict[str, int]:
    return {
        "priority_origin_targets": 0,
        "priority_origin_hydrated": 0,
        "priority_origin_hydration_failed": 0,
    }


def _commit_origin_analysis_checkpoint(
    store,
    candidates: list,
    completed_origins: list,
) -> None:
    """Commit in loss-safe order: resumable routes first, skip markers second."""

    for candidate in candidates:
        candidate.status = "pending_deep"
    # SQLite commits each method atomically. If candidate persistence fails,
    # analyzed flags remain false and the next run safely retries the origins.
    store.save_candidates(candidates)
    store.mark_evidence(completed_origins, analyzed=True)


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


def _delivery_profile_ready(candidate) -> bool:
    """Operational completeness gate; it never changes the research Rubric."""

    key_people = key_researcher_profiles(
        candidate.researchers,
        config.PARADIGM_KEY_RESEARCHER_LIMIT,
    )
    if not key_people:
        return bool(verified_organization_attribution(candidate))
    return all(
        bool(
            profile.current_affiliation
            or profile.background_summary
            or profile.prior_affiliations
            or profile.research_trajectory
            or profile.key_person_reason
        )
        and profile.contact_lookup_completed
        for profile in key_people
    )


def _delivery_primary_source_ready(candidate) -> bool:
    return any(
        evidence.evidence_type in ORIGIN_EVIDENCE_TYPES
        and primary_material_url(evidence)
        for evidence in candidate.evidence
    )
