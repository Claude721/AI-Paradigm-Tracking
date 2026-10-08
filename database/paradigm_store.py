"""技术范式、证据与跨周交付状态的 SQLite 存储。"""

from __future__ import annotations

import hashlib
import copy
import json
import logging
import re
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

import config
from database import origin_state
from paradigms.landscape import load_landscape
from paradigms.models import (
    ORIGIN_EVIDENCE_TYPES,
    ParadigmCandidate,
    TechnicalEvidence,
    candidate_from_dict,
    scrub_ephemeral_evidence,
    technical_evidence_from_dict,
)
from paradigms.researcher_identity import merge_researcher_profiles
from paradigms.completion import ResearchNotCompleteError, require_completed_research

logger = logging.getLogger(__name__)

DEEP_SYNTHESIS_CHECKPOINT_VERSION = 1
DEEP_TRAJECTORY_CHECKPOINT_VERSION = 1
DEEP_SYNTHESIS_CHECKPOINT_MAX_AGE = timedelta(hours=48)


@dataclass
class ReportOutboxJob:
    delivery_key: str
    report_date: str
    report_name: str
    status: str
    candidates: list[ParadigmCandidate]
    stats: dict
    report_content: str = ""
    attempt_count: int = 0
    render_attempt_count: int = 0
    last_error: str = ""
    failure_kind: str = ""
    quarantined_at: str = ""


@dataclass
class EvidenceCheckpointResult:
    """Result of a loss-contained evidence persistence checkpoint."""

    accepted: list[TechnicalEvidence]
    rejected_count: int = 0
    rejection_sources: dict[str, int] = field(default_factory=dict)
    stale_revision_count: int = 0

    @property
    def written_count(self) -> int:
        return len(self.accepted)


class RouteHistoryIndex:
    """Run-local matching view, updated only after an origin transaction commits.

    Route similarity is exactly zero unless two route tokens overlap. Indexing
    those tokens avoids comparing each extracted mechanism with every historical
    route, while leaving the conservative similarity decision unchanged.
    """

    def __init__(self, historical: list[tuple[str, ParadigmCandidate]]):
        self._routes: dict[str, ParadigmCandidate] = {}
        self._postings: dict[str, set[str]] = {}
        self._tokens: dict[str, set[str]] = {}
        self._order: dict[str, int] = {}
        for key, candidate in historical:
            self.upsert(candidate, key=key)

    def contains(self, key: str) -> bool:
        return key in self._routes

    def get(self, key: str) -> ParadigmCandidate | None:
        return self._routes.get(key)

    def relevant(self, candidate: ParadigmCandidate) -> list[tuple[str, ParadigmCandidate]]:
        counts: dict[str, int] = {}
        for token in _candidate_route_tokens(candidate):
            for key in self._postings.get(token, ()):
                counts[key] = counts.get(key, 0) + 1
        keys = sorted(
            (key for key, count in counts.items() if count >= 2),
            key=self._order.__getitem__,
        )
        return [(key, self._routes[key]) for key in keys]

    def upsert(self, candidate: ParadigmCandidate, *, key: str | None = None) -> None:
        route_key = key or candidate.key
        for token in self._tokens.get(route_key, ()):
            posting = self._postings[token]
            posting.discard(route_key)
            if not posting:
                del self._postings[token]
        if candidate.status == "rejected":
            self._routes.pop(route_key, None)
            self._tokens.pop(route_key, None)
            return
        tokens = _candidate_route_tokens(candidate)
        if route_key not in self._order:
            self._order[route_key] = len(self._order)
        self._routes[route_key] = candidate
        self._tokens[route_key] = tokens
        for token in tokens:
            self._postings.setdefault(token, set()).add(route_key)


