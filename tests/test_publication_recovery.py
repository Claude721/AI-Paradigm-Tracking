"""Recover only status-only publication requeues; no API or SMTP access."""
import copy
import json
from pathlib import Path
import sqlite3
import unittest
from unittest.mock import AsyncMock, patch

import config
import main
from paradigms.publication_recovery import recover_publication, snapshot_digest
from reports.paradigm_generator import ParadigmReportGenerator, _route_fragment_key
from run_audit import run_audit
from tests import test_publication_artifact_isolation as artifact


class RecoveryTests(unittest.IsolatedAsyncioTestCase):
    setUp = artifact.PublicationArtifactTests.setUp
    process = artifact.PublicationArtifactTests.process
    distinct_process = artifact.PublicationArtifactTests.distinct_process

    async def failed(self):
        process, generator, job, _ = await artifact.PublicationArtifactTests.sealed(self, total=3)
        self.store.quarantine_report_job(job.delivery_key, 'legacy report URL parser error', failure_kind='report_quality_contract')
        with self.store._connect() as conn:
            for item in job.candidates:
                payload = item.to_dict()
                payload['status'] = 'pending_deep'
                conn.execute("UPDATE paradigms SET status='pending_deep',payload_json=? WHERE paradigm_key=?",
                             (json.dumps(payload), item.key))
            conn.execute('DELETE FROM report_render_fragments WHERE delivery_key=?', (job.delivery_key,))
        old = self.store.get_report_job(job.delivery_key)
        return process, generator, old, {'snapshot_digest': snapshot_digest(old),
                                       'draft_keys': {v.key:_route_fragment_key(v) for v in old.candidates}}

    def rows(self):
        with self.store._connect() as conn:
            return conn.execute('SELECT paradigm_key,status,payload_json,last_reported_signature FROM paradigms ORDER BY paradigm_key').fetchall()

    async def test_dry_plan_leaves_research_outbox_and_failure_unchanged(self):
        _, _, job, bindings = await self.failed()
        before = self.rows()
        result = recover_publication(self.store, job.delivery_key, bindings)
        self.assertEqual(result['status'], 'planned')
        self.assertEqual(result['publishable_count'], 3)
        self.assertEqual(len(result['restore_keys']), 3)
        self.assertEqual(self.rows(), before)
        self.assertEqual(self.store.get_report_job(job.delivery_key), job)
        self.assertIsNone(self.store.load_pending_report_job())

    async def test_recovery_restores_only_status_and_preserves_original_failure(self):
        _, _, job, bindings = await self.failed()
        before = {row[0]:json.loads(row[2]) for row in self.rows()}
        with patch.object(self.store.campaigns, 'reconcile', side_effect=AssertionError('no research')):
            result = recover_publication(self.store, job.delivery_key, bindings, apply=True)
        self.assertEqual(result['restored_count'], 3)
        self.assertEqual(result['published_route_count'], 3)
        self.assertNotEqual(result['replacement_key'], job.delivery_key)
        self.assertEqual(self.store.get_report_job(job.delivery_key), job)
        replacement = self.store.load_pending_report_job()
        self.assertEqual(replacement.status, 'rendered')
        self.assertTrue(replacement.report_content)
        self.assertFalse(result['email_sent'])
        for key, status, raw, reported in self.rows():
            payload = json.loads(raw)
            before[key]['status'] = status
            self.assertEqual(payload, before[key])
            self.assertFalse(reported)

    async def test_repeated_recovery_is_idempotent_and_never_delivers(self):
        _, _, job, bindings = await self.failed()
        first = recover_publication(self.store, job.delivery_key, bindings, apply=True)
        before = self.rows()
        second = recover_publication(self.store, job.delivery_key, bindings, apply=True)
        self.assertEqual(second['status'], 'already_recovered')
        self.assertEqual(first['replacement_key'], second['replacement_key'])
        self.assertEqual(self.rows(), before)
        self.assertEqual(self.store.stats()['deliveries'], 0)

    async def test_newer_judgment_is_not_overwritten_or_published(self):
        _, _, job, bindings = await self.failed()
        bad = self.store.load_candidate_snapshots({job.candidates[0].key})[0]
        bad.thesis = 'newer technical judgment'
        self.store.save_candidates([bad])
        original = self.store.load_candidate_snapshots({bad.key})[0].to_dict()
        result = recover_publication(self.store, job.delivery_key, bindings, apply=True)
        self.assertEqual(result['restored_count'], 2)
        self.assertEqual(result['published_route_count'], 2)
        self.assertIn(bad.key, result['excluded'])
        self.assertEqual(self.store.load_candidate_snapshots({bad.key})[0].to_dict(), original)

    async def test_source_version_change_is_not_repaired_as_a_status_error(self):
        _, _, job, bindings = await self.failed()
        bad = job.candidates[0]
        revised = copy.deepcopy(bad.evidence[0])
        revised.summary += ' New authoritative source revision.'
        self.store.observe_origins([revised])
        result = recover_publication(self.store, job.delivery_key, bindings, apply=True)
        self.assertEqual(result['restored_count'], 2)
        self.assertIn(bad.key, result['excluded'])
        self.assertEqual(self.store.load_candidate_snapshots({bad.key})[0].status, 'pending_deep')

    async def test_changed_execution_attempt_is_not_inherited(self):
        _, _, job, bindings = await self.failed()
        bad = job.candidates[0]
        campaign = job.stats['research_campaign']['campaign_id']
        with self.store._connect() as conn:
            revision = conn.execute("SELECT input_revision FROM campaign_work WHERE campaign_id=? AND kind='deep' AND object_key=?",
                                    (campaign,bad.key)).fetchone()[0]
        attempts = self.store.campaigns.start_attempt(campaign,'deep',[(bad.key,revision)])
        self.store.campaigns.finish_attempts(attempts,outcome='execution_failed',reason='new failure')
        result = recover_publication(self.store, job.delivery_key, bindings, apply=True)
        self.assertEqual(result['restored_count'], 2)
        self.assertEqual(result['excluded'][bad.key], 'ExecutionReceiptChanged')

    async def test_one_bad_cached_draft_does_not_undo_successful_technical_judgment(self):
        _, _, job, bindings = await self.failed()
        bad = job.candidates[0]
        self.store.campaigns.cache_save('weekly_publication_draft', bindings['draft_keys'][bad.key], {'content':'malformed'})
        result = recover_publication(self.store, job.delivery_key, bindings, apply=True)
        self.assertEqual(result['restored_count'], 3)
        self.assertEqual(result['published_route_count'], 2)
        self.assertIn(bad.key,result['draft_excluded'])
        self.assertFalse(self.store.load_pending_deep_candidates())

    async def test_foreign_binding_or_identity_cannot_modify_state(self):
        _, _, job, bindings = await self.failed()
        before = self.rows()
        with self.assertRaisesRegex(ValueError,'原稿绑定不属于'):
            recover_publication(self.store, job.delivery_key, {**bindings,'snapshot_digest':'0'*64}, apply=True)
        wrong = copy.deepcopy(bindings)
        first, second = job.candidates[:2]
        wrong['draft_keys'][first.key] = wrong['draft_keys'][second.key]
        with self.assertRaisesRegex(ValueError,'原稿键没有绑定'):
            recover_publication(self.store, job.delivery_key, wrong, apply=True)
        self.assertEqual(self.rows(),before)

    async def test_failure_after_new_outbox_rolls_back_all_status_changes(self):
        _, _, job, bindings = await self.failed()
        before = self.rows()
        repair = main._repair_weekly_publication
        def fail(*args,**kwargs):
            repair(*args,**kwargs)
            raise sqlite3.OperationalError('write failed before status repair')
        with patch.object(main,'_repair_weekly_publication',side_effect=fail):
            with self.assertRaises(sqlite3.OperationalError):
                recover_publication(self.store,job.delivery_key,bindings,apply=True)
        self.assertEqual(self.rows(),before)
        self.assertIsNone(self.store.load_pending_report_job())
        self.assertEqual(self.store.get_report_job(job.delivery_key),job)

    async def test_manual_feedback_and_unrelated_pending_work_are_preserved(self):
        _, _, job, bindings = await self.failed()
        with self.store._connect() as conn:
            row=conn.execute('SELECT payload_json FROM paradigms WHERE paradigm_key=?',(job.candidates[0].key,)).fetchone()
            payload=json.loads(row[0]); payload['human_feedback']={'retain':False,'note':'my preference'}
            conn.execute('UPDATE paradigms SET payload_json=? WHERE paradigm_key=?',(json.dumps(payload),job.candidates[0].key))
        unfinished = copy.deepcopy(job.candidates[0]); unfinished.key='truly-unfinished'
        unfinished.status='pending_deep'; unfinished.deep_checkpoint_stage=''
        self.store.save_candidates([unfinished])
        result=recover_publication(self.store,job.delivery_key,bindings,apply=True)
        self.assertEqual(result['restored_count'],3)
        with self.store._connect() as conn:
            payload=json.loads(conn.execute('SELECT payload_json FROM paradigms WHERE paradigm_key=?',(job.candidates[0].key,)).fetchone()[0])
        self.assertEqual(payload['human_feedback'],{'retain':False,'note':'my preference'})
        self.assertEqual([v.key for v in self.store.load_pending_deep_candidates()],['truly-unfinished'])

    async def test_next_pipeline_restores_report_without_starting_research(self):
        process,generator,job,bindings=await self.failed()
        result=recover_publication(self.store,job.delivery_key,bindings,apply=True)
        process.run=AsyncMock(side_effect=AssertionError('must not run research'))
        with patch('main._check_env'),patch('main._print_model_banner'), \
             patch('agents.paradigm_orchestrator.ParadigmOrchestrator',return_value=process), \
             patch('reports.paradigm_generator.ParadigmReportGenerator',return_value=generator), \
             patch.object(config,'EMAIL_PUSH_ENABLED',True), \
             patch('notifications.email_notifier.send_report_email',AsyncMock(return_value=True)) as smtp, \
             patch.object(run_audit,'write',return_value={'audit_markdown_path':'offline.md'}):
            delivered=await main._run_pipeline_once()
        process.run.assert_not_awaited()
        smtp.assert_awaited_once()
        self.assertTrue(delivered['recovered_delivery_only'])
        self.assertTrue(delivered['email_sent'])
        self.assertEqual(delivered['published_route_count'],3)
        self.assertEqual(self.store.get_report_job(job.delivery_key),job)
        self.assertEqual(self.store.get_report_job(result['replacement_key']).status,'delivered')
