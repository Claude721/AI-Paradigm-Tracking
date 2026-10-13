"""Bibliography lookup narrows recall without weakening identity receipts."""
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx

import config
from database.paradigm_store import ParadigmStore
from runtime_clock import research_window
from sources.bibliography_resolver import BibliographyResolver, match_work, normalized_title
from tests.test_input_closure import NYU, payload, record, work

NOW = datetime(2026, 10, 13, tzinfo=timezone.utc)


class BibliographyQueryContractTests(unittest.IsolatedAsyncioTestCase):
    async def test_nested_quotes_and_operators_are_literal_title_only_search(self):
        title = 'Discussion of “The ICML Ranking Experiment: OR NOT AND”'
        calls = []
        def handler(request):
            calls.append(request)
            self.assertEqual(request.url.params['search.title'], '"discussion of the icml ranking experiment or not and"')
            self.assertNotIn('search', request.url.params)
            self.assertNotIn('filter', request.url.params)
            self.assertNotIn('api_key', request.url.params)
            self.assertEqual(request.url.params['per-page'], '100')
            return httpx.Response(200, json=payload(work(title)))
        with research_window(NOW), patch.object(config, 'OPENALEX_API_KEY', 'offline-only'):
            async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
                resolver = BibliographyResolver(enabled=True, max_requests=1)
                rows = await resolver.resolve(client, NYU, [record(title=title)], lookback_days=7)
        self.assertEqual(len(calls), 1)
        self.assertEqual(rows[0]['title'], title)
        self.assertEqual(rows[0]['status'], 'resolved_url')
        self.assertNotIn('primary_published_at', rows[0])

    async def test_nonsearchable_title_cannot_consume_a_request(self):
        client = SimpleNamespace(get=AsyncMock())
        with research_window(NOW), patch.object(config, 'OPENALEX_API_KEY', 'offline-only'):
            rows = await BibliographyResolver(enabled=True).resolve(client, NYU, [record(title='?! — “ ”')], lookback_days=7)
        client.get.assert_not_awaited()
        self.assertEqual(rows[0]['reason'], 'missing_citation_identity')
        self.assertEqual(rows[0]['attempt_count'], 0)

    def test_truncated_duplicate_and_error_results_never_confirm_identity(self):
        truncated = payload(work())
        truncated['meta']['count'] = 101
        self.assertEqual(match_work(record(), truncated)['reason'], 'identity_search_truncated')
        for response in (payload(work(), work(author='Another Person')),
                {**payload(work()), 'error': 'provider error'},
                payload(work(), {'id': 'external', 'display_name': 'Other work'})):
            with self.subTest(response=response), self.assertRaises(ValueError):
                match_work(record(), response)

    async def test_query_change_keeps_legacy_manifest_receipt_and_attempt_rotation(self):
        calls = []
        def handler(request):
            calls.append(request.url.params['search.title'])
            return httpx.Response(200, json=payload(work('Later Work')))
        with tempfile.TemporaryDirectory() as directory, research_window(NOW), patch.object(config, 'OPENALEX_API_KEY', 'offline-only'):
            cache = ParadigmStore(Path(directory) / 'state.db').campaigns
            resolver = BibliographyResolver(stage_cache=cache, enabled=True, max_requests=1)
            records = [record('verified'), record('failed', 'Earlier Work'), record('fresh', 'Later Work')]
            cache.cache_save('bibliography_manifest', resolver.signature(NYU, 7, 'manifest'),
                             {'records': {row['key']: row for row in records}})
            cache.cache_save('bibliography_identity', resolver.signature(NYU, 7, 'verified'),
                {'status': 'resolved_url', 'work_id': 'https://openalex.org/W1',
                 'url': 'https://arxiv.org/abs/2610.00001', 'attempt_count': 1})
            cache.cache_save('bibliography_identity', resolver.signature(NYU, 7, 'failed'),
                             {'status': 'pending', 'reason': 'identity_search_truncated', 'attempt_count': 1})
            async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
                rows = await resolver.resolve(client, NYU, [], lookback_days=7)
        self.assertEqual(calls, ['"' + normalized_title('Later Work') + '"'])
        self.assertEqual({row['key'] for row in rows}, {'verified', 'failed', 'fresh'})
        by_key = {row['key']: row for row in rows}
        self.assertEqual(by_key['verified']['url'], 'https://arxiv.org/abs/2610.00001')
        self.assertEqual(by_key['verified']['attempt_count'], 1)
        self.assertEqual(by_key['failed']['attempt_count'], 1)
        self.assertEqual(by_key['fresh']['status'], 'resolved_url')
