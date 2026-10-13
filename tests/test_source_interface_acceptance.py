"""Granular authorization, request bounds and no-new-PAYG acceptance."""
from __future__ import annotations

import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stdout, redirect_stderr
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import AsyncMock, patch

import httpx

import config
from scripts import source_interface_check as check
from source_audit import Entry, SourceAudit, NYU_BIBLIOGRAPHY_URL

NOW = datetime(2026, 10, 13, tzinfo=timezone.utc)
NYU = Entry('official:nyu-test', 'official', NYU_BIBLIOGRAPHY_URL)
TAVILY = Entry('tavily', 'separate-acceptance', 'https://api.tavily.com/search',
    credential='TAVILY_API_KEY', deferred_reason='requires_separate_authorized_platform_acceptance')
OUTSIDE = Entry('x', 'separate-acceptance', 'https://api.x.com/2/tweets/search/recent')


async def no_sleep(_):
    pass


def usage(**overrides):
    account = dict(plan_usage=0, plan_limit=1000, paygo_usage=0, paygo_limit=0)
    account.update(overrides)
    return {'account': account, 'key': {'usage': 0, 'limit': 1000}}


class InterfacePlanTests(unittest.TestCase):
    def test_exact_scope_never_selects_other_platforms(self):
        manifest = [OUTSIDE, NYU, TAVILY]
        self.assertEqual(check.select_entries(manifest, 'nyu'), [NYU])
        self.assertEqual(check.select_entries(manifest, 'tavily'), [TAVILY])
        self.assertEqual(check.select_entries(manifest, 'both'), [NYU, TAVILY])

    def test_missing_duplicate_disabled_or_unknown_scope_rejects(self):
        for manifest, scope in (([TAVILY], 'nyu'), ([NYU, NYU], 'nyu'),
                ([Entry('tavily', 'separate-acceptance', TAVILY.url, enabled=False)], 'tavily'),
                ([NYU, TAVILY], 'x')):
            with self.subTest(scope=scope), self.assertRaises(ValueError):
                check.select_entries(manifest, scope)

    def test_list_is_network_free_and_does_not_require_credentials(self):
        with patch.object(check, 'build_entries', return_value=[NYU, TAVILY]), \
                patch.object(check, 'execute', new_callable=AsyncMock) as execute, \
                redirect_stdout(io.StringIO()) as stdout:
            self.assertEqual(check.main(['--scope', 'both', '--list']), 0)
        execute.assert_not_called()
        self.assertEqual(len(json.loads(stdout.getvalue())), 2)

    def test_live_authorization_is_required_before_execution(self):
        with patch.object(check, 'execute', new_callable=AsyncMock) as execute, \
                redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            check.main(['--scope', 'both'])
        execute.assert_not_called()

    def test_preflight_failure_saves_zero_request_reason_and_version_without_keys(self):
        with tempfile.TemporaryDirectory() as directory, \
                patch.dict(os.environ, {'GITHUB_STEP_SUMMARY': str(Path(directory) / 'mock-step-summary.md')}), \
                patch.object(check, 'execute', new_callable=AsyncMock,
                    side_effect=check.InterfacePreflightError('OPENALEX_API_KEY_not_configured')), \
                redirect_stdout(io.StringIO()):
            code = check.main(['--scope', 'nyu', '--live-authorized', '--output-dir', directory])
            result = json.loads((Path(directory) / 'source_interface_latest.json').read_text())
            summary = (Path(directory) / 'mock-step-summary.md').read_text()
        self.assertEqual(code, 1)
        self.assertEqual(result['request_count'], 0)
        self.assertEqual(result['reason'], 'OPENALEX_API_KEY_not_configured')
        self.assertTrue(result['runtime_provenance']['source_fingerprint'])
        self.assertFalse(result['production_v0_accepted'])
        self.assertIn('OPENALEX_API_KEY_not_configured', summary)

    def test_unexpected_execution_exception_never_exposes_text_or_claims_zero_usage(self):
        with tempfile.TemporaryDirectory() as directory, \
                patch.dict(os.environ, {'GITHUB_STEP_SUMMARY': str(Path(directory) / 'mock-step-summary.md')}), \
                patch.object(check, 'execute', new_callable=AsyncMock,
                    side_effect=RuntimeError('secret-value external provider response')), \
                redirect_stdout(io.StringIO()):
            code = check.main(['--scope', 'tavily', '--live-authorized', '--output-dir', directory])
            serialized = (Path(directory) / 'source_interface_latest.json').read_text()
            result = json.loads(serialized)
            summary = (Path(directory) / 'mock-step-summary.md').read_text()
        self.assertEqual(code, 1)
        self.assertIsNone(result['request_count'])
        self.assertEqual(result['reason'], 'execution_error_usage_unknown')
        self.assertNotIn('secret-value', serialized)
        self.assertNotIn('external provider response', serialized)
        self.assertNotIn('secret-value', summary)

    def test_invalid_limits_are_not_silently_clamped(self):
        for limit in (0, 9, True, -1, '8'):
            with self.subTest(limit=limit), self.assertRaises(ValueError):
                SourceAudit(include_authenticated=True, bibliography_requests=limit)
        with self.assertRaises(ValueError):
            SourceAudit(bibliography_requests=1)
        with self.assertRaises(ValueError):
            SourceAudit(tavily_included_only=True)

    def test_workflow_is_manual_granular_and_default_unauthorized(self):
        root = Path(__file__).resolve().parents[1]
        text = (root / '.github/workflows/source-interface-acceptance.yml').read_text()
        self.assertIn('on:\n  workflow_dispatch:\n', text)
        self.assertNotIn('schedule:', text)
        self.assertNotIn('push:', text)
        live_input = text.split('      live_authorized:', 1)[1].split('\npermissions:', 1)[0]
        self.assertIn('        default: false\n', live_input)
        self.assertIn('        options: [nyu, tavily, both]', text)
        self.assertIn('    if: inputs.live_authorized\n', text)
        self.assertEqual(text.split('permissions:\n', 1)[1].split('\nconcurrency:', 1)[0].strip(), 'contents: read')
        for forbidden in ('DASHSCOPE_API_KEY', 'SMTP_PASSWORD', 'TWITTER_BEARER_TOKEN',
                'REDDIT_CLIENT_SECRET', 'SEMANTIC_SCHOLAR_API_KEY', 'main.py', 'state_migration'):
            self.assertNotIn(forbidden, text)
        self.assertIn('python scripts/offline_checks.py', text)
        self.assertIn('if: always()', text)
        job_env = text.split('    env:', 1)[1].split('    steps:', 1)[0]
        self.assertNotIn('secrets.', job_env)
        live_step = text.split('      - name: 本次授权的接口子集（不调用主流程）', 1)[1]
        self.assertIn('          OPENALEX_API_KEY:', live_step)
        self.assertIn('          TAVILY_API_KEY:', live_step)


