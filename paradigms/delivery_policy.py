"""V0: bounded research, local failures, evidence-qualified delivery.

Acquisition is best effort, not an exhaustive census. The durable scope is an
execution plan, not a technical verdict. Unprocessed objects stay unprocessed.
Legacy outbox receipts deliberately retain their original strict contract.
"""
from __future__ import annotations

from collections import defaultdict, deque
import json

from .models import ORIGIN_EVIDENCE_TYPES, _evidence_datetime, nonnegative_number, technical_evidence_from_dict

VERSION = 5
PRIMARY_ADAPTERS = frozenset({
    "arxiv", "huggingface-papers", "openalex", "openreview", "research-blog",
    "priority-research-page", "curated-kol-feeds", "high-signal-forums",
    "official-repository-release",
})
HEALTHY = frozenset({"completed", "completed_after_retry", "completed_with_warnings"})


def validated_discovery(batch):
    """Reject malformed records before serializing the whole campaign snapshot."""
    from .discovery import DiscoveryBatch
    counts = {}
    values = {}
    for lane in ("origins", "supporting"):
        values[lane] = []
        rejected = 0
        for item in getattr(batch, lane):
            try:
                canonical = technical_evidence_from_dict(item.to_dict())
                canonical.fingerprint
                json.dumps(canonical.to_dict(), allow_nan=False)
                values[lane].append(canonical)
            except (AttributeError, TypeError, ValueError, OverflowError, RecursionError):
                rejected += 1
        counts["discovery_rejected_" + lane] = rejected
    return DiscoveryBatch(values["origins"], values["supporting"], batch.source_counts, batch.coverage), counts


def bounded_origins(origins, *, limit, reference_time, days):
    """Round-robin publishers/sources after priority sorting; never score here."""
    from datetime import timedelta
    cutoff = reference_time - timedelta(days=days)
    pools = defaultdict(deque)
    seen = set()
    for item in sorted(origins, key=lambda value: (
        nonnegative_number(value.raw.get("origin_priority", 0)), value.published_at or ""
    ), reverse=True):
        if item.fingerprint in seen or item.evidence_type not in ORIGIN_EVIDENCE_TYPES:
            continue
        date = _evidence_datetime(item.published_at)
        if not item.raw.get("explicit_seed") and date and not cutoff <= date <= reference_time:
            continue
        seen.add(item.fingerprint)
        pools[item.organization or item.source].append(item)
    result = []
    while pools and len(result) < limit:
        for name in list(pools):
            result.append(pools[name].popleft())
            if not pools[name]:
                del pools[name]
            if len(result) >= limit:
                break
    return result


def acquisition_summary(coverage):
    if not isinstance(coverage, dict):
        return {"usable_primary_sources": [], "source_issue_names": []}
    health = coverage.get("source_health", {})
    if not isinstance(health, dict):
        health = {}
    indexes = coverage.get("academic_indexes", {})
    if not isinstance(indexes, dict):
        indexes = {}
    usable = []
    issues = []
    for name, item in health.items():
        if not isinstance(item, dict):
            continue
        # Typed partial results are usable even when the rest of an adapter
        # timed out. They do not turn that adapter into a successful census.
        academic = indexes.get(name)
        zero_execution = isinstance(academic, dict) and academic.get("planned_queries", 0) and not academic.get("completed_queries", 0)
        if name in PRIMARY_ADAPTERS and not zero_execution and (item.get("status") in HEALTHY or
                type(item.get("results")) is int and item["results"] > 0):
            usable.append(name)
        if item.get("status") not in HEALTHY | {"disabled", "not_configured"}:
            issues.append(name)
    return {"usable_primary_sources": sorted(usable), "source_issue_names": sorted(issues)}


def delivery_proof(stats, candidates, ledger, *, minimum_sources):
    return {
        **acquisition_summary(stats.get("frontier_coverage", {})),
        "minimum_primary_sources": minimum_sources,
        "origin_planned": ledger["origin_planned"],
        "origin_completed": ledger["origin_completed"],
        "origin_selected": stats.get("research_scope_origin_count", ledger["origin_planned"]),
        "deep_planned": ledger["deep_planned"],
        "deep_completed": ledger["deep_completed"],
        "refresh_planned": ledger["refresh_planned"],
        "refresh_completed": ledger["refresh_completed"],
        "rejected_origin_count": stats.get("discovery_rejected_origins", 0),
        "scoped_pending_count": ledger["pending_total_count"],
        **({"deep_scope_keys": ledger["deep_scope_keys"],
            "deep_scope_sealed": ledger["deep_scope_sealed"]}
           if stats.get("delivery_contract_version") in {3, 4, 5} else {}),
        **({"execution_receipts": ledger["execution_receipts"],
            **{f"{kind}_{field}": ledger[f"{kind}_{field}"]
               for kind in ("origin", "deep", "refresh")
               for field in ("attempted", "failed", "unattempted")}}
           if stats.get("delivery_contract_version") in {4, 5} else {}),
        "routes": sorted([
            {"key": item.key, "signature": item.report_signature}
            for item in candidates
        ], key=lambda item: item["key"]),
    }


