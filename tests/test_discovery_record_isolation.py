"""Bad adapter records must not discard other records before aggregation."""
import copy
import unittest
from datetime import datetime, timezone
from unittest.mock import AsyncMock

from paradigms.discovery import ParadigmDiscovery
from runtime_clock import research_window
from sources.base import RawProject
from tests.test_v0_delivery import origin


NOW = datetime(2026, 10, 8, tzinfo=timezone.utc)


class DiscoveryRecordIsolationTests(unittest.IsolatedAsyncioTestCase):
    def discovery(self):
        value = ParadigmDiscovery(broad_lookback_days=7, high_signal_lookback_days=14, bounded=True)
        for source in value._sources():
            source.safe_fetch = AsyncMock(return_value=[])
        value.arxiv.safe_fetch.return_value = [RawProject(source="arxiv", name="A valid predictive state mechanism",
            url="https://arxiv.org/abs/2610.00001", created_at="2026-10-07T00:00:00+00:00")]
        return value

    async def test_raw_conversion_failure_is_local_not_a_global_discovery_exception(self):
        discovery = self.discovery()
        discovery.arxiv.safe_fetch.return_value.append(RawProject(source="arxiv", name="Bad metadata",
            url="https://arxiv.org/abs/2610.00002", extra=None))
        with research_window(NOW):
            batch = await discovery.run()
        self.assertEqual(len(batch.origins), 1)
        health = batch.coverage["source_health"]["arxiv"]
        self.assertEqual(health["status"], "completed_with_warnings")
        self.assertEqual((health["results"], health["rejected_records"]), (1, 1))

    async def test_malformed_native_peer_does_not_discard_healthy_native_and_raw_origins(self):
        discovery = self.discovery()
        healthy = origin(3); healthy.source="openalex"
        discovery.openalex.safe_fetch.return_value = [healthy, object()]
        with research_window(NOW):
            batch = await discovery.run()
        self.assertEqual({item.url for item in batch.origins},
                         {"https://arxiv.org/abs/2610.00001",healthy.url})
        self.assertEqual(batch.coverage["source_health"]["openalex"]["rejected_records"], 1)

    async def test_unserializable_native_record_is_rejected_before_merge_or_campaign_save(self):
        discovery = self.discovery()
        broken = copy.deepcopy(origin(3));broken.raw["not_json"] = object()
        discovery.openalex.safe_fetch.return_value = [broken]
        with research_window(NOW):
            batch = await discovery.run()
        self.assertEqual(len(batch.origins), 1)
        health=batch.coverage["source_health"]["openalex"]
        self.assertEqual(health["status"], "query_failed")
        self.assertEqual((health["results"],health["rejected_records"]), (0,1))

    async def test_bad_follow_builder_date_does_not_discard_other_sources(self):
        discovery = self.discovery()
        discovery.follow_builders.safe_fetch.return_value = [RawProject(source="follow-builders-blog",
            name="Unexpected date structure",url="https://example.org/research",created_at={"date":"unknown"})]
        with research_window(NOW):
            batch = await discovery.run()
        self.assertEqual(len(batch.origins), 1)
        self.assertEqual(batch.coverage["source_health"]["follow-builders"]["rejected_records"],1)

    async def test_json_valid_but_wrong_nested_shapes_cannot_escape_into_global_merge(self):
        for field, value in (("authors",[{}]),("identifiers",{"doi":["not-a-string"]}),
                             ("raw",{"frontier_domains":[{}]}),("raw",{"selection_signals":{}}),
                             ("raw",{"document_format":[]}),("url","https://[broken")):
            with self.subTest(field=field,value=value):
                discovery=self.discovery();bad=origin(3)
                setattr(bad,field,value)
                discovery.openalex.safe_fetch.return_value=[bad]
                with research_window(NOW):
                    batch=await discovery.run()
                self.assertEqual(len(batch.origins),1)
                self.assertEqual(batch.coverage["source_health"]["openalex"]["rejected_records"],1)