class ParadigmStore:
    def __init__(self, db_path: Path | str | None = None):
        self.db_path = Path(db_path) if db_path else config.PARADIGM_DB_PATH
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    @contextmanager
    def _connect(self):
        active = getattr(self, "_active_transaction", None)
        if active is not None:
            yield active
            return
        connection = sqlite3.connect(self.db_path, timeout=30)
        connection.execute("PRAGMA busy_timeout=30000")
        try:
            yield connection
            connection.commit()
        finally:
            connection.close()

    @contextmanager
    def transaction(self):
        """Nest store operations into one atomic, version-checked checkpoint."""
        if getattr(self, "_active_transaction", None) is not None:
            yield
            return
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            self._active_transaction = conn
            try:
                yield
            finally:
                self._active_transaction = None

    def _init_db(self) -> None:
        with self._connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS evidence_state (
                    fingerprint TEXT PRIMARY KEY,
                    content_signature TEXT NOT NULL,
                    source TEXT NOT NULL,
                    evidence_type TEXT NOT NULL,
                    url TEXT,
                    payload_json TEXT NOT NULL,
                    first_seen_at TEXT NOT NULL,
                    last_seen_at TEXT NOT NULL,
                    last_analyzed_at TEXT
                );
                CREATE TABLE IF NOT EXISTS paradigms (
                    paradigm_key TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    status TEXT NOT NULL,
                    total_score REAL NOT NULL,
                    payload_json TEXT NOT NULL,
                    first_seen_at TEXT NOT NULL,
                    last_seen_at TEXT NOT NULL,
                    last_refresh_attempt_at TEXT,
                    last_reported_signature TEXT,
                    last_reported_at TEXT
                );
                CREATE TABLE IF NOT EXISTS report_deliveries (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    paradigm_key TEXT NOT NULL,
                    report_signature TEXT NOT NULL,
                    report_path TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    delivered_at TEXT NOT NULL,
                    report_kind TEXT NOT NULL,
                    delivery_key TEXT NOT NULL DEFAULT '',
                    UNIQUE(paradigm_key, report_signature)
                );
                CREATE TABLE IF NOT EXISTS report_outbox (
                    delivery_key TEXT PRIMARY KEY,
                    report_date TEXT NOT NULL,
                    report_name TEXT NOT NULL,
                    status TEXT NOT NULL,
                    candidate_payload_json TEXT NOT NULL,
                    stats_json TEXT NOT NULL,
                    report_content TEXT NOT NULL DEFAULT '',
                    attempt_count INTEGER NOT NULL DEFAULT 0,
                    render_attempt_count INTEGER NOT NULL DEFAULT 0,
                    last_error TEXT NOT NULL DEFAULT '',
                    failure_kind TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    delivered_at TEXT,
                    quarantined_at TEXT
                );
                CREATE TABLE IF NOT EXISTS report_render_fragments (
                    delivery_key TEXT NOT NULL,
                    fragment_key TEXT NOT NULL,
                    content TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY(delivery_key, fragment_key)
                );
                CREATE TABLE IF NOT EXISTS paradigm_evidence (
                    paradigm_key TEXT NOT NULL,
                    fingerprint TEXT NOT NULL,
                    first_linked_at TEXT NOT NULL,
                    PRIMARY KEY(paradigm_key, fingerprint)
                );
                CREATE TABLE IF NOT EXISTS radar_meta (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_evidence_last_seen
                    ON evidence_state(last_seen_at DESC);
                CREATE INDEX IF NOT EXISTS idx_paradigm_score
                    ON paradigms(total_score DESC);
                CREATE INDEX IF NOT EXISTS idx_report_outbox_status
                    ON report_outbox(status, created_at);
                CREATE INDEX IF NOT EXISTS idx_report_fragments_delivery
                    ON report_render_fragments(delivery_key);
                """
            )
            # Version 3 adds a delivery identifier to legacy delivery rows.
            # CREATE TABLE IF NOT EXISTS does not add columns to an existing table.
            delivery_columns = {
                str(row[1])
                for row in conn.execute("PRAGMA table_info(report_deliveries)")
            }
            if "delivery_key" not in delivery_columns:
                conn.execute(
                    "ALTER TABLE report_deliveries "
                    "ADD COLUMN delivery_key TEXT NOT NULL DEFAULT ''"
                )
            paradigm_columns = {
                str(row[1])
                for row in conn.execute("PRAGMA table_info(paradigms)")
            }
            if "last_refresh_attempt_at" not in paradigm_columns:
                conn.execute(
                    "ALTER TABLE paradigms ADD COLUMN last_refresh_attempt_at TEXT"
                )
            outbox_columns = {
                str(row[1])
                for row in conn.execute("PRAGMA table_info(report_outbox)")
            }
            if "render_attempt_count" not in outbox_columns:
                conn.execute(
                    "ALTER TABLE report_outbox ADD COLUMN "
                    "render_attempt_count INTEGER NOT NULL DEFAULT 0"
                )
            if "failure_kind" not in outbox_columns:
                conn.execute(
                    "ALTER TABLE report_outbox ADD COLUMN "
                    "failure_kind TEXT NOT NULL DEFAULT ''"
                )
            if "quarantined_at" not in outbox_columns:
                conn.execute(
                    "ALTER TABLE report_outbox ADD COLUMN quarantined_at TEXT"
                )
            origin_state.initialize(conn)

    def is_bootstrap_required(self) -> bool:
        """空状态或覆盖地图升级时使用较长发现窗口。"""
        current_landscape = str(load_landscape()["version"])
        with self._connect() as conn:
            evidence = conn.execute(
                """
                SELECT 1 FROM evidence_state
                WHERE evidence_type IN (
                    'primary_paper', 'technical_blog',
                    'concept_essay', 'original_implementation'
                )
                LIMIT 1
                """
            ).fetchone()
            paradigm = conn.execute("SELECT 1 FROM paradigms LIMIT 1").fetchone()
            landscape = conn.execute(
                "SELECT value FROM radar_meta WHERE key='frontier_landscape_version'"
            ).fetchone()
        return (
            (evidence is None and paradigm is None)
            or landscape is None
            or str(landscape[0]) != current_landscape
        )

    def work_queue_snapshot(self, *, reference_time: datetime) -> dict[str, int]:
        """Count durable unfinished work without deserializing the backlog."""
        with self._connect() as conn:
            origins = conn.execute(
                """
                SELECT COUNT(*), MIN(first_seen_at) FROM evidence_state
                WHERE last_analyzed_at IS NULL
                  AND evidence_type IN (
                      'primary_paper', 'technical_blog',
                      'concept_essay', 'original_implementation'
                  )
                """
            ).fetchone()
            deep = conn.execute(
                """
                SELECT COUNT(*), MIN(first_seen_at) FROM paradigms
                WHERE status='pending_deep'
                """
            ).fetchone()

        def age_days(value: str | None) -> int:
            if not value:
                return 0
            try:
                seen = datetime.fromisoformat(value.replace("Z", "+00:00"))
                if seen.tzinfo is None:
                    seen = seen.replace(tzinfo=timezone.utc)
                return max(int((reference_time - seen).total_seconds() // 86400), 0)
            except ValueError:
                return 0

        origin_count, origin_oldest = origins
        deep_count, deep_oldest = deep
        return {
            "pending_origin_count": int(origin_count),
            "pending_deep_count": int(deep_count),
            "pending_total_count": int(origin_count + deep_count),
            "oldest_pending_age_days": max(
                age_days(origin_oldest), age_days(deep_oldest)
            ),
        }

    def mark_landscape_version(self, version: str = "") -> None:
        """只在本轮完整研究成功后登记覆盖基线版本。"""
        value = version or str(load_landscape()["version"])
        now = datetime.now(timezone.utc).isoformat()
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO radar_meta (key, value, updated_at)
                VALUES ('frontier_landscape_version', ?, ?)
                ON CONFLICT(key) DO UPDATE SET
                    value=excluded.value,
                    updated_at=excluded.updated_at
                """,
                (value, now),
            )

    def plan_origins(
        self, origins: list[TechnicalEvidence]
    ) -> tuple[list[TechnicalEvidence], dict[str, int]]:
        """只让新论文或正文实质变化的论文再次进入 LLM。"""
        if not origins:
            return [], {"new": 0, "changed": 0, "unchanged_skip": 0}
        fingerprints = [item.fingerprint for item in origins]
        placeholders = ",".join("?" for _ in fingerprints)
        with self._connect() as conn:
            rows = conn.execute(
                f"SELECT fingerprint, content_signature, last_analyzed_at, payload_json "
                f"FROM evidence_state "
                f"WHERE fingerprint IN ({placeholders})",
                fingerprints,
            ).fetchall()
        existing = {
            fingerprint: (signature, last_analyzed_at, payload_json)
            for fingerprint, signature, last_analyzed_at, payload_json in rows
        }
        with self._connect() as conn:
            source_states = {item.fingerprint: origin_state.read(conn, item.fingerprint) for item in origins}
        selected = []
        stats = {"new": 0, "changed": 0, "unchanged_skip": 0}
        for item in origins:
            signature = _content_signature(item)
            previous = existing.get(item.fingerprint)
            source_state = source_states.get(item.fingerprint)
            unchanged = bool(previous and (
                origin_state.same_source(item, source_state) if source_state else previous[0] == signature
            ))
            item.source_revision = source_state[0] if unchanged and source_state else ""
            if previous is None:
                selected.append(item)
                stats["new"] += 1
            elif previous[1] is None:
                if unchanged:
                    if source_state:
                        origin_state.apply_checkpoint(item, json.loads(source_state[2]))
                    else:
                        _restore_execution_metadata(item, previous[2])
                selected.append(item)
                stats["new"] += 1
            elif not unchanged:
                selected.append(item)
                stats["changed"] += 1
            else:
                stats["unchanged_skip"] += 1
        return selected, stats

    def observe_origins(self, origins):
        """Register all fresh source snapshots, returning only work needing analysis."""
        valid = []
        rejected_sources: dict[str, int] = {}
        for item in origins:
            try:
                canonical = technical_evidence_from_dict(item.to_dict())
                json.dumps(canonical.to_dict())
                canonical.fingerprint
                valid.append(canonical)
            except (AttributeError, TypeError, ValueError, OverflowError):
                source = re.sub(r"[^A-Za-z0-9_.:-]+", "_", str(getattr(item, "source", "unknown")))[:64]
                rejected_sources[source] = rejected_sources.get(source, 0) + 1
        with self.transaction():
            selected, stats = self.plan_origins(valid)
            selected_keys = {item.fingerprint for item in selected}
            checkpoint = self.mark_evidence(valid, source_observation=True)
        for source, count in rejected_sources.items():
            checkpoint.rejection_sources[source] = checkpoint.rejection_sources.get(source, 0) + count
            checkpoint.rejected_count += count
        selected = [item for item in checkpoint.accepted if item.fingerprint in selected_keys]
        return selected, stats, checkpoint

    def mark_evidence(
        self, evidence: list[TechnicalEvidence], analyzed: bool = False,
        *, source_observation: bool = False, enrichment_only: bool = False,
    ) -> EvidenceCheckpointResult:
        """Validate every evidence object before opening the write transaction.

        One malformed external record must not poison the SQLite state or roll
        back healthy peers.  Nullable optional values are normalized by the
        domain model; remaining structural violations are excluded from this
        checkpoint and reported by source without logging external content.
        """

        accepted: list[TechnicalEvidence] = []
        prepared: list[tuple[TechnicalEvidence, str, str]] = []
        rejection_sources: dict[str, int] = {}
        for item in evidence:
            raw_source = getattr(item, "source", "")
            source = raw_source.strip() if isinstance(raw_source, str) else ""
            source = re.sub(r"[^A-Za-z0-9_.:-]+", "_", source)[:64] or "unknown"
            try:
                canonical = technical_evidence_from_dict(item.to_dict())
                scrub_ephemeral_evidence(canonical)
                payload = json.dumps(
                    canonical.to_dict(),
                    ensure_ascii=False,
                    sort_keys=True,
                )
                signature = _content_signature(canonical)
                # Compute the fingerprint before the transaction as well; a
                # malformed required scalar must not leave a partial batch.
                canonical.fingerprint
            except (AttributeError, TypeError, ValueError, OverflowError) as exc:
                rejection_sources[source] = rejection_sources.get(source, 0) + 1
                logger.error(
                    "证据持久化契约拒绝一条记录 source=%s error=%s",
                    source,
                    type(exc).__name__,
                )
                continue
            accepted.append(canonical)
            prepared.append((canonical, payload, signature))

        if source_observation and enrichment_only:
            raise ValueError("来源观察与研究增强必须分别提交")
        if analyzed and (source_observation or enrichment_only):
            raise ValueError("来源观察或研究增强不能提交原点完成标记")
        now = datetime.now(timezone.utc).isoformat()
        written = []
        stale_count = 0
        state_rejected = 0
        with self._connect() as conn:
            if not conn.in_transaction:
                conn.execute("BEGIN IMMEDIATE")
            for item, payload, signature in prepared:
                invalidates_analysis = False
                if item.evidence_type in ORIGIN_EVIDENCE_TYPES:
                    try:
                        resolved = origin_state.write(
                            conn, item, source_observation=source_observation,
                            enrichment_only=enrichment_only,
                        )
                    except (TypeError, ValueError, OverflowError):
                        source = re.sub(r"[^A-Za-z0-9_.:-]+", "_", item.source)[:64] or "unknown"
                        rejection_sources[source] = rejection_sources.get(source, 0) + 1
                        state_rejected += 1
                        continue
                    if resolved is None:
                        stale_count += 1
                        continue
                    item, signature, invalidates_analysis = resolved
                    payload = json.dumps(item.to_dict(), ensure_ascii=False, sort_keys=True)
                else:
                    previous = conn.execute("SELECT content_signature FROM evidence_state WHERE fingerprint=?", (item.fingerprint,)).fetchone()
                    invalidates_analysis = bool(previous and previous[0] != signature)
                analyzed_at = now if analyzed else None
                conn.execute(
                    """
                    INSERT INTO evidence_state (
                        fingerprint, content_signature, source, evidence_type,
                        url, payload_json, first_seen_at, last_seen_at, last_analyzed_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(fingerprint) DO UPDATE SET
                        content_signature=excluded.content_signature,
                        source=excluded.source,
                        evidence_type=excluded.evidence_type,
                        url=excluded.url,
                        payload_json=excluded.payload_json,
                        last_seen_at=excluded.last_seen_at,
                        last_analyzed_at=CASE
                            WHEN excluded.last_analyzed_at IS NOT NULL
                                THEN excluded.last_analyzed_at
                            WHEN ?
                                THEN NULL
                            ELSE evidence_state.last_analyzed_at
                        END
                    """,
                    (
                        item.fingerprint,
                        signature,
                        item.source,
                        item.evidence_type.value,
                        item.url,
                        payload,
                        now,
                        now,
                        analyzed_at,
                        invalidates_analysis,
                    ),
                )
                written.append(item)
        return EvidenceCheckpointResult(
            accepted=written,
            rejected_count=len(evidence) - len(accepted) + state_rejected,
            rejection_sources=rejection_sources,
            stale_revision_count=stale_count,
        )

    def load_pending_origins(
        self,
        exclude_fingerprints: set[str] | None = None,
        limit: int | None = None,
    ) -> list[TechnicalEvidence]:
        """恢复已发现但尚未做机制抽取的原始材料，避免跨出窗口后丢失。"""
        excluded = exclude_fingerprints or set()
        target_limit = limit if limit is not None and limit > 0 else None
        limit_sql = "LIMIT ?" if target_limit else ""
        params = (
            (target_limit + len(excluded),) if target_limit is not None else ()
        )
        with self._connect() as conn:
            rows = conn.execute(
                f"""
                SELECT payload_json FROM evidence_state
                WHERE last_analyzed_at IS NULL
                  AND evidence_type IN (
                      'primary_paper', 'technical_blog',
                      'concept_essay', 'original_implementation'
                  )
                ORDER BY first_seen_at ASC
                {limit_sql}
                """,
                params,
            ).fetchall()
        results = []
        for row in rows:
            item = technical_evidence_from_dict(json.loads(row[0]))
            if item.fingerprint in excluded:
                continue
            results.append(item)
            if target_limit is not None and len(results) >= target_limit:
                break
        return results

    def prepare_report(
        self, candidates: list[ParadigmCandidate]
    ) -> list[ParadigmCandidate]:
        """同一范式+同一证据签名绝不跨周重复；有实质新证据时标为更新。"""
        selected = []
        with self._connect() as conn:
            for candidate in candidates:
                row = conn.execute(
                    "SELECT last_reported_signature FROM paradigms WHERE paradigm_key=?",
                    (candidate.key,),
                ).fetchone()
                previous = row[0] if row else None
                if previous == candidate.report_signature:
                    continue
                if previous:
                    if not config.PARADIGM_ALLOW_UPDATES:
                        continue
                    candidate.report_kind = "update"
                else:
                    candidate.report_kind = (
                        "update"
                        if candidate.freshness_assessment.get("classification")
                        == "historical_reactivated"
                        else "new"
                    )
                selected.append(candidate)
        return selected

    def enqueue_report(
        self,
        candidates: list[ParadigmCandidate],
        stats: dict,
        *,
        report_date: str,
    ) -> ReportOutboxJob:
        """Durably record research-ready material before report rendering.

        The delivery key is deterministic for a date and candidate-signature set.
        Retrying the same weekly result therefore reuses one outbox item instead of
        creating another email after a renderer or SMTP failure.
        """

        require_completed_research(stats, candidates=candidates)
        # Verify the durable queue too, not merely the caller's summary flags.
        with self.transaction():
            require_completed_research({
                **stats,
                "work_queue_after": self.work_queue_snapshot(reference_time=datetime.now(timezone.utc)),
            })
            if any(not self.candidate_inputs_current(item) for item in candidates):
                raise ResearchNotCompleteError(["候选一手来源版本与当前研究记录不一致"])
            return self._enqueue_completed_report(candidates, stats, report_date=report_date)

    def _enqueue_completed_report(
        self, candidates: list[ParadigmCandidate], stats: dict, *, report_date: str,
    ) -> ReportOutboxJob:
        candidates = [_persistable_candidate(item) for item in candidates]

        report_name = f"paradigm_radar_{report_date}.md"
        identity = {
            "report_date": report_date,
            "candidates": sorted(
                (candidate.key, candidate.report_signature)
                for candidate in candidates
            ),
        }
        # 完整空报告同样是一项研究结果；不同完整召回结果使用不同幂等键。
        if not candidates:
            identity["empty_result_basis"] = {
                key: stats.get(key)
                for key in (
                    "origin_count",
                    "planned_analysis_count",
                    "analysis_completed_count",
                    "pending_work_count",
                    "run_incomplete",
                    "frontier_coverage",
                )
            }
        delivery_key = hashlib.sha256(
            json.dumps(identity, ensure_ascii=False, sort_keys=True).encode("utf-8")
        ).hexdigest()
        candidate_payload = json.dumps(
            [candidate.to_dict() for candidate in candidates],
            ensure_ascii=False,
            sort_keys=True,
        )
        stats_payload = json.dumps(stats, ensure_ascii=False, sort_keys=True)
        now = datetime.now(timezone.utc).isoformat()
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO report_outbox (
                    delivery_key, report_date, report_name, status,
                    candidate_payload_json, stats_json, created_at, updated_at
                ) VALUES (?, ?, ?, 'pending_render', ?, ?, ?, ?)
                ON CONFLICT(delivery_key) DO UPDATE SET
                    candidate_payload_json=CASE
                        WHEN report_outbox.status = 'delivered'
                            THEN report_outbox.candidate_payload_json
                        ELSE excluded.candidate_payload_json
                    END,
                    stats_json=CASE
                        WHEN report_outbox.status = 'delivered'
                            THEN report_outbox.stats_json
                        ELSE excluded.stats_json
                    END,
                    status=CASE
                        WHEN report_outbox.status = 'delivered'
                            THEN report_outbox.status
                        ELSE 'pending_render'
                    END,
                    render_attempt_count=CASE
                        WHEN report_outbox.status = 'quarantined' THEN 0
                        ELSE report_outbox.render_attempt_count
                    END,
                    last_error=CASE
                        WHEN report_outbox.status = 'quarantined' THEN ''
                        ELSE report_outbox.last_error
                    END,
                    failure_kind=CASE
                        WHEN report_outbox.status = 'quarantined' THEN ''
                        ELSE report_outbox.failure_kind
                    END,
                    quarantined_at=CASE
                        WHEN report_outbox.status = 'quarantined' THEN NULL
                        ELSE report_outbox.quarantined_at
                    END,
                    updated_at=excluded.updated_at
                """,
                (
                    delivery_key,
                    report_date,
                    report_name,
                    candidate_payload,
                    stats_payload,
                    now,
                    now,
                ),
            )
        job = self.get_report_job(delivery_key)
        assert job is not None
        return job

    def get_report_job(self, delivery_key: str) -> ReportOutboxJob | None:
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT delivery_key, report_date, report_name, status,
                       candidate_payload_json, stats_json, report_content,
                       attempt_count, render_attempt_count, last_error,
                       failure_kind, quarantined_at
                FROM report_outbox WHERE delivery_key=?
                """,
                (delivery_key,),
            ).fetchone()
        return _report_job_from_row(row) if row else None

    def load_pending_report_job(self) -> ReportOutboxJob | None:
        """Return the oldest research-ready report that is not yet delivered."""

        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT delivery_key, report_date, report_name, status,
                       candidate_payload_json, stats_json, report_content,
                       attempt_count, render_attempt_count, last_error,
                       failure_kind, quarantined_at
                FROM report_outbox
                WHERE status IN ('pending_render', 'rendered', 'sending')
                ORDER BY created_at ASC
                LIMIT 1
                """
            ).fetchone()
        return _report_job_from_row(row) if row else None

    def latest_completed_report_job(self) -> ReportOutboxJob | None:
        """Historical regeneration uses the original closed run, not today's stats."""
        with self._connect() as conn:
            rows = conn.execute(
                """SELECT delivery_key, report_date, report_name, status,
                       candidate_payload_json, stats_json, report_content,
                       attempt_count, render_attempt_count, last_error,
                       failure_kind, quarantined_at
                FROM report_outbox WHERE status='delivered'
                ORDER BY delivered_at DESC, created_at DESC"""
            ).fetchall()
        for row in rows:
            job = _report_job_from_row(row)
            try:
                require_completed_research(job.stats, content=job.report_content, candidates=job.candidates)
            except ResearchNotCompleteError:
                continue
            return job
        return None

    def save_rendered_report(self, delivery_key: str, content: str) -> None:
        now = datetime.now(timezone.utc).isoformat()
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE report_outbox
                SET report_content=?, status='rendered', last_error='',
                    failure_kind='', updated_at=?
                WHERE delivery_key=?
                  AND status IN ('pending_render', 'rendered', 'sending')
                """,
                (content, now, delivery_key),
            )

    def invalidate_rendered_report(self, delivery_key: str, reason: str) -> None:
        """Return a stale rendered artifact to the renderer after contract changes."""

        now = datetime.now(timezone.utc).isoformat()
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE report_outbox
                SET report_content='', status='pending_render', last_error=?,
                    failure_kind='stale_render_contract', updated_at=?
                WHERE delivery_key=?
                  AND status IN ('pending_render', 'rendered', 'sending')
                """,
                (_safe_error_text(reason), now, delivery_key),
            )

    def load_report_fragments(self, delivery_key: str) -> dict[str, str]:
        """恢复已通过路线级闸门的总编辑中间制品。"""

        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT fragment_key, content FROM report_render_fragments
                WHERE delivery_key=? ORDER BY fragment_key
                """,
                (delivery_key,),
            ).fetchall()
        return {str(key): str(content) for key, content in rows}

    def save_report_fragment(
        self,
        delivery_key: str,
        fragment_key: str,
        content: str,
    ) -> None:
        """单条路线起草成功后立即落盘，后续超时不重新消耗它。"""

        now = datetime.now(timezone.utc).isoformat()
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO report_render_fragments (
                    delivery_key, fragment_key, content, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(delivery_key, fragment_key) DO UPDATE SET
                    content=excluded.content,
                    updated_at=excluded.updated_at
                """,
                (delivery_key, fragment_key, content, now, now),
            )

    def begin_delivery_attempt(self, delivery_key: str) -> None:
        now = datetime.now(timezone.utc).isoformat()
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE report_outbox
                SET status='sending', attempt_count=attempt_count + 1,
                    last_error='', failure_kind='', updated_at=?
                WHERE delivery_key=?
                  AND status IN ('pending_render', 'rendered', 'sending')
                """,
                (now, delivery_key),
            )

    def begin_render_attempt(self, delivery_key: str) -> int:
        """Increment and return the durable renderer attempt counter."""

        now = datetime.now(timezone.utc).isoformat()
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE report_outbox
                SET render_attempt_count=render_attempt_count + 1,
                    last_error='', failure_kind='', updated_at=?
                WHERE delivery_key=?
                  AND status IN ('pending_render', 'rendered', 'sending')
                """,
                (now, delivery_key),
            )
            row = conn.execute(
                "SELECT render_attempt_count FROM report_outbox "
                "WHERE delivery_key=?",
                (delivery_key,),
            ).fetchone()
        if row is None:
            raise ValueError(f"不存在交付任务: {delivery_key}")
        return int(row[0] or 0)

    def record_delivery_failure(
        self,
        delivery_key: str,
        error: Exception | str,
        *,
        rendered: bool,
        failure_kind: str = "delivery_transient",
    ) -> None:
        now = datetime.now(timezone.utc).isoformat()
        retry_status = "rendered" if rendered else "pending_render"
        safe_error = _safe_error_text(error)
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE report_outbox
                SET status=?, last_error=?, failure_kind=?, updated_at=?
                WHERE delivery_key=?
                  AND status IN ('pending_render', 'rendered', 'sending')
                """,
                (retry_status, safe_error, failure_kind, now, delivery_key),
            )

    def quarantine_report_job(
        self,
        delivery_key: str,
        error: Exception | str,
        *,
        failure_kind: str,
        requeue_candidates: bool = True,
    ) -> None:
        """Isolate an unrecoverable snapshot and atomically requeue its research.

        A quarantined outbox item remains available for audit, but no longer
        blocks newer delivery work.  Its candidates return to ``pending_deep``
        in the same SQLite transaction, so a later research run can rebuild the
        missing person/source contract instead of silently dropping the route.
        """

        now = datetime.now(timezone.utc).isoformat()
        safe_error = _safe_error_text(error)
        with self._connect() as conn:
            row = conn.execute(
                "SELECT candidate_payload_json FROM report_outbox "
                "WHERE delivery_key=?",
                (delivery_key,),
            ).fetchone()
            if row is None:
                raise ValueError(f"不存在交付任务: {delivery_key}")
            candidates = [
                _persistable_candidate(candidate_from_dict(value))
                for value in json.loads(row[0])
            ]
            for candidate in candidates:
                if not requeue_candidates and conn.execute(
                    "SELECT 1 FROM paradigms WHERE paradigm_key=?", (candidate.key,)
                ).fetchone() is not None:
                    # Retiring an old partial delivery must not overwrite newer
                    # research. Recreate only an otherwise missing snapshot.
                    continue
                candidate.status = "pending_deep"
                payload = json.dumps(
                    candidate.to_dict(),
                    ensure_ascii=False,
                    sort_keys=True,
                )
                conn.execute(
                    """
                    INSERT INTO paradigms (
                        paradigm_key, name, status, total_score, payload_json,
                        first_seen_at, last_seen_at
                    ) VALUES (?, ?, 'pending_deep', ?, ?, ?, ?)
                    ON CONFLICT(paradigm_key) DO UPDATE SET
                        name=excluded.name,
                        status='pending_deep',
                        total_score=excluded.total_score,
                        payload_json=excluded.payload_json,
                        last_seen_at=excluded.last_seen_at
                    """,
                    (
                        candidate.key,
                        candidate.name,
                        candidate.total_score,
                        payload,
                        now,
                        now,
                    ),
                )
                for evidence in candidate.evidence:
                    conn.execute(
                        """
                        INSERT OR IGNORE INTO paradigm_evidence (
                            paradigm_key, fingerprint, first_linked_at
                        ) VALUES (?, ?, ?)
                        """,
                        (
                            candidate.key,
                            evidence.fingerprint,
                            now,
                        ),
                    )
            conn.execute(
                """
                UPDATE report_outbox
                SET status='quarantined', last_error=?, failure_kind=?,
                    quarantined_at=?, updated_at=?
                WHERE delivery_key=? AND status != 'delivered'
                """,
                (safe_error, failure_kind, now, now, delivery_key),
            )
            conn.execute(
                "DELETE FROM report_render_fragments WHERE delivery_key=?",
                (delivery_key,),
            )

    def mark_delivery_delivered(
        self, delivery_key: str, report_path: Path
    ) -> None:
        """Atomically mark the outbox item and its route signatures delivered."""

        now = datetime.now(timezone.utc).isoformat()
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT candidate_payload_json, stats_json, report_content FROM report_outbox
                WHERE delivery_key=?
                """,
                (delivery_key,),
            ).fetchone()
            if row is None:
                raise ValueError(f"不存在交付任务: {delivery_key}")
            candidates = [
                candidate_from_dict(value) for value in json.loads(row[0])
            ]
            require_completed_research(json.loads(row[1]), content=row[2], candidates=candidates)
            for candidate in candidates:
                self._mark_candidate_reported(
                    conn,
                    candidate,
                    report_path,
                    now,
                    delivery_key=delivery_key,
                )
            conn.execute(
                """
                UPDATE report_outbox
                SET status='delivered', delivered_at=?, updated_at=?,
                    last_error='', failure_kind='', report_content='',
                    quarantined_at=NULL
                WHERE delivery_key=?
                """,
                (now, now, delivery_key),
            )
            conn.execute(
                "DELETE FROM report_render_fragments WHERE delivery_key=?",
                (delivery_key,),
            )

    def build_route_history_index(self) -> RouteHistoryIndex:
        """Read historical route identities once for a stream of origin commits."""
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT paradigm_key, payload_json FROM paradigms
                WHERE status != 'rejected'
                """
            ).fetchall()
        return RouteHistoryIndex([
            (key, candidate_from_dict(json.loads(payload)))
            for key, payload in rows
        ])

    def attach_history(
        self,
        candidates: list[ParadigmCandidate],
        *,
        history_index: RouteHistoryIndex | None = None,
    ) -> list[ParadigmCandidate]:
        """先与跨周路线图对齐，再附回历史证据。

        不能只依赖模型本周生成的 canonical key。相同能力边界可能随着论文
        术语变化而改名；这里用路线、问题、机制词和覆盖领域做保守匹配。
        """
        index = history_index or self.build_route_history_index()
        with self._connect() as conn:
            for candidate in candidates:
                previous = index.get(candidate.key)
                if previous is None:
                    matches = [
                        (_route_similarity(candidate, item), key, item)
                        for key, item in index.relevant(candidate)
                    ]
                    score, key, previous = max(
                        matches,
                        default=(0.0, "", None),
                        key=lambda item: item[0],
                    )
                    if previous is None or score < 0.42:
                        previous = None
                    else:
                        candidate.key = key
                if previous is not None:
                    candidate.lineage_path = list(
                        dict.fromkeys(
                            [
                                *previous.lineage_path,
                                *candidate.lineage_path,
                            ]
                        )
                    )
                    candidate.researchers = merge_researcher_profiles(
                        previous.researchers, candidate.researchers
                    )
                    existing = {item.fingerprint for item in candidate.evidence}
                    for evidence in previous.evidence:
                        if evidence.fingerprint not in existing:
                            retained = copy.deepcopy(evidence)
                            retained.raw["historical"] = True
                            candidate.evidence.append(retained)
                            existing.add(evidence.fingerprint)

            candidates = _merge_same_key_candidates(candidates)
            for candidate in candidates:
                rows = conn.execute(
                    """
                    SELECT e.payload_json
                    FROM paradigm_evidence pe
                    JOIN evidence_state e ON e.fingerprint = pe.fingerprint
                    WHERE pe.paradigm_key=?
                    ORDER BY pe.first_linked_at ASC
                    """,
                    (candidate.key,),
                ).fetchall()
                existing = {item.fingerprint for item in candidate.evidence}
                for row in rows:
                    item = technical_evidence_from_dict(json.loads(row[0]))
                    if item.fingerprint in existing:
                        continue
                    item.raw = {**item.raw, "historical": True}
                    candidate.evidence.append(item)
                    existing.add(item.fingerprint)
        return candidates

    def load_refresh_candidates(
        self,
        exclude_keys: set[str] | None = None,
        limit: int | None = None,
    ) -> list[ParadigmCandidate]:
        """载入观察中/已报告路线，供本周新增讨论重新触发评估。"""
        excluded = exclude_keys or set()
        configured = config.PARADIGM_REFRESH_SAFETY_LIMIT
        target_limit = limit if limit is not None else configured
        target_limit = target_limit if target_limit and target_limit > 0 else None
        limit_sql = "LIMIT ?" if target_limit else ""
        params = (
            (target_limit + len(excluded),) if target_limit is not None else ()
        )
        with self._connect() as conn:
            rows = conn.execute(
                f"""
                SELECT payload_json FROM paradigms
                WHERE status NOT IN ('rejected', 'pending_deep')
                ORDER BY COALESCE(last_refresh_attempt_at, first_seen_at) ASC,
                         first_seen_at ASC
                {limit_sql}
                """,
                params,
            ).fetchall()
        candidates = []
        for row in rows:
            candidate = candidate_from_dict(json.loads(row[0]))
            if candidate.key in excluded:
                continue
            for evidence in candidate.evidence:
                evidence.raw = {**evidence.raw, "historical": True}
            candidates.append(candidate)
            if target_limit is not None and len(candidates) >= target_limit:
                break
        return candidates

    def mark_refresh_attempted(self, candidates: list[ParadigmCandidate]) -> None:
        """Rotate bounded refresh work fairly without changing research facts."""

        keys = list(dict.fromkeys(candidate.key for candidate in candidates))
        if not keys:
            return
        now = datetime.now(timezone.utc).isoformat()
        with self._connect() as conn:
            conn.executemany(
                """
                UPDATE paradigms SET last_refresh_attempt_at=?
                WHERE paradigm_key=?
                """,
                ((now, key) for key in keys),
            )

    def load_pending_deep_candidates(
        self,
        exclude_keys: set[str] | None = None,
        limit: int | None = None,
    ) -> list[ParadigmCandidate]:
        """按 FIFO 恢复已抽取、但尚未完成外部证据深挖的路线。"""
        excluded = exclude_keys or set()
        target_limit = limit if limit is not None and limit > 0 else None
        limit_sql = "LIMIT ?" if target_limit else ""
        params = (
            (target_limit + len(excluded),) if target_limit is not None else ()
        )
        with self._connect() as conn:
            rows = conn.execute(
                f"""
                SELECT payload_json FROM paradigms
                WHERE status = 'pending_deep'
                ORDER BY first_seen_at ASC
                {limit_sql}
                """,
                params,
            ).fetchall()
        results = []
        for row in rows:
            candidate = candidate_from_dict(json.loads(row[0]))
            if candidate.key in excluded:
                continue
            results.append(candidate)
            if target_limit is not None and len(results) >= target_limit:
                break
        return results

    def load_candidate_snapshots(self, keys: set[str]) -> list[ParadigmCandidate]:
        """Reload committed downstream inputs after a bounded origin visit."""
        if not keys:
            return []
        results = []
        ordered = sorted(keys)
        with self._connect() as conn:
            for offset in range(0, len(ordered), 400):
                chunk = ordered[offset:offset + 400]
                placeholders = ",".join("?" for _ in chunk)
                rows = conn.execute(
                    f"SELECT payload_json FROM paradigms WHERE paradigm_key IN ({placeholders})",
                    chunk,
                ).fetchall()
                results.extend(candidate_from_dict(json.loads(row[0])) for row in rows)
        return results

    def candidate_inputs_current(self, candidate: ParadigmCandidate) -> bool:
        """Check upstream source versions before paying for downstream work."""
        with self._connect() as conn:
            return _deep_origin_revisions_current(conn, candidate)

    def rebase_analyzed_candidate_inputs(
        self, key: str
    ) -> ParadigmCandidate | None:
        """Revalidate a pending hypothesis after its revised origins close.

        A revised origin can legitimately produce no new route. That must not
        strand an existing pending route on the old source revision forever.
        Only analyzed revisions can replace its inputs; their technical verdict
        is not inherited as a route verdict. Final synthesis must run again.
        """
        with self.transaction():
            with self._connect() as conn:
                row = conn.execute(
                    "SELECT status, payload_json FROM paradigms WHERE paradigm_key=?",
                    (key,),
                ).fetchone()
                if row is None or row[0] != "pending_deep":
                    return None
                candidate = candidate_from_dict(json.loads(row[1]))
                revised = []
                changed = False
                for item in candidate.evidence:
                    state = origin_state.read(conn, item.fingerprint) if (
                        item.evidence_type in ORIGIN_EVIDENCE_TYPES
                    ) else None
                    if state is None:
                        if item.evidence_type in ORIGIN_EVIDENCE_TYPES and item.source_revision:
                            return None
                        revised.append(item)
                        continue
                    if item.source_revision == state[0]:
                        revised.append(item)
                        continue
                    view = conn.execute(
                        "SELECT last_analyzed_at, payload_json FROM evidence_state "
                        "WHERE fingerprint=?",
                        (item.fingerprint,),
                    ).fetchone()
                    if view is None or not view[0]:
                        return None
                    current = technical_evidence_from_dict(json.loads(view[1]))
                    if current.source_revision != state[0]:
                        return None
                    if item.raw.get("historical"):
                        current.raw["historical"] = True
                    revised.append(current)
                    changed = True
                if not changed:
                    return candidate
                # Keep the route's hypothesis, identity, other support and
                # verified people, but none of its old derived acceptance.
                pending = ParadigmCandidate(
                    key=candidate.key, name=candidate.name, thesis=candidate.thesis,
                    problem_shift=candidate.problem_shift, mechanism=candidate.mechanism,
                    route_family=candidate.route_family, keywords=candidate.keywords,
                    lineage_parent=candidate.lineage_parent, lineage_path=candidate.lineage_path,
                    evidence=revised, researchers=candidate.researchers,
                    status="pending_deep", report_kind=candidate.report_kind,
                    screening_rubric={"revalidation_reason": "一手材料已修订；旧假说需重新综合核验"},
                    execution_failure_count=candidate.execution_failure_count,
                    last_execution_failure_at=candidate.last_execution_failure_at,
                )
                self.save_candidates([pending])
                return pending

    def load_synthesized_checkpoint(
        self, candidate: ParadigmCandidate
    ) -> ParadigmCandidate | None:
        """Resume a closed synthesis or person stage for unchanged inputs."""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT status, payload_json FROM paradigms WHERE paradigm_key=?",
                (candidate.key,),
            ).fetchone()
            if row is None or row[0] != "pending_deep":
                return None
            snapshot = candidate_from_dict(json.loads(row[1]))
            signature = _deep_checkpoint_input_signature(candidate)
            if (
                snapshot.deep_checkpoint_stage not in {"synthesized", "research_complete"}
                or snapshot.deep_checkpoint_input_signature != signature
                or not snapshot.deep_checkpoint_support_signature
                or _deep_checkpoint_input_signature(snapshot) != signature
                or snapshot.deep_checkpoint_synthesis_rubric.get("decision")
                not in {"report", "observe", "reject"}
                or not _deep_checkpoint_is_recent(snapshot.deep_checkpoint_created_at)
                or not _deep_origin_revisions_current(conn, snapshot)
                or (
                    snapshot.deep_checkpoint_stage == "research_complete"
                    and snapshot.deep_checkpoint_trajectory_signature
                    != _trajectory_checkpoint_policy_signature()
                )
            ):
                return None
            # Final scoring may have marked an otherwise complete synthesis as
            # pending due to a later objective/profile contract. Re-evaluate it
            # from the frozen technical answers, not that downstream verdict.
            snapshot.rubric_assessment = snapshot.deep_checkpoint_synthesis_rubric.copy()
            return snapshot

    def save_synthesized_checkpoint(
        self, original: ParadigmCandidate, synthesized: ParadigmCandidate
    ) -> None:
        """Compare-and-swap a scrubbed, closed synthesis without losing newer work."""
        if original.key != synthesized.key or original.status != "pending_deep":
            raise ValueError("深挖检查点候选身份或状态不一致")
        if synthesized.rubric_assessment.get("decision") not in {"report", "observe", "reject"}:
            raise ValueError("最终 Rubric 未闭合，不能标记综合完成")
        if not synthesized.deep_checkpoint_support_signature:
            raise ValueError("深挖检查点缺少支持证据输入签名")
        signature = _deep_checkpoint_input_signature(original)
        if _deep_checkpoint_input_signature(synthesized) != signature:
            raise ValueError("深挖期间一手材料输入发生变化")
        if _persistable_candidate(synthesized).to_dict() != synthesized.to_dict():
            raise ValueError("深挖检查点包含未清除的社区用户正文")
        synthesized.status = "pending_deep"
        synthesized.deep_checkpoint_stage = "synthesized"
        synthesized.deep_checkpoint_input_signature = signature
        synthesized.deep_checkpoint_created_at = datetime.now(timezone.utc).isoformat()
        synthesized.deep_checkpoint_trajectory_signature = ""
        synthesized.deep_checkpoint_synthesis_rubric = (
            synthesized.rubric_assessment.copy()
        )
        synthesized = candidate_from_dict(synthesized.to_dict())
        with self.transaction():
            with self._connect() as conn:
                row = conn.execute(
                    "SELECT status, payload_json FROM paradigms WHERE paradigm_key=?",
                    (original.key,),
                ).fetchone()
                if (
                    row is None or row[0] != "pending_deep"
                    or json.loads(row[1]) != original.to_dict()
                    or not _deep_origin_revisions_current(conn, original)
                ):
                    raise ValueError("深挖检查点与当前候选或一手材料版本冲突")
                self.save_candidates([synthesized])

    def save_completed_deep_checkpoint(
        self, synthesized: ParadigmCandidate, completed: ParadigmCandidate
    ) -> ParadigmCandidate:
        """Atomically retain completed person research before report assembly.

        The row stays pending_deep until the normal report/outbox path commits;
        a crash after this point can reuse the work without claiming delivery.
        """
        if (
            synthesized.key != completed.key
            or synthesized.deep_checkpoint_stage != "synthesized"
            or not synthesized.deep_checkpoint_support_signature
            or not _deep_checkpoint_is_recent(synthesized.deep_checkpoint_created_at)
            or completed.rubric_assessment.get("decision")
            not in {"report", "observe", "reject"}
        ):
            raise ValueError("人物研究检查点身份或 Rubric 未闭合")
        if _deep_checkpoint_input_signature(synthesized) != _deep_checkpoint_input_signature(completed):
            raise ValueError("人物研究期间一手材料输入发生变化")
        if _persistable_candidate(synthesized).to_dict() != synthesized.to_dict():
            raise ValueError("人物研究输入检查点包含未清除的社区用户正文")
        saved = _persistable_candidate(completed)
        saved.status = "pending_deep"
        saved.deep_checkpoint_stage = "research_complete"
        saved.deep_checkpoint_input_signature = synthesized.deep_checkpoint_input_signature
        saved.deep_checkpoint_support_signature = synthesized.deep_checkpoint_support_signature
        saved.deep_checkpoint_created_at = datetime.now(timezone.utc).isoformat()
        saved.deep_checkpoint_trajectory_signature = _trajectory_checkpoint_policy_signature()
        saved.deep_checkpoint_synthesis_rubric = synthesized.deep_checkpoint_synthesis_rubric.copy()
        with self.transaction():
            with self._connect() as conn:
                row = conn.execute(
                    "SELECT status, payload_json FROM paradigms WHERE paradigm_key=?",
                    (synthesized.key,),
                ).fetchone()
                current_payload = json.loads(row[1]) if row is not None else {}
                # A later objective gate may have replaced only the final
                # verdict with "incomplete". The loader restores the closed
                # technical answers, so normalize that one derived field for
                # CAS without granting permission to overwrite other changes.
                current_payload["rubric_assessment"] = (
                    synthesized.deep_checkpoint_synthesis_rubric.copy()
                )
                if (
                    row is None or row[0] != "pending_deep"
                    or current_payload != synthesized.to_dict()
                    or not _deep_origin_revisions_current(conn, synthesized)
                ):
                    raise ValueError("人物研究检查点与当前候选或一手材料版本冲突")
                self.save_candidates([saved])
        return saved

    def save_candidates(self, candidates: list[ParadigmCandidate]) -> None:
        # Validate the same domain contract used by artifact recovery before any
        # write. A stage must never mark an origin complete with unreadable output.
        candidates = [_persistable_candidate(item) for item in candidates]
        now = datetime.now(timezone.utc).isoformat()
        with self._connect() as conn:
            for candidate in candidates:
                payload = json.dumps(candidate.to_dict(), ensure_ascii=False, sort_keys=True)
                conn.execute(
                    """
                    INSERT INTO paradigms (
                        paradigm_key, name, status, total_score, payload_json,
                        first_seen_at, last_seen_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(paradigm_key) DO UPDATE SET
                        name=excluded.name,
                        status=excluded.status,
                        total_score=excluded.total_score,
                        payload_json=excluded.payload_json,
                        last_seen_at=excluded.last_seen_at
                    """,
                    (
                        candidate.key,
                        candidate.name,
                        candidate.status,
                        candidate.total_score,
                        payload,
                        now,
                        now,
                    ),
                )
                for evidence in candidate.evidence:
                    conn.execute(
                        """
                        INSERT OR IGNORE INTO paradigm_evidence (
                            paradigm_key, fingerprint, first_linked_at
                        ) VALUES (?, ?, ?)
                        """,
                        (candidate.key, evidence.fingerprint, now),
                    )

    def mark_reported(
        self, candidates: list[ParadigmCandidate], report_path: Path
    ) -> None:
        now = datetime.now(timezone.utc).isoformat()
        with self._connect() as conn:
            for candidate in candidates:
                self._mark_candidate_reported(
                    conn, candidate, report_path, now, delivery_key=""
                )

    @staticmethod
    def _mark_candidate_reported(
        conn: sqlite3.Connection,
        candidate: ParadigmCandidate,
        report_path: Path,
        delivered_at: str,
        *,
        delivery_key: str,
    ) -> None:
        candidate = _persistable_candidate(candidate)
        conn.execute(
            """
            UPDATE paradigms
            SET last_reported_signature=?, last_reported_at=?
            WHERE paradigm_key=?
            """,
            (candidate.report_signature, delivered_at, candidate.key),
        )
        conn.execute(
            """
            INSERT OR IGNORE INTO report_deliveries (
                paradigm_key, report_signature, report_path, payload_json,
                delivered_at, report_kind, delivery_key
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                candidate.key,
                candidate.report_signature,
                str(report_path),
                json.dumps(candidate.to_dict(), ensure_ascii=False, sort_keys=True),
                delivered_at,
                candidate.report_kind,
                delivery_key,
            ),
        )

    def stats(self) -> dict[str, int]:
        with self._connect() as conn:
            paradigms = conn.execute("SELECT COUNT(*) FROM paradigms").fetchone()[0]
            evidence = conn.execute("SELECT COUNT(*) FROM evidence_state").fetchone()[0]
            deliveries = conn.execute("SELECT COUNT(*) FROM report_deliveries").fetchone()[0]
            pending_deliveries = conn.execute(
                "SELECT COUNT(*) FROM report_outbox "
                "WHERE status IN ('pending_render', 'rendered', 'sending')"
            ).fetchone()[0]
            quarantined_deliveries = conn.execute(
                "SELECT COUNT(*) FROM report_outbox WHERE status = 'quarantined'"
            ).fetchone()[0]
        return {
            "paradigms": paradigms,
            "evidence": evidence,
            "deliveries": deliveries,
            "pending_deliveries": pending_deliveries,
            "quarantined_deliveries": quarantined_deliveries,
        }

    def delivery_queue_snapshot(self, limit: int = 10) -> dict:
        """Return a redacted, read-only operational view of the outbox."""

        target = max(1, min(int(limit or 10), 50))
        with self._connect() as conn:
            status_counts = {
                str(status): int(count)
                for status, count in conn.execute(
                    "SELECT status, COUNT(*) FROM report_outbox GROUP BY status"
                )
            }
            rows = conn.execute(
                """
                SELECT delivery_key, report_date, status,
                       candidate_payload_json, attempt_count,
                       render_attempt_count, failure_kind, last_error,
                       created_at, updated_at, quarantined_at
                FROM report_outbox
                WHERE status != 'delivered'
                ORDER BY created_at ASC
                LIMIT ?
                """,
                (target,),
            ).fetchall()
        jobs = []
        for row in rows:
            try:
                candidate_count = len(json.loads(row[3]))
            except (json.JSONDecodeError, TypeError):
                candidate_count = -1
            jobs.append(
                {
                    "delivery_key": str(row[0])[:12],
                    "report_date": str(row[1]),
                    "status": str(row[2]),
                    "candidate_count": candidate_count,
                    "delivery_attempt_count": int(row[4] or 0),
                    "render_attempt_count": int(row[5] or 0),
                    "failure_kind": str(row[6] or ""),
                    "last_error": str(row[7] or "")[:500],
                    "created_at": str(row[8] or ""),
                    "updated_at": str(row[9] or ""),
                    "quarantined_at": str(row[10] or ""),
                }
            )
        return {"status_counts": status_counts, "jobs": jobs}

    def latest_reported_candidates(self, limit: int = 20) -> list[ParadigmCandidate]:
        with self._connect() as conn:
            latest = conn.execute(
                """
                SELECT delivery_key, report_path FROM report_deliveries
                ORDER BY delivered_at DESC LIMIT 1
                """
            ).fetchone()
            if latest is None:
                return []
            delivery_key, report_path = latest
            if delivery_key:
                rows = conn.execute(
                    """
                    SELECT payload_json FROM report_deliveries
                    WHERE delivery_key=?
                    ORDER BY delivered_at DESC LIMIT ?
                    """,
                    (delivery_key, limit),
                ).fetchall()
            else:
                rows = conn.execute(
                    """
                    SELECT payload_json FROM report_deliveries
                    WHERE report_path=?
                    ORDER BY delivered_at DESC LIMIT ?
                    """,
                    (report_path, limit),
                ).fetchall()
        return [candidate_from_dict(json.loads(row[0])) for row in rows]


