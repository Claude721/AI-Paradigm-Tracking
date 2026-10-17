"""Current V0 priority: weekly delivery, independent of research completeness."""
import asyncio
import copy
import json
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

import config
import main
from paradigms.completion import ResearchNotCompleteError, require_completed_research
from paradigms.delivery_policy import delivery_proof, weekly_result_kind
from paradigms.weekly_delivery import prepare_weekly_delivery
from reports.paradigm_generator import ParadigmReportGenerator, _route_fragment_key
from notifications.email_notifier import _send_sync
from run_audit import run_audit
from tests import test_v0_delivery as fixture
from tests import test_execution_delivery as execution


def prose(item):
    return ("### 状态预测如何改变行动接口\n\n"
            + "这条路线从旧方法的能力边界出发，解释训练信号如何改变状态预测和推理接口，同时保留实验尚未验证的边界。" * 9
            + f"\n\nA. Researcher 参与这项研究。[一手原文]({item.evidence[0].url})。\n\n"
            + "**讨论势能判断：** 目前主要是发布方提出的机制，尚无独立复现；部分检索通道不可用，不据此推断没有讨论。")


class WeeklyPublicationTests(unittest.IsolatedAsyncioTestCase):
    setUp = fixture.V0DeliveryTests.setUp

    def process(self, *args, **kwargs):
        value = fixture.V0DeliveryTests.process(self, *args, **kwargs)
        value.delivery_contract_version = 5
        return value

    distinct_process = execution.ExecutionDeliveryTests.distinct_process

    def generator(self):
        value = ParadigmReportGenerator(self.path, client=False)
        value._draft_one_route = AsyncMock(side_effect=lambda date, item, **kw: prose(item))
        return value

    async def publish(self, process, stats, generator=None):
        generator = generator or self.generator()
        selected, stats, fragments = await prepare_weekly_delivery(
            self.store, generator, process.pending_delivery, stats, report_date=stats['report_date'])
        job = self.store.enqueue_report(selected, stats, report_date=stats['report_date'])
        for key, content in fragments.items():
            self.store.save_report_fragment(job.delivery_key, key, content)
        with patch.object(config, 'EMAIL_PUSH_ENABLED', False), patch.object(run_audit, 'write', return_value={'audit_markdown_path': 'offline.md'}):
            result = await main._deliver_paradigm_job(self.store, generator, job, recovered=False)
        return result, job, generator

    async def test_budget_and_unattempted_dependency_do_not_hold_healthy_route(self):
        process = self.process(accepted=True)
        stats = await process.run(reference_time=fixture.NOW)
        key = stats['research_campaign']['campaign_id']
        self.store.campaigns.include(key, 'refresh', [('unvisited-history', 'input-v1')])
        ledger = self.store.campaigns.reconcile(key)
        stats.update(research_campaign=ledger, run_budget_exhausted=True, research_incomplete=True,
                     coverage_incomplete=True, execution_complete=False)
        stats['delivery_scope'] = delivery_proof(stats, process.pending_delivery, ledger, minimum_sources=2)
        self.store.campaigns.save_stats(key, stats, ready=True)
        result, job, _ = await self.publish(process, stats)
        self.assertEqual(result['result_kind'], 'weekly_memo')
        self.assertEqual(len(job.candidates), 1)
        self.assertEqual(result['delivery_scope']['refresh_unattempted'], 1)
        self.assertEqual(self.store.campaigns.snapshot(key)['refresh_pending'], 1)
        content = Path(result['report_path']).read_text()
        self.assertIn('因预算保护停止新增调用', content)
        self.assertNotIn('阶段性', content)
        self.assertIsNone(self.store.campaigns.active())

    async def test_writer_failure_only_excludes_that_route_and_preserves_judgment(self):
        process, _ = self.distinct_process(total=3, fail=-1)
        stats = await process.run(reference_time=fixture.NOW)
        generator = self.generator()
        bad = process.pending_delivery[0]
        before = copy.deepcopy(bad.to_dict())
        async def draft(date, item, **kwargs):
            if item.key == bad.key:
                raise RuntimeError('offline writer failure')
            return prose(item)
        generator._draft_one_route.side_effect = draft
        result, job, _ = await self.publish(process, stats, generator)
        self.assertEqual(len(job.candidates), 2)
        self.assertEqual(result['publication_excluded'], {bad.key: 'RuntimeError'})
        self.assertEqual(bad.to_dict(), before)
        saved = self.store.load_candidate_snapshots([bad.key])[0]
        self.assertNotEqual(saved.status, 'pending_deep')
        with self.store._connect() as conn:
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM report_deliveries').fetchone()[0], 2)
            self.assertFalse(conn.execute('SELECT last_reported_signature FROM paradigms WHERE paradigm_key=?', (bad.key,)).fetchone()[0])

    async def test_all_model_failures_send_receipt_not_no_important_technology(self):
        process, _ = self.distinct_process()
        process.synthesizer.run = AsyncMock(side_effect=RuntimeError('offline model outage'))
        stats = await process.run(reference_time=fixture.NOW)
        self.assertEqual(stats['result_kind'], 'weekly_receipt')
        result, job, generator = await self.publish(process, stats)
        self.assertEqual(job.candidates, [])
        generator._draft_one_route.assert_not_awaited()
        content = Path(result['report_path']).read_text()
        self.assertIn('没有可发表的已核验技术路线', content)
        self.assertIn('不代表本周没有重要技术', content)
        self.assertNotIn('本期研究与交付已完成', content)
        self.assertEqual(len(self.store.load_pending_deep_candidates()), 3)

    async def test_all_source_outage_still_delivers_truthful_weekly_receipt(self):
        process = self.process(fixture.batch([], outage=True))
        stats = await process.run(reference_time=fixture.NOW)
        result, job, _ = await self.publish(process, stats)
        self.assertEqual(job.stats['delivery_scope']['usable_primary_sources'], [])
        self.assertTrue(result['coverage_incomplete'])

    async def test_discovery_exception_produces_receipt_without_fake_research_success(self):
        process = self.process()
        process.discovery.run = AsyncMock(side_effect=RuntimeError('offline discovery outage'))
        stats = await process.run(reference_time=fixture.NOW)
        self.assertTrue(stats['research_stage_interrupted'])
        result, _, _ = await self.publish(process, stats)
        self.assertEqual(result['result_kind'], 'weekly_receipt')
        self.assertFalse(result['execution_complete'])

    async def test_late_stage_exception_recovers_committed_healthy_route(self):
        process = self.process(accepted=True)
        base = process._run_research
        async def crash_after_commit():
            await base()
            raise RuntimeError('offline late aggregate fault')
        process._run_research = crash_after_commit
        stats = await process.run(reference_time=fixture.NOW)
        self.assertTrue(stats['research_stage_interrupted'])
        self.assertEqual(len(process.pending_delivery), 1)
        result, job, _ = await self.publish(process, stats)
        self.assertEqual(len(job.candidates), 1)

    async def test_writer_timeout_preserves_successful_peer_without_overview_request(self):
        process, _ = self.distinct_process(total=3, fail=-1)
        stats = await process.run(reference_time=fixture.NOW)
        generator = self.generator()
        bad = process.pending_delivery[0].key
        async def draft(date, item, **kwargs):
            if item.key == bad:
                await asyncio.Event().wait()
            return prose(item)
        generator._draft_one_route.side_effect = draft
        with patch.object(config, 'PARADIGM_REPORT_TIMEOUT_SECONDS', 0.05), patch.object(generator, '_editorial_frame', AsyncMock(side_effect=AssertionError('must not call overview'))):
            result, job, _ = await self.publish(process, stats, generator)
        self.assertEqual(len(job.candidates), 2)
        self.assertEqual(result['publication_excluded'][bad], 'PublicationTimeout')

    async def test_all_writer_failures_deliver_receipt_and_keep_technical_success(self):
        process = self.process(accepted=True)
        stats = await process.run(reference_time=fixture.NOW)
        generator = self.generator()
        generator._draft_one_route.side_effect = RuntimeError('offline budget exhausted')
        result, job, _ = await self.publish(process, stats, generator)
        self.assertFalse(job.candidates)
        self.assertEqual(result['new_paradigms'], 0)
        self.assertEqual(result['delivery_scope']['deep_completed'], 1)
        self.assertEqual(len(self.store.load_pending_deep_candidates()), 0)

    async def test_wrong_urls_exclude_only_bad_prose_not_good_peer(self):
        process, _ = self.distinct_process(total=3, fail=-1)
        stats = await process.run(reference_time=fixture.NOW)
        generator = self.generator()
        bad = process.pending_delivery[0].key
        generator._draft_one_route.side_effect = lambda date, item, **kw: prose(item) + (' [猜测链接](https://untrusted.invalid/fake)' if item.key == bad else '')
        result, job, _ = await self.publish(process, stats, generator)
        self.assertEqual(len(job.candidates), 2)
        self.assertNotIn('untrusted.invalid', Path(result['report_path']).read_text())

    async def test_tampered_receipts_are_still_rejected(self):
        process = self.process(accepted=True)
        stats = await process.run(reference_time=fixture.NOW)
        stats['delivery_scope']['deep_completed'] += 1
        with self.assertRaises(ResearchNotCompleteError):
            require_completed_research(stats)

    async def test_smtp_failure_remains_retryable_not_false_delivery(self):
        process = self.process(accepted=True)
        stats = await process.run(reference_time=fixture.NOW)
        generator = self.generator()
        selected, stats, fragments = await prepare_weekly_delivery(self.store, generator, process.pending_delivery, stats, report_date=stats['report_date'])
        job = self.store.enqueue_report(selected, stats, report_date=stats['report_date'])
        for key, value in fragments.items():
            self.store.save_report_fragment(job.delivery_key, key, value)
        with patch.object(config, 'EMAIL_PUSH_ENABLED', True), patch('notifications.email_notifier.send_report_email', AsyncMock(side_effect=RuntimeError('offline SMTP down'))), patch.object(run_audit, 'write', return_value={'audit_markdown_path': 'offline.md'}):
            with self.assertRaisesRegex(RuntimeError, 'SMTP down'):
                await main._deliver_paradigm_job(self.store, generator, job, recovered=False)
        self.assertIsNotNone(self.store.load_pending_report_job())
        with self.store._connect() as conn:
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM report_deliveries').fetchone()[0], 0)

    async def test_latest_main_path_delivers_memo_with_failed_peer_and_one_writer_call(self):
        process = self.process(fixture.batch([fixture.origin(), fixture.origin(2)]), accepted=True, omit={fixture.origin(2).fingerprint})
        base = process.run
        process.run = lambda: base(reference_time=fixture.NOW)
        generator = self.generator()
        sender = AsyncMock(return_value=True)
        with patch('main._check_env'), patch('main._print_model_banner'), patch('agents.paradigm_orchestrator.ParadigmOrchestrator', return_value=process), patch('reports.paradigm_generator.ParadigmReportGenerator', return_value=generator), patch('notifications.email_notifier.send_report_email', sender), patch.object(config, 'EMAIL_PUSH_ENABLED', True), patch.object(config, 'EMAIL_PUSH_REQUIRED', True), patch.object(run_audit, 'write', return_value={'audit_markdown_path': 'offline.md'}):
            result = await main._run_pipeline_once()
        self.assertTrue(result['email_sent'])
        self.assertEqual(result['result_kind'], 'weekly_memo')
        self.assertEqual(result['delivery_scope']['origin_failed'], 1)
        self.assertEqual(generator._draft_one_route.await_count, 1)
        sender.assert_awaited_once()

    async def test_main_receipt_is_mailed_even_when_discovery_throws(self):
        process = self.process()
        process.discovery.run = AsyncMock(side_effect=RuntimeError('offline discovery outage'))
        base = process.run
        process.run = lambda: base(reference_time=fixture.NOW)
        sender = AsyncMock(return_value=True)
        with patch('main._check_env'), patch('main._print_model_banner'), patch('agents.paradigm_orchestrator.ParadigmOrchestrator', return_value=process), patch('reports.paradigm_generator.ParadigmReportGenerator', return_value=self.generator()), patch('notifications.email_notifier.send_report_email', sender), patch.object(config, 'EMAIL_PUSH_ENABLED', True), patch.object(config, 'EMAIL_PUSH_REQUIRED', True), patch.object(run_audit, 'write', return_value={'audit_markdown_path': 'offline.md'}):
            result = await main._run_pipeline_once()
        self.assertEqual(result['result_kind'], 'weekly_receipt')
        self.assertEqual(result['research_health'], 'interrupted')
        self.assertTrue(result['email_sent'])
        sender.assert_awaited_once()

    async def test_actual_email_receipt_subject_and_body_do_not_claim_research_complete(self):
        process = self.process(fixture.batch([], outage=True))
        stats = await process.run(reference_time=fixture.NOW)
        result, _, _ = await self.publish(process, stats)
        with patch.object(config, 'SMTP_TO', ['offline@example.invalid']), patch.object(config, 'SMTP_USE_SSL', True), patch('notifications.email_notifier.smtplib.SMTP_SSL') as smtp:
            _send_sync(Path(result['report_path']), result)
        message = smtp.return_value.__enter__.return_value.send_message.call_args.args[0]
        body = message.get_body(preferencelist=('plain',)).get_content()
        self.assertIn('运行回执', message['Subject'])
        self.assertNotIn('阶段性', message['Subject'] + body)
        self.assertNotIn('本期研究与交付已完成', body)
        self.assertIn('不声称所有研究已完成', body)

    async def test_cached_prose_recovers_missing_outbox_fragments_without_model_calls(self):
        process = self.process(accepted=True)
        stats = await process.run(reference_time=fixture.NOW)
        generator = self.generator()
        selected, stats, fragments = await prepare_weekly_delivery(self.store, generator, process.pending_delivery, stats, report_date=stats['report_date'])
        job = self.store.enqueue_report(selected, stats, report_date=stats['report_date'])
        # Simulate a crash before attaching fragment rows. The independently
        # durable evidence-bound draft cache must still prevent repeat spending.
        generator._draft_one_route.reset_mock()
        with patch.object(config, 'EMAIL_PUSH_ENABLED', False), patch.object(run_audit, 'write', return_value={'audit_markdown_path': 'offline.md'}):
            result = await main._deliver_paradigm_job(self.store, generator, job, recovered=True)
        generator._draft_one_route.assert_not_awaited()
        self.assertTrue(Path(result['report_path']).exists())

    async def test_missing_freshness_excludes_only_that_route(self):
        process, _ = self.distinct_process(total=3, fail=-1)
        stats = await process.run(reference_time=fixture.NOW)
        bad = process.pending_delivery[0]
        bad.freshness_assessment = {}
        # The boundary must not trust a missing assessment as an include.
        result, job, _ = await self.publish(process, stats)
        self.assertEqual(len(job.candidates), 2)
        self.assertEqual(result['publication_excluded'][bad.key], 'PublicationInputNotReady')

    async def test_open_v4_plan_upgrades_without_changing_existing_judgments(self):
        process = self.process(accepted=True)
        process.delivery_contract_version = 4
        stats = await process.run(reference_time=fixture.NOW)
        key = stats['research_campaign']['campaign_id']
        before = self.store.load_candidate_snapshots([row['key'] for row in stats['delivery_scope']['routes']])[0].to_dict()
        migrated = self.store.campaigns.upgrade_execution_contract(key, target_version=5)
        self.assertEqual(migrated.stats['delivery_contract_version'], 5)
        self.assertEqual(migrated.reference_time, fixture.NOW)
        self.assertEqual(self.store.load_candidate_snapshots([before['key']])[0].to_dict(), before)
        # Upgrade alone does not fabricate a newly approved publication proof.
        self.assertNotIn('delivery_scope', migrated.stats)

    async def test_frozen_v4_outbox_is_not_silently_upgraded(self):
        process = self.process(accepted=True)
        process.delivery_contract_version = 4
        stats = await process.run(reference_time=fixture.NOW)
        job = self.store.enqueue_report(process.pending_delivery, stats, report_date=stats['report_date'])
        migrated = self.store.campaigns.upgrade_execution_contract(stats['research_campaign']['campaign_id'], target_version=5)
        self.assertEqual(migrated.stats['delivery_contract_version'], 4)
        self.assertEqual(self.store.get_report_job(job.delivery_key).stats['delivery_contract_version'], 4)

    async def test_pipeline_result_distinguishes_delivered_receipt_from_healthy_research(self):
        process = self.process(fixture.batch([], outage=True))
        stats = await process.run(reference_time=fixture.NOW)
        result, _, _ = await self.publish(process, stats)
        with patch('main.Path', return_value=self.path / 'pipeline_result.json'):
            path = main._write_pipeline_result(result)
        value = json.loads(path.read_text())
        self.assertTrue(value['delivery_ready'])
        self.assertFalse(value['fresh_research_completed'])
        self.assertEqual(value['research_health'], 'with_gaps')

    async def test_unsealed_origin_scope_does_not_hold_completed_route(self):
        bad = fixture.origin(2)
        process = self.process(fixture.batch([fixture.origin(), bad]), accepted=True, omit={bad.fingerprint})
        stats = await process.run(reference_time=fixture.NOW)
        key = stats['research_campaign']['campaign_id']
        # Fixture simulates a real budget receipt instead of a failed response.
        with self.store._connect() as conn:
            conn.execute("UPDATE research_attempts SET outcome='budget_deferred' WHERE campaign_id=? AND kind='origin' AND object_key=?", (key, bad.fingerprint))
        ledger = self.store.campaigns.reconcile(key)
        self.assertFalse(ledger['deep_scope_sealed'])
        self.assertEqual(ledger['origin_unattempted'], 1)
        stats.update(research_campaign=ledger, run_budget_exhausted=True)
        stats['delivery_scope'] = delivery_proof(stats, process.pending_delivery, ledger, minimum_sources=2)
        self.store.campaigns.save_stats(key, stats, ready=True)
        result, job, _ = await self.publish(process, stats)
        self.assertEqual(len(job.candidates), 1)
        self.assertEqual(result['delivery_scope']['origin_unattempted'], 1)

    async def test_unattempted_deep_peer_does_not_hold_successful_peers(self):
        process, _ = self.distinct_process(total=3, fail=0)
        stats = await process.run(reference_time=fixture.NOW)
        key = stats['research_campaign']['campaign_id']
        with self.store._connect() as conn:
            conn.execute("UPDATE research_attempts SET outcome='dependency_pending' WHERE campaign_id=? AND kind='deep' AND outcome='execution_failed'", (key,))
        ledger = self.store.campaigns.reconcile(key)
        self.assertEqual(ledger['deep_unattempted'], 1)
        stats.update(research_campaign=ledger)
        stats['delivery_scope'] = delivery_proof(stats, process.pending_delivery, ledger, minimum_sources=2)
        self.store.campaigns.save_stats(key, stats, ready=True)
        result, job, _ = await self.publish(process, stats)
        self.assertEqual(len(job.candidates), 2)
        self.assertEqual(result['delivery_scope']['deep_unattempted'], 1)

    async def test_tampered_persisted_receipt_is_regenerated_not_mailed(self):
        process = self.process(fixture.batch([], outage=True))
        stats = await process.run(reference_time=fixture.NOW)
        generator = self.generator()
        selected, stats, _ = await prepare_weekly_delivery(self.store, generator, [], stats, report_date=stats['report_date'])
        job = self.store.enqueue_report([], stats, report_date=stats['report_date'])
        self.store.save_rendered_report(job.delivery_key, '[猜测链接](https://untrusted.invalid/tampered)')
        job = self.store.get_report_job(job.delivery_key)
        with patch.object(config, 'EMAIL_PUSH_ENABLED', False), patch.object(run_audit, 'write', return_value={'audit_markdown_path': 'offline.md'}):
            result = await main._deliver_paradigm_job(self.store, generator, job, recovered=True)
        self.assertNotIn('untrusted.invalid', Path(result['report_path']).read_text())
        self.assertIn('本周运行回执', Path(result['report_path']).read_text())

    def test_auxiliary_review_mail_failure_cannot_flip_delivered_weekly_job(self):
        path = Path(__file__).resolve().parents[1] / '.github/workflows/weekly-radar.yml'
        text = path.read_text()
        step = text.split('id: mail_reviews', 1)[1].split('- name:', 1)[0]
        self.assertIn('continue-on-error: true', step)
        self.assertIn('steps.mail_reviews.outcome == \'failure\'', text)
        self.assertIn('本轮数据留存未完成', text)




