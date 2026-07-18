"""技术范式、证据与跨周交付状态的 SQLite 存储。"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

import config
from paradigms.models import (
    ParadigmCandidate,
    TechnicalEvidence,
    candidate_from_dict,
    technical_evidence_from_dict,
)


class ParadigmStore:
    def __init__(self, db_path: Path | str | None = None):
        self.db_path = Path(db_path) if db_path else config.PARADIGM_DB_PATH
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    @contextmanager
    def _connect(self):
        connection = sqlite3.connect(self.db_path)
        try:
            yield connection
            connection.commit()
        finally:
            connection.close()

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
                    UNIQUE(paradigm_key, report_signature)
                );
                CREATE TABLE IF NOT EXISTS paradigm_evidence (
                    paradigm_key TEXT NOT NULL,
                    fingerprint TEXT NOT NULL,
                    first_linked_at TEXT NOT NULL,
                    PRIMARY KEY(paradigm_key, fingerprint)
                );
                CREATE INDEX IF NOT EXISTS idx_evidence_last_seen
                    ON evidence_state(last_seen_at DESC);
                CREATE INDEX IF NOT EXISTS idx_paradigm_score
                    ON paradigms(total_score DESC);
                """
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
                f"SELECT fingerprint, content_signature FROM evidence_state "
                f"WHERE fingerprint IN ({placeholders})",
                fingerprints,
            ).fetchall()
        existing = {fingerprint: signature for fingerprint, signature in rows}
        selected = []
        stats = {"new": 0, "changed": 0, "unchanged_skip": 0}
        for item in origins:
            signature = _content_signature(item)
            previous = existing.get(item.fingerprint)
            if previous is None:
                selected.append(item)
                stats["new"] += 1
            elif previous != signature:
                selected.append(item)
                stats["changed"] += 1
            else:
                stats["unchanged_skip"] += 1
        return selected, stats

    def mark_evidence(
        self, evidence: list[TechnicalEvidence], analyzed: bool = False
    ) -> None:
        now = datetime.now(timezone.utc).isoformat()
        with self._connect() as conn:
            for item in evidence:
                payload = json.dumps(item.to_dict(), ensure_ascii=False, sort_keys=True)
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
                        last_analyzed_at=COALESCE(excluded.last_analyzed_at, evidence_state.last_analyzed_at)
                    """,
                    (
                        item.fingerprint,
                        _content_signature(item),
                        item.source,
                        item.evidence_type.value,
                        item.url,
                        payload,
                        now,
                        now,
                        analyzed_at,
                    ),
                )

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
                    candidate.report_kind = "new"
                selected.append(candidate)
        return selected

    def attach_history(self, candidates: list[ParadigmCandidate]) -> None:
        """把既有证据作为历史上下文附回候选，供趋势评分与更新报告使用。"""
        with self._connect() as conn:
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

    def save_candidates(self, candidates: list[ParadigmCandidate]) -> None:
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
                conn.execute(
                    """
                    UPDATE paradigms
                    SET last_reported_signature=?, last_reported_at=?
                    WHERE paradigm_key=?
                    """,
                    (candidate.report_signature, now, candidate.key),
                )
                conn.execute(
                    """
                    INSERT OR IGNORE INTO report_deliveries (
                        paradigm_key, report_signature, report_path, payload_json,
                        delivered_at, report_kind
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        candidate.key,
                        candidate.report_signature,
                        str(report_path),
                        json.dumps(candidate.to_dict(), ensure_ascii=False, sort_keys=True),
                        now,
                        candidate.report_kind,
                    ),
                )

    def stats(self) -> dict[str, int]:
        with self._connect() as conn:
            paradigms = conn.execute("SELECT COUNT(*) FROM paradigms").fetchone()[0]
            evidence = conn.execute("SELECT COUNT(*) FROM evidence_state").fetchone()[0]
            deliveries = conn.execute("SELECT COUNT(*) FROM report_deliveries").fetchone()[0]
        return {"paradigms": paradigms, "evidence": evidence, "deliveries": deliveries}

    def latest_reported_candidates(self, limit: int = 20) -> list[ParadigmCandidate]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT payload_json FROM report_deliveries
                WHERE report_path = (
                    SELECT report_path FROM report_deliveries
                    ORDER BY delivered_at DESC LIMIT 1
                )
                ORDER BY delivered_at DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [candidate_from_dict(json.loads(row[0])) for row in rows]


def _content_signature(item: TechnicalEvidence) -> str:
    # 不纳入引用/点赞等浮动数字，避免同一内容每周反复进入 LLM。
    payload = {
        "title": item.title.strip(),
        "summary": item.summary.strip(),
        "authors": item.authors,
        "organization": item.organization,
        "identifiers": item.identifiers,
    }
    serialized = json.dumps(payload, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()
