"""A closed research result is the only admissible formal delivery input.

Execution checkpoints remain useful internally. They are never a substitute
for a completed report, including when restoring an older outbox snapshot.
"""

from __future__ import annotations

import math
import re


_PENDING_COUNTS = (
    "pending_work_count", "evidence_checkpoint_rejected_count",
    "analysis_deferred_count", "analysis_failed_count",
    "analysis_mechanism_slice_deferred_count", "analysis_safety_deferred_count",
    "analysis_budget_deferred_count", "pending_origin_backlog_remaining",
    "candidate_deferred_count", "candidate_safety_deferred_count",
    "candidate_budget_deferred_count", "candidate_execution_deferred_count",
    "candidate_input_deferred_count", "candidate_research_incomplete_count",
    "refresh_deferred_count", "refresh_safety_deferred_count",
    "refresh_budget_deferred_count", "refresh_execution_deferred_count",
    "delivery_profile_deferred_count", "delivery_source_deferred_count",
    "report_safety_deferred_count",
)
_CLOSED_STATUSES = {
    "domains": {"covered", "searched_zero_hits"},
    "recall_lanes": {"covered", "searched_zero_hits"},
    "source_health": {
        "completed", "completed_after_retry", "completed_with_warnings", "not_configured", "disabled",
    },
    "academic_indexes": {"completed", "completed_after_retry", "not_configured", "disabled"},
}
_PARTIAL_REPORT_MARKERS = (
    "阶段性研究", "阶段性 memo", "阶段性路线判断", "本期运行状态 Memo",
    "研究未完成状态", "研究链路**尚未完成**",
    "本轮仍有研究事务待续跑", "覆盖受限条件下没有可交付信号",
)


class ResearchNotCompleteError(RuntimeError):
    def __init__(self, violations: list[str]):
        self.violations = list(dict.fromkeys(violations))
        super().__init__(
            "本期研究未闭合，禁止生成或发送正式报告；检查点已保留："
            + "；".join(self.violations)
        )


def _count(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("count must be a nonnegative integer")
    if not math.isfinite(value) or value < 0 or int(value) != value:
        raise ValueError("count must be a nonnegative integer")
    return int(value)


def partial_report_violations(content: str) -> list[str]:
    return ["制品包含阶段性研究状态，不能作为正式报告"] if any(
        marker.casefold() in content.casefold() for marker in _PARTIAL_REPORT_MARKERS
    ) else []


def research_completion_violations(stats: dict) -> list[str]:
    """Fail closed on absent proof, conflicting flags, queues or coverage.

    Explicit negative closure flags provide compatibility with older healthy
    snapshots. Detail ledgers are checked independently; setting a summary flag
    to false must never conceal unfinished work.
    """
    if not isinstance(stats, dict):
        return ["缺少可核验的研究完成记录"]
    issues = []
    research = stats.get("research_incomplete", stats.get("run_incomplete"))
    coverage = stats.get("coverage_incomplete", stats.get("recall_coverage_incomplete"))
    if research is not False:
        issues.append("研究完成记录缺失或仍未闭合")
    if stats.get("run_incomplete", False) is not False:
        issues.append("兼容研究账本仍未闭合")
    if coverage is not False:
        issues.append("覆盖完成记录缺失或仍未闭合")
    if stats.get("recall_coverage_incomplete", False) is not False:
        issues.append("召回覆盖仍未闭合")
    if stats.get("result_kind") in {
        "partial_memo", "research_incomplete", "research_blocked",
        "coverage_limited_memo", "coverage_limited_no_signal",
    }:
        issues.append("运行结果不是已闭合的研究结论")

    def number(container, key):
        try:
            return _count(container.get(key, 0))
        except (TypeError, ValueError, OverflowError):
            issues.append(f"{key} 的完成计数无效")
            return 0

    for key in _PENDING_COUNTS:
        value = number(stats, key)
        if value:
            issues.append(f"{key}={value}")
    for planned, complete in (
        ("planned_analysis_count", "analysis_completed_count"),
        ("planned_deep_candidate_count", "deep_candidate_count"),
        ("current_window_origin_planned_count", "current_window_origin_completed_count"),
        ("current_window_deep_planned_count", "current_window_deep_completed_count"),
    ):
        if planned in stats and number(stats, planned) > number(stats, complete):
            issues.append(f"{planned} 尚未全部完成")
    queue = stats.get("work_queue_after") or {}
    if not isinstance(queue, dict):
        issues.append("持久化队列完成记录无效")
    else:
        for key in ("pending_total_count", "pending_origin_count", "pending_deep_count"):
            if number(queue, key):
                issues.append(f"持久化 {key} 尚未清零")

    frontier = stats.get("frontier_coverage") or {}
    if not isinstance(frontier, dict):
        issues.append("召回覆盖账本无效")
        return issues
    for section in ("domains", "recall_lanes", "source_health", "academic_indexes"):
        entries = frontier.get(section) or {}
        if not isinstance(entries, dict):
            issues.append(f"{section} 覆盖账本无效")
            continue
        for name, value in entries.items():
            if not isinstance(value, dict):
                issues.append(f"{section} 单入口覆盖账本无效")
                continue
            status = str(value.get("status", ""))
            failed = status not in _CLOSED_STATUSES[section]
            if failed:
                label = re.sub(r"[^A-Za-z0-9_.:-]", "_", str(name))[:64]
                issues.append(f"{section}/{label}={status[:40]}")
    for section, planned, checked, failure_keys in (
        ("official_pages", "total_pages", "checked_pages",
         ("request_failed", "parse_zero_links", "detail_failures")),
        ("official_repositories", "configured_organizations", "checked_organizations",
         ("failed_organizations", "repository_page_failures", "unverified_primary_releases")),
    ):
        value = frontier.get(section) or {}
        if not isinstance(value, dict):
            issues.append(f"{section} 覆盖账本无效")
            continue
        if number(value, checked) < number(value, planned) or any(
            value.get(key) for key in failure_keys
        ):
            issues.append(f"{section} 尚未闭合")
    if frontier.get("query_failures"):
        issues.append("领域查询仍有失败")
    return list(dict.fromkeys(issues))


def require_completed_research(stats: dict, *, content: str = "", candidates=None) -> None:
    violations = research_completion_violations(stats) + partial_report_violations(content)
    if candidates is not None and any(item.status == "pending_deep" for item in candidates):
        violations.append("报告候选仍有 pending_deep 研究检查点")
    if violations:
        raise ResearchNotCompleteError(violations)
