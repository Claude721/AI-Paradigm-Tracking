"""Source-owned revisions and monotonic execution checkpoints (schema v7).

The evidence table remains a compatible enriched read view. It is deliberately
not the authority for deciding whether an original publication changed.
"""
from __future__ import annotations

import copy
import hashlib
import json

from paradigms.models import ORIGIN_EVIDENCE_TYPES, technical_evidence_from_dict


EXECUTION_KEYS = (
    "analysis_failure_count", "last_analysis_failure_at",
    "origin_eligibility_decision", "origin_eligibility_reason",
    "technical_report_checkpoint_version", "technical_report_mechanism_seeds",
    "technical_report_completed_mechanisms", "technical_report_mechanism_failure_counts",
    "technical_report_slice_pending", "technical_report_partial_failure",
    "technical_report_last_run_failure",
)


def source_signature(item, *, ignore_organization=False):
    linked = sorted(
        (str(value.get("title", "")).strip(), str(value.get("url", "")).strip().rstrip("/"))
        for value in (item.raw.get("linked_research_documents") or [])
        if isinstance(value, dict) and value.get("url")
    )
    payload = {
        "title": item.title.strip(), "summary": item.summary.strip(),
        "authors": item.authors,
        "organization": "" if ignore_organization else item.organization,
        "identifiers": item.identifiers, "url": item.url.strip().rstrip("/"),
        "published_at": item.published_at,
        "updated_at": str(item.raw.get("updated_at", "")),
        "arxiv_comment": str(item.raw.get("arxiv_comment", "")),
        "linked_research_documents": linked,
    }
    return hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def execution_checkpoint(item):
    return copy.deepcopy({key: item.raw[key] for key in EXECUTION_KEYS if key in item.raw})


def apply_checkpoint(item, checkpoint):
    for key in EXECUTION_KEYS:
        item.raw.pop(key, None)
    item.raw.update(copy.deepcopy(checkpoint))


def same_source(item, row):
    """Legacy hydration may have filled organization; reconcile once, not forever."""
    if source_signature(item) == row[0]:
        return True
    if row[3] != "legacy_unverified":
        return False
    old = technical_evidence_from_dict(json.loads(row[1]))
    return source_signature(item, ignore_organization=True) == source_signature(
        old, ignore_organization=True,
    )


def merge_checkpoint(previous, incoming):
    validate_checkpoint(previous)
    validate_checkpoint(incoming)
    result = {**previous, **incoming}
    if "analysis_failure_count" in previous or "analysis_failure_count" in incoming:
        result["analysis_failure_count"] = max(
            previous.get("analysis_failure_count", 0), incoming.get("analysis_failure_count", 0),
        )
    seeds_key = "technical_report_mechanism_seeds"
    version_key = "technical_report_checkpoint_version"
    for key in (seeds_key, version_key):
        if previous.get(key) and incoming.get(key) and previous[key] != incoming[key]:
            raise ValueError("同一来源版本的机制索引不一致；拒绝覆盖检查点")
    completed_key = "technical_report_completed_mechanisms"
    completed = {**incoming.get(completed_key, {}), **previous.get(completed_key, {})}
    if completed_key in previous or completed_key in incoming:
        # A stale snapshot may contain fewer completed mechanisms. Never subtract.
        result[completed_key] = completed
    failure_key = "technical_report_mechanism_failure_counts"
    if failure_key in previous or failure_key in incoming:
        failures = dict(previous.get(failure_key, {}))
        for key, value in incoming.get(failure_key, {}).items():
            failures[key] = max(int(value), int(failures.get(key, 0)))
        result[failure_key] = {key: value for key, value in failures.items() if key not in completed}
    seeds = result.get(seeds_key, [])
    if seeds:
        if len(completed) >= len(seeds):
            for key in ("technical_report_slice_pending", "technical_report_partial_failure",
                        "technical_report_last_run_failure"):
                result.pop(key, None)
        else:
            result["technical_report_slice_pending"] = True
    return result


def initialize(conn):
    existed = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='origin_research_state'"
    ).fetchone() is not None
    conn.execute("""CREATE TABLE IF NOT EXISTS origin_research_state (
        fingerprint TEXT PRIMARY KEY,
        source_signature TEXT NOT NULL,
        source_payload_json TEXT NOT NULL,
        checkpoint_json TEXT NOT NULL,
        baseline_status TEXT NOT NULL
    )""")
    if existed:
        return
    # Only a newly created table is migrated. Never invalidate completed research merely
    # because the storage schema changed. Domain validation rejects corrupt rows.
    rows = conn.execute("SELECT fingerprint, payload_json FROM evidence_state").fetchall()
    for fingerprint, payload in rows:
        item = technical_evidence_from_dict(json.loads(payload))
        if item.evidence_type not in ORIGIN_EVIDENCE_TYPES:
            continue
        signature = source_signature(item)
        conn.execute("INSERT INTO origin_research_state VALUES (?, ?, ?, ?, ?)", (
            fingerprint, signature, payload,
            json.dumps(execution_checkpoint(item), ensure_ascii=False, sort_keys=True),
            "legacy_unverified",
        ))
        item.source_revision = signature
        conn.execute("UPDATE evidence_state SET content_signature=?, payload_json=? WHERE fingerprint=?", (
            signature, json.dumps(item.to_dict(), ensure_ascii=False, sort_keys=True), fingerprint,
        ))