def _report_job_from_row(row: tuple) -> ReportOutboxJob:
    return ReportOutboxJob(
        delivery_key=str(row[0]),
        report_date=str(row[1]),
        report_name=str(row[2]),
        status=str(row[3]),
        candidates=[candidate_from_dict(value) for value in json.loads(row[4])],
        stats=dict(json.loads(row[5]) or {}),
        report_content=str(row[6] or ""),
        attempt_count=int(row[7] or 0),
        render_attempt_count=int(row[8] or 0),
        last_error=str(row[9] or ""),
        failure_kind=str(row[10] or ""),
        quarantined_at=str(row[11] or ""),
    )


def _safe_error_text(error: Exception | str) -> str:
    if isinstance(error, BaseException):
        parts = []
        current: BaseException | None = error
        seen: set[int] = set()
        while current is not None and id(current) not in seen and len(parts) < 4:
            seen.add(id(current))
            detail = str(current).replace("\n", " ").strip()
            label = type(current).__name__
            parts.append(f"{label}: {detail}" if detail else label)
            current = current.__cause__ or current.__context__
        value = " <- ".join(parts)
    else:
        value = str(error).replace("\n", " ")
    for name in (
        "LLM_API_KEY",
        "SUB_AGENT_API_KEY",
        "MAIN_AGENT_API_KEY",
        "OPENALEX_API_KEY",
        "SEMANTIC_SCHOLAR_API_KEY",
        "GITHUB_TOKEN",
        "TWITTER_BEARER_TOKEN",
        "TAVILY_API_KEY",
        "REDDIT_CLIENT_SECRET",
        "SMTP_PASSWORD",
    ):
        secret = str(getattr(config, name, "") or "")
        if len(secret) >= 6:
            value = value.replace(secret, "***")
    return value[:500]


