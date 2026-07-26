from __future__ import annotations

import asyncio
import tempfile
import unittest
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from zoneinfo import ZoneInfo

from apscheduler.triggers.cron import CronTrigger

import config
import main as app_main
from agents.sourcing_agent import SourcingAgent
from notifications.email_notifier import _send_sync, send_report_email
from run_audit import RunAudit
from sources.base import RawProject


class WeeklyPipelineTests(unittest.TestCase):
    def test_lookback_filter_drops_old_dated_items(self) -> None:
        now = datetime.now(timezone.utc)
        fresh = RawProject(
            source="test",
            name="fresh",
            url="https://example.com/fresh",
            created_at=now.isoformat(),
        )
        old = RawProject(
            source="test",
            name="old",
            url="https://example.com/old",
            created_at=(now - timedelta(days=8)).isoformat(),
        )
        undated = RawProject(
            source="test",
            name="undated",
            url="https://example.com/undated",
        )

        with patch.object(config, "SOURCING_LOOKBACK_DAYS", 7):
            kept = SourcingAgent._filter_by_lookback([fresh, old, undated])

        self.assertEqual([project.name for project in kept], ["fresh", "undated"])

    def test_weekly_cron_fires_on_friday_in_shanghai(self) -> None:
        timezone_shanghai = ZoneInfo("Asia/Shanghai")
        trigger = CronTrigger(
            day_of_week="fri",
            hour=9,
            minute=0,
            timezone=timezone_shanghai,
        )
        now = datetime(2026, 7, 18, 12, 0, tzinfo=timezone_shanghai)

        next_fire = trigger.get_next_fire_time(None, now)

        self.assertIsNotNone(next_fire)
        self.assertEqual(next_fire.isoformat(), "2026-07-24T09:00:00+08:00")

    def test_email_builds_attachment_without_network(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            report = Path(directory) / "deal_flow_2026-07-18.md"
            report.write_text("# Weekly report", encoding="utf-8")
            audit = Path(directory) / "run_audit_latest.md"
            audit.write_text("# Audit", encoding="utf-8")

            with (
                patch.object(config, "SMTP_HOST", "smtp.example.com"),
                patch.object(config, "SMTP_PORT", 465),
                patch.object(config, "SMTP_USERNAME", "sender@example.com"),
                patch.object(config, "SMTP_PASSWORD", "app-password"),
                patch.object(config, "SMTP_FROM", "sender@example.com"),
                patch.object(config, "SMTP_TO", ["receiver@example.com"]),
                patch.object(config, "SMTP_USE_SSL", True),
                patch("notifications.email_notifier.smtplib.SMTP_SSL") as smtp,
            ):
                _send_sync(
                    report,
                    {
                        "high_value_count": 2,
                        "raw_count": 10,
                        "saved_count": 2,
                        "audit_attachments": [str(audit)],
                    },
                )

            server = smtp.return_value.__enter__.return_value
            server.login.assert_called_once_with("sender@example.com", "app-password")
            server.send_message.assert_called_once()
            message = server.send_message.call_args.args[0]
            self.assertEqual(message.get_filename(), None)
            self.assertEqual(message.get_payload()[1].get_filename(), report.name)
            self.assertEqual(
                message.get_payload()[2].get_filename(), audit.name
            )

    def test_run_audit_records_usage_and_decisions_without_model_content(self) -> None:
        response = SimpleNamespace(
            usage=SimpleNamespace(
                prompt_tokens=120,
                completion_tokens=30,
                total_tokens=150,
                completion_tokens_details=SimpleNamespace(reasoning_tokens=8),
                prompt_tokens_details=SimpleNamespace(cached_tokens=20),
            )
        )
        audit = RunAudit()
        audit.record_llm(
            stage="paradigm_extraction",
            role="sub",
            model="qwen3.7-plus",
            subject="Example Work",
            response=response,
        )
        audit.record_origin(
            {
                "title": "Example Work",
                "initial_gate_passed": False,
                "gate_reason": "技术外延过窄",
            }
        )
        audit.record_candidate(
            {
                "name": "Example Route",
                "reportable": True,
                "admission_reason": "存在独立承接",
                "mental_model_components": [
                    "anchor_and_tension",
                    "training_flow",
                    "inference_flow",
                ],
            }
        )
        with tempfile.TemporaryDirectory() as directory:
            result = audit.write(
                {"origin_count": 1, "analysis_count": 1},
                output_dir=directory,
            )
            payload = json.loads(
                Path(result["audit_json_path"]).read_text(encoding="utf-8")
            )
            markdown = Path(result["audit_markdown_path"]).read_text(
                encoding="utf-8"
            )
        self.assertEqual(payload["llm_summary"]["llm_total_tokens"], 150)
        self.assertEqual(payload["llm_summary"]["llm_reasoning_tokens"], 8)
        self.assertNotIn("prompt", payload["llm_calls"][0])
        self.assertNotIn("response", payload["llm_calls"][0])
        self.assertIn("心智模型脚手架已形成 3 个有效部件", markdown)

    def test_required_email_failure_fails_the_task(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            report = Path(directory) / "paradigm_radar_2026-07-18.md"
            report.write_text("# Radar", encoding="utf-8")
            with (
                patch.object(config, "EMAIL_PUSH_ENABLED", True),
                patch.object(config, "EMAIL_PUSH_REQUIRED", True),
                patch.object(config, "SMTP_HOST", "smtp.qq.com"),
                patch.object(config, "SMTP_USERNAME", "sender@qq.com"),
                patch.object(config, "SMTP_PASSWORD", "authorization-code"),
                patch.object(config, "SMTP_TO", ["receiver@example.com"]),
                patch(
                    "notifications.email_notifier._send_sync",
                    side_effect=OSError("network unavailable"),
                ),
            ):
                with self.assertRaisesRegex(RuntimeError, "邮件发送失败"):
                    asyncio.run(send_report_email(report, {}))

    def test_failed_pipeline_rolls_back_dedup_state(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "radar.db"
            database.write_bytes(b"state-before-run")

            async def fail_after_write() -> dict:
                database.write_bytes(b"partial-failed-run")
                raise RuntimeError("email failed")

            with (
                patch.object(config, "PIPELINE_MODE", "paradigm"),
                patch.object(config, "PARADIGM_DB_PATH", database),
                patch("main._run_pipeline_once", new=fail_after_write),
            ):
                with self.assertRaisesRegex(RuntimeError, "email failed"):
                    asyncio.run(app_main.run_pipeline())

            self.assertEqual(database.read_bytes(), b"state-before-run")


if __name__ == "__main__":
    unittest.main()