def read(conn, fingerprint):
    return conn.execute("""SELECT source_signature, source_payload_json,
        checkpoint_json, baseline_status FROM origin_research_state WHERE fingerprint=?""",
        (fingerprint,),
    ).fetchone()


def write(conn, item, *, source_observation=False, enrichment_only=False):
    """Return (read view, source signature, invalidates_analysis), or None if stale."""
    row = read(conn, item.fingerprint)
    signature = source_signature(item)
    checkpoint = execution_checkpoint(item)
    invalidates = False
    if row is None:
        if enrichment_only or (item.source_revision and not source_observation):
            # Enrichment and version-bound callbacks cannot register unseen origins.
            return None
        source = copy.deepcopy(item)
        if source_observation:
            checkpoint = {}
        baseline = "observed"
    elif source_observation:
        unchanged = same_source(item, row)
        invalidates = not unchanged
        source = copy.deepcopy(item)
        baseline = "observed"
        checkpoint = json.loads(row[2]) if unchanged else {}
        if unchanged:
            prior = conn.execute("SELECT payload_json FROM evidence_state WHERE fingerprint=?",
                                 (item.fingerprint,)).fetchone()
            if prior:
                enriched = technical_evidence_from_dict(json.loads(prior[0]))
                item.raw = {**enriched.raw, **item.raw}
                item.organization = item.organization or enriched.organization
    else:
        # Version-bound callbacks cannot write newer or older source revisions.
        # Legacy callbacks without a token must still match the source snapshot.
        if item.source_revision:
            if item.source_revision != row[0]:
                return None
        elif not same_source(item, row):
            return None
        signature = row[0]
        source = technical_evidence_from_dict(json.loads(row[1]))
        baseline = row[3]
        checkpoint = json.loads(row[2]) if enrichment_only else merge_checkpoint(json.loads(row[2]), checkpoint)
    source.source_revision = signature
    apply_checkpoint(source, {})
    item.source_revision = signature
    apply_checkpoint(item, checkpoint)
    validate_checkpoint(checkpoint)
    conn.execute("""INSERT INTO origin_research_state VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(fingerprint) DO UPDATE SET source_signature=excluded.source_signature,
        source_payload_json=excluded.source_payload_json, checkpoint_json=excluded.checkpoint_json,
        baseline_status=excluded.baseline_status""", (
        item.fingerprint, signature, json.dumps(source.to_dict(), ensure_ascii=False, sort_keys=True),
        json.dumps(checkpoint, ensure_ascii=False, sort_keys=True), baseline,
    ))
    return item, signature, invalidates


def validate_checkpoint(checkpoint):
    if not isinstance(checkpoint, dict) or set(checkpoint) - set(EXECUTION_KEYS):
        raise ValueError("原点执行检查点结构无效")
    for key in ("technical_report_completed_mechanisms", "technical_report_mechanism_failure_counts"):
        if key in checkpoint and not isinstance(checkpoint[key], dict):
            raise ValueError("原点机制检查点必须为映射")
    for value in checkpoint.get("technical_report_completed_mechanisms", {}).values():
        if not isinstance(value, dict):
            raise ValueError("已完成机制必须为结构化对象")
    seeds = checkpoint.get("technical_report_mechanism_seeds", [])
    if not isinstance(seeds, list) or any(not isinstance(value, dict) for value in seeds):
        raise ValueError("原点机制索引必须为对象数组")
    counts = [checkpoint.get("analysis_failure_count", 0),
              *checkpoint.get("technical_report_mechanism_failure_counts", {}).values()]
    if any(type(value) is not int or value < 0 for value in counts):
        raise ValueError("执行失败次数必须为非负整数")
    for key in ("technical_report_slice_pending", "technical_report_partial_failure",
                "technical_report_last_run_failure"):
        if key in checkpoint and type(checkpoint[key]) is not bool:
            raise ValueError("执行检查点标记必须为布尔值")
    for key in ("last_analysis_failure_at", "origin_eligibility_decision",
                "origin_eligibility_reason", "technical_report_checkpoint_version"):
        if key in checkpoint and not isinstance(checkpoint[key], str):
            raise ValueError("执行检查点说明必须为字符串")


def validate(conn):
    for fingerprint, signature, source_json, checkpoint_json, baseline in conn.execute(
        "SELECT * FROM origin_research_state"
    ):
        source = technical_evidence_from_dict(json.loads(source_json))
        checkpoint = json.loads(checkpoint_json)
        if (source.fingerprint != fingerprint or source_signature(source) != signature
                or baseline not in {"observed", "legacy_unverified"}
                or source.evidence_type not in ORIGIN_EVIDENCE_TYPES):
            raise ValueError("原点来源版本与快照不一致")
        validate_checkpoint(checkpoint)
        view = conn.execute("SELECT payload_json, content_signature FROM evidence_state WHERE fingerprint=?",
                            (fingerprint,)).fetchone()
        if view is None:
            raise ValueError("原点检查点缺少证据记录")
        item = technical_evidence_from_dict(json.loads(view[0]))
        if item.source_revision != signature or view[1] != signature or execution_checkpoint(item) != checkpoint:
            raise ValueError("原点兼容视图与来源版本/检查点不一致")
