"""
本地 SQLite 存储模块 - 持久化高分项目
"""

from __future__ import annotations

import hashlib
import json
import logging
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Generator

import config
from config import DB_PATH
from sources.base import RawProject

logger = logging.getLogger(__name__)


class ProjectStore:
    """SQLite 项目存储"""

    def __init__(self, db_path: Path | str | None = None):
        self.db_path = Path(db_path) if db_path else DB_PATH
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _init_db(self) -> None:
        with self._connect() as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS projects (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    source TEXT NOT NULL,
                    name TEXT NOT NULL,
                    url TEXT NOT NULL UNIQUE,
                    score INTEGER NOT NULL,
                    one_liner TEXT,
                    innovation TEXT,
                    founder_guess TEXT,
                    category TEXT,
                    reasoning TEXT,
                    stars INTEGER DEFAULT 0,
                    language TEXT DEFAULT '',
                    topics TEXT DEFAULT '',
                    author TEXT DEFAULT '',
                    discovered_at TEXT NOT NULL,
                    created_at TEXT DEFAULT ''
                )
            """)
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_projects_score
                ON projects(score DESC)
            """)
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_projects_discovered
                ON projects(discovered_at DESC)
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS incremental_state (
                    url_key TEXT PRIMARY KEY,
                    url TEXT NOT NULL,
                    source TEXT NOT NULL,
                    signature TEXT NOT NULL,
                    first_seen_at TEXT NOT NULL,
                    last_seen_at TEXT NOT NULL,
                    last_analyzed_at TEXT NOT NULL,
                    analyzed_count INTEGER DEFAULT 0,
                    change_count INTEGER DEFAULT 0,
                    last_seen_stars INTEGER DEFAULT 0
                )
            """)
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_inc_seen
                ON incremental_state(last_seen_at DESC)
            """)
            for col in [
                "ai_integration TEXT DEFAULT ''",
                "key_design TEXT DEFAULT ''",
                "risks TEXT DEFAULT ''",
            ]:
                try:
                    conn.execute(f"ALTER TABLE projects ADD COLUMN {col}")
                except sqlite3.OperationalError:
                    pass
            # 兼容老版本增量表
            try:
                conn.execute("ALTER TABLE incremental_state ADD COLUMN last_seen_stars INTEGER DEFAULT 0")
            except sqlite3.OperationalError:
                pass
        logger.info(f"数据库已初始化: {self.db_path}")

    @contextmanager
    def _connect(self) -> Generator[sqlite3.Connection, None, None]:
        conn = sqlite3.connect(str(self.db_path))
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def save_project(self, project: dict) -> bool:
        """
        保存单个项目。
        - 新 URL: 插入
        - 已存在 URL: 更新为最新分析结果
        返回是否为“新插入”。
        """
        topics = project.get("topics", [])
        if isinstance(topics, list):
            topics = ", ".join(topics)

        with self._connect() as conn:
            existed = conn.execute(
                "SELECT 1 FROM projects WHERE url = ? LIMIT 1",
                (project["url"],),
            ).fetchone() is not None
            cursor = conn.execute(
                """
                INSERT INTO projects
                (source, name, url, score, one_liner, innovation,
                 key_design, risks, ai_integration,
                 founder_guess, category, reasoning,
                 stars, language, topics, author, discovered_at, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(url) DO UPDATE SET
                    source=excluded.source,
                    name=excluded.name,
                    score=excluded.score,
                    one_liner=excluded.one_liner,
                    innovation=excluded.innovation,
                    key_design=excluded.key_design,
                    risks=excluded.risks,
                    ai_integration=excluded.ai_integration,
                    founder_guess=excluded.founder_guess,
                    category=excluded.category,
                    reasoning=excluded.reasoning,
                    stars=excluded.stars,
                    language=excluded.language,
                    topics=excluded.topics,
                    author=excluded.author,
                    created_at=excluded.created_at
                """,
                (
                    project["source"],
                    project["name"],
                    project["url"],
                    project["score"],
                    project.get("one_liner", ""),
                    project.get("innovation", ""),
                    project.get("key_design", ""),
                    project.get("risks", ""),
                    project.get("ai_integration", ""),
                    project.get("founder_guess", ""),
                    project.get("category", ""),
                    project.get("reasoning", ""),
                    project.get("stars", 0),
                    project.get("language", ""),
                    topics,
                    project.get("author", ""),
                    datetime.now(timezone.utc).isoformat(),
                    project.get("created_at", ""),
                ),
            )
            inserted = (not existed) and cursor.rowcount > 0

        if not inserted:
            logger.debug(f"项目已存在，已更新: {project['url']}")
        return inserted

    def save_batch(self, projects: list[dict]) -> int:
        """批量保存，返回新增数量"""
        saved = sum(1 for p in projects if self.save_project(p))
        logger.info(f"批量保存完成: {saved}/{len(projects)} 个新增项目")
        return saved

    @staticmethod
    def _url_key(url: str) -> str:
        return (url or "").strip().lower().rstrip("/")

    def get_existing_url_keys(self) -> set[str]:
        """获取数据库中所有 URL 的标准化 key（用于 LLM 前去重）"""
        with self._connect() as conn:
            rows = conn.execute("SELECT url FROM projects").fetchall()
        return {self._url_key(r[0]) for r in rows if r and r[0]}

    def filter_new_projects(self, projects: list[dict]) -> tuple[list[dict], int]:
        """
        过滤已入库项目（按 URL 标准化 key 去重）
        Returns: (new_projects, skipped_existing_count)
        """
        existing = self.get_existing_url_keys()
        new_items: list[dict] = []
        skipped = 0
        for p in projects:
            key = self._url_key(str(p.get("url", "")))
            if not key:
                skipped += 1
                continue
            if key in existing:
                skipped += 1
                continue
            new_items.append(p)
        return new_items, skipped

    def get_today_projects(self) -> list[dict]:
        """获取今日发现的所有项目"""
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        with self._connect() as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                """
                SELECT * FROM projects
                WHERE discovered_at LIKE ?
                ORDER BY score DESC
                """,
                (f"{today}%",),
            ).fetchall()
        return [dict(row) for row in rows]

    def get_top_projects(self, limit: int = 50, min_score: int = 7) -> list[dict]:
        """获取历史高分项目"""
        with self._connect() as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                """
                SELECT * FROM projects
                WHERE score >= ?
                ORDER BY score DESC, discovered_at DESC
                LIMIT ?
                """,
                (min_score, limit),
            ).fetchall()
        return [dict(row) for row in rows]

    def get_stats(self) -> dict:
        """获取数据库统计信息"""
        with self._connect() as conn:
            total = conn.execute("SELECT COUNT(*) FROM projects").fetchone()[0]
            by_source = conn.execute(
                "SELECT source, COUNT(*) FROM projects GROUP BY source"
            ).fetchall()
            avg_score = conn.execute(
                "SELECT AVG(score) FROM projects"
            ).fetchone()[0]
        return {
            "total": total,
            "by_source": dict(by_source),
            "avg_score": round(avg_score, 1) if avg_score else 0,
        }

    @staticmethod
    def _url_key(url: str) -> str:
        return (url or "").strip().lower().rstrip("/")

    @staticmethod
    def _project_signature(p: RawProject) -> str:
        """内容签名：只基于项目的实质内容，忽略每日自然波动的数值字段"""
        payload = {
            "source": p.source,
            "url": p.url,
            "name": p.name,
            "description": (p.description or "")[:600],
            "readme_summary": (p.readme_summary or "")[:1200],
            "topics": list(p.topics or [])[:12],
            "author": p.author or "",
            "created_at": p.created_at or "",
        }
        s = json.dumps(payload, sort_keys=True, ensure_ascii=False)
        return hashlib.sha256(s.encode("utf-8")).hexdigest()

    @staticmethod
    def _parse_iso(ts: str) -> datetime | None:
        if not ts:
            return None
        try:
            return datetime.fromisoformat(ts.replace("Z", "+00:00")).astimezone(
                timezone.utc
            )
        except Exception:
            return None

    def plan_incremental_candidates(
        self, projects: list[RawProject]
    ) -> tuple[list[RawProject], dict]:
        """
        智能增量决策（LLM 前）：
        - 新 URL：进入分析
        - 已存在且签名变化：进入分析
        - 已存在且签名未变化：默认跳过
        - 可配置：签名未变化但超过 N 天可强制重分析
        """
        if not projects:
            return [], {
                "new": 0,
                "changed": 0,
                "stale_reanalyze": 0,
                "unchanged_skip": 0,
            }

        now = datetime.now(timezone.utc)
        now_iso = now.isoformat()

        keys = [self._url_key(p.url) for p in projects if p.url]
        if not keys:
            return [], {
                "new": 0,
                "changed": 0,
                "stale_reanalyze": 0,
                "unchanged_skip": len(projects),
            }

        placeholders = ",".join(["?"] * len(set(keys)))
        with self._connect() as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                f"""
                SELECT url_key, signature, last_analyzed_at, last_seen_stars, last_seen_at
                FROM incremental_state
                WHERE url_key IN ({placeholders})
                """,
                list(set(keys)),
            ).fetchall()
        existing = {r["url_key"]: dict(r) for r in rows}

        to_analyze: list[RawProject] = []
        unchanged_keys: list[str] = []
        stats = {"new": 0, "changed": 0, "stale_reanalyze": 0, "unchanged_skip": 0}
        stale_days = max(config.INCREMENTAL_REANALYZE_DAYS, 0)

        for p in projects:
            key = self._url_key(p.url)
            if not key:
                stats["unchanged_skip"] += 1
                continue

            sig = self._project_signature(p)
            row = existing.get(key)
            if not row:
                to_analyze.append(p)
                stats["new"] += 1
                continue
                
            # 将真实的“近期增量势能”注入给 QuantFilter 使用
            last_stars = int(row.get("last_seen_stars", 0) or 0)
            if last_stars > 0 and p.stars > last_stars:
                last_seen_dt = self._parse_iso(str(row.get("last_seen_at", "")))
                if last_seen_dt:
                    days_diff = max((now - last_seen_dt).total_seconds() / (3600 * 24), 1.0)
                    real_velocity = (p.stars - last_stars) / days_diff
                    p.extra["real_recent_velocity"] = real_velocity
                    p.extra["real_recent_added_stars"] = p.stars - last_stars

            if sig != row.get("signature", ""):
                to_analyze.append(p)
                stats["changed"] += 1
                logger.debug(
                    f"[增量] 内容变化: {p.name} (描述/README/标签发生了实质性改动)"
                )
                continue

            # 内容签名未变化 → 检查是否出现了"新一轮爆发"（势能闸门）
            if last_stars > 0 and p.stars > last_stars:
                added = p.stars - last_stars
                growth_ratio = added / max(last_stars, 1)
                min_added = config.INCREMENTAL_MIN_STAR_BURST
                min_ratio = config.INCREMENTAL_MIN_GROWTH_RATIO
                if added >= min_added or growth_ratio >= min_ratio:
                    to_analyze.append(p)
                    stats["stale_reanalyze"] += 1
                    logger.info(
                        f"[增量] 势能爆发: {p.name} "
                        f"({last_stars}→{p.stars}, +{added}, x{growth_ratio:.1f})"
                    )
                    continue

            if stale_days > 0:
                last_dt = self._parse_iso(str(row.get("last_analyzed_at", "")))
                if last_dt and (now - last_dt).days >= stale_days:
                    to_analyze.append(p)
                    stats["stale_reanalyze"] += 1
                    continue

            unchanged_keys.append(key)
            stats["unchanged_skip"] += 1

        # 只更新“跳过项”的 last_seen_at，避免每次都写整表
        if unchanged_keys:
            with self._connect() as conn:
                conn.executemany(
                    "UPDATE incremental_state SET last_seen_at = ? WHERE url_key = ?",
                    [(now_iso, k) for k in unchanged_keys],
                )

        return to_analyze, stats

    def mark_incremental_analyzed(self, projects: list[RawProject]) -> None:
        """将本次送入 LLM 的项目写入/更新增量状态表"""
        if not projects:
            return
        now_iso = datetime.now(timezone.utc).isoformat()
        with self._connect() as conn:
            for p in projects:
                key = self._url_key(p.url)
                if not key:
                    continue
                sig = self._project_signature(p)
                conn.execute(
                    """
                    INSERT INTO incremental_state
                    (url_key, url, source, signature, first_seen_at, last_seen_at,
                     last_analyzed_at, analyzed_count, change_count, last_seen_stars)
                    VALUES (?, ?, ?, ?, ?, ?, ?, 1, 0, ?)
                    ON CONFLICT(url_key) DO UPDATE SET
                        url=excluded.url,
                        source=excluded.source,
                        last_seen_at=excluded.last_seen_at,
                        last_analyzed_at=excluded.last_analyzed_at,
                        last_seen_stars=excluded.last_seen_stars,
                        change_count = CASE
                            WHEN incremental_state.signature != excluded.signature
                            THEN incremental_state.change_count + 1
                            ELSE incremental_state.change_count
                        END,
                        analyzed_count = incremental_state.analyzed_count + 1,
                        signature=excluded.signature
                    """,
                    (
                        key,
                        p.url,
                        p.source,
                        sig,
                        now_iso,
                        now_iso,
                        now_iso,
                        int(p.stars),
                    ),
                )