def violations(stats):
    """Validate a scoped receipt; store boundaries also recompute it from SQL."""
    issues = []
    proof = stats.get("delivery_scope")
    if not isinstance(proof, dict):
        return ["缺少本期有界研究交付凭据"]
    count_fields = ("minimum_primary_sources", "origin_planned", "origin_completed", "origin_selected", "deep_planned", "deep_completed", "refresh_planned", "refresh_completed", "rejected_origin_count", "scoped_pending_count")
    if any(type(proof.get(key)) is not int or proof[key] < 0 for key in count_fields):
        return ["本期研究交付计数无效"]
    minimum = proof["minimum_primary_sources"]
    if minimum < 1:
        issues.append("有效一手信源最小数无效")
    actual = acquisition_summary(stats.get("frontier_coverage", {}))["usable_primary_sources"]
    if proof.get("usable_primary_sources") != actual or (stats.get("delivery_contract_version") != 5 and len(actual) < minimum):
        issues.append("有效一手取材入口不足，不能把抓取失败写成无信号")
    routes = proof.get("routes")
    if not isinstance(routes, list) or any(not isinstance(row, dict) or
            not isinstance(row.get("key"), str) or not row["key"] or
            not isinstance(row.get("signature"), str) or not row["signature"] for row in routes):
        return [*issues, "入选路线身份凭据无效"]
    if len({row["key"] for row in routes}) != len(routes):
        issues.append("入选路线身份重复")
    ledger = stats.get("research_campaign", {})
    if not isinstance(ledger, dict) or not ledger.get("campaign_id"):
        issues.append("缺少本期持久化研究批次")
    elif any(proof[key] != ledger.get(key) for key in ("origin_planned", "origin_completed", "deep_planned", "deep_completed", "refresh_planned", "refresh_completed")) or proof["scoped_pending_count"] != ledger.get("pending_total_count"):
        issues.append("本期研究凭据与任务账本不一致")
    if proof["origin_completed"] > proof["origin_planned"]:
        issues.append("完成原点数超过研究计划")
    if proof["deep_completed"] > proof["deep_planned"]:
        issues.append("完成深挖数超过研究计划")
    if proof["refresh_completed"] > proof["refresh_planned"]:
        issues.append("完成刷新数超过研究计划")
    if stats.get("delivery_contract_version") in {4, 5}:
        return [*issues, *_execution_violations(stats, proof, ledger, routes)]
    # A route entering the cohort is a research obligation, never a top-N
    # editorial sample. Nonempty output cannot hide unfinished peer judgments.
    if proof["scoped_pending_count"] or proof["origin_completed"] != proof["origin_planned"] or proof["origin_selected"] != proof["origin_planned"]:
        issues.append("本期选定原点未全部完成，禁止交付部分研究结果")
    if proof["deep_completed"] != proof["deep_planned"] or proof["refresh_completed"] != proof["refresh_planned"]:
        issues.append("本期深挖/刷新候选未全部闭合，禁止截取部分路线交付")
    if stats.get("delivery_contract_version") == 3:
        keys = proof.get("deep_scope_keys")
        if (proof.get("deep_scope_sealed") is not True or
                not isinstance(keys, list) or any(not isinstance(key, str) or not key for key in keys) or
                len(set(keys)) != len(keys) or len(keys) != proof["deep_planned"] or
                keys != ledger.get("deep_scope_keys") or ledger.get("deep_scope_sealed") is not True):
            issues.append("全部深挖候选的身份清单未封存或与任务账本不一致")
        if any(stats.get(key, 0) for key in ("delivery_profile_deferred_count", "delivery_source_deferred_count", "report_safety_deferred_count")):
            issues.append("本期候选人物、原文或报告资格仍未全部闭合")
    if routes:
        if len(routes) > proof["deep_completed"] + proof["refresh_completed"]:
            issues.append("非空报告缺少完成的逐路线研究判断")
        if stats.get("result_kind") != "complete_memo":
            issues.append("非空报告没有合格交付结论")
    else:
        if proof["scoped_pending_count"] or proof["origin_completed"] != proof["origin_planned"] or proof["origin_selected"] != proof["origin_planned"]:
            issues.append("本期选定研究未完成，不能形成成功空报告")
        if proof["deep_completed"] != proof["deep_planned"] or proof["refresh_completed"] != proof["refresh_planned"]:
            issues.append("本期选定路线判断未完成，不能形成成功空报告")
        if stats.get("result_kind") != "complete_no_signal":
            issues.append("空报告没有完成判断凭据")
        if proof["rejected_origin_count"]:
            issues.append("一手输入仍有结构故障，不能形成成功空报告")
    return issues


