"""Finite, durable research campaigns, independent of process and delivery.

One campaign keeps its research clock and discovery snapshot across restarts.
Work is never removed from its manifest. A receipt may close only a version
whose downstream state exists; running attempts become retryable after a crash.
"""

from __future__ import annotations

import json
import math
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone

from paradigms.completion import ResearchNotCompleteError, require_completed_research
from paradigms.discovery import DiscoveryBatch
from paradigms.models import (
    candidate_from_dict, scrub_ephemeral_evidence,
    technical_evidence_from_dict,
)


def initialize(conn):
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS research_campaigns (
            campaign_id TEXT PRIMARY KEY,
            reference_time TEXT NOT NULL,
            ordinary_days INTEGER NOT NULL,
            high_signal_days INTEGER NOT NULL,
            bootstrap INTEGER NOT NULL,
            seeds_json TEXT NOT NULL,
            status TEXT NOT NULL,
            discovery_json TEXT NOT NULL DEFAULT '',
            stats_json TEXT NOT NULL DEFAULT '{}',
            delivery_key TEXT NOT NULL DEFAULT '',
            attempt_count INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        CREATE UNIQUE INDEX IF NOT EXISTS idx_one_active_campaign
            ON research_campaigns((1)) WHERE status != 'delivered';
        CREATE TABLE IF NOT EXISTS campaign_work (
            campaign_id TEXT NOT NULL,
            kind TEXT NOT NULL,
            object_key TEXT NOT NULL,
            input_revision TEXT NOT NULL,
            status TEXT NOT NULL,
            reason TEXT NOT NULL DEFAULT '',
            payload_json TEXT NOT NULL DEFAULT '',
            updated_at TEXT NOT NULL,
            PRIMARY KEY(campaign_id, kind, object_key)
        );
        CREATE TABLE IF NOT EXISTS research_attempts (
            attempt_id INTEGER PRIMARY KEY AUTOINCREMENT,
            campaign_id TEXT NOT NULL,
            kind TEXT NOT NULL,
            object_key TEXT NOT NULL,
            input_revision TEXT NOT NULL,
            outcome TEXT NOT NULL,
            reason TEXT NOT NULL DEFAULT '',
            started_at TEXT NOT NULL,
            finished_at TEXT
        );
        CREATE TABLE IF NOT EXISTS research_stage_cache (
            stage TEXT NOT NULL,
            input_signature TEXT NOT NULL,
            result_json TEXT NOT NULL,
            created_at TEXT NOT NULL,
            PRIMARY KEY(stage, input_signature)
        );
    """)


def _now():
    return datetime.now(timezone.utc).isoformat()


def _encode(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False)


@dataclass
class ResearchCampaign:
    campaign_id: str
    reference_time: datetime
    ordinary_days: int
    high_signal_days: int
    bootstrap: bool
    seeds: list[str]
    status: str
    discovery: DiscoveryBatch | None
    stats: dict
    delivery_key: str
    attempt_count: int


def _batch_payload(batch):
    def evidence(items):
        values = []
        for item in items:
            value = technical_evidence_from_dict(item.to_dict())
            scrub_ephemeral_evidence(value)
            values.append(value.to_dict())
        return values
    return {
        "origins": evidence(batch.origins), "supporting": evidence(batch.supporting),
        "source_counts": batch.source_counts, "coverage": batch.coverage,
    }


def _batch_from_payload(value):
    if not isinstance(value, dict) or not isinstance(value.get("source_counts"), dict) or not isinstance(value.get("coverage"), dict):
        raise ValueError("研究批次发现账本无效")
    if any(not isinstance(value.get(field), list) for field in ("origins", "supporting")):
        raise ValueError("研究批次发现快照必须包含原点/支持数组")
    if any(not isinstance(name, str) or not name or type(count) is not int or count < 0 for name, count in value["source_counts"].items()):
        raise ValueError("研究批次信源基数无效")
    return DiscoveryBatch(
        origins=[technical_evidence_from_dict(item) for item in value["origins"]],
        supporting=[technical_evidence_from_dict(item) for item in value["supporting"]],
        source_counts=value["source_counts"], coverage=value["coverage"],
    )


def _campaign(row):
    reference = datetime.fromisoformat(row[1])
    if reference.tzinfo is None or row[6] not in {"discovering", "researching", "ready", "waiting_delivery", "delivered"}:
        raise ValueError("研究批次时钟或状态无效")
    seeds, stats = json.loads(row[5]), json.loads(row[8])
    if not isinstance(seeds, list) or any(not isinstance(item, str) for item in seeds) or not isinstance(stats, dict):
        raise ValueError("研究批次配置或统计无效")
    if int(row[2]) < 1 or int(row[3]) < int(row[2]) or int(row[10]) < 0:
        raise ValueError("研究批次窗口或尝试计数无效")
    if not isinstance(row[0], str) or not row[0] or type(row[4]) is not int or row[4] not in {0, 1}:
        raise ValueError("研究批次身份或初始化标记无效")
    for field in ("reserved_research_seconds", "cumulative_research_seconds", "cumulative_llm_total_tokens", "usage_unknown_attempts", "cumulative_unreported_usage_calls", "last_accounted_attempt"):
        count = stats.get(field, 0)
        if isinstance(count, bool) or not isinstance(count, (int, float)) or not math.isfinite(count) or count < 0:
            raise ValueError("研究批次用量账本无效：" + field)
        if field not in {"reserved_research_seconds", "cumulative_research_seconds"} and int(count) != count:
            raise ValueError("研究批次计数必须为整数：" + field)
    if stats.get("last_accounted_attempt", 0) > row[10]:
        raise ValueError("研究批次确认次数超过实际尝试")
    return ResearchCampaign(
        row[0], reference.astimezone(timezone.utc), int(row[2]), int(row[3]), bool(row[4]),
        seeds, row[6], _batch_from_payload(json.loads(row[7])) if row[7] else None,
        stats, row[9], int(row[10]),
    )


_COLUMNS = "campaign_id, reference_time, ordinary_days, high_signal_days, bootstrap, seeds_json, status, discovery_json, stats_json, delivery_key, attempt_count"


class CampaignStore:
    def __init__(self, store):
        self.store = store

    def active(self, *, load_discovery=True):
        columns = _COLUMNS if load_discovery else _COLUMNS.replace("discovery_json", "''")
        with self.store._connect() as conn:
            row = conn.execute(f"SELECT {columns} FROM research_campaigns WHERE status != 'delivered'").fetchone()
        return _campaign(row) if row else None

    def get(self, key, *, load_discovery=True):
        columns = _COLUMNS if load_discovery else _COLUMNS.replace("discovery_json", "''")
        with self.store._connect() as conn:
            row = conn.execute(f"SELECT {columns} FROM research_campaigns WHERE campaign_id=?", (key,)).fetchone()
        return _campaign(row) if row else None

    def begin(self, *, reference_time, ordinary_days, high_signal_days, bootstrap, seeds):
        if reference_time.tzinfo is None:
            raise ValueError("研究批次需要带时区的参考时钟")
        with self.store.transaction():
            active = self.active()
            if active:
                return active
            key, now = uuid.uuid4().hex, _now()
            with self.store._connect() as conn:
                conn.execute("""INSERT INTO research_campaigns
                    (campaign_id, reference_time, ordinary_days, high_signal_days,
                     bootstrap, seeds_json, status, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, 'discovering', ?, ?)""",
                    (key, reference_time.astimezone(timezone.utc).isoformat(), ordinary_days,
                     high_signal_days, int(bootstrap), _encode(seeds), now, now))
            return self.get(key)

    def begin_run(self, key):
        import config
        previous = self.get(key, load_discovery=False)
        stats = dict(previous.stats)
        if previous.attempt_count > int(stats.get("last_accounted_attempt", 0)):
            stats["usage_unknown_attempts"] = int(stats.get("usage_unknown_attempts", 0)) + 1
        stats["reserved_research_seconds"] = float(stats.get("reserved_research_seconds", 0)) + config.PARADIGM_RUN_BUDGET_SECONDS
        with self.store._connect() as conn:
            # A process died before an attempt receipt. No completed result is
            # inferred; downstream state is reconciled independently below.
            conn.execute("UPDATE research_attempts SET outcome='interrupted', reason='process_restart', finished_at=? WHERE campaign_id=? AND outcome='running'", (_now(), key))
            conn.execute("UPDATE research_campaigns SET attempt_count=attempt_count+1, stats_json=?, updated_at=? WHERE campaign_id=? AND status != 'delivered'", (_encode(stats), _now(), key))
        return self.get(key)

    def save_discovery(self, key, batch):
        payload = _encode(_batch_payload(batch))
        # Validate the exact persisted representation before committing it.
        _batch_from_payload(json.loads(payload))
        with self.store._connect() as conn:
            conn.execute("UPDATE research_campaigns SET discovery_json=?, status='researching', updated_at=? WHERE campaign_id=? AND status != 'delivered'", (payload, _now(), key))

    def include(self, key, kind, objects):
        if kind not in {"origin", "deep", "refresh"}:
            raise ValueError("未知研究任务类型")
        with self.store._connect() as conn:
            for entry in objects:
                object_key, revision = entry[:2]
                if not isinstance(object_key, str) or not object_key or not isinstance(revision, str) or not revision:
                    raise ValueError("研究任务身份或输入版本无效")
                payload = ""
                if len(entry) > 2:
                    item = technical_evidence_from_dict(entry[2]) if kind == "origin" else candidate_from_dict(entry[2])
                    if (item.fingerprint if kind == "origin" else item.key) != object_key:
                        raise ValueError("研究任务恢复快照身份冲突")
                    if kind == "origin":
                        scrub_ephemeral_evidence(item)
                    else:
                        from database.paradigm_store import _persistable_candidate
                        item = _persistable_candidate(item)
                    payload = _encode(item.to_dict())
                conn.execute("""INSERT INTO campaign_work
                    (campaign_id, kind, object_key, input_revision, status, payload_json, updated_at)
                    VALUES (?, ?, ?, ?, 'pending', ?, ?)
                    ON CONFLICT(campaign_id, kind, object_key) DO UPDATE SET
                        status=CASE WHEN input_revision != excluded.input_revision THEN 'pending' ELSE status END,
                        payload_json=CASE WHEN excluded.payload_json != '' THEN excluded.payload_json WHEN input_revision != excluded.input_revision THEN '' ELSE payload_json END,
                        input_revision=excluded.input_revision, updated_at=excluded.updated_at""",
                    (key, kind, object_key, revision, payload, _now()))

    def pending_keys(self, key, kind):
        with self.store._connect() as conn:
            return {row[0] for row in conn.execute("SELECT object_key FROM campaign_work WHERE campaign_id=? AND kind=? AND status='pending'", (key, kind))}

    def candidate_keys(self, key):
        with self.store._connect() as conn:
            return {row[0] for row in conn.execute("SELECT DISTINCT object_key FROM campaign_work WHERE campaign_id=? AND kind IN ('deep', 'refresh')", (key,))}

    def seal_refresh_scope(self, key, candidates):
        from database.paradigm_store import _deep_checkpoint_input_signature
        with self.store.transaction():
            campaign = self.get(key, load_discovery=False)
            if not campaign.stats.get("refresh_scope_sealed"):
                from database.paradigm_store import _persistable_candidate
                self.include(key, "refresh", [(item.key, _deep_checkpoint_input_signature(item), _persistable_candidate(item).to_dict()) for item in candidates])
                with self.store._connect() as conn:
                    conn.execute("UPDATE research_campaigns SET stats_json=? WHERE campaign_id=?", (_encode({**campaign.stats, "refresh_scope_sealed": True}), key))
        return self.pending_keys(key, "refresh")

    def start_attempt(self, key, kind, objects):
        self.include(key, kind, objects)
        attempts = []
        with self.store._connect() as conn:
            for entry in objects:
                object_key, revision = entry[:2]
                cursor = conn.execute("""INSERT INTO research_attempts
                    (campaign_id, kind, object_key, input_revision, outcome, started_at)
                    VALUES (?, ?, ?, ?, 'running', ?)""", (key, kind, object_key, revision, _now()))
                attempts.append(cursor.lastrowid)
        return attempts

    def finish_attempts(self, attempts, *, outcome, reason=""):
        if outcome not in {"returned", "completed", "budget_deferred", "execution_failed", "dependency_pending"}:
            raise ValueError("未知研究尝试结果")
        with self.store._connect() as conn:
            for attempt in attempts:
                conn.execute("UPDATE research_attempts SET outcome=?, reason=?, finished_at=? WHERE attempt_id=? AND outcome='running'", (outcome, reason, _now(), attempt))

    def complete_refresh(self, key, keys):
        # Called in the transaction that saves the checked snapshots.
        with self.store._connect() as conn:
            for object_key in keys:
                row = conn.execute("SELECT status FROM paradigms WHERE paradigm_key=?", (object_key,)).fetchone()
                snapshots = self.store.load_candidate_snapshots([object_key])
                if row and row[0] != "pending_deep" and snapshots and self.store.candidate_inputs_current(snapshots[0]):
                    conn.execute("UPDATE campaign_work SET status='completed', reason='checked', updated_at=? WHERE campaign_id=? AND kind='refresh' AND object_key=?", (_now(), key, object_key))

    def reconcile(self, key):
        """Derive receipts from committed state, never from in-memory returns."""
        from database.paradigm_store import _deep_checkpoint_input_signature, _persistable_candidate
        with self.store.transaction():
            with self.store._connect() as conn:
                origins = conn.execute("""SELECT e.fingerprint, o.source_signature, e.last_analyzed_at
                    FROM evidence_state e JOIN origin_research_state o ON e.fingerprint=o.fingerprint
                    WHERE e.evidence_type IN ('primary_paper','technical_blog','concept_essay','original_implementation')""").fetchall()
                pending = [(fp, rev) for fp, rev, done in origins if done is None]
                self.include(key, "origin", pending)
                origin_state = {fp: (rev, done) for fp, rev, done in origins}
                for fp, rev in conn.execute("SELECT object_key, input_revision FROM campaign_work WHERE campaign_id=? AND kind='origin'", (key,)).fetchall():
                    current = origin_state.get(fp)
                    closed = current is not None and current[0] == rev and current[1] is not None
                    conn.execute("UPDATE campaign_work SET status=?, updated_at=? WHERE campaign_id=? AND kind='origin' AND object_key=?", ("completed" if closed else "pending", _now(), key, fp))
                candidates = {row[0]: candidate_from_dict(json.loads(row[1])) for row in conn.execute("SELECT paradigm_key, payload_json FROM paradigms")}
                for object_key, payload in conn.execute("SELECT object_key, payload_json FROM campaign_work WHERE campaign_id=? AND kind IN ('deep','refresh')", (key,)).fetchall():
                    if object_key not in candidates and payload:
                        restored = candidate_from_dict(json.loads(payload))
                        restored.status = "pending_deep"
                        self.store.save_candidates([restored])
                        candidates[object_key] = restored
                # A completed task's policy/model input can change between
                # processes. Reopen its candidate as well as its ledger row;
                # otherwise it would stay pending with no executable queue item.
                for object_key, revision in conn.execute("SELECT object_key, input_revision FROM campaign_work WHERE campaign_id=? AND kind='deep'", (key,)).fetchall():
                    item = candidates.get(object_key)
                    if item is not None and item.status != "pending_deep" and revision != _deep_checkpoint_input_signature(item):
                        item.status = "pending_deep"
                        self.store.save_candidates([item])
                self.include(key, "deep", [(item.key, _deep_checkpoint_input_signature(item), _persistable_candidate(item).to_dict()) for item in candidates.values() if item.status == "pending_deep"])
                for object_key, rev in conn.execute("SELECT object_key, input_revision FROM campaign_work WHERE campaign_id=? AND kind='deep'", (key,)).fetchall():
                    item = candidates.get(object_key)
                    closed = item is not None and item.status != "pending_deep" and self.store.candidate_inputs_current(item) and rev == _deep_checkpoint_input_signature(item)
                    conn.execute("UPDATE campaign_work SET status=?, updated_at=? WHERE campaign_id=? AND kind='deep' AND object_key=?", ("completed" if closed else "pending", _now(), key, object_key))
                    if closed:
                        conn.execute("UPDATE campaign_work SET status='completed', reason='superseded_by_completed_deep', updated_at=? WHERE campaign_id=? AND kind='refresh' AND object_key=?", (_now(), key, object_key))
        return self.snapshot(key)

    def snapshot(self, key):
        with self.store._connect() as conn:
            counts = conn.execute("SELECT kind, status, COUNT(*) FROM campaign_work WHERE campaign_id=? GROUP BY kind, status", (key,)).fetchall()
            failures = conn.execute("SELECT outcome, COUNT(*) FROM research_attempts WHERE campaign_id=? GROUP BY outcome", (key,)).fetchall()
        campaign = self.get(key, load_discovery=False)
        result = {"campaign_id": key, "reference_time": campaign.reference_time.isoformat(), "status": campaign.status, "attempt_count": campaign.attempt_count, "attempt_outcomes": dict(failures)}
        for field in ("reserved_research_seconds", "cumulative_research_seconds", "cumulative_llm_total_tokens", "usage_unknown_attempts", "cumulative_unreported_usage_calls"):
            result[field] = campaign.stats.get(field, 0)
        for kind in ("origin", "deep", "refresh"):
            result[f"{kind}_planned"] = sum(count for lane, _, count in counts if lane == kind)
            result[f"{kind}_completed"] = sum(count for lane, status, count in counts if lane == kind and status == "completed")
            result[f"{kind}_pending"] = result[f"{kind}_planned"] - result[f"{kind}_completed"]
        result["pending_total_count"] = sum(result[f"{kind}_pending"] for kind in ("origin", "deep", "refresh"))
        return result

    def save_stats(self, key, stats, *, ready=False):
        previous = self.get(key, load_discovery=False)
        accounting = {field: previous.stats.get(field, 0) for field in (
            "reserved_research_seconds", "cumulative_research_seconds", "cumulative_llm_total_tokens", "usage_unknown_attempts", "cumulative_unreported_usage_calls")}
        if previous.attempt_count > previous.stats.get("last_accounted_attempt", 0):
            accounting["cumulative_research_seconds"] += stats.get("elapsed_seconds", 0)
            accounting["cumulative_llm_total_tokens"] += stats.get("llm_total_tokens", 0)
            accounting["cumulative_unreported_usage_calls"] += stats.get("llm_unreported_usage_count", 0)
        stats = {"refresh_scope_sealed": previous.stats.get("refresh_scope_sealed", False), **stats,
                 **accounting, "last_accounted_attempt": previous.attempt_count}
        with self.store._connect() as conn:
            conn.execute("UPDATE research_campaigns SET stats_json=?, status=?, updated_at=? WHERE campaign_id=? AND status != 'delivered'", (_encode(stats), "ready" if ready else "researching", _now(), key))

    def bind_delivery(self, key, delivery_key, *, delivered=False):
        self.require_ready(key)
        snapshot = self.reconcile(key)
        if snapshot["pending_total_count"]:
            raise ResearchNotCompleteError(["持久化研究批次仍有未闭合任务"])
        with self.store._connect() as conn:
            conn.execute("UPDATE research_campaigns SET delivery_key=?, status=?, updated_at=? WHERE campaign_id=?", (delivery_key, "delivered" if delivered else "waiting_delivery", _now(), key))

    def require_ready(self, key, candidates=()):
        campaign = self.get(key)
        if campaign is None or campaign.status not in {"ready", "waiting_delivery"} or campaign.discovery is None or not campaign.stats.get("refresh_scope_sealed"):
            raise ResearchNotCompleteError(["持久化研究批次没有有效的完整研究凭据"])
        require_completed_research(campaign.stats)
        require_completed_research({"research_incomplete": False, "coverage_incomplete": False, "frontier_coverage": campaign.discovery.coverage})
        if any(item.key not in self.candidate_keys(key) for item in candidates):
            raise ResearchNotCompleteError(["报告候选不属于已闭合研究批次"])

    def cache_get(self, stage, signature):
        with self.store._connect() as conn:
            row = conn.execute("SELECT result_json FROM research_stage_cache WHERE stage=? AND input_signature=?", (stage, signature)).fetchone()
        return json.loads(row[0]) if row else None

    def cache_save(self, stage, signature, result):
        with self.store._connect() as conn:
            conn.execute("INSERT OR REPLACE INTO research_stage_cache VALUES (?, ?, ?, ?)", (stage, signature, _encode(result), _now()))


def validate(conn):
    for row in conn.execute(f"SELECT {_COLUMNS} FROM research_campaigns"):
        _campaign(row)
    for key, kind, object_key, revision, status, payload in conn.execute("SELECT campaign_id, kind, object_key, input_revision, status, payload_json FROM campaign_work"):
        if not object_key or not revision or kind not in {"origin", "deep", "refresh"} or status not in {"pending", "completed"}:
            raise ValueError("研究任务清单损坏")
        if conn.execute("SELECT 1 FROM research_campaigns WHERE campaign_id=?", (key,)).fetchone() is None:
            raise ValueError("研究任务缺少所属批次")
        if payload:
            item = candidate_from_dict(json.loads(payload)) if kind != "origin" else technical_evidence_from_dict(json.loads(payload))
            if (item.key if kind != "origin" else item.fingerprint) != object_key:
                raise ValueError("研究任务恢复快照身份冲突")
    for outcome, key, kind, identity, revision in conn.execute("SELECT outcome, campaign_id, kind, object_key, input_revision FROM research_attempts"):
        if kind not in {"origin", "deep", "refresh"} or not identity or not revision or outcome not in {"running", "interrupted", "returned", "completed", "budget_deferred", "execution_failed", "dependency_pending"} or conn.execute("SELECT 1 FROM research_campaigns WHERE campaign_id=?", (key,)).fetchone() is None:
            raise ValueError("研究执行尝试账本损坏")
    for stage, signature, payload in conn.execute("SELECT stage, input_signature, result_json FROM research_stage_cache"):
        if not stage or not signature or not isinstance(json.loads(payload), dict):
            raise ValueError("研究子阶段缓存损坏")
