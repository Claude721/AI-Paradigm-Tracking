from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

import httpx

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
from reports.paradigm_generator import ParadigmReportGenerator, _valid_editorial_report
from skills.loader import SkillLoader
from sources.paradigm_evidence_source import _is_relevant_repository
from sources.researcher_profile_source import ResearcherProfileClient


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
        route_family="World models for embodied control",
        thesis="从像素生成转向可行动的状态演变预测。",
        background="逐帧像素生成很难直接为行动规划提供紧凑状态。",
        problem_shift="从生成下一帧转向学习可供行动规划使用的状态动力学。",
        design_philosophy="先学习可行动的状态变化，再决定动作。",
        mechanism="从无标注视频中学习离散潜在动作并预测未来状态。",
        technical_explanation="把视频变化压缩成离散潜在动作，并在该空间预测未来状态。",
        application_value="让机器人从互联网视频中获得可迁移的动态先验。",
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

            # 主观评分的轻微波动不是新事实，不能导致下一周重复发送。
            signature = item.report_signature
            item.total_score += 4.9
            self.assertEqual(item.report_signature, signature)
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
                current_affiliation="Example Lab",
                background_summary="长期研究视频世界模型与机器人策略。",
                profile_urls={"orcid": "https://orcid.org/0000-0000-0000-0001"},
                contact_search_notes=["已检索 OpenAlex", "已检索 ORCID"],
            )
        ]
        score_candidate(item)
        with tempfile.TemporaryDirectory() as directory:
            path = asyncio.run(
                ParadigmReportGenerator(directory, client=False).generate(
                    [item], {"origin_count": 1}
                )
            )
            content = path.read_text(encoding="utf-8")
        self.assertIn("https://orcid.org/0000-0000-0000-0001", content)
        self.assertNotIn("@example", content)
        self.assertIn("## 本期研究 Memo", content)
        self.assertNotIn("评分拆解", content)
        self.assertNotIn("| 新颖性 |", content)

    def test_editorial_gate_rejects_scores_and_tables(self) -> None:
        item = candidate()
        item.researchers = [ResearcherProfile(name="A. Researcher")]
        memo = "本期技术路线从旧方法的边界出发，解释设计思想如何落到训练与推理。" * 18
        body = "技术部分继续分析问题、机制、证据和应用价值。" * 25
        report = (
            "# AI 技术范式雷达\n\n## 本期研究 Memo\n\n"
            f"{memo}\n\n## **技术路线**开始改变能力边界\n\n"
            f"{body} **关键机制**值得继续验证。A. Researcher 是本期关键作者。\n\n"
            "## 接下来真正值得盯的信号\n\n观察独立复现与有内容的二次讨论。"
        )
        self.assertTrue(_valid_editorial_report(report, [item]))
        self.assertFalse(_valid_editorial_report(report + "\n\n总分：92", [item]))
        self.assertFalse(
            _valid_editorial_report(report + "\n\n| 项目 | 数据 |\n|---|---|", [item])
        )

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
        self.assertIn('"route_family"', prompt)
        self.assertIn('"design_philosophy"', prompt)
        self.assertIn("2607.1", prompt)

        synthesis = SkillLoader().render(
            "paradigm_synthesis",
            provisional_name="World Model",
            route_family="Embodied world models",
            provisional_thesis="thesis",
            background="background",
            problem_shift="shift",
            design_philosophy="philosophy",
            mechanism="mechanism",
            technical_explanation="explanation",
            lineage_parent="video prediction",
            evidence="[]",
        )
        self.assertIn('"trend_interpretation"', synthesis)
        self.assertIn('"excluded_evidence_indices"', synthesis)
        self.assertIn('证据列表：[]', synthesis)

        editorial = SkillLoader().render(
            "weekly_research_memo",
            date="2026-07-18",
            lookback_days=7,
            stats="{}",
            candidate_dossiers="[]",
        )
        self.assertIn("约 450 到 650 个中文字", editorial)
        self.assertIn("不展示总分", editorial)

    def test_skill_loader_keeps_embedded_json_valid(self) -> None:
        prompt = SkillLoader().render(
            "weekly_research_memo",
            date="2026-07-18",
            lookback_days=7,
            stats='{"origin_count": 3}',
            candidate_dossiers='[{"name": "route"}]',
        )
        self.assertIn('{"origin_count": 3}', prompt)
        self.assertNotIn('{{"origin_count"', prompt)

    def test_github_paper_aggregator_is_not_implementation(self) -> None:
        item = candidate()
        aggregator = {
            "full_name": "someone/arxiv-daily",
            "description": "Daily papers including latent action world models",
        }
        implementation = {
            "full_name": "lab/latent-action-world-models",
            "description": "Official implementation for learning latent actions from video",
        }
        self.assertFalse(_is_relevant_repository(item, aggregator))
        self.assertTrue(_is_relevant_repository(item, implementation))

    def test_researcher_seed_survives_without_semantic_scholar_or_openalex(self) -> None:
        evidence = paper()
        evidence.organization = "Example Robotics Lab"
        with patch.object(config, "OPENALEX_API_KEY", ""):
            profiles = asyncio.run(
                ResearcherProfileClient().enrich(evidence, existing=[])
            )
        self.assertEqual(profiles[0].name, "A. Researcher")
        self.assertEqual(profiles[0].current_affiliation, "Example Robotics Lab")
        self.assertTrue(profiles[0].contact_search_notes)

    def test_researcher_lookup_failure_degrades_to_search_trace(self) -> None:
        client = ResearcherProfileClient()
        with (
            patch.object(config, "OPENALEX_API_KEY", "configured"),
            patch.object(
                client,
                "_openalex",
                new=AsyncMock(side_effect=httpx.ConnectError("offline")),
            ),
        ):
            profiles = asyncio.run(client.enrich(paper(), existing=[]))
        self.assertEqual(profiles[0].name, "A. Researcher")
        self.assertTrue(
            any("OpenAlex 检索失败" in note for note in profiles[0].contact_search_notes)
        )


if __name__ == "__main__":
    unittest.main()