def execution_result_kind(ledger, candidates):
    if any(ledger.get(f"{kind}_unattempted", 1) for kind in ("origin", "deep", "refresh")):
        return "research_blocked"
    return "complete_memo" if candidates else "complete_no_selection"


def weekly_result_kind(candidates):
    """Publication outcome, never a claim that the research plan is complete."""
    return "weekly_memo" if candidates else "weekly_receipt"


def _execution_violations(stats, proof, ledger, routes):
    """All tasks must be visited; only selected routes must succeed."""
    issues = []
    weekly = stats.get("delivery_contract_version") == 5
    receipts = proof.get("execution_receipts")
    if not isinstance(receipts, list) or receipts != ledger.get("execution_receipts"):
        return ["执行回执与持久化任务账本不一致"]
    identities = set()
    counts = defaultdict(lambda: defaultdict(int))
    for row in receipts:
        if (not isinstance(row, dict) or row.get("kind") not in {"origin", "deep", "refresh"}
                or not isinstance(row.get("key"), str) or not row["key"]
                or not isinstance(row.get("revision"), str) or not row["revision"]
                or row.get("outcome") not in {"completed", "failed", "unattempted"}
                or type(row.get("attempt_id")) is not int or row["attempt_id"] < 0
                or row["outcome"] == "failed" and not row["attempt_id"]
                or (row["kind"], row["key"]) in identities):
            return ["逐对象执行回执身份、版本或结果无效"]
        identities.add((row["kind"], row["key"]))
        counts[row["kind"]][row["outcome"]] += 1
    for kind in ("origin", "deep", "refresh"):
        expected = {"completed": counts[kind]["completed"], "failed": counts[kind]["failed"],
                    "unattempted": counts[kind]["unattempted"]}
        derived = {"planned": sum(expected.values()), "completed": expected["completed"],
                   "attempted": expected["completed"] + expected["failed"],
                   "failed": expected["failed"], "unattempted": expected["unattempted"]}
        if any(type(proof.get(f"{kind}_{field}")) is not int
               or proof[f"{kind}_{field}"] != value or ledger.get(f"{kind}_{field}") != value
               for field, value in derived.items()):
            issues.append(f"{kind} 执行计数与逐对象回执不一致")
        if expected["unattempted"] and not weekly:
            issues.append(f"{kind} 仍有未尝试或预算/依赖中断的任务")
    keys = sorted(row["key"] for row in receipts if row["kind"] == "deep")
    if (proof.get("deep_scope_keys") != keys or ledger.get("deep_scope_keys") != keys
            or type(proof.get("deep_scope_sealed")) is not bool
            or proof.get("deep_scope_sealed") != ledger.get("deep_scope_sealed")
            or not weekly and proof.get("deep_scope_sealed") is not True):
        issues.append("全部深挖候选身份清单未封存")
    if proof["origin_selected"] != proof["origin_planned"]:
        issues.append("选定原点与执行计划不一致")
    closed = {row["key"] for row in receipts if row["kind"] in {"deep", "refresh"} and row["outcome"] == "completed"}
    if any(row["key"] not in closed for row in routes):
        issues.append("入选路线没有自身成功的研究回执")
    if weekly:
        if stats.get("result_kind") != weekly_result_kind(routes):
            issues.append("周报类型与本次入选路线不一致")
        return issues
    judgments = proof["deep_completed"] + proof["refresh_completed"]
    if not routes and ((proof["deep_planned"] + proof["refresh_planned"] > 0 and not judgments)
                       or proof["origin_planned"] > 0 and not proof["origin_completed"] and not judgments):
        issues.append("没有成功的研究判断，不能把系统性失败写成空结论")
    if not routes and proof["rejected_origin_count"]:
        issues.append("一手输入存在未归因结构故障，不能形成空结论")
    if stats.get("result_kind") != ("complete_memo" if routes else "complete_no_selection"):
        issues.append("交付结果类型与已核验路线不一致")
    if stats.get("report_safety_deferred_count", 0):
        issues.append("显式报告熔断仍截断合格路线，不能隐瞒已核验的交付对象")
    return issues
