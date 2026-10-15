"""Private review retention: fake models, temporary files, no external calls."""
import asyncio
import copy
import io
import json
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace as NS
import unittest
from unittest.mock import AsyncMock, patch
import zipfile

import config
import research_archive as archive
from run_audit import RunAudit
from paradigms.analyzer import ParadigmAnalyzer
from paradigms.models import EvidenceType, TechnicalEvidence
from scripts import review_archive as cli
from tests.test_paradigm_pipeline import candidate, rubric_answers


def origin(suffix="1", **extra):
    return TechnicalEvidence(source="arxiv", evidence_type=EvidenceType.PRIMARY_PAPER,
        title="Same title", url=f"https://arxiv.org/abs/2610.0000{suffix}",
        identifiers={"arxiv": f"2610.0000{suffix}"}, summary="完整输入摘要" * 200,
        raw={"document_excerpt": "原文尾部限定条件", **extra})


def response(content='{"decision":"reject"}'):
    return NS(choices=[NS(message=NS(content=content, reasoning_content="PRIVATE REASONING"), finish_reason="stop")],
        usage=NS(prompt_tokens=123, completion_tokens=20, total_tokens=143))


class ArchiveFixture:
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(dir="/private/tmp" if Path("/private/tmp").exists() else None)
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.directory = self.root / "archive"
        for name, value in {"RESEARCH_ARCHIVE_ENABLED": True, "RESEARCH_ARCHIVE_DIR": self.directory}.items():
            patcher = patch.object(config, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        archive.review_archive.reset()
        self.addCleanup(archive.review_archive.reset)
        self.store = archive.ArchiveStore(self.directory)

    def call(self, evidence=None, role="sub", stage="paradigm_extraction", context=None, run_id="run-1", **extra):
        return self.store.begin_call(run_id=run_id, stage=stage, role=role, subject="subject",
            context=context or {"origins": [evidence or origin()]},
            request={"model": "offline-model", "messages": [{"role": "user", "content": "原始内容\n完整尾部"}], **extra})

    def read_call(self, call_id):
        return archive._read(self.directory / "calls" / (call_id + ".json"))

    def roundtrip(self):
        path = self.root / "snapshot.zip"
        self.store.export(path)
        restored = archive.restore_bundle(path.read_bytes(), self.root / "restored")
        return restored, path.read_bytes()


class ArchiveTests(ArchiveFixture, unittest.TestCase):
    def test_exact_messages_standards_and_both_roles(self):
        item = origin()
        sub = self.call(item)
        route = candidate([item])
        main = self.call(role="main", stage="paradigm_synthesis", context={"candidate": route})
        self.store.finish_call(sub, response=response())
        self.store.finish_call(main, response=response('{"judgment":"observe"}'))
        first, second = self.read_call(sub), self.read_call(main)
        self.assertEqual(first["origin_refs"], second["origin_refs"])
        self.assertEqual(first["request"]["messages"][0]["content"], "原始内容\n完整尾部")
        standard = archive._read(self.directory / "standards" / (first["standard_id"] + ".json"))
        self.assertIn("skills/paradigm_extraction/SKILL.md", standard["skill_templates"])
        self.assertIn("common_criteria", standard["rubric"])
        self.assertNotIn("PRIVATE REASONING", json.dumps(first))
        self.assertEqual(first["response"]["parsed_json"]["decision"], "reject")
        self.assertEqual(second["candidate_key"], route.key)

    def test_invalid_visible_response_preserved_before_validation(self):
        call_id = self.call()
        self.store.finish_call(call_id, response=response("not JSON 原始回答"))
        self.store.validation(call_id, ValueError("sensitive exception text"))
        row = self.read_call(call_id)
        self.assertEqual(row["response"]["content"], "not JSON 原始回答")
        self.assertIsNone(row["response"]["parsed_json"])
        self.assertEqual(row["validation"], {"status": "failed", "error_type": "ValueError"})
        self.assertNotIn("sensitive exception", json.dumps(row))

    def test_nonfinite_json_response_is_saved_as_visible_invalid_text(self):
        call_id = self.call()
        self.store.finish_call(call_id, response=response('{"result": NaN}'))
        row = self.read_call(call_id)
        self.assertEqual(row["response"]["content"], '{"result": NaN}')
        self.assertIsNone(row["response"]["parsed_json"])

    def test_identity_enrichment_preserves_human_feedback(self):
        item = origin()
        item.identifiers = {}
        first = self.store.register(item)
        feedback = archive._read(self.directory / "feedback.json")
        feedback["annotations"][first["origin_id"]].update(retain=False, note="我不认同此路线")
        archive._write(self.directory / "feedback.json", feedback)
        item.identifiers = {"doi": "10.1234/EXAMPLE", "arxiv": "2610.00001v2"}
        item.summary += "新的补充"
        second = self.store.register(item)
        self.assertEqual(first["origin_id"], second["origin_id"])
        self.assertNotEqual(first["content_version"], second["content_version"])
        self.assertEqual(archive._read(self.directory / "feedback.json"), feedback)
        self.assertEqual(len(list((self.directory / "origins").glob("*.json"))), 1)

    def test_different_origins_same_title_do_not_merge(self):
        self.assertNotEqual(self.store.register(origin("1"))["origin_id"], self.store.register(origin("2"))["origin_id"])

    def test_conflicting_explicit_aliases_never_merge_feedback(self):
        first, second = origin("1"), origin("2")
        self.store.register(first)
        self.store.register(second)
        first.identifiers["doi"] = "10.1234/same"
        self.store.register(first)
        second.identifiers["doi"] = "10.1234/same"
        with self.assertRaisesRegex(archive.ArchiveError, "Conflicting"):
            self.store.register(second)

    def test_feedback_cas_idempotent_and_models_immutable(self):
        ref = self.store.register(origin())
        self.store.judgment(run_id="one", stage="candidate_final", context={"candidate": candidate([origin()])}, result={"system_retention": True})
        judgment_file = next((self.directory / "judgments").glob("*.json"))
        original = judgment_file.read_bytes()
        patch_data = archive._read(self.directory / "feedback.json")
        patch_data["annotations"][ref["origin_id"]].update(retain=False, note="人工不留存")
        self.store.import_feedback(patch_data)
        self.store.import_feedback(patch_data)  # safe repeated application
        self.store.register(origin())
        row = archive._read(self.directory / "feedback.json")["annotations"][ref["origin_id"]]
        self.assertEqual((row["retain"], row["revision"]), (False, 1))
        stale = copy.deepcopy(patch_data)
        stale["annotations"][ref["origin_id"]]["retain"] = True
        with self.assertRaisesRegex(archive.ArchiveError, "Stale"):
            self.store.import_feedback(stale)
        self.assertEqual(original, judgment_file.read_bytes())
        view = cli.label_view(self.store)["origins"][0]
        self.assertTrue(view["system_retention"])
        self.assertFalse(view["effective_dataset_label"])

    def test_feedback_invalid_or_unknown_patch_is_atomic(self):
        ref = self.store.register(origin())
        path = self.directory / "feedback.json"
        before = path.read_bytes()
        data = archive._read(path)
        data["annotations"][ref["origin_id"]]["retain"] = "false"
        with self.assertRaises(archive.ArchiveError):
            self.store.import_feedback(data)
        data["annotations"][ref["origin_id"]]["retain"] = False
        data["annotations"]["f" * 64] = dict(data["annotations"][ref["origin_id"]])
        with self.assertRaises(archive.ArchiveError):
            self.store.import_feedback(data)
        self.assertEqual(before, path.read_bytes())

    def test_plain_json_zip_roundtrip_and_private_permissions(self):
        self.store.register(origin())
        self.store.register(origin("2"))
        restored, token = self.roundtrip()
        with zipfile.ZipFile(io.BytesIO(token)) as bundle:
            self.assertEqual(bundle.namelist(), [archive.BUNDLE_MEMBER])
            self.assertIn("完整输入摘要", bundle.read(archive.BUNDLE_MEMBER).decode())
        self.assertEqual(len(list((restored.directory / "origins").glob("*.json"))), 2)
        self.assertEqual((restored.directory / "feedback.json").stat().st_mode & 0o777, 0o600)
        self.assertEqual(restored.directory.stat().st_mode & 0o777, 0o700)
        with self.assertRaisesRegex(archive.ArchiveError, "empty"):
            archive.restore_bundle(token, restored.directory)

    def test_invalid_zip_rejected_before_restore(self):
        self.store.register(origin())
        _, token = self.roundtrip()
        for value in (b"not a zip", token[:10]):
            target = self.root / "bad"
            with self.assertRaises(archive.ArchiveError):
                archive.restore_bundle(value, target)
            self.assertFalse(target.exists())

    def test_zip_member_traversal_and_oversize_rejected(self):
        for member in ("../review-archive.json", archive.BUNDLE_MEMBER):
            stream = io.BytesIO()
            with zipfile.ZipFile(stream, "w") as bundle:
                bundle.writestr(member, b"x" * 100)
            with patch.object(archive, "MAX_BUNDLE_BYTES", 50), self.assertRaises(archive.ArchiveError):
                archive.restore_bundle(stream.getvalue(), self.root / "bad")
        self.assertFalse((self.root / "bad").exists())

    def test_path_traversal_and_broken_links_rejected(self):
        for files in ({"../feedback.json": {}}, {"calls/" + "a" * 32 + ".json": {"schema_version": 1, "call_id": "wrong"}}):
            stream = io.BytesIO()
            with zipfile.ZipFile(stream, "w") as bundle:
                bundle.writestr(archive.BUNDLE_MEMBER, json.dumps({"schema_version": 1, "files": files}))
            with self.assertRaises(archive.ArchiveError):
                archive.restore_bundle(stream.getvalue(), self.root / "bad")
        call_id = self.call()
        path = self.directory / "calls" / (call_id + ".json")
        data = archive._read(path)
        data["request"]["messages"] = []
        archive._write(path, data)
        with self.assertRaisesRegex(archive.ArchiveError, "checksum"):
            self.store.export(self.root / "bad.zip")

    def test_symlink_subdirectory_rejected(self):
        other = self.root / "other"
        other.mkdir()
        (self.directory / "calls").rmdir()
        (self.directory / "calls").symlink_to(other, target_is_directory=True)
        with self.assertRaisesRegex(archive.ArchiveError, "symlink"):
            archive.ArchiveStore(self.directory)

    def test_sensitive_material_redaction_without_collecting_headers(self):
        secret = "secret-value-never-retain"
        with patch.object(config, "MAIN_AGENT_LLM_API_KEY", secret):
            call_id = self.call(messages=[{"role": "user", "content": secret + " https://user:pass@host.invalid/a?token=abc"}],
                extra_headers={"Authorization": secret})
            self.store.finish_call(call_id, response=response(secret))
        data = self.read_call(call_id)
        text = json.dumps(data)
        self.assertNotIn(secret, text)
        self.assertNotIn("user:pass", text)
        self.assertNotIn("token=abc", text)
        self.assertNotIn("extra_headers", text)
        self.assertTrue(data["redacted"])

    def test_secret_word_in_material_url_is_not_a_credential_field(self):
        item = origin()
        item.identifiers = {}
        item.url = "https://lab.invalid/blog/api-key-security?token=private"
        self.call(item)
        self.roundtrip()

    def test_cache_provenance_does_not_invent_old_inputs(self):
        item = origin()
        call_id = self.call(item, context={"origins": [item], "cache_signatures": ["known-signature"]})
        for signature in ("known-signature", "pre-feature-signature"):
            self.store.judgment(run_id="rerun", stage="cache", context={"origins": [item]},
                result={"decision": "screen_out"}, cache_signature=signature)
        rows = [archive._read(path) for path in (self.directory / "judgments").glob("*.json")]
        self.assertEqual(next(row for row in rows if row["cache_signature"] == "known-signature")["actual_request_ids"], [call_id])
        self.assertEqual(next(row for row in rows if row["cache_signature"] == "pre-feature-signature")["input_provenance"], "no_exact_cached_request_available")
        self.assertEqual(len(list((self.directory / "calls").glob("*.json"))), 1)

    def test_final_candidate_links_only_its_own_main_calls_and_all_origins(self):
        route = candidate([origin(), origin("2")])
        other = candidate([origin()])
        other.key = "different-mechanism"
        wanted = self.call(role="main", stage="paradigm_synthesis", context={"candidate": route})
        unwanted = self.call(role="main", stage="paradigm_synthesis", context={"candidate": other})
        self.store.judgment(run_id="run-1", stage="candidate_final", context={"candidate": route}, result={"system_retention": True})
        row = archive._read(next((self.directory / "judgments").glob("*.json")))
        self.assertEqual(row["actual_request_ids"], [wanted])
        self.assertNotIn(unwanted, row["actual_request_ids"])
        self.assertEqual(len(row["origin_refs"]), 2)

    def test_human_feedback_survives_local_zip_roundtrip_and_next_review(self):
        ref = self.store.register(origin())
        feedback = archive._read(self.directory / "feedback.json")
        feedback["annotations"][ref["origin_id"]]["retain"] = False
        self.store.import_feedback(feedback)
        restored, _ = self.roundtrip()
        restored.register(origin("2"))
        restored.judgment(run_id="new", stage="candidate_final", context={"candidate": candidate([origin()])}, result={"system_retention": True})
        row = next(row for row in cli.label_view(restored)["origins"] if row["origin_id"] == ref["origin_id"])
        self.assertTrue(row["system_retention"])
        self.assertFalse(row["human_feedback"]["retain"])
        self.assertFalse(row["effective_dataset_label"])

    def test_latest_dataset_label_does_not_vote_over_old_runs(self):
        for run_id, verdict in (("old", True), ("new", False)):
            self.store.judgment(run_id=run_id, stage="origin_screening", context={"origins": [origin()]}, result={"system_retention": verdict})
        row = cli.label_view(self.store)["origins"][0]
        self.assertFalse(row["system_retention"])
        self.assertEqual(row["label_run_id"], "new")

    def test_entering_deep_research_is_not_a_final_positive_label(self):
        self.store.judgment(run_id="pending", stage="origin_screening", context={"origins": [origin()]}, result={"system_retention": True})
        row = cli.label_view(self.store)["origins"][0]
        self.assertIsNone(row["system_retention"])
        self.assertEqual(row["screening_labels"], [True])
        self.assertEqual(row["final_candidate_labels"], {})

    def test_cancelled_or_started_request_still_exportable(self):
        call_id = self.call()
        self.store.finish_call(call_id, cancelled=True)
        self.call(origin("2"))  # process vanished before response
        restored, _ = self.roundtrip()
        rows = [archive._read(path) for path in (restored.directory / "calls").glob("*.json")]
        self.assertEqual({row["transport_status"] for row in rows}, {"started", "cancelled_usage_unknown"})

    def test_repeated_local_merge_preserves_feedback_and_model_bytes(self):
        self.call()
        _, data = self.roundtrip()
        master = archive.ArchiveStore(self.root / "master")
        master.merge(data)
        feedback = archive._read(master.directory / "feedback.json")
        row = next(iter(feedback["annotations"].values()))
        row.update(retain=False, note="本地人工标签")
        archive._write(master.directory / "feedback.json", feedback)
        before = {path.name: path.read_bytes() for path in (master.directory / "calls").glob("*.json")}
        master.merge(data)
        self.assertEqual(archive._read(master.directory / "feedback.json"), feedback)
        self.assertEqual({path.name: path.read_bytes() for path in (master.directory / "calls").glob("*.json")}, before)

    def test_merge_links_enriched_origin_without_replacing_human_label(self):
        item = origin()
        item.identifiers = {}
        first = self.call(item)
        self.store.export(self.root / "first.zip")
        master = archive.ArchiveStore(self.root / "master")
        master.merge((self.root / "first.zip").read_bytes())
        original_id = self.read_call(first)["origin_refs"][0]["origin_id"]
        feedback = archive._read(master.directory / "feedback.json")
        feedback["annotations"][original_id].update(retain=False, note="不要覆盖")
        archive._write(master.directory / "feedback.json", feedback)
        second = archive.ArchiveStore(self.root / "second")
        enriched = origin()
        other_id = second.begin_call(run_id="next", stage="paradigm_extraction", role="sub", subject="same",
            context={"origins": [enriched]}, request={"model": "offline", "messages": []})
        second.export(self.root / "second.zip")
        master.merge((self.root / "second.zip").read_bytes())
        self.assertEqual(len(list((master.directory / "origins").glob("*.json"))), 1)
        merged = archive._read(master.directory / "calls" / (other_id + ".json"))
        self.assertEqual(merged["origin_refs"][0]["origin_id"], original_id)
        self.assertIn("imported_origin_id", merged["origin_refs"][0])
        self.assertEqual(archive._read(master.directory / "feedback.json"), feedback)
        master.merge((self.root / "second.zip").read_bytes())

    def test_conflicting_immutable_data_does_not_overwrite_master(self):
        call_id = self.call()
        _, data = self.roundtrip()
        master = archive.ArchiveStore(self.root / "master")
        master.merge(data)
        before = master.files()
        self.store.finish_call(call_id, response=response("a changed response"))
        self.store.export(self.root / "changed.zip")
        with self.assertRaisesRegex(archive.ArchiveError, "immutable"):
            master.merge((self.root / "changed.zip").read_bytes())
        self.assertEqual(master.files(), before)

    def test_interrupted_local_merge_recovers_without_overwriting_feedback(self):
        self.call()
        _, data = self.roundtrip()
        master = archive.ArchiveStore(self.root / "master")
        with patch.object(master, "_commit_merge", side_effect=OSError("interrupted")):
            with self.assertRaises(OSError):
                master.merge(data)
        self.assertTrue((master.directory / ".merge-pending").exists())
        master = archive.ArchiveStore(master.directory)
        self.assertFalse((master.directory / ".merge-pending").exists())
        self.assertEqual(len(list((master.directory / "calls").glob("*.json"))), 1)

    def test_merge_recovery_preserves_later_manual_correction(self):
        ref = self.store.register(origin())
        self.call()
        self.store.export(self.root / "first.zip")
        master = archive.ArchiveStore(self.root / "master")
        master.merge((self.root / "first.zip").read_bytes())
        self.call(origin("2"))
        self.store.export(self.root / "second.zip")
        with patch.object(master, "_commit_merge", side_effect=OSError("interrupted")):
            with self.assertRaises(OSError):
                master.merge((self.root / "second.zip").read_bytes())
        feedback = archive._read(master.directory / "feedback.json")
        feedback["annotations"][ref["origin_id"]].update(retain=False, note="中断后人工更正")
        archive._write(master.directory / "feedback.json", feedback)
        recovered = archive.ArchiveStore(master.directory)
        self.assertEqual(archive._read(recovered.directory / "feedback.json")["annotations"][ref["origin_id"]], feedback["annotations"][ref["origin_id"]])
        self.assertEqual(len(list((recovered.directory / "calls").glob("*.json"))), 2)

    def test_incremental_zip_includes_current_run_and_referenced_cache_only(self):
        old = self.call(context={"origins": [origin()], "cache_signatures": ["sig"]}, run_id="old")
        wanted = self.call(origin("2"), run_id="new:1:uuid")
        self.call(origin("3"), run_id="unrelated")
        self.store.judgment(run_id="new:1:uuid", stage="origin_eligibility_cache", context={"origins": [origin()]},
            result={"decision": "screen_out"}, cache_signature="sig")
        output = self.root / "current-only.zip"
        self.store.export(output, run_prefix="new:1:")
        files = archive.read_bundle(output.read_bytes())
        self.assertEqual({row["call_id"] for name, row in files.items() if name.startswith("calls/")}, {old, wanted})
        self.assertEqual(sum(name.startswith("origins/") for name in files), 2)

    def test_out_of_order_mail_merge_does_not_make_old_judgment_latest(self):
        for label, when, verdict in (("new", "2026-10-15T00:00:00+00:00", False), ("old", "2026-10-14T00:00:00+00:00", True)):
            store = archive.ArchiveStore(self.root / label)
            with patch.object(archive, "_now", return_value=when):
                store.judgment(run_id=label, stage="candidate_final", context={"candidate": candidate([origin()])}, result={"system_retention": verdict})
            store.export(self.root / (label + ".zip"))
        master = archive.ArchiveStore(self.root / "master")
        for label in ("new", "old"):
            master.merge((self.root / (label + ".zip")).read_bytes())
        row = cli.label_view(master)["origins"][0]
        self.assertEqual(row["label_run_id"], "new")
        self.assertFalse(row["system_retention"])

    def test_retention_failures_are_isolated_and_reported(self):
        with patch.object(archive.ArchiveStore, "begin_call", side_effect=OSError("sensitive")):
            self.assertIsNone(archive.review_archive.begin("paradigm_extraction", "sub", "subject", {}, {"origins": [origin()]}))
        status = archive._read(self.directory / "capture-status.json")
        self.assertEqual(status["runs"][archive.review_archive.run_id]["errors"], [{"operation": "begin_call", "error_type": "OSError"}])
        self.assertNotIn("sensitive", json.dumps(status))

    def test_workflow_emails_zip_even_on_failure_and_never_uploads_it(self):
        text = (config.BASE_DIR / ".github/workflows/weekly-radar.yml").read_text()
        step = text.split("id: mail_reviews", 1)[1].split("- name:", 1)[0]
        self.assertIn("always()", step)
        self.assertIn(".private/mail/research-reviews-", step)
        self.assertIn("scripts/review_archive.py mail", step)
        self.assertIn("github.run_attempt", step)
        self.assertNotIn("reset_state", step)
        for removed in ("RESEARCH_ARCHIVE_KEY", "archive_initialize", "archive_feedback", "upload_reviews", "cloud-prepare"):
            self.assertNotIn(removed, text)
        for block in text.split("uses: actions/upload-artifact")[1:]:
            self.assertNotIn(".private", block.split("- name:", 1)[0])


class ModelCaptureTests(ArchiveFixture, unittest.IsolatedAsyncioTestCase):
    async def test_data_email_uses_existing_recipients_and_single_plain_zip(self):
        from notifications import email_notifier as mailer
        self.call()
        archive.review_archive.checkpoint()
        path = self.root / "reviews.zip"
        settings = {"EMAIL_PUSH_ENABLED": True, "SMTP_HOST": "smtp.offline.invalid", "SMTP_USERNAME": "sender@offline.invalid",
            "SMTP_PASSWORD": "offline-smtp-password", "SMTP_FROM": "sender@offline.invalid", "SMTP_TO": ["owner@offline.invalid"], "SMTP_USE_SSL": True}
        with patch.multiple(config, **settings), patch.dict(os.environ, {}, clear=True), \
                patch.object(mailer, "scheduled_date", return_value="2026-10-15"), patch.object(mailer, "_deliver_message") as send:
            self.assertEqual(await cli.mail_archive(self.directory, path), 0)
        message = send.call_args.args[0]
        self.assertEqual(message["To"], "owner@offline.invalid")
        self.assertTrue(send.call_args.kwargs["require_all_recipients"])
        self.assertIn("研究数据留存", message["Subject"])
        attachments = list(message.iter_attachments())
        self.assertEqual(len(attachments), 1)
        self.assertEqual(attachments[0].get_content_type(), "application/zip")
        self.assertEqual(attachments[0].get_payload(decode=True), path.read_bytes())
        files = archive.read_bundle(path.read_bytes())
        self.assertTrue(any(name.startswith("calls/") for name in files))
        self.assertNotIn("offline-smtp-password", json.dumps(files))

    async def test_data_email_failure_leaves_zip_and_does_not_modify_feedback(self):
        from notifications import email_notifier as mailer
        self.call()
        archive.review_archive.checkpoint()
        before = (self.directory / "feedback.json").read_bytes()
        output = self.root / "failed-delivery.zip"
        with patch.dict(os.environ, {}, clear=True), patch.object(mailer, "send_review_archive_email", new=AsyncMock(side_effect=RuntimeError("failed"))):
            with self.assertRaises(RuntimeError):
                await cli.mail_archive(self.directory, output)
        self.assertTrue(output.exists())
        self.assertEqual((self.directory / "feedback.json").read_bytes(), before)

    async def test_data_capture_errors_mailed_then_reported_not_silently_successful(self):
        self.call()
        archive.review_archive.errors.append({"operation": "finish_call", "error_type": "OSError"})
        archive.review_archive.checkpoint()
        with patch.dict(os.environ, {}, clear=True), patch("notifications.email_notifier.send_review_archive_email", new=AsyncMock()) as send:
            with self.assertRaisesRegex(archive.ArchiveError, "capture errors"):
                await cli.mail_archive(self.directory, self.root / "healthy-data.zip")
            send.assert_awaited_once()
            self.assertEqual(send.call_args.args[1]["capture_errors"], 1)

    async def test_attachment_size_limit_fails_before_smtp_without_truncation(self):
        from notifications import email_notifier as mailer
        self.call()
        path = self.root / "large.zip"
        self.store.export(path)
        before = path.read_bytes()
        with patch.object(config, "EMAIL_MAX_ATTACHMENT_BYTES", 1), patch.object(mailer, "_deliver_message") as send:
            with self.assertRaisesRegex(archive.ArchiveError, "EMAIL_MAX_ATTACHMENT_BYTES"):
                mailer._send_review_archive_sync(path, {})
            send.assert_not_called()
        self.assertEqual(before, path.read_bytes())

    async def test_disabled_retention_never_sends_data_email(self):
        with patch.object(config, "RESEARCH_ARCHIVE_ENABLED", False), patch("notifications.email_notifier.send_review_archive_email", new=AsyncMock()) as send:
            self.assertEqual(await cli.mail_archive(self.root / "unused", self.root / "unused.zip"), 0)
            send.assert_not_awaited()

    async def test_partial_recipient_refusal_is_not_data_delivery_success(self):
        from notifications import email_notifier as mailer
        self.call()
        path = self.root / "reviews.zip"
        self.store.export(path)
        settings = {"EMAIL_PUSH_ENABLED": True, "SMTP_HOST": "smtp.offline.invalid", "SMTP_USERNAME": "sender@offline.invalid",
            "SMTP_PASSWORD": "offline-smtp-password", "SMTP_TO": ["owner@offline.invalid"], "SMTP_USE_SSL": True}
        with patch.multiple(config, **settings), patch.object(mailer.smtplib, "SMTP_SSL") as smtp:
            smtp.return_value.__enter__.return_value.send_message.return_value = {"owner@offline.invalid": (550, b"refused")}
            with self.assertRaisesRegex(RuntimeError, "not confirmed delivered"):
                await mailer.send_review_archive_email(path, {})

    async def test_concurrent_same_title_same_response_object_correct_association(self):
        audit = RunAudit()
        same_response = response()
        client = NS(chat=NS(completions=NS(create=AsyncMock(return_value=same_response))))
        async def one(item):
            answer = await audit.chat_completion(client, stage="paradigm_extraction", role="sub", subject=item.title,
                archive_context={"origins": [item]}, model="offline", messages=[{"role": "user", "content": item.url}])
            await asyncio.sleep(0)
            audit.record_llm(stage="paradigm_extraction", role="sub", subject=item.title, model="offline", response=answer)
        await asyncio.gather(one(origin("1")), one(origin("2")))
        rows = [archive._read(path) for path in (self.directory / "calls").glob("*.json")]
        self.assertEqual(len(rows), 2)
        self.assertTrue(all(row["validation"]["status"] == "passed" for row in rows))
        for row in rows:
            original = archive._read(self.directory / "origins" / (row["origin_refs"][0]["origin_id"] + ".json"))
            self.assertEqual(row["request"]["messages"][0]["content"], next(iter(original["versions"].values()))["url"])
        self.assertEqual(client.chat.completions.create.call_count, 2)

    async def test_enabled_disabled_business_output_and_sdk_arguments_identical(self):
        payload = {"canonical_name": "模拟机制", "thesis": "机制", "innovation_types": ["architecture"], "rubric_answers": rubric_answers()}
        outputs, kwargs = [], []
        for enabled in (False, True):
            with patch.object(config, "RESEARCH_ARCHIVE_ENABLED", enabled):
                client = NS(chat=NS(completions=NS(create=AsyncMock(return_value=response(json.dumps(payload))))))
                analyzer = ParadigmAnalyzer()
                with patch.object(analyzer, "_get_client", return_value=(client, "offline")):
                    outputs.append(await analyzer.extract(origin()))
                kwargs.append(client.chat.completions.create.call_args_list)
                self.assertEqual(client.chat.completions.create.call_count, 1)
        self.assertEqual(outputs[0], outputs[1])
        self.assertEqual(kwargs[0], kwargs[1])
        self.assertNotIn("archive_context", kwargs[0][0].kwargs)
        judgments = [archive._read(path) for path in (self.directory / "judgments").glob("*.json")]
        self.assertEqual(len(judgments), 1)
        self.assertEqual(len(judgments[0]["actual_request_ids"]), 1)

    async def test_rejected_extraction_retained_not_only_accepted(self):
        payload = {"innovation_types": ["architecture"], "rubric_answers": rubric_answers(weak_ids={"paradigm_shift"})}
        # Use the actual rubric's weakest choices, not an assumed criterion ID.
        from paradigms.rubric import load_rubric
        ids = {row["id"] for row in load_rubric()["common_criteria"]}
        payload["rubric_answers"] = rubric_answers(weak_ids=ids)
        client = NS(chat=NS(completions=NS(create=AsyncMock(return_value=response(json.dumps(payload))))))
        analyzer = ParadigmAnalyzer()
        with patch.object(analyzer, "_get_client", return_value=(client, "offline")):
            result = await analyzer.extract(origin())
        self.assertFalse(result[0].is_candidate)
        rows = [archive._read(path) for path in (self.directory / "judgments").glob("*.json")]
        self.assertIs(rows[0]["result"]["system_retention"], False)

    async def test_transport_exception_and_cancellation_preserve_input(self):
        audit = RunAudit()
        for exception in (RuntimeError("private-error"), asyncio.CancelledError()):
            client = NS(chat=NS(completions=NS(create=AsyncMock(side_effect=exception))))
            with self.assertRaises(type(exception)):
                await audit.chat_completion(client, stage="paradigm_extraction", role="sub", subject="subject",
                    archive_context={"origins": [origin()]}, model="offline", messages=[{"role": "user", "content": "request"}])
        rows = [archive._read(path) for path in (self.directory / "calls").glob("*.json")]
        self.assertEqual({row["transport_status"] for row in rows}, {"failed", "cancelled_usage_unknown"})
        self.assertNotIn("private-error", json.dumps(rows))

    async def test_repair_messages_and_validated_judgment_link_to_correct_attempt(self):
        payload = {"innovation_types": ["architecture"], "rubric_answers": rubric_answers()}
        client = NS(chat=NS(completions=NS(create=AsyncMock(side_effect=[response("invalid JSON"), response(json.dumps(payload))]))))
        analyzer = ParadigmAnalyzer()
        with patch.object(analyzer, "_get_client", return_value=(client, "offline")):
            await analyzer.extract(origin())
        calls = sorted([archive._read(path) for path in (self.directory / "calls").glob("*.json")], key=lambda row: row["started_at"])
        self.assertEqual(len(calls), 2)
        self.assertEqual([row["validation"]["status"] for row in calls], ["failed", "passed"])
        self.assertIn("上一轮", calls[1]["request"]["messages"][0]["content"])
        judgment = archive._read(next((self.directory / "judgments").glob("*.json")))
        self.assertEqual(judgment["actual_request_ids"], [calls[1]["call_id"]])

    async def test_technical_report_mechanisms_keep_separate_request_edges(self):
        payload = {"innovation_types": ["architecture"], "rubric_answers": rubric_answers()}
        client = NS(chat=NS(completions=NS(create=AsyncMock(return_value=response(json.dumps(payload))))))
        analyzer = ParadigmAnalyzer()
        with patch.object(analyzer, "_get_client", return_value=(client, "offline")):
            for number in (1, 2):
                result, error = await analyzer._assess_report_mechanism(origin(),
                    {"mechanism_id": str(number), "canonical_name": f"mechanism-{number}", "innovation_types": ["architecture"]}, index=number)
                self.assertEqual(error, "")
                self.assertIsNotNone(result)
        judgments = [archive._read(path) for path in (self.directory / "judgments").glob("*.json")]
        self.assertEqual(len(judgments), 2)
        for row in judgments:
            call = self.read_call(row["actual_request_ids"][0])
            self.assertEqual(row["result"]["normalized_extraction"]["canonical_name"], "mechanism-" + call["mechanism_id"])

    async def test_batch_qualification_records_missing_identity_without_false_decision(self):
        items = [origin(), origin("2")]
        payload = {"decisions": [{"fingerprint": items[0].fingerprint, "decision": "screen_out", "reason": "only an incremental change"},
                                 {"fingerprint": "foreign", "decision": "full_review", "reason": "foreign"}]}
        client = NS(chat=NS(completions=NS(create=AsyncMock(return_value=response(json.dumps(payload))))))
        analyzer = ParadigmAnalyzer()
        with patch.object(analyzer, "_get_client", return_value=(client, "offline")):
            decisions = await analyzer._screen_uncached_origin_batch(items)
        self.assertEqual(set(decisions), {items[0].fingerprint})
        rows = [archive._read(path) for path in (self.directory / "judgments").glob("*.json")]
        self.assertEqual(len(rows), 2)
        self.assertEqual({row["result"]["decision"] for row in rows}, {"screen_out", None})
        self.assertTrue(all(len(row["actual_request_ids"]) == 1 for row in rows))


if __name__ == "__main__":
    unittest.main()
