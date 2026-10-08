"""Runtime identity must follow source changes and ignore private run data."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from runtime_provenance import source_fingerprint


class RuntimeProvenanceTests(unittest.TestCase):
    def test_secrets_and_generated_state_do_not_enter_source_fingerprint(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "config.py").write_text("MODE = 'paradigm'\n", encoding="utf-8")
            baseline = source_fingerprint(root)
            (root / ".env").write_text("SMTP_PASSWORD=private-value\n", encoding="utf-8")
            (root / "database").mkdir()
            (root / "database/state-meta.json").write_text('{"run_id":"123"}', encoding="utf-8")
            (root / "database/export.json").write_text('{"private":"research-data"}', encoding="utf-8")
            (root / "reports/output").mkdir(parents=True)
            (root / "reports/output/report.md").write_text("Private report", encoding="utf-8")
            self.assertEqual(source_fingerprint(root), baseline)
            (root / "config.py").write_text("MODE = 'new-policy'\n", encoding="utf-8")
            self.assertNotEqual(source_fingerprint(root), baseline)


if __name__ == "__main__":
    unittest.main()
