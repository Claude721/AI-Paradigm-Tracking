"""Separate this process's work from the durable research campaign totals."""
from __future__ import annotations


def execution_summary(stats):
    recovered = stats.get("recovered_delivery_only") is True
    reused = stats.get("discovery_resumed")
    discovery = stats.get("discovery_executed") is True or reused is False
    ledger = stats.get("research_campaign") or {}
    mode = ("delivery_only" if recovered else "resumed_research" if reused is True or ledger.get("attempt_count", 0) > 1
            else "new_research" if reused is False else "unknown")
    proof = stats.get("delivery_scope") or {}
    return {
        "run_kind": mode,
        "research_executed": not recovered,
        "discovery_executed": discovery and not recovered,
        "discovery_snapshot_reused": reused is True and not recovered,
        "campaign_id": ledger.get("campaign_id", ""),
        "campaign_attempt": ledger.get("attempt_count"),
        "current_run": {
            "origin_completed": stats.get("analysis_completed_count", 0) if not recovered else 0,
            "deep_completed": stats.get("current_window_deep_completed_count", stats.get("deep_candidate_count", 0)) if not recovered else 0,
            "refresh_completed": stats.get("refreshed_paradigms", 0) if not recovered else 0,
        },
        "campaign_cumulative": {key: ledger.get(key) for key in (
            "origin_planned", "origin_completed", "deep_planned", "deep_completed",
            "refresh_planned", "refresh_completed", "cumulative_research_seconds", "cumulative_llm_total_tokens")},
        "campaign_pending_count": proof.get("scoped_pending_count", ledger.get("pending_total_count")),
        "global_backlog_count": stats.get("pending_work_count", 0),
    }
