"""技术范式、证据与跨周交付状态的 SQLite 存储。"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

import config
from paradigms.landscape import load_landscape
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
                CREATE TABLE IF NOT EXISTS radar_meta (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_evidence_last_seen
                    ON evidence_state(last_seen_at DESC);
                CREATE INDEX IF NOT EXISTS idx_paradigm_score
                    ON paradigms(total_score DESC);
                """
            )

    def is_bootstrap_required(self) -> bool:
        """空状态或覆盖地图升级时使用较长发现窗口。"""
        current_landscape = str(load_landscape()["version"])
        with self._connect() as conn:
            evidence = conn.execute(
                """
                SELECT 1 FROM evidence_state
                WHERE evidence_type IN ('primary_paper', 'technical_blog')
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
        selected = []
        stats = {"new": 0, "changed": 0, "unchanged_skip": 0}
        for item in origins:
            signature = _content_signature(item)
            previous = existing.get(item.fingerprint)
            if previous is None:
                selected.append(item)
                stats["new"] += 1
            elif previous[1] is None:
                if previous[0] == signature:
                    _restore_execution_metadata(item, previous[2])
                selected.append(item)
                stats["new"] += 1
            elif previous[0] != signature:
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
                        last_analyzed_at=CASE
                            WHEN excluded.last_analyzed_at IS NOT NULL
                                THEN excluded.last_analyzed_at
                            WHEN excluded.content_signature != evidence_state.content_signature
                                THEN NULL
                            ELSE evidence_state.last_analyzed_at
                        END
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
                  AND evidence_type IN ('primary_paper', 'technical_blog')
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
                    candidate.report_kind = "new"
                selected.append(candidate)
        return selected

    def attach_history(
        self, candidates: list[ParadigmCandidate]
    ) -> list[ParadigmCandidate]:
        """先与跨周路线图对齐，再附回历史证据。

        不能只依赖模型本周生成的 canonical key。相同能力边界可能随着论文
        术语变化而改名；这里用路线、问题、机制词和覆盖领域做保守匹配。
        """
        with self._connect() as conn:
            history_rows = conn.execute(
                """
                SELECT paradigm_key, payload_json FROM paradigms
                WHERE status NOT IN ('rejected', 'pending_deep')
                """
            ).fetchall()
            historical = [
                (key, candidate_from_dict(json.loads(payload)))
                for key, payload in history_rows
            ]
            for candidate in candidates:
                if any(key == candidate.key for key, _ in historical):
                    continue
                matches = [
                    (_route_similarity(candidate, previous), key, previous)
                    for key, previous in historical
                ]
                score, key, previous = max(
                    matches,
                    default=(0.0, "", None),
                    key=lambda item: item[0],
                )
                if previous is not None and score >= 0.42:
                    candidate.key = key
                    candidate.lineage_path = list(
                        dict.fromkeys(
                            [
                                *previous.lineage_path,
                                *candidate.lineage_path,
                            ]
                        )
                    )
                    candidate.researchers = _merge_researchers(
                        previous.researchers, candidate.researchers
                    )

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
                ORDER BY last_seen_at DESC
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


def _restore_execution_metadata(item: TechnicalEvidence, payload_json: str) -> None:
    """把 pending 重试元数据带到本周重新发现的同一内容上。"""
    try:
        previous = json.loads(payload_json)
        previous_raw = previous.get("raw") or {}
    except (AttributeError, TypeError, ValueError, json.JSONDecodeError):
        return
    for key in ("analysis_failure_count", "last_analysis_failure_at"):
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
        existing.researchers = _merge_researchers(
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


def _merge_researchers(existing: list, new: list) -> list:
    by_name = {profile.name.casefold(): profile for profile in existing}
    for profile in new:
        by_name.setdefault(profile.name.casefold(), profile)
    return list(by_name.values())