class InterfaceExecutionTests(unittest.IsolatedAsyncioTestCase):
    def config_context(self):
        return patch.multiple(config, OPENALEX_API_KEY='offline-openalex', TAVILY_API_KEY='offline-tavily',
            TAVILY_MONTHLY_CREDIT_LIMIT=1000, PRIORITY_BIBLIOGRAPHY_RESOLUTION_ENABLED=False)

    async def execute(self, scope, handler, limit=8):
        with self.config_context(), patch.object(check, 'build_entries', return_value=[NYU, TAVILY, OUTSIDE]):
            return await check.execute(scope, limit, transport=httpx.MockTransport(handler),
                reference_time=NOW, sleeper=no_sleep)

    async def test_missing_nyu_credential_stops_entire_plan_before_tavily(self):
        with self.config_context(), patch.object(config, 'OPENALEX_API_KEY', ''), \
                patch.object(check, 'build_entries', return_value=[NYU, TAVILY]), \
                self.assertRaises(ValueError):
            await check.execute('both', 8, transport=httpx.MockTransport(lambda _: self.fail('HTTP before admission')))

    async def test_invalid_configuration_syntax_stops_before_network(self):
        with self.config_context(), patch.object(check, 'build_entries', return_value=[NYU, TAVILY]), \
                patch.object(check, 'configuration_syntax_checks', return_value=[type('Check', (), {'status': 'missing', 'note': 'bad'})()]), \
                self.assertRaises(ValueError):
            await check.execute('tavily', 8, transport=httpx.MockTransport(lambda _: self.fail('HTTP before syntax check')))

    async def test_eight_identity_queries_keep_ninth_task_and_no_production_success(self):
        calls = []
        def handler(request):
            calls.append((request.url.host, request.method))
            if request.url.host == 'wp.nyu.edu':
                return httpx.Response(200, text=''.join(
                    f'<p>Chen, A. (2026). “Adaptive mechanism {i} architecture”. Journal.</p>' for i in range(9)))
            self.assertEqual(request.url.host, 'api.openalex.org')
            self.assertEqual(request.headers['Authorization'], 'Bearer offline-openalex')
            return httpx.Response(200, json={'meta': {'count': 0}, 'results': []})
        result = await self.execute('nyu', handler)
        self.assertEqual(calls.count(('api.openalex.org', 'GET')), 8)
        self.assertEqual(result['request_count'], 9)
        row = result['entries'][0]
        tasks = row['coverage']['bibliography_diagnostics']['resolution_tasks']
        self.assertEqual(len(tasks), 9)
        self.assertEqual(sum(t['reason'] == 'identity_request_safety_limit' for t in tasks), 1)
        self.assertFalse(result['selected_protocols_passed'])
        self.assertFalse(result['acceptance_complete'])
        self.assertFalse(result['production_v0_accepted'])
        self.assertEqual(result['untested_entry_keys'], ['tavily', 'x'])
        self.assertEqual(len(result['full_manifest']), 3)
        self.assertFalse(config.PRIORITY_BIBLIOGRAPHY_RESOLUTION_ENABLED)

    async def test_one_authorization_rejection_stops_further_identity_queries(self):
        calls = []
        def handler(request):
            calls.append(request.url.host)
            if request.url.host == 'wp.nyu.edu':
                return httpx.Response(200, text=''.join(
                    f'<p>Chen, A. (2026). “Adaptive mechanism {i} architecture”. Journal.</p>' for i in range(9)))
            return httpx.Response(401)
        result = await self.execute('nyu', handler)
        self.assertEqual(calls, ['wp.nyu.edu', 'api.openalex.org'])
        self.assertFalse(result['selected_protocols_passed'])

    async def test_included_credit_allows_only_usage_and_one_basic_search(self):
        calls = []
        def handler(request):
            calls.append((request.url.host, request.url.path, request.method))
            self.assertEqual(request.url.host, 'api.tavily.com')
            self.assertEqual(request.headers['Authorization'], 'Bearer offline-tavily')
            if request.url.path == '/usage':
                return httpx.Response(200, json=usage())
            body = json.loads(request.content)
            self.assertEqual(body['search_depth'], 'basic')
            self.assertEqual(body['max_results'], 1)
            self.assertFalse(body['auto_parameters'])
            return httpx.Response(200, json={'results': [], 'usage': {'credits': 1}})
        result = await self.execute('tavily', handler)
        self.assertEqual(calls, [('api.tavily.com', '/usage', 'GET'), ('api.tavily.com', '/search', 'POST')])
        self.assertTrue(result['selected_protocols_passed'])
        self.assertFalse(result['acceptance_complete'])
        self.assertFalse(result['campaign_coverage_complete'])
        self.assertFalse(result['production_v0_accepted'])
        self.assertEqual(result['entries'][0]['platform_usage_credits'], 1)
        self.assertNotIn('offline-tavily', json.dumps(result))

    async def test_unknown_or_exhausted_limits_and_enabled_payg_never_search(self):
        bad = [usage(plan_limit=0), usage(plan_usage=1000), usage(paygo_limit=1),
            usage(paygo_limit=None), usage(plan_limit=True), usage(plan_usage=-1),
            usage(paygo_usage=True), {'account': {'plan_usage': 0, 'paygo_usage': 0}},
            {**usage(), 'key': {'usage': 1000, 'limit': 1000}},
            {**usage(), 'key': {'usage': 0, 'limit': None}}, {**usage(), 'key': None}]
        for payload in bad:
            calls = []
            def handler(request):
                calls.append(request.method)
                self.assertEqual(request.url.path, '/usage')
                return httpx.Response(200, json=payload)
            with self.subTest(payload=payload):
                result = await self.execute('tavily', handler)
                self.assertEqual(calls, ['GET'])
                self.assertFalse(result['selected_protocols_passed'])
                self.assertTrue(result['entries'][0]['reason'].startswith('tavily_included_'))
                self.assertEqual(result['entries'][0]['diagnosis']['category'], 'included_credit_guard')

    async def test_disabled_monthly_guard_stops_before_any_request(self):
        with self.config_context(), patch.object(config, 'TAVILY_MONTHLY_CREDIT_LIMIT', 0), \
                patch.object(check, 'build_entries', return_value=[NYU, TAVILY]), \
                self.assertRaises(ValueError):
            await check.execute('tavily', 8, transport=httpx.MockTransport(lambda _: self.fail('HTTP without monthly guard')))

    async def test_usage_redirect_does_not_follow_or_search(self):
        calls = []
        def handler(request):
            calls.append(str(request.url))
            return httpx.Response(302, headers={'Location': 'https://other.example/usage'})
        result = await self.execute('tavily', handler)
        self.assertEqual(calls, ['https://api.tavily.com/usage'])
        self.assertFalse(result['selected_protocols_passed'])

    async def test_search_failure_or_unknown_usage_is_not_retried_or_green(self):
        for response in (httpx.Response(429), httpx.Response(200, json={'results': []}),
                httpx.Response(200, json={'results': [], 'usage': {'credits': 2}})):
            calls = []
            def handler(request):
                calls.append(request.url.path)
                return httpx.Response(200, json=usage()) if request.url.path == '/usage' else response
            with self.subTest(status=response.status_code):
                result = await self.execute('tavily', handler)
                self.assertEqual(calls, ['/usage', '/search'])
                self.assertFalse(result['selected_protocols_passed'])

    async def test_explicit_limit_never_enables_other_official_bibliographies(self):
        other = Entry('official:other', 'official', 'https://lab.example/research')
        with patch('source_audit.PriorityResearchPageSource') as source, \
                patch.object(config, 'PRIORITY_BIBLIOGRAPHY_RESOLUTION_ENABLED', False):
            source.return_value.fetch = AsyncMock(return_value=[])
            source.return_value.coverage.return_value = {'pages': {other.url: {}}, 'detail_failures': 0,
                'request_failed': 0, 'parse_zero_links': 0, 'unresolved_citations': 0}
            audit = SourceAudit(transport=httpx.MockTransport(lambda _: self.fail('No HTTP')), include_authenticated=True, bibliography_requests=8)
            await audit.run([other], reference_time=NOW)
        resolver = source.call_args.kwargs['bibliography_resolver']
        self.assertFalse(resolver.enabled)
        self.assertEqual(resolver.max_requests, 1)
