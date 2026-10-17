"""Weekly publication is a snapshot of qualified work, not a barrier join.

The research ledger retains failures and unfinished work. Only published route
identities are frozen into the outbox; no judgment or human feedback is changed.
"""
from __future__ import annotations

from paradigms.delivery_policy import delivery_proof, weekly_result_kind
from paradigms.scoring import is_reportable
from paradigms.search_receipts import blocking_searches
from reports.paradigm_generator import _route_fragment_key
from run_audit import run_audit


async def prepare_weekly_delivery(store, generator, candidates, stats, *, report_date):
    if stats.get("delivery_contract_version") != 5:
        return candidates, stats, {}
    campaign_id = stats["research_campaign"]["campaign_id"]
    ledger = store.campaigns.reconcile(campaign_id)
    closed = {row["key"] for row in ledger["execution_receipts"]
              if row["kind"] in {"deep", "refresh"} and row["outcome"] == "completed"}
    eligible, excluded = [], {}
    for item in candidates:
        if (item.key in closed and item.status != "pending_deep" and is_reportable(item)
                and store.candidate_inputs_current(item) and not blocking_searches(item, allow_partial=True)
                and item.freshness_assessment.get("decision") == "include"):
            eligible.append(item)
        else:
            excluded[item.key] = "PublicationInputNotReady"
    cached = {key: payload.get("content", "") for key, payload in store.campaigns.cache_get_many(
        "weekly_publication_draft", [_route_fragment_key(item) for item in eligible]).items() if isinstance(payload, dict)}
    selected, fragments, failures = await generator.prepare_weekly_routes(
        eligible, date=report_date, cached=cached,
        save_fragment=lambda key, content: store.campaigns.cache_save(
            "weekly_publication_draft", key, {"content": content}),
    )
    excluded.update(failures)
    # CAS still applies after writing. A changed route cannot invalidate peers.
    current = []
    for item in selected:
        if store.candidate_inputs_current(item):
            current.append(item)
        else:
            excluded[item.key] = "PublicationInputChanged"
    selected = current
    with store.transaction():
        ledger = store.campaigns.reconcile(campaign_id)
        closed = {row["key"] for row in ledger["execution_receipts"]
                  if row["kind"] in {"deep", "refresh"} and row["outcome"] == "completed"}
        for item in selected:
            if item.key not in closed:
                excluded[item.key] = "PublicationCheckpointChanged"
        selected = [item for item in selected if item.key in closed]
        stats = {**stats, **run_audit.token_totals(), "research_campaign": ledger,
                 "result_kind": weekly_result_kind(selected), "delivery_ready": True,
                 "publication_excluded": excluded, "publication_excluded_count": len(excluded),
                 "published_route_count": len(selected),
                 "new_paradigms": sum(item.report_kind != "update" for item in selected),
                 "updated_paradigms": sum(item.report_kind == "update" for item in selected)}
        stats["delivery_scope"] = delivery_proof(stats, selected, ledger,
            minimum_sources=stats["delivery_scope"]["minimum_primary_sources"])
        stats["research_health"] = ("interrupted" if stats.get("research_stage_interrupted") else
            "with_gaps" if excluded or ledger["pending_total_count"] or stats.get("coverage_incomplete")
            or stats.get("run_budget_exhausted") or len(stats["delivery_scope"]["usable_primary_sources"]) < stats["delivery_scope"]["minimum_primary_sources"]
            else "healthy")
        store.campaigns.save_stats(campaign_id, stats, ready=True)
    run_audit.event("weekly_publication", "snapshot_sealed",
                    f"发表 {len(selected)} 条；单条发表问题 {len(excluded)} 条；研究待办 {ledger['pending_total_count']} 条")
    return selected, stats, fragments
