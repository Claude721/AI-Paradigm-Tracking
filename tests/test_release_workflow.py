"""Catch regressions on push/PR without needing a live production run."""
import unittest
from pathlib import Path


class ReleaseWorkflowTests(unittest.TestCase):
    def test_offline_ci_covers_local_and_runner_python_without_production_authority(self):
        workflow = (Path(__file__).resolve().parents[1] / ".github/workflows/offline-regression.yml").read_text()
        for required in ("  push:", "  pull_request:", 'python: ["3.11", "3.12"]',
                         "fail-fast: false", "python scripts/offline_checks.py", "contents: read"):
            self.assertIn(required, workflow)
        for forbidden in ("${{ secrets.", "actions: write", "gh workflow run", "run: python main.py"):
            self.assertNotIn(forbidden, workflow)
