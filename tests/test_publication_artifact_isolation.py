"""Final indexes, local publication failures and durable delivery, offline."""
import copy
import sqlite3
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

import config
import main
from paradigms.models import (TechnicalEvidence, EvidenceType, ResearcherProfile,
                             key_researcher_profiles, delivery_researcher_profiles)
from paradigms.weekly_delivery import prepare_weekly_delivery
from reports.paradigm_generator import (
    ParadigmReportGenerator, ReportQualityContractError, _all_http_urls,
    _publication_route_violations, _route_draft_violations, _route_fragment_key,
    _ungrounded_url_violations, _attach_researcher_index,
)
from run_audit import run_audit
from tests import test_v0_delivery as fixture
from tests import test_execution_delivery as execution
from tests.test_weekly_publication import prose


class UrlBoundaryTests(unittest.TestCase):
    def test_bare_unicode_prose_delimiters_do_not_change_url(self):
        target = 'https://huggingface.co/nvidia'
        for left, right in [('（', '）'), ('【', '】'), ('《', '》'), ('“', '”'), ('「', '」')]:
            with self.subTest(right=right):
                self.assertEqual(_all_http_urls(left + target + right), {target})

    def test_explicit_markdown_target_is_exact_not_silently_repaired(self):
        self.assertIn('https://example.org/）', _all_http_urls('[原文](<https://example.org/）>)'))
        self.assertEqual(_all_http_urls('[原文](<https://example.org/a_(b)?x=1&y=2>)'),
                         {'https://example.org/a_(b)?x=1&y=2'})

    def test_link_labels_cannot_hide_an_extra_url(self):
        self.assertEqual(_all_http_urls('[https://evil.example](<https://good.example>)'),
                         {'https://evil.example', 'https://good.example'})


