"""Schema startup is transactional and idempotent across independent workers."""
from __future__ import annotations

import sqlite3
import json
import subprocess
import sys
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from threading import Barrier
from unittest.mock import patch

from database.paradigm_store import ParadigmStore


class DatabaseInitializationTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.path = Path(temporary.name) / "state.db"

    def schema(self):
        with sqlite3.connect(self.path) as conn:
            return (conn.execute("PRAGMA schema_version").fetchone()[0],
                    conn.execute("SELECT name, sql FROM sqlite_master ORDER BY name").fetchall())

    def test_reopening_current_state_never_drops_or_recreates_its_schema(self):
        ParadigmStore(self.path)
        before = self.schema()
        for _ in range(5):
            ParadigmStore(self.path)
        self.assertEqual(self.schema(), before)

    def test_late_initialization_failure_rolls_back_all_new_tables(self):
        with patch("database.provider_budget.initialize", side_effect=RuntimeError("late schema fault")):
            with self.assertRaisesRegex(RuntimeError, "late schema fault"):
                ParadigmStore(self.path)
        with sqlite3.connect(self.path) as conn:
            tables = conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
        self.assertEqual(tables, [])
        ParadigmStore(self.path)  # A retry can finish; no partial schema to reconcile.

    def test_shared_store_does_not_lend_a_transaction_connection_to_another_thread(self):
        store = ParadigmStore(self.path)
        store.provider_budgets.observe("tavily", "2026-10", 0, through=0)
        with ThreadPoolExecutor(max_workers=1) as executor:
            with store.transaction():
                store.provider_budgets.reserve("tavily", "2026-10", limit=2)
                # A concurrent reader sees only committed state on its own
                # connection, not this thread's uncommitted reservation.
                seen = executor.submit(store.provider_budgets.used, "tavily", "2026-10").result(timeout=5)
                self.assertEqual(seen, 0)
        self.assertEqual(store.provider_budgets.used("tavily", "2026-10"), 1)

    def legacy_campaign(self):
        store = ParadigmStore(self.path)
        campaign = store.campaigns.begin(reference_time=datetime(2026, 10, 27, tzinfo=timezone.utc),
            ordinary_days=7, high_signal_days=14, bootstrap=False, seeds=[])
        store.campaigns.supersede(campaign.campaign_id)
        store.provider_budgets.observe("tavily", "2026-10", 5, through=0)
        store.provider_budgets.reserve("tavily", "2026-10", limit=8)
        with sqlite3.connect(self.path) as conn:
            conn.execute("DROP INDEX idx_one_active_campaign")
            conn.execute("CREATE UNIQUE INDEX idx_one_active_campaign ON research_campaigns((1)) WHERE status != 'delivered'")
        return store, campaign.campaign_id

    def test_legacy_index_upgrade_is_once_only_and_keeps_history_and_credit_reservations(self):
        _, previous = self.legacy_campaign()
        store = ParadigmStore(self.path)
        before = self.schema()
        for _ in range(4):
            ParadigmStore(self.path)
        self.assertEqual(self.schema(), before)
        self.assertEqual(store.campaigns.get(previous).status, "superseded")
        self.assertEqual(store.provider_budgets.used("tavily", "2026-10"), 6)
        current = store.campaigns.begin(reference_time=datetime(2026, 10, 27, tzinfo=timezone.utc),
            ordinary_days=7, high_signal_days=14, bootstrap=False, seeds=[])
        self.assertNotEqual(current.campaign_id, previous)
        with self.assertRaises(sqlite3.IntegrityError), sqlite3.connect(self.path) as conn:
            conn.execute("UPDATE research_campaigns SET status='researching' WHERE campaign_id=?", (previous,))

    def test_failed_legacy_migration_restores_old_index_and_history_without_partial_commit(self):
        store, previous = self.legacy_campaign()
        before = self.schema()
        with patch("database.provider_budget.initialize", side_effect=RuntimeError("late upgrade fault")):
            with self.assertRaisesRegex(RuntimeError, "late upgrade fault"):
                ParadigmStore(self.path)
        self.assertEqual(self.schema(), before)
        self.assertEqual(store.campaigns.get(previous).status, "superseded")
        self.assertEqual(store.provider_budgets.used("tavily", "2026-10"), 6)
        ParadigmStore(self.path)

    def test_eight_workers_can_open_cold_or_warm_state_without_schema_churn_or_overspend(self):
        for _ in range(3):
            start = Barrier(8)
            def reserve(index):
                start.wait(timeout=10)
                store = ParadigmStore(self.path)
                store.provider_budgets.observe("tavily", "2026-10", 0, through=0)
                return [store.provider_budgets.reserve("tavily", "2026-10", limit=8) for _ in range(4)]
            with ThreadPoolExecutor(max_workers=8) as executor:
                results = list(executor.map(reserve, range(8)))
            store = ParadigmStore(self.path)
            self.assertEqual(store.provider_budgets.used("tavily", "2026-10"), 8)
            if _ == 0:
                self.assertEqual(sum(value is not None for values in results for value in values), 8)
                before = self.schema()
            else:
                self.assertTrue(all(value is None for values in results for value in values))
                self.assertEqual(self.schema(), before)

    def test_parallel_old_column_and_origin_state_migration_preserves_original_rows(self):
        from tests.test_v0_delivery import origin
        store=ParadigmStore(self.path)
        item=store.observe_origins([origin()])[0][0]
        store.mark_evidence([item],analyzed=True)
        with sqlite3.connect(self.path) as conn:
            conn.execute("DROP TABLE origin_research_state")
            for table,column in (("paradigms","last_refresh_attempt_at"),
                                 ("report_deliveries","delivery_key"),
                                 ("report_outbox","failure_kind"),("report_outbox","quarantined_at")):
                conn.execute(f"ALTER TABLE {table} DROP COLUMN {column}")
            before=conn.execute("SELECT fingerprint,last_analyzed_at,first_seen_at FROM evidence_state").fetchall()
        start=Barrier(6)
        def upgrade(_):
            start.wait(timeout=10)
            return ParadigmStore(self.path).stats()
        with ThreadPoolExecutor(max_workers=6) as executor:
            list(executor.map(upgrade,range(6)))
        with sqlite3.connect(self.path) as conn:
            self.assertEqual(conn.execute("SELECT fingerprint,last_analyzed_at,first_seen_at FROM evidence_state").fetchall(),before)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM origin_research_state").fetchone()[0],1)
            self.assertIn("failure_kind",{row[1] for row in conn.execute("PRAGMA table_info(report_outbox)")})
        self.assertEqual(ParadigmStore(self.path).load_pending_origins(),[])

    def test_independent_processes_share_cold_schema_and_atomic_budget(self):
        # Unlike the historical ThreadPool test, these really are separate
        # interpreters and cannot share an in-memory lock or Python state.
        script = """
import json, sys
from database.paradigm_store import ParadigmStore
reserved = []
for _ in range(12):
    store = ParadigmStore(sys.argv[1])
    store.provider_budgets.observe('tavily', '2026-10', 0, through=0)
    reserved.append(store.provider_budgets.reserve('tavily', '2026-10', limit=8))
print(json.dumps(reserved))
"""
        workers = []
        try:
            for _ in range(4):
                workers.append(subprocess.Popen([sys.executable, "-c", script, str(self.path)],
                    cwd=Path(__file__).resolve().parents[1], stdin=subprocess.DEVNULL,
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True))
            identities = []
            for worker in workers:
                stdout, stderr = worker.communicate(timeout=25)
                self.assertEqual(worker.returncode, 0, stderr)
                identities.extend(value for value in json.loads(stdout) if value is not None)
            self.assertEqual(len(identities), 8)
            self.assertEqual(len(set(identities)), 8)
            store = ParadigmStore(self.path)
            self.assertEqual(store.provider_budgets.used("tavily", "2026-10"), 8)
            before = self.schema()
            ParadigmStore(self.path)
            self.assertEqual(self.schema(), before)
        finally:
            for worker in workers:
                if worker.poll() is None:
                    worker.kill()
                worker.communicate()
