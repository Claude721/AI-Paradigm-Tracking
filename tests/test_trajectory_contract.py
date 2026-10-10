"""Repair only representation; never synthesize facts or count unknown as zero."""
import json
import math
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock

from paradigms.analyzer import ResearcherTrajectoryAnalyzer, _validated_trajectory_payload
from paradigms.models import ParadigmCandidate, ResearcherProfile
from run_audit import run_audit


def payload(value=4):
    return {'background_summary':'可核验的职业背景','trajectory_summary':'代表作与当前机制持续关联',
            'key_person_reason':'沿此机制持续研究','current_role_note':'','trajectory_consistency':value}


def response(value):
    return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=json.dumps(value)))],
                           usage=SimpleNamespace(total_tokens=30,prompt_tokens=20,completion_tokens=10))


class TrajectoryContractTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        run_audit.reset()

    def test_lossless_numeric_strings_are_normalized(self):
        self.assertEqual(_validated_trajectory_payload(payload('4.5'))['trajectory_consistency'],4.5)

    def test_unknown_percentages_booleans_fractions_and_nonfinite_are_not_guessed(self):
        for value in ('80%','8/10','高',None,True,float('nan'),float('inf'),11,-1):
            with self.subTest(value=value), self.assertRaises(ValueError):
                _validated_trajectory_payload(payload(value))

    async def analyze(self, values):
        call=AsyncMock(side_effect=[response(value) for value in values])
        client=SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=call)))
        profile=ResearcherProfile(name='Example Researcher',representative_works=[{'title':'Public work'}])
        candidate=ParadigmCandidate(key='route',name='机制路线',thesis='',problem_shift='',mechanism='机制',researchers=[profile])
        analyzer=ResearcherTrajectoryAnalyzer(client=client,model='offline')
        return analyzer,candidate,profile,call

    async def test_one_format_repair_counts_both_calls_once(self):
        analyzer,candidate,profile,call=await self.analyze([payload('8/10'),payload(8)])
        await analyzer.run([candidate])
        self.assertEqual(call.await_count,2)
        self.assertEqual(profile.trajectory_consistency,8)
        self.assertEqual(run_audit.token_totals()['llm_call_count'],2)
        self.assertEqual(run_audit.token_totals()['llm_total_tokens'],60)

    async def test_still_invalid_after_one_repair_remains_execution_failure(self):
        analyzer,candidate,profile,call=await self.analyze([payload(None),payload(None)])
        with self.assertRaises(RuntimeError):
            await analyzer.run([candidate])
        self.assertEqual(call.await_count,2)
        self.assertEqual(profile.background_summary,'')
        self.assertEqual(run_audit.token_totals()['llm_call_count'],2)


if __name__ == '__main__':
    unittest.main()
