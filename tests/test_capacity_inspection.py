"""Read-only triage does not depend on secrets, production state or wall-clock dates."""
import hashlib
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from database.paradigm_store import ParadigmStore
from scripts.inspect_capacity import inspect_capacity


class CapacityInspectionTests(unittest.TestCase):
    def test_read_only_with_legacy_unknown_timings_and_no_payload_exposure(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            state = path/'state.db'
            ParadigmStore(state)
            audit = path/'audit.json'
            audit.write_text(json.dumps({'pipeline_stats':{},'llm_calls':[
                {'stage':'synthesis','status':'passed','usage_reported':True,'prompt_tokens':5,'completion_tokens':4,'reasoning_tokens':2,'total_tokens':9,'subject':'private ignored subject'}]}))
            before = hashlib.sha256(state.read_bytes()).hexdigest()
            result = inspect_capacity(state,audit)
            self.assertEqual(hashlib.sha256(state.read_bytes()).hexdigest(), before)
            self.assertIsNone(result['llm_by_stage']['synthesis']['request_seconds'])
            self.assertNotIn('private ignored subject',json.dumps(result))
            self.assertTrue(result['read_only'])

    def test_timing_zero_is_measured_but_unknown_usage_remains_unknown(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)
            ParadigmStore(path/'state.db')
            (path/'audit.json').write_text(json.dumps({'pipeline_stats':{},'llm_calls':[
                {'stage':'synthesis','elapsed_seconds':0,'status':'failed','usage_reported':False}]}))
            result=inspect_capacity(path/'state.db',path/'audit.json')
            self.assertEqual(result['llm_by_stage']['synthesis']['request_seconds'],0)
            self.assertEqual(result['llm_by_stage']['synthesis']['unknown_usage'],1)

    def test_missing_database_is_never_created(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)
            (path/'audit.json').write_text('{"pipeline_stats":{},"llm_calls":[]}')
            with self.assertRaises(sqlite3.OperationalError):
                inspect_capacity(path/'missing.db',path/'audit.json')
            self.assertFalse((path/'missing.db').exists())