class PublicationArtifactTests(unittest.IsolatedAsyncioTestCase):
    setUp = fixture.V0DeliveryTests.setUp
    distinct_process = execution.ExecutionDeliveryTests.distinct_process

    def process(self, *args, **kwargs):
        value = fixture.V0DeliveryTests.process(self, *args, **kwargs)
        value.delivery_contract_version = 5
        return value

    async def sealed(self, total=2):
        process, _ = self.distinct_process(total=total, fail=-1)
        stats = await process.run(reference_time=fixture.NOW)
        generator = ParadigmReportGenerator(self.path, client=False)
        generator._draft_one_route = AsyncMock(side_effect=lambda date, item, **kw: prose(item))
        selected, stats, fragments = await prepare_weekly_delivery(
            self.store, generator, process.pending_delivery, stats, report_date=stats['report_date'])
        job = self.store.enqueue_report(selected, stats, report_date=stats['report_date'])
        for key, content in fragments.items():
            self.store.save_report_fragment(job.delivery_key, key, content)
        return process, generator, job, fragments

    async def deliver(self, generator, job, *, mail=True):
        with patch.object(config, 'EMAIL_PUSH_ENABLED', True), \
             patch('notifications.email_notifier.send_report_email', AsyncMock(return_value=mail)) as smtp, \
             patch.object(run_audit, 'write', return_value={'audit_markdown_path': 'offline.md'}):
            result = await main._deliver_paradigm_job(self.store, generator, job, recovered=False)
        return result, smtp

    async def test_real_generated_person_index_with_parenthesized_url_passes(self):
        _, _, job, fragments = await self.sealed()
        item = job.candidates[0]
        profile = item.researchers[0]
        target = 'https://research.example/alice'
        profile.profile_urls['homepage'] = target
        profile.background_summary = f'已核验公开职业背景（{target}），当前工作与此人的公开研究方向对应。'
        body = next(value for key, value in fragments.items() if ':' + item.key + ':' in key)
        self.assertFalse(_publication_route_violations(body, item))

    async def test_optional_biography_with_unverified_link_does_not_block_technology(self):
        _, _, job, fragments = await self.sealed()
        item = job.candidates[0]
        original = copy.deepcopy(item.researchers[0])
        item.researchers[0].background_summary = '履历存在未核验链接 https://unverified.example/profile 。'
        body = next(value for key, value in fragments.items() if ':' + item.key + ':' in key)
        self.assertFalse(_publication_route_violations(body, item))
        index = _attach_researcher_index('正文', [item])
        self.assertNotIn('https://unverified.example', index)
        self.assertIn('人物补充材料未通过发表核验', index)
        self.assertIn('https://unverified.example', item.researchers[0].background_summary)
        self.assertNotEqual(item.researchers[0], original)

    async def test_collective_historical_profile_is_retained_but_not_a_person(self):
        _, _, job, _ = await self.sealed()
        item = job.candidates[0]
        collective = copy.deepcopy(item.researchers[0])
        collective.name = 'NVIDIA'
        item.researchers.append(collective)
        self.assertNotIn(collective, key_researcher_profiles(item.researchers))
        self.assertNotIn(collective, delivery_researcher_profiles(item.researchers))
        self.assertIn(collective, item.researchers)
        index = _attach_researcher_index('正文', [item])
        self.assertNotIn('**NVIDIA**', index)

    async def test_metadata_only_artifact_failure_excludes_one_before_freezing(self):
        process, _ = self.distinct_process(total=3, fail=-1)
        stats = await process.run(reference_time=fixture.NOW)
        bad = process.pending_delivery[0]
        bad.route_family += ' https://not-evidence.example'
        generator = ParadigmReportGenerator(self.path, client=False)
        generator._draft_one_route = AsyncMock(side_effect=lambda date, item, **kw: prose(item))
        before = copy.deepcopy(bad.to_dict())
        selected, result, _ = await prepare_weekly_delivery(
            self.store, generator, process.pending_delivery, stats, report_date=stats['report_date'])
        self.assertNotIn(bad, selected)
        self.assertEqual(len(selected), 2)
        self.assertIn(bad.key, result['publication_excluded'])
        self.assertEqual(bad.to_dict(), before)

    async def test_frozen_bad_prose_does_not_suppress_two_healthy_peers(self):
        _, generator, job, fragments = await self.sealed(total=3)
        bad = job.candidates[0]
        key = _route_fragment_key(bad)
        self.store.save_report_fragment(job.delivery_key, key, fragments[key] + '\nhttps://guessed.example')
        snapshots = [item.to_dict() for item in self.store.load_candidate_snapshots({v.key for v in job.candidates})]
        result, smtp = await self.deliver(generator, job)
        self.assertTrue(result['email_sent'])
        self.assertEqual(result['published_route_count'], 2)
        self.assertTrue(result['publication_repaired'])
        self.assertEqual(generator._draft_one_route.await_count, 3)  # initial prose only
        smtp.assert_awaited_once()
        self.assertEqual([item.to_dict() for item in self.store.load_candidate_snapshots({v.key for v in job.candidates})], snapshots)
        with self.store._connect() as conn:
            rows = dict(conn.execute('SELECT paradigm_key,last_reported_signature FROM paradigms'))
        self.assertFalse(rows[bad.key])
        self.assertEqual(sum(bool(v) for v in rows.values()), 2)

    async def test_assembly_failure_sends_receipt_without_undoing_research(self):
        _, generator, job, _ = await self.sealed()
        snapshots = [v.to_dict() for v in self.store.load_candidate_snapshots({i.key for i in job.candidates})]
        import reports.paradigm_generator as report_module
        original = report_module._editorial_violations
        def fail_combination(content, candidates=None, **kwargs):
            return ['组合故障'] if len(candidates or []) > 1 else original(content, candidates, **kwargs)
        with patch.object(report_module, '_editorial_violations', side_effect=fail_combination):
            result, smtp = await self.deliver(generator, job)
        self.assertEqual(result['result_kind'], 'weekly_receipt')
        self.assertTrue(result['email_sent'])
        self.assertEqual(result['published_route_count'], 0)
        self.assertIn('已有技术研究结果', Path(result['report_path']).read_text())
        self.assertEqual([v.to_dict() for v in self.store.load_candidate_snapshots({i.key for i in job.candidates})], snapshots)
        self.assertFalse(self.store.load_pending_deep_candidates())
        smtp.assert_awaited_once()

    async def test_report_error_recovery_is_deterministic_not_another_model_call(self):
        _, generator, job, _ = await self.sealed()
        generator.generate = AsyncMock(side_effect=ReportQualityContractError(['renderer contract']))
        result, _ = await self.deliver(generator, job)
        self.assertTrue(result['email_sent'])
        self.assertEqual(result['published_route_count'], 2)
        generator.generate.assert_awaited_once()
        self.assertEqual(generator._draft_one_route.await_count, 2)

    async def test_all_frozen_bodies_bad_still_send_receipt_not_research_rollback(self):
        _, generator, job, fragments = await self.sealed()
        for key in fragments:
            self.store.save_report_fragment(job.delivery_key, key, '不可发表的旧草稿')
        result, _ = await self.deliver(generator, job)
        self.assertEqual(result['result_kind'], 'weekly_receipt')
        self.assertTrue(result['email_sent'])
        self.assertFalse(self.store.load_pending_deep_candidates())

    async def test_smtp_failure_after_repair_is_not_false_delivery(self):
        _, generator, job, _ = await self.sealed()
        generator.generate = AsyncMock(side_effect=ReportQualityContractError(['one render failure']))
        with patch.object(config, 'EMAIL_PUSH_ENABLED', True), \
             patch('notifications.email_notifier.send_report_email', AsyncMock(side_effect=RuntimeError('smtp outage'))), \
             patch.object(run_audit, 'write', return_value={'audit_markdown_path': 'offline.md'}):
            with self.assertRaisesRegex(RuntimeError, 'smtp outage'):
                await main._deliver_paradigm_job(self.store, generator, job, recovered=False)
        pending = self.store.load_pending_report_job()
        self.assertIsNotNone(pending)
        self.assertEqual(pending.status, 'rendered')
        self.assertFalse(self.store.load_pending_deep_candidates())
        with self.store._connect() as conn:
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM report_deliveries').fetchone()[0], 0)

    async def test_storage_failure_cannot_be_reclassified_as_publication_or_success(self):
        _, generator, job, _ = await self.sealed()
        with patch.object(self.store, 'save_rendered_report', side_effect=sqlite3.OperationalError('disk failure')):
            with self.assertRaises(sqlite3.OperationalError):
                await self.deliver(generator, job)
        self.assertFalse(self.store.load_pending_deep_candidates())
        self.assertEqual(self.store.get_report_job(job.delivery_key).status, 'pending_render')

    async def test_weekly_quarantine_does_not_rewrite_research_or_drop_fragments(self):
        _, _, job, fragments = await self.sealed()
        snapshots = [v.to_dict() for v in self.store.load_candidate_snapshots({i.key for i in job.candidates})]
        self.store.quarantine_report_job(job.delivery_key, 'publication failure', failure_kind='report_quality_contract')
        self.assertEqual(self.store.load_report_fragments(job.delivery_key), fragments)
        self.assertEqual([v.to_dict() for v in self.store.load_candidate_snapshots({i.key for i in job.candidates})], snapshots)
        self.assertEqual(self.store.campaigns.get(job.stats['research_campaign']['campaign_id']).status, 'superseded')

    async def test_fragment_storage_failure_propagates_not_a_writer_exclusion(self):
        _, generator, job, fragments = await self.sealed()
        with self.assertRaises(sqlite3.OperationalError):
            await generator.prepare_weekly_routes(job.candidates, date=job.report_date, cached=fragments,
                save_fragment=lambda *args: (_ for _ in ()).throw(sqlite3.OperationalError('disk full')))

    async def test_model_timeout_is_route_local_not_a_storage_failure(self):
        _, generator, job, _ = await self.sealed()
        bad = job.candidates[0]
        async def draft(date, item, **kwargs):
            if item.key == bad.key:
                raise TimeoutError('model timeout')
            return prose(item)
        generator._draft_one_route.side_effect = draft
        selected, _, failures = await generator.prepare_weekly_routes(job.candidates, date=job.report_date,
            cached={}, save_fragment=lambda *args: None)
        self.assertEqual(len(selected), 1)
        self.assertEqual(failures, {bad.key: 'TimeoutError'})

    async def test_replacement_write_failure_rolls_back_subset_and_campaign(self):
        _, generator, job, fragments = await self.sealed()
        bad_key = _route_fragment_key(job.candidates[0])
        self.store.save_report_fragment(job.delivery_key, bad_key, fragments[bad_key] + '\nhttps://invalid.example')
        campaign_id = job.stats['research_campaign']['campaign_id']
        before = self.store.campaigns.reconcile(campaign_id)
        with self.store._connect() as conn:
            count = conn.execute('SELECT COUNT(*) FROM report_outbox').fetchone()[0]
        with patch.object(self.store, 'save_rendered_report', side_effect=sqlite3.OperationalError('save failed')):
            with self.assertRaises(sqlite3.OperationalError):
                await self.deliver(generator, job)
        with self.store._connect() as conn:
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM report_outbox').fetchone()[0], count)
        self.assertEqual(self.store.campaigns.reconcile(campaign_id), before)
        self.assertEqual(self.store.get_report_job(job.delivery_key).status, 'pending_render')
        self.assertFalse(self.store.load_pending_deep_candidates())

    async def test_source_revision_changed_before_reuse_excludes_only_that_route(self):
        _, generator, job, _ = await self.sealed(total=3)
        bad = job.candidates[0]
        revised = copy.deepcopy(bad.evidence[0])
        revised.summary += ' A substantive new source revision.'
        self.store.observe_origins([revised])
        result, smtp = await self.deliver(generator, job)
        self.assertEqual(result['published_route_count'], 2)
        self.assertEqual(result['publication_excluded'][bad.key], 'PublicationArtifactNotReady')
        self.assertTrue(result['email_sent'])
        smtp.assert_awaited_once()
        with self.store._connect() as conn:
            self.assertFalse(conn.execute('SELECT last_reported_signature FROM paradigms WHERE paradigm_key=?', (bad.key,)).fetchone()[0])

    async def test_frozen_older_policy_keys_revalidate_without_paying_to_rewrite(self):
        _, generator, job, fragments = await self.sealed()
        frozen = {item.key: _route_fragment_key(item) for item in job.candidates}
        original = _route_fragment_key
        with patch('reports.paradigm_generator._route_fragment_key', side_effect=lambda item: 'changed:' + original(item)):
            result, _ = await self.deliver(generator, job)
        self.assertEqual(result['published_route_count'], 2)
        self.assertTrue(result['email_sent'])
        self.assertEqual(generator._draft_one_route.await_count, 2)
        self.assertEqual(job.stats['publication_draft_keys'], frozen)

    async def test_delivery_repair_never_runs_research_reconciliation(self):
        _, generator, job, _ = await self.sealed()
        generator.generate = AsyncMock(side_effect=RuntimeError('deterministic renderer fault'))
        with patch.object(self.store.campaigns, 'reconcile', side_effect=AssertionError('delivery must be read only')):
            result, smtp = await self.deliver(generator, job)
        self.assertTrue(result['email_sent'])
        self.assertEqual(result['published_route_count'], 2)
        generator.generate.assert_awaited_once()
        smtp.assert_awaited_once()

    async def test_stale_outbox_snapshot_cannot_overwrite_newer_frozen_stats(self):
        _, generator, job, fragments = await self.sealed()
        with self.store._connect() as conn:
            import json
            newer = {**job.stats, 'newer_marker': 'retain me'}
            conn.execute('UPDATE report_outbox SET stats_json=? WHERE delivery_key=?',
                         (json.dumps(newer), job.delivery_key))
        with self.assertRaisesRegex(ValueError, '冻结交付快照已变化'):
            main._repair_weekly_publication(self.store, job, fragments)
        self.assertEqual(self.store.get_report_job(job.delivery_key).stats, newer)

    async def test_collective_profile_never_triggers_trajectory_model(self):
        _, _, job, _ = await self.sealed()
        item = job.candidates[0]
        item.researchers = [ResearcherProfile(name='NVIDIA', background_summary='集体署名')]
        from paradigms.analyzer import ResearcherTrajectoryAnalyzer
        analyzer = ResearcherTrajectoryAnalyzer(client=False)
        analyzer._analyze_one = AsyncMock(side_effect=AssertionError('no person model for an organization'))
        await analyzer.run([item])
        analyzer._analyze_one.assert_not_awaited()
        self.assertEqual(item.researchers[0].name, 'NVIDIA')

    async def test_unconfirmed_required_mail_cannot_look_like_delivery_success(self):
        _, generator, job, _ = await self.sealed()
        with patch.object(config, 'EMAIL_PUSH_REQUIRED', True):
            with self.assertRaisesRegex(RuntimeError, 'SMTP 未确认发送'):
                await self.deliver(generator, job, mail=False)
        self.assertEqual(self.store.load_pending_report_job().status, 'rendered')
        self.assertFalse(self.store.load_pending_deep_candidates())
        with self.store._connect() as conn:
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM report_deliveries').fetchone()[0], 0)

    async def test_changed_execution_receipt_excludes_only_affected_publication(self):
        _, generator, job, _ = await self.sealed(total=3)
        bad = job.candidates[0]
        campaign_id = job.stats['research_campaign']['campaign_id']
        with self.store._connect() as conn:
            conn.execute("UPDATE campaign_work SET status='pending' WHERE campaign_id=? AND object_key=? AND kind IN ('deep','refresh')",
                         (campaign_id, bad.key))
        generator.generate = AsyncMock(side_effect=ReportQualityContractError(['retry frozen artifact']))
        result, smtp = await self.deliver(generator, job)
        self.assertTrue(result['email_sent'])
        self.assertEqual(result['published_route_count'], 2)
        self.assertIn(bad.key, result['publication_excluded'])
        smtp.assert_awaited_once()
