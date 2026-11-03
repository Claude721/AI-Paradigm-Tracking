"""Proof-bound repair of legacy publication-only research requeues.

No model, search, SMTP, research reconciliation, or historical verdict changes.
Only an exact frozen successful snapshot whose status alone was changed can
return to its former status. A distinct outbox retains the original failure.
"""
from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from datetime import datetime, timezone

from paradigms.completion import require_completed_research
from reports.paradigm_generator import _publication_route_violations, _route_fragment_key
from runtime_clock import research_window


def snapshot_digest(job):
    material = {"key": job.delivery_key, "date": job.report_date,
                "candidates": [item.to_dict() for item in job.candidates],
                "stats": job.stats, "failure_kind": job.failure_kind,
                "quarantined_at": job.quarantined_at, "last_error": job.last_error}
    return hashlib.sha256(json.dumps(material, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def _check_job(job):
    if (job is None or job.status != "quarantined" or job.failure_kind != "report_quality_contract"
            or job.stats.get("delivery_contract_version") != 5 or not job.candidates):
        raise ValueError("只能恢复v5最终成稿失败的非空冻结交付")
    require_completed_research(job.stats, candidates=job.candidates)


def plan_recovery(store, job, bindings):
    _check_job(job)
    if bindings.get("snapshot_digest") != snapshot_digest(job):
        raise ValueError("原稿绑定不属于当前冻结交付")
    campaign_id = job.stats["research_campaign"]["campaign_id"]
    ledger = store.campaigns.snapshot(campaign_id)
    original = job.stats["research_campaign"]["execution_receipts"]
    saved = {item.key: item for item in store.load_candidate_snapshots({v.key for v in job.candidates})}
    keys = bindings.get("draft_keys")
    if not isinstance(keys, dict) or set(keys) != {v.key for v in job.candidates}:
        raise ValueError("原稿绑定身份和基数与冻结交付不一致")
    for item in job.candidates:
        if not isinstance(keys[item.key], str) or not re.fullmatch(
                r"route:[0-9a-f]{12}:" + re.escape(item.key) + r":[0-9a-f]{20}", keys[item.key]):
            raise ValueError("原稿键没有绑定当前路线身份")
    cached = store.campaigns.cache_get_many("weekly_publication_draft", list(keys.values()))
    restorable, drafts, excluded, draft_excluded = [], {}, {}, {}
    for item in job.candidates:
        current = saved.get(item.key)
        before = [row for row in original if row["key"] == item.key and row["kind"] in {"deep", "refresh"}]
        now = [row for row in ledger["execution_receipts"] if row["key"] == item.key and row["kind"] in {"deep", "refresh"}]
        if (not any(row["outcome"] == "completed" for row in before) or now != before):
            excluded[item.key] = "ExecutionReceiptChanged"
            continue
        if current is None or not store.candidate_inputs_current(item):
            excluded[item.key] = "SourceOrSnapshotChanged"
            continue
        current_payload, frozen_payload = current.to_dict(), item.to_dict()
        current_payload.pop("status")
        frozen_payload.pop("status")
        if (current_payload != frozen_payload or current.status != "pending_deep"
                or item.status == "pending_deep" or item.deep_checkpoint_stage != "research_complete"):
            excluded[item.key] = "NotAnExactPublicationRequeue"
            continue
        restorable.append(item.key)
        value = cached.get(keys[item.key], {})
        content = value.get("content", "") if isinstance(value, dict) else ""
        try:
            invalid = not isinstance(content, str) or not content or bool(_publication_route_violations(content, item))
        except (sqlite3.Error, OSError):
            raise
        except Exception:
            invalid = True
        if invalid:
            draft_excluded[item.key] = "FrozenDraftNotPublishable"
            continue
        drafts[_route_fragment_key(item)] = content
    return {"snapshot_digest": snapshot_digest(job), "delivery_key": job.delivery_key,
            "candidate_count": len(job.candidates), "restore_keys": restorable,
            "excluded": excluded, "draft_excluded": draft_excluded,
            "publishable_count": len(drafts)}, drafts


def recover_publication(store, delivery_key, bindings, *, apply=False):
    """Plan or atomically repair; missing/changed peers remain untouched."""
    marker_key = "publication_recovery:" + delivery_key
    with store._connect() as conn:
        marker = conn.execute("SELECT value FROM radar_meta WHERE key=?", (marker_key,)).fetchone()
    if marker:
        receipt = json.loads(marker[0])
        replacement = store.get_report_job(receipt["replacement_key"])
        if replacement is None:
            raise ValueError("恢复回执存在但下游交付丢失，拒绝重复改写研究")
        return {**receipt, "status": "already_recovered"}
    job = store.get_report_job(delivery_key)
    _check_job(job)
    reference = datetime.fromisoformat(job.stats["research_campaign"]["reference_time"])
    campaign = store.campaigns.get(job.stats["research_campaign"]["campaign_id"], load_discovery=False)
    with research_window(reference, observation_time=reference, lookback_days=campaign.ordinary_days):
        plan, _ = plan_recovery(store, job, bindings)
        if not apply:
            return {**plan, "status": "planned"}
        if not plan["restore_keys"] or not plan["publishable_count"]:
            raise ValueError("没有能证明是交付误退且有合格原稿的对象，不修改状态")
        with store.transaction():
            # Re-read the full snapshot and all facts under the write lock.
            locked = store.get_report_job(delivery_key)
            current_plan, drafts = plan_recovery(store, locked, bindings)
            if current_plan != plan:
                raise ValueError("恢复计划已变化，不覆盖新状态")
            from main import _repair_weekly_publication
            replacement = _repair_weekly_publication(store, locked, drafts, recovery_parent=True)
            originals = {item.key: item for item in locked.candidates}
            now = datetime.now(timezone.utc).isoformat()
            # Downstream outbox and content are durable before status repair.
            with store._connect() as conn:
                for key in plan["restore_keys"]:
                    row = conn.execute("SELECT payload_json FROM paradigms WHERE paradigm_key=?", (key,)).fetchone()
                    payload = json.loads(row[0])
                    payload["status"] = originals[key].status
                    updated = conn.execute("UPDATE paradigms SET status=?, payload_json=? "
                                           "WHERE paradigm_key=? AND status='pending_deep' AND payload_json=?",
                                           (payload["status"], json.dumps(payload, ensure_ascii=False, sort_keys=True), key, row[0]))
                    if updated.rowcount != 1:
                        raise ValueError("候选状态CAS冲突，恢复整体回滚")
                receipt = {**plan, "status": "recovered_pending_delivery", "replacement_key": replacement.delivery_key,
                           "restored_count": len(plan["restore_keys"]), "published_route_count": len(replacement.candidates),
                           "email_sent": False, "model_calls": 0, "recovered_at": now,
                           "original_failure_kind": locked.failure_kind,
                           "original_last_error": locked.last_error, "original_quarantined_at": locked.quarantined_at}
                conn.execute("INSERT INTO radar_meta(key,value,updated_at) VALUES(?,?,?)",
                             (marker_key, json.dumps(receipt, ensure_ascii=False, sort_keys=True), now))
            return receipt