def _content_signature(item: TechnicalEvidence) -> str:
    return origin_state.source_signature(item)


def _restore_execution_metadata(item: TechnicalEvidence, payload_json: str) -> None:
    """把 pending 重试元数据带到本周重新发现的同一内容上。"""
    try:
        previous = json.loads(payload_json)
        previous_raw = previous.get("raw") or {}
    except (AttributeError, TypeError, ValueError, json.JSONDecodeError):
        return
    for key in (
        "analysis_failure_count",
        "last_analysis_failure_at",
        "origin_eligibility_decision",
        "origin_eligibility_reason",
        "technical_report_checkpoint_version",
        "technical_report_mechanism_seeds",
        "technical_report_completed_mechanisms",
        "technical_report_mechanism_failure_counts",
        "technical_report_slice_pending",
        "technical_report_partial_failure",
    ):
        if key in previous_raw:
            item.raw[key] = previous_raw[key]


_ROUTE_STOPWORDS = {
    "model",
    "models",
    "learning",
    "method",
    "system",
    "framework",
    "approach",
    "using",
    "based",
    "technical",
    "intelligence",
    "neural",
    "training",
    "模型",
    "学习",
    "方法",
    "系统",
    "技术",
    "机制",
    "能力",
    "路线",
}


def _route_similarity(
    current: ParadigmCandidate, historical: ParadigmCandidate
) -> float:
    # A report's different mechanisms share almost all publication metadata.
    # Shared background/keywords must not collapse those independent children.
    current_reports = {
        item.fingerprint for item in current.evidence
        if item.raw.get("origin_kind") == "technical_report"
    }
    if current.key != historical.key and any(
        item.fingerprint in current_reports for item in historical.evidence
    ):
        return 0.0
    current_family = _route_tokens(current.route_family)
    previous_family = _route_tokens(historical.route_family)
    current_all = _candidate_route_tokens(current)
    previous_all = _candidate_route_tokens(historical)
    shared = current_all & previous_all
    if len(shared) < 2:
        return 0.0

    family_score = _jaccard(current_family, previous_family)
    content_score = _jaccard(current_all, previous_all)
    current_domains = _candidate_domains(current)
    previous_domains = _candidate_domains(historical)
    domain_score = 1.0 if current_domains & previous_domains else 0.0
    if (
        current.route_family
        and historical.route_family
        and _compact(current.route_family) == _compact(historical.route_family)
    ):
        family_score = 1.0
    return 0.45 * family_score + 0.4 * content_score + 0.15 * domain_score


