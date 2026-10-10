"""技术范式雷达 v2 流水线编排。"""

from __future__ import annotations

import asyncio
import copy
import logging
import sqlite3
import time
from collections import deque
from collections import Counter
from contextlib import aclosing, nullcontext
from itertools import islice
from datetime import datetime, timedelta, timezone

import config
from database.paradigm_store import EvidenceCheckpointResult, ParadigmStore
from paradigms.analyzer import (
    ParadigmAnalyzer,
    ParadigmSynthesizer,
    ResearcherTrajectoryAnalyzer,
    _should_prefilter_origin,
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
    _evidence_datetime,
    assess_candidate_freshness,
    delivery_researcher_profiles,
    is_verified_substantive_discussion,
    primary_material_url,
    verified_organization_attribution,
)
from paradigms.publication import classify_publication
from paradigms.scoring import is_reportable, score_candidate
from paradigms.scheduler import ResearchLaneScheduler
from paradigms.async_utils import gather_scoped
from run_audit import run_audit
from runtime_clock import research_now, research_window, scheduled_date, observation_now

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
            stage_cache=self.store.campaigns,
        )
        self.analyzer = ParadigmAnalyzer(
            enable_batch_prefilter=config.PARADIGM_ORIGIN_PREFILTER_ENABLED,
            technical_report_mechanism_slice=(
                config.PARADIGM_TECHNICAL_REPORT_MECHANISM_SLICE
            ),
            stage_cache=self.store.campaigns,
        )
        self.enricher = EvidenceEnricher()
        self.synthesizer = ParadigmSynthesizer()
        self.trajectory = ResearcherTrajectoryAnalyzer(stage_cache=self.store.campaigns)
        self.deep_concurrency = config.PARADIGM_DEEP_CONCURRENCY
        # 由统一入口在邮件成功后再登记交付，避免“数据库显示已交付但邮件失败”。
        self.pending_delivery: list = []

    async def run(self, *, reference_time: datetime | None = None, resume_only: bool = False) -> dict:
        active = self.store.campaigns.active()
        if resume_only and active is None:
            raise RuntimeError("没有待续跑的研究批次；--resume-research 不启动新发现")
        with research_window(active.reference_time if active else reference_time,
                             observation_time=reference_time or datetime.now(timezone.utc),
                             lookback_days=active.ordinary_days if active else self.ordinary_discovery_lookback_days):
            if active is None:
                active = self.store.campaigns.begin(
                    reference_time=research_now(), ordinary_days=self.ordinary_discovery_lookback_days,
                    high_signal_days=self.high_signal_discovery_lookback_days,
                    bootstrap=self.bootstrap_mode, seeds=list(config.PARADIGM_SEED_ARXIV_IDS),
                )
            self.campaign = self.store.campaigns.begin_run(active.campaign_id)
            self.ordinary_discovery_lookback_days = active.ordinary_days
            self.high_signal_discovery_lookback_days = active.high_signal_days
            self.discovery_lookback_days = active.high_signal_days
            self.bootstrap_mode = active.bootstrap
            # The same window applies even when the next process starts weeks
            # later. Do not let a resume silently become another discovery run.
            if isinstance(self.discovery, ParadigmDiscovery):
                self.discovery = ParadigmDiscovery(
                    broad_lookback_days=active.ordinary_days,
                    high_signal_lookback_days=active.high_signal_days,
                    stage_cache=self.store.campaigns,
                )
                self.discovery.arxiv.seed_arxiv_ids = active.seeds
            return await self._run_research()

    async def _run_research(self) -> dict:
        started = datetime.now(timezone.utc)
        started_monotonic = time.monotonic()
        stats: dict = {
            "pipeline_mode": "paradigm",
            "reference_time": research_now().isoformat(),
            "observation_time": observation_now().isoformat(),
            "report_date": scheduled_date(research_now()),
            "run_budget_seconds": config.PARADIGM_RUN_BUDGET_SECONDS,
        }
        queue_before = self.store.work_queue_snapshot(reference_time=research_now())
        stats["work_queue_before"] = queue_before

        if self.campaign.discovery is None:
            batch = await self.discovery.run()
            stats["discovery_resumed"] = False
        else:
            from paradigms.completion import discovery_retry_sources
            retry_sources = discovery_retry_sources(self.campaign.discovery.coverage)
            stats["discovery_resumed"] = True
            stats["discovery_retry_sources"] = sorted(retry_sources)
            batch = await self.discovery.retry(self.campaign.discovery) if retry_sources else self.campaign.discovery
        # 发现源的耗时不可预知，尤其冷启动会翻阅更长窗口。研究阶段的份额
        # 必须在发现完成后按剩余时间重新划分，否则慢发现会把机制抽取窗口
        # 直接吃完，形成“抓到近两万条、只分析六条”的假运行。
        _, _, deep_deadline, effective_reserve = (
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
            if value.get("status") not in {"completed", "completed_after_retry", "not_configured", "disabled"}
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
            or official_coverage.get("unresolved_citations")
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
                f"未解析书目 {official_coverage.get('unresolved_citations', 0)}；"
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
        from paradigms.completion import discovery_retry_sources
        stats["recall_coverage_incomplete"] = bool(stats["recall_coverage_incomplete"] or discovery_retry_sources(batch.coverage))
        # 覆盖地图基线只受普通窗口内的 landscape 车道和 OpenAlex 核心
        # 学术召回影响。Technical Report、重点研究者、OpenReview、官方网页
        # 与 Feed 的局部失败仍会进入本期覆盖边界，但不能把 60 天 bootstrap
        # 永久锁住；这些入口本来就是高信号回补或扩展覆盖，不是地图基线。
        stats["landscape_coverage_incomplete"] = (
            _landscape_baseline_incomplete(
                domain_coverage_incomplete=domain_coverage_incomplete,
                failed_lanes=failed_lanes,
                degraded_indexes=degraded_indexes,
                recall_lanes=recall_lanes,
            )
        )
        with self.store.transaction():
            # Persist the downstream campaign snapshot before the upstream
            # discovery records are acknowledged. A crash commits both or none.
            self.store.campaigns.save_discovery(self.campaign.campaign_id, batch)
            origins, incremental, origin_checkpoint = self.store.observe_origins(batch.origins)
            self.store.campaigns.reconcile(self.campaign.campaign_id)
        stats.update({f"origin_{key}": value for key, value in incremental.items()})
        # 发现和分析必须是两个独立检查点。先把本轮所有新原点写成 pending，
        # 即使后续只处理其中一部分，也不会把运行预算误写成研究淘汰。
        stats["evidence_checkpoint_rejected_count"] = (
            origin_checkpoint.rejected_count
        )
        checkpoint_rejection_sources = dict(
            origin_checkpoint.rejection_sources
        )
        if origin_checkpoint.rejected_count:
            # 核心学术原点未能形成可恢复检查点时，本轮覆盖地图也不闭合；
            # 否则可能在实际漏掉原点的情况下错误推进 bootstrap 基线。
            stats["landscape_coverage_incomplete"] = True
            run_audit.event(
                "evidence_checkpoint_contract",
                "warning",
                f"发现阶段有 {origin_checkpoint.rejected_count} 条记录未满足持久化"
                f"契约；按来源隔离 {checkpoint_rejection_sources}，健康记录继续处理",
            )
        pending_origins = self.store.load_pending_origins(
            exclude_fingerprints={item.fingerprint for item in origins}
        )
        for origin in [*pending_origins, *origins]:
            _revalidate_legacy_technical_report(origin)
        origins = _origin_execution_order(
            pending_origins, origins,
            reference_time=research_now(),
            window_days=self.ordinary_discovery_lookback_days,
        )
        stats["pending_origin_backlog_loaded"] = len(pending_origins)
        planned_count = len(origins)
        stats["current_window_origin_planned_count"] = sum(
            _origin_is_in_delivery_window(
                item, reference_time=research_now(),
                window_days=self.ordinary_discovery_lookback_days,
            )
            for item in origins
        )
        origins, safety_deferred_origins = _apply_safety_limit(
            origins, config.PARADIGM_ANALYSIS_SAFETY_LIMIT
        )
        stats["planned_analysis_count"] = planned_count
        stats["analysis_safety_deferred_count"] = len(safety_deferred_origins)
        stats["current_window_origin_safety_deferred_count"] = sum(
            _origin_is_in_delivery_window(
                item, reference_time=research_now(),
                window_days=self.ordinary_discovery_lookback_days,
            )
            for item in safety_deferred_origins
        )

        new_candidate_keys: set[str] = set()
        service = await self._service_research_lanes(
            origins, batch.supporting, deep_deadline, effective_reserve,
            candidate_keys=new_candidate_keys, stats=stats,
        )
        stats["research_service"] = service["research_service"]
        if origins:
            (
                extractions,
                analyzed_origin_count,
                failed_origin_count,
                mechanism_slice_deferred_count,
                budget_deferred_origins,
                hydration_stats,
                completed_origins,
                resumable_checkpoint_origins,
            ) = service["origin_result"]
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
            stats["origin_prefilter_rejected_count"] = sum(
                item.rubric_assessment.get("version")
                == "origin-eligibility-v1"
                for item in extractions
            )
            stats["origin_full_review_count"] = (
                analyzed_origin_count
                - failed_origin_count
                - mechanism_slice_deferred_count
                - stats["origin_prefilter_rejected_count"]
            )
            if resumable_checkpoint_origins:
                run_audit.event(
                    "technical_report_mechanism_checkpoint",
                    "saved",
                    f"{len(resumable_checkpoint_origins)} 份 Technical Report "
                    "已逐原点原子保存候选与机制续跑进度",
                )
        else:
            new_candidates = []
            analyzed_origin_count = 0
            failed_origin_count = 0
            mechanism_slice_deferred_count = 0
            budget_deferred_origins = []
            completed_origins = []
            resumable_checkpoint_origins = []
            stats["candidate_extractions"] = 0
            stats["origin_prefilter_rejected_count"] = 0
            stats["origin_full_review_count"] = 0
            stats.update(_empty_hydration_stats())

        stats["analysis_count"] = analyzed_origin_count
        stats["analysis_failed_count"] = failed_origin_count
        stats["analysis_mechanism_slice_deferred_count"] = (
            mechanism_slice_deferred_count
        )
        stats["analysis_completed_count"] = len(completed_origins)
        stats["current_window_origin_completed_count"] = sum(
            _origin_is_in_delivery_window(
                item, reference_time=research_now(),
                window_days=self.ordinary_discovery_lookback_days,
            )
            for item in completed_origins
        )
        stats["current_window_origin_budget_deferred_count"] = sum(
            _origin_is_in_delivery_window(
                item, reference_time=research_now(),
                window_days=self.ordinary_discovery_lookback_days,
            )
            for item in budget_deferred_origins
        )
        run_audit.event(
            "current_window_origin_service",
            (
                "warning"
                if stats["current_window_origin_planned_count"]
                and not stats["current_window_origin_completed_count"]
                else "observed"
            ),
            (
                f"本期发布日期原点完成 "
                f"{stats['current_window_origin_completed_count']}/"
                f"{stats['current_window_origin_planned_count']}；"
                f"安全熔断延期 "
                f"{stats['current_window_origin_safety_deferred_count']}；"
                f"时间预算延期 "
                f"{stats['current_window_origin_budget_deferred_count']}"
            ),
        )
        stats["analysis_budget_deferred_count"] = len(budget_deferred_origins)
        stats["analysis_deferred_count"] = max(planned_count - len(completed_origins), 0)
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

        stats["pending_deep_backlog_loaded"] = service["pending_deep_backlog_loaded"]
        deep_pool = service["deep_pool"]
        stats["current_window_deep_planned_count"] = sum(
            _candidate_has_current_primary(
                item, reference_time=research_now(),
                window_days=self.ordinary_discovery_lookback_days,
            )
            for item in deep_pool
        )
        deep_candidates = service["deep_candidates"]
        safety_deferred_candidates = service["safety_deferred_candidates"]
        stats["planned_deep_candidate_count"] = len(deep_pool)
        stats["refresh_reserved_seconds"] = 0
        budget_deferred_candidates = service["budget_deferred_candidates"]
        execution_deferred_candidates = service["execution_deferred_candidates"]
        input_deferred_candidates = service["input_deferred_candidates"]
        deferred_candidates = [
            *safety_deferred_candidates,
            *budget_deferred_candidates,
            *execution_deferred_candidates,
            *input_deferred_candidates,
        ]
        for candidate in input_deferred_candidates:
            _record_deferred_candidate(
                candidate, "候选的一手来源版本尚未与上游检查点对齐；等待原点更新，"
                "保留 pending_deep，不发起过期输入的付费深挖",
            )
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
        stats["current_window_deep_stage_returned_count"] = sum(
            _candidate_has_current_primary(
                item, reference_time=research_now(),
                window_days=self.ordinary_discovery_lookback_days,
            )
            for item in deep_candidates
        )
        stats["candidate_safety_deferred_count"] = len(
            safety_deferred_candidates
        )
        stats["candidate_budget_deferred_count"] = len(
            budget_deferred_candidates
        )
        stats["candidate_execution_deferred_count"] = len(
            execution_deferred_candidates
        )
        stats["candidate_input_deferred_count"] = len(input_deferred_candidates)
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

        refresh_safety_deferred = service["refresh_safety_deferred"]
        (
            refreshed,
            refresh_unchanged,
            refresh_budget_deferred,
            refresh_execution_deferred,
            refresh_attempted,
        ) = service["refresh_result"]
        stats["refresh_analysis_count"] = refresh_attempted
        stats["refresh_input_redirected_count"] = service.get("refresh_input_redirected_count", 0)
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
        # Withholding an incomplete run must not lose already closed routes.
        # An unchanged, unreported historical snapshot is still eligible for
        # current policy/freshness checks; prepare_report handles deduplication.
        candidates = [
            *new_candidates, *refreshed,
            *(item for item in refresh_unchanged if is_reportable(item)),
        ]
        current_keys = {item.key for item in candidates}
        recovered = self.store.load_candidate_snapshots(
            self.store.campaigns.candidate_keys(self.campaign.campaign_id) - current_keys
        )
        recovered = [item for item in recovered if item.status != "pending_deep" and is_reportable(item)]
        candidates.extend(recovered)
        stats["completed_outputs_recovered"] = len(recovered)
        # Tavily/Reddit 的用户正文只供本轮综合与人物核验，之后即清除；
        # 数据库和邮件只保留链接、指标、覆盖状态和已提炼的分析。
        candidates = self.enricher.finalize(candidates)
        stats["verified_secondary_discussion_count"] = sum(
            is_verified_substantive_discussion(
                evidence, route_key=candidate.key
            )
            for candidate in candidates
            for evidence in candidate.evidence
        )
        stats["unverified_secondary_lead_count"] = sum(
            evidence.evidence_type in {
                EvidenceType.COMMUNITY_DISCUSSION,
                EvidenceType.SECONDARY_INTERPRETATION,
            }
            and not is_verified_substantive_discussion(
                evidence, route_key=candidate.key
            )
            for candidate in candidates
            for evidence in candidate.evidence
        )
        stats["unverified_implementation_lead_count"] = sum(
            evidence.evidence_type == EvidenceType.IMPLEMENTATION
            and evidence.raw.get("independence")
            not in {"independent", "official", "publisher"}
            for candidate in candidates
            for evidence in candidate.evidence
        )
        run_audit.event(
            "route_uptake_validation", "observed",
            (
                f"本轮已深挖/刷新路线核验实质二次讨论 "
                f"{stats['verified_secondary_discussion_count']} 条；"
                f"未核验社区线索 {stats['unverified_secondary_lead_count']} 条；"
                f"未核验实现线索 {stats['unverified_implementation_lead_count']} 条"
            ),
        )
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

        stats["current_window_deep_completed_count"] = sum(
            candidate.status != "pending_deep"
            and _candidate_has_current_primary(
                candidate, reference_time=research_now(),
                window_days=self.ordinary_discovery_lookback_days,
            )
            for candidate in new_candidates
        )
        run_audit.event(
            "current_window_deep_service",
            (
                "warning"
                if stats["current_window_deep_planned_count"]
                and not stats["current_window_deep_completed_count"]
                else "observed"
            ),
            (
                f"本期路线最终 Rubric 闭合 "
                f"{stats['current_window_deep_completed_count']}/"
                f"{stats['current_window_deep_planned_count']}；"
                f"深挖阶段返回 "
                f"{stats['current_window_deep_stage_returned_count']}；"
                f"安全/预算/执行延期合计 "
                f"{stats['candidate_deferred_count']}"
            ),
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
        supporting_checkpoint = self.store.mark_evidence(
            batch.supporting,
            analyzed=False,
        )
        candidate_evidence_checkpoint = self.store.mark_evidence(
            [evidence for candidate in candidates for evidence in candidate.evidence],
            analyzed=False,
            enrichment_only=True,
        )
        for result in (supporting_checkpoint, candidate_evidence_checkpoint):
            stats["evidence_checkpoint_rejected_count"] += result.rejected_count
            for source, count in result.rejection_sources.items():
                checkpoint_rejection_sources[source] = (
                    checkpoint_rejection_sources.get(source, 0) + count
                )
        stale_writes = sum(
            result.stale_revision_count
            for result in (supporting_checkpoint, candidate_evidence_checkpoint)
        )
        stats["evidence_checkpoint_stale_revision_count"] = stale_writes
        if stale_writes:
            run_audit.event(
                "evidence_checkpoint_revision", "skipped",
                f"拒绝 {stale_writes} 条无当前来源版本写入权限的增强快照；保留现有原点与进度",
            )
        if stats["evidence_checkpoint_rejected_count"]:
            stats["evidence_checkpoint_rejection_sources"] = (
                checkpoint_rejection_sources
            )
            run_audit.event(
                "evidence_checkpoint_contract",
                "warning",
                f"本轮共隔离 {stats['evidence_checkpoint_rejected_count']} 条不满足"
                f"持久化契约的记录；来源 {checkpoint_rejection_sources}",
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
            or not self.store.candidate_inputs_current(candidate)
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
                "安全的一手论文/官方材料 URL 或当前来源版本；保留 pending_deep，不发送"
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
                # 高信号窗口只负责索引晚到/历史漏召回；交付新鲜度必须使用
                # 普通周报窗口，否则 60 天补扫会被误写成本期新发布。
                window_days=self.ordinary_discovery_lookback_days,
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

        stats["coverage_incomplete"] = bool(
            stats["recall_coverage_incomplete"]
        )
        stats["research_incomplete"] = bool(
            stats["evidence_checkpoint_rejected_count"]
            or stats["analysis_deferred_count"]
            or stats["candidate_deferred_count"]
            or stats["candidate_research_incomplete_count"]
            or stats["refresh_deferred_count"]
            or stats["delivery_profile_deferred_count"]
            or stats["delivery_source_deferred_count"]
            or stats["report_safety_deferred_count"]
        )
        # 保留兼容统计，但未闭合研究/覆盖均不得进入报告或邮件交付。
        stats["run_incomplete"] = stats["research_incomplete"]
        stats["pending_work_count"] = (
            stats["evidence_checkpoint_rejected_count"]
            + stats["analysis_deferred_count"]
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
            "research_blocked"
            if stats["research_incomplete"] or stats["coverage_incomplete"]
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
        queue_after = self.store.work_queue_snapshot(reference_time=research_now())
        stats["work_queue_after"] = queue_after
        stats["pending_queue_net_change"] = (
            queue_after["pending_total_count"] - queue_before["pending_total_count"]
        )
        stats["oldest_pending_age_days"] = queue_after["oldest_pending_age_days"]
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
        stats.update(run_audit.token_totals())
        ledger = self.store.campaigns.reconcile(self.campaign.campaign_id)
        stats["research_campaign"] = ledger
        stats["research_incomplete"] = bool(stats["research_incomplete"] or ledger["pending_total_count"] or queue_after["pending_total_count"])
        stats["run_incomplete"] = stats["research_incomplete"]
        stats["pending_work_count"] = max(stats["pending_work_count"], ledger["pending_total_count"], queue_after["pending_total_count"])
        stats["result_kind"] = "research_blocked" if stats["research_incomplete"] or stats["coverage_incomplete"] else "complete_memo" if reportable else "complete_no_signal"
        from paradigms.completion import research_completion_violations
        ready = not research_completion_violations(stats)
        self.store.campaigns.save_stats(self.campaign.campaign_id, stats, ready=ready)
        stats["research_campaign"] = self.store.campaigns.snapshot(self.campaign.campaign_id)
        run_audit.checkpoint(stats)
        logger.info(
            "范式研究阶段完成：原始材料=%s，范式候选=%s，待交付=%s",
            stats["origin_count"],
            len(candidates),
            len(reportable),
        )
        logger.info(
            "研究闭合账本：原点完成=%s/%s，深挖返回=%s/%s，"
            "待办=%s，持久化待办=%s，研究未闭合=%s，覆盖未闭合=%s",
            stats["analysis_completed_count"], stats["planned_analysis_count"],
            stats["deep_candidate_count"], stats["planned_deep_candidate_count"],
            stats["pending_work_count"], queue_after["pending_total_count"],
            stats["research_incomplete"], stats["coverage_incomplete"],
        )
        return stats

    async def _service_research_lanes(
        self, origins: list, supporting: list, deadline: float,
        reserve: int, *, candidate_keys: set[str], stats: dict, clock=None,
    ) -> dict:
        """Advance bounded visits across durable origin/deep/refresh queues.

        Every origin visit commits before the next candidate snapshot is read.
        A subsequent origin for an already visited route invalidates that old
        in-memory outcome and queues the newly committed input for research.
        """
        now = research_now()
        if hasattr(self.analyzer, "_eligibility_prefetch_failures"):
            self.analyzer._eligibility_prefetch_failures.clear()
        window = self.ordinary_discovery_lookback_days
        lane_for = lambda item: (
            "current" if _candidate_has_current_primary(
                item, reference_time=now, window_days=window
            ) else "backfill"
        )
        origin_queues = {name: deque() for name in ("current", "backfill")}
        for item in origins:
            lane = "current" if _origin_is_in_delivery_window(
                item, reference_time=now, window_days=window
            ) else "backfill"
            origin_queues[lane].append(item)
        initial_deep = self.store.load_pending_deep_candidates()
        deep_tasks = {item.key: item for item in initial_deep}
        deep_plan = dict(deep_tasks)
        deep_queues = {name: deque() for name in ("current", "backfill")}
        deep_lanes = {}
        for item in _deep_execution_order(
            initial_deep, [], reference_time=now, window_days=window
        ):
            lane = lane_for(item)
            deep_lanes[item.key] = lane
            deep_queues[lane].append(item.key)
        historical = self.store.load_refresh_candidates(
            exclude_keys=set(deep_tasks), limit=0
        )
        campaign = getattr(self, "campaign", None)
        campaign_key = campaign.campaign_id if campaign else None
        if campaign_key:
            pending_refresh = self.store.campaigns.seal_refresh_scope(campaign_key, historical)
            historical = [item for item in historical if item.key in pending_refresh]
            self.store.campaigns.reconcile(campaign_key)
        historical, refresh_safety = _apply_safety_limit(
            historical, config.PARADIGM_REFRESH_SAFETY_LIMIT
        )
        refresh_queue = deque(historical)
        refresh_keys = {item.key for item in historical}
        monotonic = clock or time.monotonic
        scheduler = ResearchLaneScheduler(
            deadline, deep_reserve_seconds=reserve, clock=monotonic
        )
        history_index = self.store.build_route_history_index()
        previous_stage = dict.fromkeys(origin_queues, "origin")
        extractions, completed_origins, partial_origins, origin_budget = [], [], [], []
        analyzed = failed = partial = 0
        hydration = _empty_hydration_stats()
        completed_deep, deep_budget, deep_failed, deep_blocked = {}, {}, {}, {}
        refreshed, unchanged, refresh_failed = {}, {}, {}
        refresh_budget = []
        refresh_attempted = 0
        refresh_input_redirected = set()
        visited_deep_keys = set()
        visited_origins = set()
        origin_completed, origin_partial, origin_failed, origin_budget_by_key = {}, {}, set(), {}

        def start_attempts(kind, group):
            if not campaign_key:
                return {}
            from database.paradigm_store import _deep_checkpoint_input_signature, _persistable_candidate
            objects = [(item.fingerprint, item.source_revision) for item in group] if kind == "origin" else [(item.key, _deep_checkpoint_input_signature(item), _persistable_candidate(item).to_dict()) for item in group]
            attempts = self.store.campaigns.start_attempt(campaign_key, kind, objects)
            return dict(zip((entry[0] for entry in objects), attempts))

        def finish_attempts(attempts, done, budget, failures, *, kind):
            if not campaign_key:
                return
            identity = (lambda item: item.fingerprint) if kind == "origin" else (lambda item: item.key)
            done_ids, budget_ids, failed_ids = ({identity(item) for item in values} for values in (done, budget, failures))
            for key, attempt in attempts.items():
                outcome = "returned" if key in done_ids else "budget_deferred" if key in budget_ids else "execution_failed" if key in failed_ids else "dependency_pending"
                self.store.campaigns.finish_attempts([attempt], outcome=outcome)

        def clean_deep_queue(lane):
            queue = deep_queues[lane]
            while queue and (
                queue[0] not in deep_tasks or deep_lanes.get(queue[0]) != lane
            ):
                queue.popleft()

        def active_lanes():
            active = set()
            for lane in origin_queues:
                clean_deep_queue(lane)
                if origin_queues[lane] or deep_queues[lane]:
                    active.add(lane)
            while refresh_queue and refresh_queue[0].key not in refresh_keys:
                refresh_queue.popleft()
            if refresh_queue:
                active.add("updates")
            return active

        def queue_committed(keys):
            for item in self.store.load_candidate_snapshots(keys):
                if item.status != "pending_deep":
                    continue
                key = item.key
                # A new committed input supersedes any earlier outcome from
                # this run. Never write that earlier result back at closeout.
                completed_deep.pop(key, None)
                deep_budget.pop(key, None)
                deep_failed.pop(key, None)
                deep_blocked.pop(key, None)
                refreshed.pop(key, None)
                unchanged.pop(key, None)
                refresh_failed.pop(key, None)
                refresh_keys.discard(key)
                lane = lane_for(item)
                if key not in deep_tasks or deep_lanes.get(key) != lane:
                    deep_queues[lane].append(key)
                deep_tasks[key] = item
                deep_plan[key] = item
                deep_lanes[key] = lane

        while True:
            active = active_lanes()
            lane = scheduler.choose(active)
            if lane is None:
                break
            stage = "refresh" if lane == "updates" else (
                "deep" if deep_queues[lane] and (
                    not origin_queues[lane] or previous_stage[lane] == "origin"
                ) else "origin"
            )
            visit_deadline = scheduler.visit_deadline(
                lane, active, stage=stage
            )
            visit_started = monotonic()
            if stage == "origin":
                # Eligibility batching is independent of the small full-review
                # visit. Interleaved protected origins must not force every
                # ordinary qualification request down to ~4-6 records.
                prefetch = getattr(self.analyzer, "prefetch_origin_eligibility", None)
                if (getattr(self.analyzer, "enable_batch_prefilter", False)
                        and getattr(self.analyzer, "stage_cache", None) is not None
                        and callable(prefetch)):
                    lookahead = list(islice(origin_queues[lane], config.PARADIGM_ORIGIN_PREFILTER_BATCH_SIZE * 2))
                    needs_eligibility = lambda item: _should_prefilter_origin(item) and item.raw.get("origin_eligibility_decision") not in {"full_review", "screen_out"}
                    ordinary = ([item for item in lookahead if needs_eligibility(item)][:config.PARADIGM_ORIGIN_PREFILTER_BATCH_SIZE]
                                if any(needs_eligibility(item) for item in lookahead[:config.PARADIGM_ANALYSIS_BATCH_SIZE]) else [])
                    if ordinary:
                        try:
                            decisions = await asyncio.wait_for(prefetch(ordinary), timeout=_remaining_seconds(visit_deadline))
                            for item in ordinary:
                                if item.fingerprint in decisions:
                                    item.raw["origin_eligibility_decision"], item.raw["origin_eligibility_reason"] = decisions[item.fingerprint]
                        except asyncio.TimeoutError:
                            # No completion receipt is inferred; healthy cached
                            # decisions survive, all origins remain in the queue.
                            scheduler.account(lane, visit_started, stage=stage)
                            continue
                batch_size = config.PARADIGM_ANALYSIS_BATCH_SIZE
                if getattr(self.analyzer, "enable_batch_prefilter", False) and all(
                    _should_prefilter_origin(item) for item in list(origin_queues[lane])[:config.PARADIGM_ORIGIN_PREFILTER_BATCH_SIZE]
                ):
                    batch_size = config.PARADIGM_ORIGIN_PREFILTER_BATCH_SIZE
                group = [
                    origin_queues[lane].popleft()
                    for _ in range(min(
                        len(origin_queues[lane]), batch_size
                    ))
                ]
                committed_keys = set()
                before_progress = {item.fingerprint: len(item.raw.get("technical_report_completed_mechanisms", {})) for item in group}
                attempts = start_attempts("origin", group)
                result = await self._analyze_origins_in_batches(
                    group, visit_deadline, candidate_keys=committed_keys,
                    history_index=history_index,
                )
                values, count, failures, slices, budget, hydrated, done, resumable = result
                done_ids = {item.fingerprint for item in [*done, *resumable, *budget]}
                finish_attempts(attempts, done, budget, [item for item in group if item.fingerprint not in done_ids], kind="origin")
                extractions.extend(values)
                visited_origins.update(item.fingerprint for item in group if item.fingerprint not in {value.fingerprint for value in budget})
                for item in done:
                    origin_completed[item.fingerprint] = item
                    origin_partial.pop(item.fingerprint, None)
                    origin_failed.discard(item.fingerprint)
                    origin_budget_by_key.pop(item.fingerprint, None)
                for item in resumable:
                    origin_partial[item.fingerprint] = item
                    if item.raw.get("technical_report_last_run_failure"):
                        origin_failed.add(item.fingerprint)
                    else:
                        origin_failed.discard(item.fingerprint)
                    # A mechanism slice is a fair visit, not a forced new run.
                    # Requeue only demonstrated progress; failed/no-progress
                    # mechanisms stay durable without spinning this process.
                    if not item.raw.get("technical_report_last_run_failure") and len(item.raw.get("technical_report_completed_mechanisms", {})) > before_progress.get(item.fingerprint, 0):
                        snapshots = self.store.load_origin_snapshots([item.fingerprint])
                        if snapshots:
                            origin_queues[lane].append(snapshots[0])
                origin_failed.update(item.fingerprint for item in group if item.fingerprint not in {value.fingerprint for value in [*done, *resumable, *budget]})
                origin_budget_by_key.update((item.fingerprint, item) for item in budget)
                analyzed, failed = len(visited_origins), len(origin_failed)
                partial = len(set(origin_partial) - origin_failed - set(origin_budget_by_key))
                completed_origins = list(origin_completed.values())
                partial_origins = list(origin_partial.values())
                origin_budget = list(origin_budget_by_key.values())
                for key, value in hydrated.items():
                    hydration[key] += value
                candidate_keys.update(committed_keys)
                done_fingerprints = {item.fingerprint for item in done}
                # A revised origin may now be rejected and create no route.
                # Wake old dependencies anyway; the next deep visit rebases
                # only fully analyzed revisions and revalidates that hypothesis.
                unblocked = {
                    key for key, item in deep_blocked.items()
                    if any(evidence.fingerprint in done_fingerprints
                           for evidence in item.evidence)
                }
                queue_committed(committed_keys | unblocked)
                previous_stage[lane] = "origin"
            elif stage == "deep":
                group = []
                examined = 0
                while deep_queues[lane] and examined < max(config.PARADIGM_DEEP_BATCH_SIZE, getattr(self, "deep_concurrency", 1)):
                    examined += 1
                    key = deep_queues[lane].popleft()
                    if key not in deep_tasks or deep_lanes.get(key) != lane:
                        continue
                    if not self.store.candidate_inputs_current(deep_tasks[key]):
                        rebased = self.store.rebase_analyzed_candidate_inputs(key)
                        if rebased is None:
                            snapshot = _deferred_deep_snapshot(self.store, deep_tasks.pop(key), supporting)
                            deep_blocked[key] = snapshot
                            deep_lanes.pop(key, None)
                            self.store.save_candidates([snapshot])
                            continue
                        deep_tasks[key] = rebased
                        deep_plan[key] = rebased
                        history_index.upsert(rebased)
                    limit = config.PARADIGM_DEEP_SAFETY_LIMIT
                    if limit > 0 and key not in visited_deep_keys and len(visited_deep_keys) >= limit:
                        # Remove only from this process's runnable queue. The
                        # durable snapshot and final safety ledger retain it.
                        deep_lanes.pop(key, None)
                        continue
                    visited_deep_keys.add(key)
                    group.append(deep_tasks.pop(key))
                    deep_lanes.pop(key, None)
                if group:
                    attempts = start_attempts("deep", group)
                    done, budget, failures = await self._deep_analyze_in_batches(
                        group, supporting, visit_deadline
                    )
                    completed_deep.update((item.key, item) for item in done)
                    deep_budget.update((item.key, item) for item in budget)
                    deep_failed.update((item.key, item) for item in failures)
                    # Paid substage results and failure/support snapshots are
                    # durable before the next lane runs or the process ends.
                    self.store.save_candidates([*done, *budget, *failures])
                    finish_attempts(attempts, done, budget, failures, kind="deep")
                    for item in [*done, *budget, *failures]:
                        history_index.upsert(item)
                previous_stage[lane] = "deep"
            else:
                group = []
                while refresh_queue and len(group) < max(config.PARADIGM_DEEP_BATCH_SIZE, getattr(self, "deep_concurrency", 1)):
                    item = refresh_queue.popleft()
                    if item.key in refresh_keys:
                        refresh_keys.remove(item.key)
                        if not self.store.candidate_inputs_current(item):
                            snapshot = copy.deepcopy(item)
                            snapshot.status = "pending_deep"
                            # Downstream executable snapshot BEFORE the parent
                            # is redirected; the refresh manifest stays pending.
                            with self.store.transaction():
                                self.store.save_candidates([snapshot])
                                if campaign_key:
                                    from database.paradigm_store import _deep_checkpoint_input_signature
                                    self.store.campaigns.include(campaign_key, "deep", [(snapshot.key, _deep_checkpoint_input_signature(snapshot), snapshot.to_dict())])
                            queue_committed({snapshot.key})
                            refresh_input_redirected.add(snapshot.key)
                            run_audit.event("refresh_input_dependency", "deferred", f"{snapshot.key[:80]}：一手来源换版，保留刷新父任务并转入可恢复深挖；未调用过期输入的社区/模型接口")
                            continue
                        group.append(item)
                if group:
                    attempts = start_attempts("refresh", group)
                    values, stable, budget, failures, count = await self._refresh_in_batches(
                        group, supporting, visit_deadline
                    )
                    refreshed.update((item.key, item) for item in values)
                    unchanged.update((item.key, item) for item in stable)
                    refresh_failed.update((item.key, item) for item in failures)
                    refresh_budget.extend(budget)
                    refresh_attempted += count
                    for item in values:
                        score_candidate(item)
                    saved = EvidenceEnricher.finalize(copy.deepcopy([
                        *values, *stable, *failures
                    ]))
                    with self.store.transaction():
                        self.store.save_candidates(saved)
                        if campaign_key:
                            self.store.campaigns.complete_refresh(campaign_key, [item.key for item in [*values, *stable]])
                            finish_attempts(attempts, [*values, *stable], budget, failures, kind="refresh")
                    for item in saved:
                        history_index.upsert(item)
            scheduler.account(lane, visit_started, stage=stage)
            run_audit.checkpoint({
                **stats,
                "research_service": scheduler.snapshot(),
                "analysis_count": analyzed,
                "analysis_completed_count": len(completed_origins),
                "analysis_deferred_count": failed + partial + len(origin_budget)
                + sum(len(queue) for queue in origin_queues.values()),
                "deep_candidate_count": len(completed_deep),
                "candidate_deferred_count": len(deep_tasks)
                + len(deep_budget) + len(deep_failed) + len(deep_blocked),
                "candidate_input_deferred_count": len(deep_blocked),
                "refresh_analysis_count": refresh_attempted,
            })

        for queue in origin_queues.values():
            origin_budget_by_key.update((item.fingerprint, item) for item in queue)
        origin_budget = list(origin_budget_by_key.values())
        partial = len(set(origin_partial) - origin_failed - set(origin_budget_by_key))
        safety_deep = []
        for key, item in deep_tasks.items():
            snapshot = _deferred_deep_snapshot(self.store, item, supporting)
            if config.PARADIGM_DEEP_SAFETY_LIMIT > 0 and key not in visited_deep_keys and len(visited_deep_keys) >= config.PARADIGM_DEEP_SAFETY_LIMIT:
                safety_deep.append(snapshot)
            else:
                deep_budget[key] = snapshot
        refresh_budget.extend(item for item in refresh_queue if item.key in refresh_keys)
        service = scheduler.snapshot()
        # Temporary partial-visit placeholders are not final origin verdicts.
        # Once their parent closes, keep only the actual mechanism assessments.
        extractions = [value for value in extractions if not (
            value.evidence.fingerprint in origin_completed
            and not value.canonical_name and not value.rubric_assessment
            and bool(value.rejection_reason)
        )]
        run_audit.event(
            "research_service_lanes", "observed",
            f"本期/更新/补课按实际耗时轮转：{service['seconds']}；"
            f"阶段访问次数 {service['operations']}；未访问对象保留原检查点",
        )
        return {
            "origin_result": (extractions, analyzed, failed, partial, origin_budget,
                              hydration, completed_origins, partial_origins),
            "pending_deep_backlog_loaded": len(initial_deep),
            "deep_pool": list(deep_plan.values()),
            "deep_candidates": list(completed_deep.values()),
            "safety_deferred_candidates": safety_deep,
            "budget_deferred_candidates": list(deep_budget.values()),
            "execution_deferred_candidates": list(deep_failed.values()),
            "input_deferred_candidates": list(deep_blocked.values()),
            "refresh_result": (list(refreshed.values()), list(unchanged.values()),
                               [item for item in refresh_budget if item.key not in deep_plan],
                               list(refresh_failed.values()), refresh_attempted),
            "refresh_safety_deferred": [item for item in refresh_safety if item.key not in deep_plan],
            "refresh_input_redirected_count": len(refresh_input_redirected),
            "research_service": service,
        }

    async def _analyze_origins_in_batches(
        self,
        origins: list,
        deadline: float,
        *,
        candidate_keys: set[str] | None = None,
        history_index=None,
    ) -> tuple[list, int, int, int, list, dict[str, int], list, list]:
        """逐原点接收并提交；取消只影响尚未确认提交的工作。"""
        candidate_keys = candidate_keys if candidate_keys is not None else set()
        index_builder = getattr(self.store, "build_route_history_index", None)
        if history_index is None and callable(index_builder):
            history_index = index_builder()
        input_snapshots = {item.fingerprint: copy.deepcopy(item) for item in origins}
        extractions = []
        analyzed_count = 0
        failed_count = 0
        mechanism_slice_deferred_count = 0
        hydration_totals = _empty_hydration_stats()
        completed_origins = []
        resumable_checkpoint_origins = []
        budget_deferred = []
        batch_size = config.PARADIGM_ANALYSIS_BATCH_SIZE
        if getattr(self.analyzer, "enable_batch_prefilter", False) and all(_should_prefilter_origin(item) for item in origins):
            batch_size = config.PARADIGM_ORIGIN_PREFILTER_BATCH_SIZE

        def committed_mechanisms(item):
            pairs = set()
            receipts = item.raw.get("technical_report_committed_candidates", {})
            payloads = item.raw.get("technical_report_completed_mechanisms", {})
            for key, keys in receipts.items():
                snapshots = self.store.load_candidate_snapshots(keys)
                if len(snapshots) != len(keys) or any(
                    not self.store.candidate_inputs_current(candidate) or not any(
                        evidence.fingerprint == item.fingerprint and evidence.source_revision == item.source_revision
                        for evidence in candidate.evidence
                    ) for candidate in snapshots
                ):
                    continue
                payload = payloads.get(key, {})
                if payload:
                    pairs.add((payload.get("canonical_name"), payload.get("mechanism")))
            return pairs

        def save_report_progress(item, extraction, mechanism_key):
            values = [extraction] if extraction is not None else []
            candidates = cluster_extractions(values)
            with self.store.transaction():
                candidates = self.store.attach_history(candidates, history_index=history_index) if history_index is not None else self.store.attach_history(candidates)
                if mechanism_key:
                    item.raw.setdefault("technical_report_committed_candidates", {})[mechanism_key] = [candidate.key for candidate in candidates]
                _commit_origin_analysis_checkpoint(self.store, candidates, [], [item])
            candidate_keys.update(candidate.key for candidate in candidates)
            if history_index is not None:
                for candidate in candidates:
                    history_index.upsert(candidate)

        def record_failed(items: list, reason: str) -> None:
            nonlocal analyzed_count, failed_count, mechanism_slice_deferred_count
            now = datetime.now(timezone.utc).isoformat()
            for item in items:
                item.raw["analysis_failure_count"] = (
                    _safe_int(item.raw.get("analysis_failure_count", 0)) + 1
                )
                item.raw["last_analysis_failure_at"] = now
                # Never persist a failed candidate's new mechanism checkpoint:
                # its downstream snapshot was not committed. Keep prior progress.
                retry = copy.deepcopy(input_snapshots[item.fingerprint])
                retry.raw["analysis_failure_count"] = item.raw["analysis_failure_count"]
                retry.raw["last_analysis_failure_at"] = now
                self.store.mark_evidence([retry], analyzed=False)
            analyzed_count += len(items)
            failed_count += len(items)
            run_audit.event(
                "origin_analysis_item",
                "deferred",
                f"{len(items)} 条原点因 {reason} 保留 pending；未写成技术淘汰",
            )

        def commit_result(item, values) -> None:
            nonlocal analyzed_count, failed_count, mechanism_slice_deferred_count
            try:
                returned, unknown = _validated_origin_stage_output([item], values)
                if unknown:
                    run_audit.event("origin_analysis_contract", "warning",
                                    f"丢弃 {unknown} 条不属于当前原点的输出")
                if not returned:
                    raise _StageOutputContractError("当前原点没有可归因输出")
                if any(
                    value.evidence.source_revision != item.source_revision
                    for value in returned
                ):
                    raise _StageOutputContractError("抽取结果与输入来源版本不一致")
                for value in returned:
                    placeholder = (
                        not value.canonical_name
                        and not value.rubric_assessment
                        and bool(value.rejection_reason)
                    )
                    if placeholder:
                        continue
                    decision = value.rubric_assessment.get("decision")
                    if decision not in {"deep_dive", "observe", "reject"}:
                        raise _StageOutputContractError("原点 Rubric 未闭合")
                    if decision == "deep_dive" and initial_gate_reason(value):
                        raise _StageOutputContractError("进入深挖的机制结构不完整")
                partial = bool(item.raw.get("technical_report_slice_pending"))
                failed = any(
                    not value.canonical_name and not value.rubric_assessment
                    and bool(value.rejection_reason)
                    and (not partial or item.raw.get("technical_report_last_run_failure"))
                    for value in returned
                )
                if failed and not partial:
                    record_failed([item], "analysis_failed")
                    return
                if failed:
                    item.raw["analysis_failure_count"] = (
                        _safe_int(item.raw.get("analysis_failure_count", 0)) + 1
                    )
                    item.raw["last_analysis_failure_at"] = (
                        datetime.now(timezone.utc).isoformat()
                    )
                already_committed = committed_mechanisms(item)
                candidates = cluster_extractions([
                    value for value in returned if (value.canonical_name, value.mechanism) not in already_committed
                ])
                transaction = getattr(self.store, "transaction", None)
                with transaction() if callable(transaction) else nullcontext():
                    if history_index is None:
                        candidates = self.store.attach_history(candidates)
                    else:
                        candidates = self.store.attach_history(
                            candidates, history_index=history_index
                        )
                    _commit_origin_analysis_checkpoint(
                        self.store, candidates,
                        [] if partial else [item], [item] if partial else [],
                    )
            except (sqlite3.DatabaseError, OSError):
                # Storage-wide failures are not a bad research item. Stop without
                # replaying paid calls; earlier independent commits remain durable.
                raise
            except Exception as exc:
                logger.exception("单条原点检查点未闭合；保留旧快照并继续健康同批材料")
                record_failed([item], type(exc).__name__)
                return
            if history_index is not None:
                for candidate in candidates:
                    history_index.upsert(candidate)
            candidate_keys.update(candidate.key for candidate in candidates)
            extractions.extend(returned)
            analyzed_count += 1
            if partial:
                resumable_checkpoint_origins.append(item)
                if failed:
                    failed_count += 1
                else:
                    mechanism_slice_deferred_count += 1
            else:
                completed_origins.append(item)

        async def analyze_group(group: list) -> None:
            """Split only unexpected batch failures; accept valid peer outputs."""

            nonlocal analyzed_count, failed_count, mechanism_slice_deferred_count
            remaining = _remaining_seconds(deadline)
            if remaining <= 0:
                budget_deferred.extend(group)
                return
            processed = set()
            expected = {item.fingerprint: item for item in group}

            async def consume():
                stream = getattr(self.analyzer, "iter_results", None)
                if callable(stream):
                    callback = getattr(self.analyzer, "checkpoint_callback", None)
                    if isinstance(self.analyzer, ParadigmAnalyzer):
                        self.analyzer.checkpoint_callback = save_report_progress
                    try:
                        async with aclosing(stream(group)) as results:
                            async for origin, values in results:
                                key = origin.fingerprint
                                if key not in expected or key in processed:
                                    run_audit.event("origin_analysis_contract", "warning", "忽略外来或重复的原点结果包")
                                    continue
                                commit_result(expected[key], values)
                                processed.add(key)
                    finally:
                        if isinstance(self.analyzer, ParadigmAnalyzer):
                            self.analyzer.checkpoint_callback = callback
                else:
                    # Compatibility for non-streaming local/test analyzers.
                    values = await self.analyzer.run(group)
                    returned, unknown = _validated_origin_stage_output(group, values)
                    if unknown:
                        run_audit.event(
                            "origin_analysis_contract", "warning",
                            f"丢弃 {unknown} 条外来抽取输出",
                        )
                    for item in group:
                        commit_result(item, [
                            value for value in returned
                            if value.evidence.fingerprint == item.fingerprint
                        ])
                        processed.add(item.fingerprint)

            try:
                await asyncio.wait_for(consume(), timeout=remaining)
            except asyncio.TimeoutError:
                budget_deferred.extend(
                    item for item in group if item.fingerprint not in processed
                )
                return
            except (sqlite3.DatabaseError, OSError):
                raise
            except Exception as exc:
                unfinished = [item for item in group if item.fingerprint not in processed]
                if len(unfinished) > 1:
                    midpoint = len(unfinished) // 2
                    run_audit.event(
                        "origin_analysis_batch",
                        "isolating",
                        f"{len(group)} 条原点批次发生 {type(exc).__name__}；"
                        "二分隔离坏样本，健康同批材料继续",
                    )
                    await analyze_group(unfinished[:midpoint])
                    await analyze_group(unfinished[midpoint:])
                    return
                logger.exception("单条机制抽取异常；原点保留 pending")
                record_failed(unfinished, type(exc).__name__)
                return

            missing = [item for item in group if item.fingerprint not in processed]
            if missing:
                record_failed(missing, "stream_output_missing")

        for offset in range(0, len(origins), batch_size):
            remaining = _remaining_seconds(deadline)
            if remaining <= 0:
                return (
                    extractions,
                    analyzed_count,
                    failed_count,
                    mechanism_slice_deferred_count,
                    origins[offset:],
                    hydration_totals,
                    completed_origins,
                    resumable_checkpoint_origins,
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
                    mechanism_slice_deferred_count,
                    origins[offset:],
                    hydration_totals,
                    completed_origins,
                    resumable_checkpoint_origins,
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
                    mechanism_slice_deferred_count,
                    budget_deferred,
                    hydration_totals,
                    completed_origins,
                    resumable_checkpoint_origins,
                )
        return (
            extractions,
            analyzed_count,
            failed_count,
            mechanism_slice_deferred_count,
            [],
            hydration_totals,
            completed_origins,
            resumable_checkpoint_origins,
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
        concurrency = getattr(self, "deep_concurrency", 1)
        batch_size = max(config.PARADIGM_DEEP_BATCH_SIZE, concurrency)

        def resumable(candidate):
            return _resumable_deep_snapshot(
                getattr(self, "store", None), candidate, supporting
            )

        def deferred_snapshot(candidate):
            return _deferred_deep_snapshot(
                getattr(self, "store", None), candidate, supporting
            )

        async def process_group(group: list) -> None:
            remaining = _remaining_seconds(deadline)
            if remaining <= 0:
                budget_deferred.extend(deferred_snapshot(item) for item in group)
                return

            async def process_batch():
                cached = {item.key: resumable(item) for item in group}
                fresh_originals = [item for item in group if cached[item.key] is None]
                fresh = copy.deepcopy(fresh_originals)
                for item in fresh:
                    item.deep_checkpoint_stage = ""
                    item.deep_checkpoint_input_signature = ""
                    item.deep_checkpoint_support_signature = ""
                    item.deep_checkpoint_created_at = ""
                    item.deep_checkpoint_trajectory_signature = ""
                    item.deep_checkpoint_synthesis_rubric = {}
                if fresh:
                    values = await self.enricher.run(fresh, supporting)
                    _validate_candidate_stage_output(
                        fresh, values, "external_enrichment"
                    )
                    values = await self.synthesizer.run(values)
                    _validate_candidate_stage_output(
                        fresh, values, "paradigm_synthesis"
                    )
                    saver = getattr(
                        getattr(self, "store", None), "save_synthesized_checkpoint", None
                    )
                    originals = {item.key: item for item in fresh_originals}
                    for candidate in values:
                        if (
                            saver is None
                            or candidate.rubric_assessment.get("decision")
                            not in {"report", "observe", "reject"}
                        ):
                            continue
                        snapshot = EvidenceEnricher.finalize([copy.deepcopy(candidate)])[0]
                        snapshot.deep_checkpoint_support_signature = (
                            EvidenceEnricher.supporting_signature(
                                originals[candidate.key], supporting
                            )
                        )
                        saver(originals[candidate.key], snapshot)
                        candidate.deep_checkpoint_stage = snapshot.deep_checkpoint_stage
                        candidate.deep_checkpoint_input_signature = (
                            snapshot.deep_checkpoint_input_signature
                        )
                        candidate.deep_checkpoint_support_signature = (
                            snapshot.deep_checkpoint_support_signature
                        )
                        candidate.deep_checkpoint_created_at = (
                            snapshot.deep_checkpoint_created_at
                        )
                        candidate.deep_checkpoint_trajectory_signature = ""
                        candidate.deep_checkpoint_synthesis_rubric = (
                            snapshot.deep_checkpoint_synthesis_rubric.copy()
                        )
                    cached.update({item.key: item for item in values})
                trajectory_inputs = [
                    cached[item.key] for item in group
                    if cached[item.key].deep_checkpoint_stage != "research_complete"
                ]
                before_trajectory = {
                    item.key: EvidenceEnricher.finalize([copy.deepcopy(item)])[0]
                    for item in trajectory_inputs
                    if item.deep_checkpoint_stage == "synthesized"
                }
                if trajectory_inputs:
                    trajectory_values = await self.trajectory.run(trajectory_inputs)
                    _validate_candidate_stage_output(
                        trajectory_inputs, trajectory_values, "researcher_trajectory"
                    )
                else:
                    trajectory_values = []
                completed_saver = getattr(
                    getattr(self, "store", None), "save_completed_deep_checkpoint", None
                )
                for candidate in trajectory_values:
                    previous = before_trajectory.get(candidate.key)
                    if previous is None or completed_saver is None:
                        continue
                    if candidate.rubric_assessment.get("decision") not in {
                        "report", "observe", "reject"
                    }:
                        continue
                    saved = completed_saver(previous, candidate)
                    candidate.deep_checkpoint_stage = saved.deep_checkpoint_stage
                    candidate.deep_checkpoint_input_signature = (
                        saved.deep_checkpoint_input_signature
                    )
                    candidate.deep_checkpoint_support_signature = (
                        saved.deep_checkpoint_support_signature
                    )
                    candidate.deep_checkpoint_created_at = (
                        saved.deep_checkpoint_created_at
                    )
                    candidate.deep_checkpoint_trajectory_signature = (
                        saved.deep_checkpoint_trajectory_signature
                    )
                    candidate.deep_checkpoint_synthesis_rubric = (
                        saved.deep_checkpoint_synthesis_rubric.copy()
                    )
                by_key = {item.key: item for item in trajectory_values}
                return [by_key.get(item.key, cached[item.key]) for item in group]

            try:
                values = await asyncio.wait_for(
                    process_batch(), timeout=remaining
                )
            except asyncio.TimeoutError:
                budget_deferred.extend(deferred_snapshot(item) for item in group)
                return
            except (sqlite3.DatabaseError, OSError):
                raise
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
                failed = deferred_snapshot(group[0])
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
            if concurrency > 1:
                for start in range(0, len(group), concurrency):
                    await gather_scoped(*(process_group([item]) for item in group[start:start + concurrency]))
            else:
                await process_group(group)
            if budget_deferred:
                # The untouched queue tail may have received unique support in
                # this discovery window. Preserve it before it ages out, just
                # as for the timed-out group itself.
                budget_deferred.extend(
                    deferred_snapshot(item)
                    for item in candidates[offset + len(group) :]
                )
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
        concurrency = getattr(self, "deep_concurrency", 1)
        batch_size = max(config.PARADIGM_DEEP_BATCH_SIZE, concurrency)

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
            except (sqlite3.DatabaseError, OSError):
                raise
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
            if concurrency > 1:
                for start in range(0, len(group), concurrency):
                    await gather_scoped(*(process_group([item]) for item in group[start:start + concurrency]))
            else:
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


def _revalidate_legacy_technical_report(evidence) -> bool:
    """Downgrade only legacy report flags that lack the current provenance.

    Older state snapshots promoted every hit from the report query.  Current
    sources persist one of three auditable classification reasons.  Re-running
    the conservative classifier keeps real branded/arXiv reports while making
    stale query-only rows use the ordinary one-call path.  The return value says
    whether a downgrade occurred.
    """

    raw = evidence.raw
    if raw.get("origin_kind") != "technical_report":
        return False
    reason = str(raw.get("origin_classification_reason", ""))
    trusted_prefixes = (
        "explicit_document_metadata:",
        "official_document_with_system_scope",
        "inferred_system_scope_report",
    )
    if reason.startswith(trusted_prefixes):
        return False
    metadata = " ".join(
        str(raw.get(key, ""))
        for key in (
            "arxiv_comment",
            "document_format",
            "document_source_kind",
        )
    )
    classification = classify_publication(
        title=evidence.title,
        url=evidence.url,
        summary=evidence.summary,
        metadata=metadata,
        authors=evidence.authors,
        official=bool(
            raw.get("publisher_tier") == "established"
            or str(evidence.source).startswith("official")
        ),
        discovered_by_report_query=bool(raw.get("query_group")),
    )
    raw["origin_classification_reason"] = classification.reason
    raw["document_format"] = classification.document_format
    raw["system_layer_count"] = classification.system_layer_count
    if classification.origin_kind == "technical_report":
        return False
    raw["origin_kind"] = classification.origin_kind
    if not raw.get("explicit_seed") and not raw.get("priority_researcher_match"):
        raw["origin_priority"] = min(_safe_int(raw.get("origin_priority", 1)), 2)
    run_audit.event(
        "legacy_technical_report_reclassification",
        "corrected",
        f"{evidence.title[:80]}：旧 Technical Report 标记缺少当前分类依据，"
        f"按 {classification.reason} 改走普通材料分析",
    )
    return True


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


def _landscape_baseline_incomplete(
    *,
    domain_coverage_incomplete: bool,
    failed_lanes: list[str],
    degraded_indexes: list[str],
    recall_lanes: dict,
) -> bool:
    """Separate the weekly map baseline from optional high-signal coverage."""

    baseline_lane_failures = [
        name for name in failed_lanes if name.startswith("landscape:")
    ]
    baseline_index_failures = [
        name
        for name in degraded_indexes
        if name == "openalex" or (name == "arxiv" and not recall_lanes)
    ]
    return bool(
        domain_coverage_incomplete
        or baseline_lane_failures
        or baseline_index_failures
    )


def _origin_execution_order(
    pending: list,
    newly_discovered: list,
    *,
    reference_time: datetime | None = None,
    window_days: int | None = None,
) -> list:
    """Prefer high-signal work without starving cheaper ordinary screening.

    Technical reports can expand into many mechanism calls.  A strict
    ``high + ordinary`` ordering therefore starves thousands of ordinary
    papers whenever a cold-start report backlog exists.  Each class first
    alternates new work with FIFO backlog, then execution uses a bounded
    1-high/3-ordinary weighted round robin.  Priority remains operational and
    every item stays in the queue.
    """
    high_pending = sorted(
        (item for item in pending if _is_high_priority_origin(item)),
        # ``pending`` already arrives in first_seen FIFO order. Keep that
        # order among equal-priority debt; sorting by publication date here
        # would let newer old reports overtake the same long-waiting reports
        # every week and make the advertised backlog share illusory.
        key=lambda item: _origin_analysis_priority(item)[:-1],
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
    ordered = []
    high_index = 0
    ordinary_index = 0
    while high_index < len(high) or ordinary_index < len(ordinary):
        if high_index < len(high):
            ordered.append(high[high_index])
            high_index += 1
        for _ in range(3):
            if ordinary_index >= len(ordinary):
                break
            ordered.append(ordinary[ordinary_index])
            ordinary_index += 1
    # The first delivery question is whether an origin actually belongs to
    # this report window. A long 30/60-day report catch-up and a 10k historical
    # queue must not consume every origin visit before fresh papers are seen.
    # This is execution priority only: it does not change Rubric or freshness.
    now = research_now(reference_time)
    window = config.SOURCING_LOOKBACK_DAYS if window_days is None else window_days
    return _interleave_service_lanes(
        ordered,
        is_current=lambda item: _origin_is_in_delivery_window(
            item, reference_time=now, window_days=window
        ),
    )


def _interleave_service_lanes(ordered: list, *, is_current) -> list:
    """Serve three current-window items per backfill item without truncation."""
    current = []
    backfill = []
    for item in ordered:
        (current if is_current(item) else backfill).append(item)
    if not current:
        return ordered
    interleaved = []
    current_index = backfill_index = 0
    while current_index < len(current) or backfill_index < len(backfill):
        for _ in range(3):
            if current_index < len(current):
                interleaved.append(current[current_index])
                current_index += 1
        if backfill_index < len(backfill):
            interleaved.append(backfill[backfill_index])
            backfill_index += 1
    return interleaved


def _origin_is_in_delivery_window(
    evidence, *, reference_time: datetime, window_days: int
) -> bool:
    published = _evidence_datetime(evidence.published_at)
    if published is None:
        return False
    return bool(
        reference_time - timedelta(days=max(window_days, 1))
        <= published <= reference_time + timedelta(days=1)
    )


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


def _deep_execution_order(
    pending: list,
    newly_extracted: list,
    *,
    reference_time: datetime | None = None,
    window_days: int | None = None,
) -> list:
    """Give new research a bounded turn while old deep work keeps advancing."""
    older = sorted(pending, key=_deep_analysis_priority, reverse=True)
    current = sorted(newly_extracted, key=_deep_analysis_priority, reverse=True)
    ordered = []
    old_index = new_index = 0
    while old_index < len(older) or new_index < len(current):
        for _ in range(3):
            if new_index < len(current):
                ordered.append(current[new_index])
                new_index += 1
        if old_index < len(older):
            ordered.append(older[old_index])
            old_index += 1
    now = research_now(reference_time)
    window = config.SOURCING_LOOKBACK_DAYS if window_days is None else window_days
    return _interleave_service_lanes(
        ordered,
        is_current=lambda item: _candidate_has_current_primary(
            item, reference_time=now, window_days=window
        ),
    )


def _candidate_has_current_primary(
    candidate, *, reference_time: datetime, window_days: int
) -> bool:
    return any(
        item.evidence_type in ORIGIN_EVIDENCE_TYPES
        and _origin_is_in_delivery_window(
            item, reference_time=reference_time, window_days=window_days
        )
        for item in candidate.evidence
    )


def _refresh_reserve_seconds(deadline: float, *, has_refresh_work: bool) -> int:
    remaining = _remaining_seconds(deadline)
    if not has_refresh_work or remaining < 180:
        return 0
    # A late discovery/origin phase must not make historical refresh exactly
    # zero whenever fewer than ten minutes remain. Keep a bounded useful slot
    # without taking more than half of a short tail from new deep work.
    return min(600, max(90, int(remaining // 4)))


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
    resumable_origins: list | None = None,
) -> None:
    """Commit in loss-safe order: resumable routes first, skip markers second."""

    for candidate in candidates:
        candidate.status = "pending_deep"
    transaction = getattr(store, "transaction", None)
    with transaction() if callable(transaction) else nullcontext():
        store.save_candidates(candidates)
        results = [store.mark_evidence(completed_origins, analyzed=True)]
        if resumable_origins:
            results.append(store.mark_evidence(resumable_origins, analyzed=False))
        if any(isinstance(result, EvidenceCheckpointResult) and
               (result.rejected_count or result.stale_revision_count) for result in results):
            raise ValueError("原点检查点版本/结构冲突；候选与完成标记已回滚")


def _resumable_deep_snapshot(store, candidate, supporting):
    loader = getattr(store, "load_synthesized_checkpoint", None)
    snapshot = loader(candidate) if loader is not None else None
    if snapshot is None:
        return None
    if snapshot.deep_checkpoint_support_signature != (
        EvidenceEnricher.supporting_signature(snapshot, supporting)
    ):
        return None
    return snapshot


def _deferred_deep_snapshot(store, candidate, supporting):
    """Keep a completed synthesis or newly seen support across a deferral."""
    snapshot = _resumable_deep_snapshot(store, candidate, supporting)
    if snapshot is not None:
        return snapshot
    loader = getattr(store, "load_candidate_snapshots", None)
    if callable(loader):
        latest = loader({candidate.key})
        if latest and latest[0].status == "pending_deep":
            current = latest[0]
            if current.to_dict() != candidate.to_dict():
                snapshot = _resumable_deep_snapshot(store, current, supporting)
                if snapshot is not None:
                    return snapshot
                candidate = current
    pending = copy.deepcopy(candidate)
    original_count = len(pending.evidence)
    existing = {item.fingerprint: item for item in pending.evidence}
    EvidenceEnricher._attach_support(pending, supporting)
    if not candidate.deep_checkpoint_stage and len(pending.evidence) == original_count:
        return candidate
    for item in pending.evidence:
        previous = existing.get(item.fingerprint)
        if previous is not None and previous.evidence_type in ORIGIN_EVIDENCE_TYPES:
            continue
        existing[item.fingerprint] = item
    pending.evidence = list(existing.values())
    EvidenceEnricher.finalize([pending])
    pending.deep_checkpoint_stage = ""
    pending.deep_checkpoint_input_signature = ""
    pending.deep_checkpoint_support_signature = ""
    pending.deep_checkpoint_created_at = ""
    pending.deep_checkpoint_trajectory_signature = ""
    pending.deep_checkpoint_synthesis_rubric = {}
    return pending


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

    key_people = delivery_researcher_profiles(
        candidate.researchers,
        config.PARADIGM_KEY_RESEARCHER_LIMIT,
    )
    if not key_people:
        return bool(verified_organization_attribution(candidate))
    return True


def _delivery_primary_source_ready(candidate) -> bool:
    return any(
        evidence.evidence_type in ORIGIN_EVIDENCE_TYPES
        and primary_material_url(evidence)
        for evidence in candidate.evidence
    )
