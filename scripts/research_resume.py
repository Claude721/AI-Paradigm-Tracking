"""Decide bounded continuation; never dispatch or call services itself.

The workflow may act on this decision only after uploading a healthy state.
Extra API runs are disabled unless the repository owner explicitly enables them.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def continuation_decision(result, current, *, enabled=False, max_runs=4, total_seconds=14400,
                          next_budget_seconds=3600, max_tokens=4000000, state_saved=False):
    def stop(reason):
        return {"dispatch": False, "reason": reason}
    if not enabled:
        return stop("automatic_resume_disabled")
    if not state_saved:
        return stop("healthy_state_not_uploaded")
    if not isinstance(current, dict) or not current.get("campaign_id"):
        return stop("no_active_campaign")
    if (result.get("research_campaign") or {}).get("campaign_id") != current["campaign_id"]:
        return stop("campaign_identity_mismatch")
    if result.get("result_kind") != "research_blocked" or result.get("delivery_failure_kind") != "research_not_complete":
        return stop("not_a_research_budget_checkpoint")
    if result.get("coverage_incomplete") is not False:
        return stop("source_coverage_requires_repair")
    if not result.get("run_budget_exhausted") or not current.get("pending_total_count"):
        return stop("not_budget_deferred_work")
    # Retry transport/data/programming errors manually after repair; an auto
    # chain must not spend hours repeating a 403 or a poison candidate.
    for field in (
        "analysis_failed_count", "candidate_execution_deferred_count", "candidate_input_deferred_count",
        "candidate_research_incomplete_count", "refresh_execution_deferred_count",
        "evidence_checkpoint_rejected_count", "delivery_profile_deferred_count", "delivery_source_deferred_count",
        "analysis_safety_deferred_count", "candidate_safety_deferred_count", "refresh_safety_deferred_count", "report_safety_deferred_count",
    ):
        if result.get(field):
            return stop("non_budget_deferral_requires_attention:" + field)
    if current.get("usage_unknown_attempts"):
        return stop("unaccounted_interrupted_run")
    if current.get("cumulative_unreported_usage_calls"):
        return stop("model_usage_unavailable")
    if max_runs < 1 or current.get("attempt_count", 0) >= max_runs:
        return stop("campaign_run_limit_reached")
    if next_budget_seconds <= 0 or total_seconds <= 0 or current.get("reserved_research_seconds", 0) + next_budget_seconds > total_seconds:
        return stop("campaign_time_limit_reached")
    if max_tokens <= 0 or current.get("cumulative_llm_total_tokens", 0) >= max_tokens:
        return stop("campaign_observed_token_limit_reached")
    return {"dispatch": True, "reason": "bounded_budget_resume", "campaign_id": current["campaign_id"], "reference_time": current["reference_time"]}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state-saved", action="store_true")
    parser.add_argument("--marker", type=Path, default=ROOT / "logs/pipeline_result.json")
    args = parser.parse_args()
    import config
    from database.paradigm_store import ParadigmStore
    result = json.loads(args.marker.read_text()) if args.marker.is_file() else {}
    current = None
    if config.PARADIGM_DB_PATH.is_file():
        store = ParadigmStore(config.PARADIGM_DB_PATH)
        active = store.campaigns.active(load_discovery=False)
        current = store.campaigns.snapshot(active.campaign_id) if active else None
    print(json.dumps(continuation_decision(result, current,
        enabled=config.PARADIGM_AUTO_RESUME_ENABLED, max_runs=config.PARADIGM_AUTO_RESUME_MAX_RUNS,
        total_seconds=config.PARADIGM_AUTO_RESUME_TOTAL_BUDGET_SECONDS,
        next_budget_seconds=config.PARADIGM_RUN_BUDGET_SECONDS, max_tokens=config.PARADIGM_AUTO_RESUME_MAX_TOKENS,
        state_saved=args.state_saved), ensure_ascii=False))


if __name__ == "__main__":
    main()
