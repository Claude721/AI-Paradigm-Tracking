import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from force_handoff import database_deliveries, NoRedirect


class ForceHandoffTests(unittest.TestCase):
    def test_frozen_delivery_is_readonly_and_filters_private_fields(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            db = root / "state.db"
            candidate = {"key": "route-a", "name": "A mechanism", "mechanism": "A specific change",
                         "total_score": 99, "researchers": [{"email": "private@example.test"}], "evidence": []}
            with sqlite3.connect(db) as conn:
                conn.execute("CREATE TABLE report_outbox(delivery_key,report_date,status,candidate_payload_json,stats_json,created_at,delivered_at,report_content)")
                conn.execute("INSERT INTO report_outbox VALUES(?,?,?,?,?,?,?,?)", (
                    "delivery-a", "2026-11-03", "delivered", json.dumps([candidate]), "{}",
                    "2026-11-03T01:00:00Z", "2026-11-03T02:00:00Z", "# Original report\n"))
            meta = root / "meta.json"
            meta.write_text(json.dumps({"run_id": "run-a", "commit_sha": "commit-a"}))
            before = db.read_bytes()
            result = list(database_deliveries(db, meta, root, root / "missing.json"))
            self.assertEqual(result[0]["report"], "# Original report\n")
            self.assertNotIn("total_score", json.dumps(result))
            self.assertNotIn("private@example.test", json.dumps(result))
            self.assertEqual(db.read_bytes(), before)

    def test_authenticated_writer_does_not_follow_redirects(self):
        self.assertIsNone(NoRedirect().redirect_request(None, None, None, None, None, None))


if __name__ == "__main__":
    unittest.main()