def _candidate_route_tokens(candidate: ParadigmCandidate) -> set[str]:
    return _route_tokens(
        " ".join(
            [
                candidate.name,
                candidate.route_family,
                candidate.problem_shift,
                candidate.mechanism,
                candidate.lineage_parent,
                *candidate.keywords,
            ]
        )
    )


def _route_tokens(value: str) -> set[str]:
    latin = {
        token
        for token in re.findall(r"[a-z][a-z0-9-]{2,}", value.casefold())
        if token not in _ROUTE_STOPWORDS
    }
    chinese = {
        token
        for token in re.findall(r"[\u4e00-\u9fff]{2,8}", value)
        if token not in _ROUTE_STOPWORDS
    }
    return latin | chinese


def _candidate_domains(candidate: ParadigmCandidate) -> set[str]:
    return {
        str(domain)
        for evidence in candidate.evidence
        for domain in (evidence.raw.get("frontier_domains") or [])
        if domain
    }


def _jaccard(left: set[str], right: set[str]) -> float:
    union = left | right
    return len(left & right) / len(union) if union else 0.0


def _compact(value: str) -> str:
    return re.sub(r"[^a-z0-9\u4e00-\u9fff]", "", value.casefold())


def _merge_same_key_candidates(
    candidates: list[ParadigmCandidate],
) -> list[ParadigmCandidate]:
    by_key: dict[str, ParadigmCandidate] = {}
    for candidate in candidates:
        existing = by_key.get(candidate.key)
        if existing is None:
            by_key[candidate.key] = candidate
            continue
        by_fingerprint = {
            item.fingerprint: item
            for item in [*existing.evidence, *candidate.evidence]
        }
        existing.evidence = list(by_fingerprint.values())
        existing.keywords = sorted(set(existing.keywords) | set(candidate.keywords))
        existing.innovation_types = sorted(
            set(existing.innovation_types) | set(candidate.innovation_types)
        )
        existing.researchers = merge_researcher_profiles(
            existing.researchers, candidate.researchers
        )
        existing.lineage_path = list(
            dict.fromkeys([*existing.lineage_path, *candidate.lineage_path])
        )
        if candidate.total_score > existing.total_score:
            existing.name = candidate.name
            existing.route_family = candidate.route_family
            existing.thesis = candidate.thesis
            existing.problem_shift = candidate.problem_shift
            existing.mechanism = candidate.mechanism
            existing.screening_rubric = candidate.screening_rubric
            existing.total_score = candidate.total_score
    return list(by_key.values())


