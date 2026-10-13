"""Release fixtures cannot publish to real GitHub command-file channels."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts.offline_checks import _safe_environment

ROOT = Path(__file__).resolve().parents[1]
CHANNELS = ('GITHUB_STEP_SUMMARY', 'GITHUB_OUTPUT', 'GITHUB_ENV', 'GITHUB_STATE', 'GITHUB_PATH')


class ReleaseGateIsolationTests(unittest.TestCase):
    def test_live_writer_channels_removed_but_read_only_code_identity_preserved(self):
        injected = {name: f'/not-a-real-runner/{name}' for name in CHANNELS}
        injected['GITHUB_SHA'] = 'a' * 40
        with patch.dict(os.environ, injected):
            env = _safe_environment()
        for name in CHANNELS:
            self.assertNotIn(name, env)
        self.assertEqual(env['GITHUB_SHA'], 'a' * 40)
        self.assertEqual(env['GITHUB_TOKEN'], '')

    def test_real_cli_subprocess_cannot_modify_invoking_job_files(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            markers = {name: root / name for name in CHANNELS}
            for path in markers.values():
                path.write_text('real-job-marker\n')
            with patch.dict(os.environ, {name: str(path) for name, path in markers.items()}):
                env = _safe_environment()
            # Actual CLI, not a mocked writer: admission rejects missing
            # fixture OpenAlex credentials before ANY HTTP/model/mail call.
            output = root / 'fixture-output'
            result = subprocess.run([sys.executable, 'scripts/source_interface_check.py',
                '--scope', 'nyu', '--live-authorized', '--output-dir', str(output)],
                cwd=ROOT, env=env, capture_output=True, text=True, timeout=20)
            self.assertEqual(result.returncode, 1, result.stderr)
            report = json.loads((output / 'source_interface_latest.json').read_text())
            self.assertEqual(report['reason'], 'OPENALEX_API_KEY_not_configured')
            self.assertEqual(report['request_count'], 0)
            for path in markers.values():
                self.assertEqual(path.read_text(), 'real-job-marker\n')

    def test_fixture_can_explicitly_inject_its_own_summary_after_isolation(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            env = _safe_environment()
            summary = root / 'fixture-summary.md'
            env['GITHUB_STEP_SUMMARY'] = str(summary)
            result = subprocess.run([sys.executable, 'scripts/source_interface_check.py',
                '--scope', 'nyu', '--live-authorized', '--output-dir', str(root / 'output')],
                cwd=ROOT, env=env, capture_output=True, text=True, timeout=20)
            self.assertEqual(result.returncode, 1, result.stderr)
            self.assertIn('OPENALEX_API_KEY_not_configured', summary.read_text())
            self.assertNotIn('offline-test-only', summary.read_text())
