from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import config
from database.paradigm_store import ParadigmStore
from paradigms.clustering import cluster_extractions
from paradigms.models import (
    EvidenceType,
    ParadigmCandidate,
    ParadigmExtraction,
    ResearcherProfile,
    TechnicalEvidence,
)
from paradigms.scoring import is_reportable, score_candidate
from reports.paradigm_generator import ParadigmReportGenerator
from skills.loader import SkillLoader


def paper(suffix: str = "1") -> TechnicalEvidence:
    return TechnicalEvidence(
        source="arxiv",
        evidence_type=EvidenceType.PRIMARY_PAPER,
        title=f"Predictive Latent Action World Models {suffix}",
        url=f"https://arxiv.org/abs/2607.0000{suffix}",
        summary="Learns latent actions and predicts state transitions from internet video.",
        published_at="2026-07-18T00:00:00+00:00",
        authors=["A. Researcher"],
        identifiers={"arxiv": f"2607.0000{suffix}"},
    )


def candidate(evidence: list[TechnicalEvidence] | None = None) -> ParadigmCandidate:
    return ParadigmCandidate(
        key="latent-action-world-models",
        name="Latent-action world models",
        thesis="从像素生成转向可行动的状态演变预测。",
        problem_shift="从生成下一帧转向学习可供行动规划使用的状态动力学。",
        mechanism="从无标注视频中学习离散潜在动作并预测未来状态。",
        lineage_parent="video prediction world models",
        lineage_path=["video prediction", "latent-action world models"],
        keywords=["latent action", "world model", "video prediction"],
        evidence=evidence or [paper()],
        novelty_score=9,
        solidity_score=8,
        scope_score=9,
        incremental_penalty=0,
    )


class ParadigmPipelineTests(unittest.TestCase):
    def test_low_volume_solid_paradigm_can_pass(self) -> None:
        item = candidate()
        with patch.object(config, "PARADIGM_MIN_SCORE", 65):
            score_candidate(item)
            self.assertTrue(is_reportable(item))
            self.assertGreaterEqual(item.total_score, 65)

    def test_incremental_tweak_is_hard_rejected(self) -> None:
        item = candidate()
        item.novelty_score = 5
        item.scope_score = 4
        item.incremental_penalty = 8
        score_candidate(item)
        self.assertFalse(is_reportable(item))
        self.assertIn("小改动", item.rejection_reason)

    def test_similar_extractions_cluster_into_one_paradigm(self) -> None:
        first = ParadigmExtraction(
            evidence=paper("1"),
            is_candidate=True,
            canonical_name="Latent Action World Models",
            thesis="t",
            problem_shift="p",
            mechanism="m",
            lineage_parent="Video World Models",
            keywords=["latent action", "world model", "video dynamics"],
            novelty_score=8,
            solidity_score=8,
            scope_score=8,
        )
        second = ParadigmExtraction(
            evidence=paper("2"),
            is_candidate=True,
            canonical_name="World Models with Latent Actions",
            thesis="t2",
            problem_shift="p2",
            mechanism="m2",
            lineage_parent="Video World Models",
            keywords=["latent action", "world model", "video dynamics"],
            novelty_score=9,
            solidity_score=7,
            scope_score=9,
        )
        clusters = cluster_extractions([first, second])
        self.assertEqual(len(clusters), 1)
        self.assertEqual(len(clusters[0].evidence), 2)

    def test_report_delivery_signature_prevents_weekly_duplicate(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = ParadigmStore(Path(directory) / "radar.db")
            item = candidate()
            score_candidate(item)
            store.mark_evidence(item.evidence, analyzed=True)
            store.save_candidates([item])

            first_delivery = store.prepare_report([item])
            self.assertEqual(first_delivery[0].report_kind, "new")
            store.mark_reported(first_delivery, Path(directory) / "week1.md")
            self.assertEqual(store.prepare_report([item]), [])

            item.evidence.append(paper("3"))
            score_candidate(item)
            updated = store.prepare_report([item])
            self.assertEqual(len(updated), 1)
            self.assertEqual(updated[0].report_kind, "update")

            next_week = candidate([paper("4")])
            store.attach_history([next_week])
            self.assertEqual(len(next_week.evidence), 2)
            self.assertTrue(any(e.raw.get("historical") for e in next_week.evidence))

    def test_report_only_shows_verified_public_contacts(self) -> None:
        item = candidate()
        item.researchers = [
            ResearcherProfile(
                name="A. Researcher",
                role="第一作者",
                profile_urls={"orcid": "https://orcid.org/0000-0000-0000-0001"},
            )
        ]
        score_candidate(item)
        with tempfile.TemporaryDirectory() as directory:
            path = asyncio.run(
                ParadigmReportGenerator(directory).generate([item], {"origin_count": 1})
            )
            content = path.read_text(encoding="utf-8")
        self.assertIn("https://orcid.org/0000-0000-0000-0001", content)
        self.assertNotIn("@example", content)

    def test_paradigm_skill_renders_json_contract(self) -> None:
        prompt = SkillLoader().render(
            "paradigm_extraction",
            source="arxiv",
            title="Test",
            abstract="Abstract",
            authors="A",
            organization="Lab",
            identifiers={"arxiv": "2607.1"},
        )
        self.assertIn('"canonical_name"', prompt)
        self.assertIn("2607.1", prompt)

        synthesis = SkillLoader().render(
            "paradigm_synthesis",
            provisional_name="World Model",
            provisional_thesis="thesis",
            problem_shift="shift",
            mechanism="mechanism",
            lineage_parent="video prediction",
            evidence="[]",
        )
        self.assertIn('"trend_interpretation"', synthesis)


if __name__ == "__main__":
    unittest.main()