def _deep_checkpoint_input_signature(candidate: ParadigmCandidate) -> str:
    """Stable across synthesis prose edits, sensitive to new origin versions."""
    root = Path(__file__).resolve().parents[1]
    policy = hashlib.sha256()
    for relative in (
        "skills/paradigm_synthesis/SKILL.md",
        "skills/technical-mental-model/SKILL.md",
        "rubrics/paradigm_rubric.json",
    ):
        policy.update(relative.encode("utf-8"))
        policy.update((root / relative).read_bytes())
    payload = {
        "policy_version": DEEP_SYNTHESIS_CHECKPOINT_VERSION,
        "policy_content_signature": policy.hexdigest(),
        "key": candidate.key,
        "origins": sorted(
            (item.fingerprint, item.source_revision)
            for item in candidate.evidence
            if item.evidence_type in ORIGIN_EVIDENCE_TYPES
        ),
        "screening_rubric": candidate.screening_rubric,
    }
    return hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
    ).hexdigest()


def _trajectory_checkpoint_policy_signature() -> str:
    root = Path(__file__).resolve().parents[1]
    policy = hashlib.sha256()
    policy.update(str(DEEP_TRAJECTORY_CHECKPOINT_VERSION).encode("ascii"))
    for relative in (
        "skills/researcher_trajectory/SKILL.md",
        "rubrics/paradigm_rubric.json",
    ):
        policy.update(relative.encode("utf-8"))
        policy.update((root / relative).read_bytes())
    return policy.hexdigest()


def _persistable_candidate(candidate: ParadigmCandidate) -> ParadigmCandidate:
    result = candidate_from_dict(candidate.to_dict())
    for evidence in result.evidence:
        scrub_ephemeral_evidence(evidence)
    return result


def _deep_checkpoint_is_recent(created_at: str) -> bool:
    try:
        created = datetime.fromisoformat(created_at)
    except ValueError:
        return False
    if created.tzinfo is None:
        return False
    age = datetime.now(timezone.utc) - created.astimezone(timezone.utc)
    return timedelta(0) <= age <= DEEP_SYNTHESIS_CHECKPOINT_MAX_AGE


def _deep_origin_revisions_current(conn: sqlite3.Connection, candidate: ParadigmCandidate) -> bool:
    for item in candidate.evidence:
        if item.evidence_type not in ORIGIN_EVIDENCE_TYPES:
            continue
        row = origin_state.read(conn, item.fingerprint)
        if row is None:
            if item.source_revision:
                return False
        elif item.source_revision != row[0]:
            return False
    return True
