from __future__ import annotations

import asyncio
import json
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx

import config
from database.paradigm_store import ParadigmStore
from paradigms.analyzer import ParadigmAnalyzer
from paradigms.clustering import cluster_extractions
from paradigms.enrichment import EvidenceEnricher
from paradigms.models import (
    EvidenceType,
    ParadigmCandidate,
    ParadigmExtraction,
    ResearcherProfile,
    TechnicalEvidence,
)
from paradigms.scoring import is_reportable, score_candidate
from reports.paradigm_generator import (
    ParadigmReportGenerator,
    _candidate_dossier,
    _valid_editorial_report,
)
from skills.loader import SkillLoader
from sources.paradigm_evidence_source import _is_relevant_repository
from sources.arxiv_source import ArxivSource
from sources.priority_research_source import _discover_index_links
from sources.reddit_evidence_source import RedditEvidenceClient
from sources.researcher_profile_source import ResearcherProfileClient
from sources.social_web_search_source import _parse_results as parse_tavily_results


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
    def test_unknown_low_volume_work_stays_in_observation_pool(self) -> None:
        item = candidate()
        with patch.object(config, "PARADIGM_MIN_SCORE", 65):
            score_candidate(item)
            self.assertFalse(is_reportable(item))
            self.assertIn("发布者背景未核验", item.rejection_reason)

    def test_established_team_technical_report_gets_priority_admission(self) -> None:
        evidence = paper()
        evidence.title = "Frontier Model Technical Report"
        evidence.organization = "Moonshot AI"
        evidence.raw = {
            "origin_kind": "technical_report",
            "publisher_tier": "established",
            "publisher_evidence": "Moonshot AI official research page",
        }
        item = candidate([evidence])
        score_candidate(item)
        self.assertTrue(is_reportable(item))
        self.assertTrue(item.is_formal_technical_report)
        self.assertEqual(item.publisher_tier, "established")
        self.assertIn("优先解读", item.admission_reason)

    def test_unknown_work_needs_independent_secondary_validation(self) -> None:
        item = candidate()
        item.evidence.extend(
            [
                TechnicalEvidence(
                    source="hackernews",
                    evidence_type=EvidenceType.COMMUNITY_DISCUSSION,
                    title="Latent action world models discussion",
                    url="https://news.ycombinator.com/item?id=1",
                    metrics={"score": 12, "comments": 4},
                ),
                TechnicalEvidence(
                    source="x-title-search",
                    evidence_type=EvidenceType.SECONDARY_INTERPRETATION,
                    title="Independent interpretation",
                    url="https://x.com/researcher/status/1",
                    metrics={"likes": 24},
                    raw={"relationship": "exact_work_title_match"},
                ),
            ]
        )
        with patch.object(config, "PARADIGM_MIN_SCORE", 50):
            score_candidate(item)
            self.assertTrue(is_reportable(item))
            self.assertIn("独立讨论", item.admission_reason)

    def test_author_self_release_is_identity_evidence_not_secondary_validation(self) -> None:
        item = candidate()
        item.evidence.append(
            TechnicalEvidence(
                source="x-title-search",
                evidence_type=EvidenceType.SECONDARY_INTERPRETATION,
                title="A. Researcher announces the paper",
                url="https://x.com/author/status/1",
                metrics={"likes": 500, "retweets": 100},
                raw={"relationship": "author_self_release"},
            )
        )
        score_candidate(item)
        self.assertFalse(is_reportable(item))
        self.assertIn("缺少实质二次讨论", item.rejection_reason)

    def test_tavily_indexed_pages_do_not_validate_unknown_publisher(self) -> None:
        item = candidate()
        item.evidence.extend(
            [
                TechnicalEvidence(
                    source="tavily-reddit",
                    evidence_type=EvidenceType.COMMUNITY_DISCUSSION,
                    title="Indexed Reddit page",
                    url="https://www.reddit.com/r/MachineLearning/comments/abc/work/",
                    metrics={"search_relevance": 0.99},
                    raw={"indexed_discovery_only": True},
                ),
                TechnicalEvidence(
                    source="tavily-x",
                    evidence_type=EvidenceType.SECONDARY_INTERPRETATION,
                    title="Indexed X page",
                    url="https://x.com/researcher/status/123",
                    metrics={"search_relevance": 0.98},
                    raw={"indexed_discovery_only": True},
                ),
            ]
        )
        score_candidate(item)
        self.assertFalse(is_reportable(item))
        self.assertIn("缺少实质二次讨论", item.rejection_reason)

    def test_tavily_parses_three_platforms_as_discovery_only(self) -> None:
        item = candidate()
        title = item.evidence[0].title
        payload = {
            "request_id": "req-1",
            "results": [
                {
                    "title": f"Researcher | {title}",
                    "url": "https://x.com/researcher/status/123",
                    "content": f"Commentary on {title}",
                    "score": 0.9,
                },
                {
                    "title": f"Discussion: {title}",
                    "url": "https://www.reddit.com/r/MachineLearning/comments/abc/work/",
                    "content": f"Discussion about {title}",
                    "score": 0.8,
                },
                {
                    "title": f"作者 - {title}",
                    "url": "https://www.xiaohongshu.com/user/profile/abc?xsec=1",
                    "content": f"介绍 {title}",
                    "score": 0.7,
                },
            ],
        }
        found = parse_tavily_results(payload, item, title)
        self.assertEqual(
            {value.source for value in found},
            {"tavily-x", "tavily-reddit", "tavily-xiaohongshu"},
        )
        self.assertTrue(all(value.raw["indexed_discovery_only"] for value in found))
        self.assertTrue(all("likes" not in value.metrics for value in found))

    def test_reddit_official_search_uses_oauth_and_returns_metrics(self) -> None:
        class Response:
            def __init__(self, payload):
                self.status_code = 200
                self._payload = payload

            def json(self):
                return self._payload

        class Client:
            async def post(self, url, **kwargs):
                return Response({"access_token": "token", "expires_in": 3600})

            async def get(self, url, **kwargs):
                if url.endswith("/search"):
                    return Response(
                        {
                            "data": {
                                "children": [
                                    {
                                        "data": {
                                            "id": "abc",
                                            "title": item.evidence[0].title,
                                            "selftext": "A technical discussion",
                                            "url": item.evidence[0].url,
                                            "permalink": "/r/MachineLearning/comments/abc/work/",
                                            "author": "public_user",
                                            "subreddit": "MachineLearning",
                                            "created_utc": time.time(),
                                            "score": 42,
                                            "num_comments": 11,
                                            "upvote_ratio": 0.91,
                                        }
                                    }
                                ]
                            }
                        }
                    )
                return Response(
                    [
                        {},
                        {
                            "data": {
                                "children": [
                                    {"data": {"body": "Useful independent analysis"}}
                                ]
                            }
                        },
                    ]
                )

        item = candidate()
        with (
            patch.object(config, "REDDIT_API_ACCESS_APPROVED", True),
            patch.object(config, "REDDIT_CLIENT_ID", "client"),
            patch.object(config, "REDDIT_CLIENT_SECRET", "secret"),
            patch.object(config, "REDDIT_USER_AGENT", "python:test:v1 (by /u/test)"),
        ):
            found = asyncio.run(RedditEvidenceClient().search(Client(), item))
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0].source, "reddit")
        self.assertEqual(found[0].metrics["comments"], 11)
        self.assertIn("independent analysis", found[0].summary)

    def test_ephemeral_social_content_is_scrubbed_after_synthesis(self) -> None:
        item = candidate()
        item.secondary_discussion_summary = "社区主要讨论潜在动作是否可迁移。"
        item.evidence.append(
            TechnicalEvidence(
                source="reddit",
                evidence_type=EvidenceType.COMMUNITY_DISCUSSION,
                title="Original user title",
                url="https://www.reddit.com/r/test/comments/abc/work/",
                summary="Original post and comments",
                authors=["public_user"],
                metrics={"score": 12, "comments": 3},
                identifiers={"reddit": "abc"},
                raw={
                    "ephemeral_content": True,
                    "social_author_name": "public_user",
                    "tavily_request_id": "req-1",
                },
            )
        )
        EvidenceEnricher.finalize([item])
        scrubbed = item.evidence[-1]
        self.assertEqual(scrubbed.summary, "")
        self.assertEqual(scrubbed.authors, [])
        self.assertEqual(scrubbed.title, "Reddit 公开讨论（abc）")
        self.assertTrue(scrubbed.raw["content_scrubbed"])
        self.assertNotIn("social_author_name", scrubbed.raw)
        self.assertEqual(
            item.secondary_discussion_summary, "社区主要讨论潜在动作是否可迁移。"
        )

    def test_relevant_repository_uptake_can_validate_unknown_publisher(self) -> None:
        item = candidate()
        item.evidence.append(
            TechnicalEvidence(
                source="github",
                evidence_type=EvidenceType.IMPLEMENTATION,
                title="community/latent-action-world-models",
                url="https://github.com/community/latent-action-world-models",
                metrics={"stars": 60, "forks": 8},
                raw={"relationship": "name_and_mechanism_match"},
            )
        )
        with patch.object(config, "PARADIGM_MIN_SCORE", 50):
            score_candidate(item)
            self.assertTrue(is_reportable(item))
            self.assertIn("独立讨论或承接", item.admission_reason)

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

    def test_technical_report_can_extract_multiple_independent_mechanisms(self) -> None:
        evidence = paper()
        evidence.raw = {"origin_kind": "technical_report"}
        payload = {
            "hypotheses": [
                {
                    "is_candidate": True,
                    "canonical_name": "Sparse attention routing",
                    "route_family": "Efficient frontier architectures",
                    "thesis": "t1",
                    "problem_shift": "p1",
                    "mechanism": "m1",
                    "keywords": ["sparse attention"],
                    "novelty_score": 8,
                    "solidity_score": 8,
                    "scope_score": 8,
                },
                {
                    "is_candidate": True,
                    "canonical_name": "Residual expert composition",
                    "route_family": "Efficient frontier architectures",
                    "thesis": "t2",
                    "problem_shift": "p2",
                    "mechanism": "m2",
                    "keywords": ["expert composition"],
                    "novelty_score": 8,
                    "solidity_score": 8,
                    "scope_score": 8,
                },
            ]
        }
        response = SimpleNamespace(
            choices=[
                SimpleNamespace(
                    message=SimpleNamespace(content=json.dumps(payload))
                )
            ]
        )
        client = SimpleNamespace(
            chat=SimpleNamespace(
                completions=SimpleNamespace(
                    create=AsyncMock(return_value=response)
                )
            )
        )
        extracted = asyncio.run(
            ParadigmAnalyzer(client=client, model="test").run([evidence])
        )
        self.assertEqual(len(extracted), 2)
        self.assertEqual(
            {item.canonical_name for item in extracted},
            {"Sparse attention routing", "Residual expert composition"},
        )

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

    def test_observation_pool_is_loaded_for_future_weekly_discussion(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = ParadigmStore(Path(directory) / "radar.db")
            item = candidate()
            score_candidate(item)
            self.assertEqual(item.status, "observe")
            store.save_candidates([item])
            refreshed = store.load_refresh_candidates()
            self.assertEqual([value.key for value in refreshed], [item.key])
            self.assertTrue(
                all(
                    evidence.raw.get("historical")
                    for evidence in refreshed[0].evidence
                )
            )

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
        dossier = _candidate_dossier(item)
        self.assertEqual(
            dossier["researchers"][0]["public_contacts"]["orcid"],
            "https://orcid.org/0000-0000-0000-0001",
        )
        memo = "本期从旧方法的能力边界出发，解释技术团队如何把朴素思想落实到训练和推理。" * 16
        body = "这条路线的技术机制、验证证据和潜在价值需要放在同一个问题背景中理解。" * 24
        editorial = (
            "# AI 技术范式雷达\n\n## 本期研究 Memo\n\n"
            f"{memo}\n\n## **技术路线**正在形成新的能力边界\n\n"
            f"{body} **关键机制**仍需独立复现。A. Researcher 是关键作者，"
            "公开入口：[ORCID](https://orcid.org/0000-0000-0000-0001)。\n\n"
            "## 接下来真正值得盯的信号\n\n观察独立复现与有内容的二次讨论。"
        )
        response = SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content=editorial))]
        )
        client = SimpleNamespace(
            chat=SimpleNamespace(
                completions=SimpleNamespace(
                    create=AsyncMock(return_value=response)
                )
            )
        )
        with tempfile.TemporaryDirectory() as directory:
            path = asyncio.run(
                ParadigmReportGenerator(directory, client=client, model="test").generate(
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
        english = (
            "The method treats optimization as a recursive process where every new task "
            "must preserve all previously acquired capabilities while adapting to a changing "
            "distribution through a carefully designed verification loop."
        )
        self.assertFalse(_valid_editorial_report(report + f"\n\n{english}", [item]))

    def test_report_generation_fails_instead_of_sending_raw_fallback(self) -> None:
        item = candidate()
        invalid = SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content="too short"))]
        )
        client = SimpleNamespace(
            chat=SimpleNamespace(
                completions=SimpleNamespace(
                    create=AsyncMock(return_value=invalid)
                )
            )
        )
        with tempfile.TemporaryDirectory() as directory:
            generator = ParadigmReportGenerator(directory, client=client, model="test")
            with self.assertRaises(RuntimeError):
                asyncio.run(generator.generate([item], {"origin_count": 1}))
            self.assertEqual(list(Path(directory).glob("*.md")), [])
        self.assertEqual(client.chat.completions.create.await_count, 2)

    def test_paradigm_skill_renders_json_contract(self) -> None:
        prompt = SkillLoader().render(
            "paradigm_extraction",
            source="arxiv",
            title="Test",
            abstract="Abstract",
            authors="A",
            organization="Lab",
            identifiers={"arxiv": "2607.1"},
            origin_kind="technical_report",
            publisher_context={"organization": "Lab"},
        )
        self.assertIn('"hypotheses"', prompt)
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

        revision = SkillLoader().render(
            "weekly_memo_revision",
            date="2026-07-18",
            lookback_days=7,
            violations="出现英文原文",
            candidate_dossiers="[]",
            previous_draft="draft",
        )
        self.assertIn("严禁复制英文摘要", revision)

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

    def test_priority_page_discovers_dated_official_model_post(self) -> None:
        html = """
        <a href="https://www.kimi.com/blog/kimi-k3">2026-07-14 Kimi K3</a>
        <a href="/research">Research</a>
        """
        links = _discover_index_links(html, "https://www.moonshot.ai/")
        self.assertEqual(len(links), 1)
        self.assertEqual(links[0].title, "Kimi K3")
        self.assertEqual(links[0].published_at, "2026-07-14")

    def test_arxiv_report_query_failure_does_not_drop_regular_feed(self) -> None:
        source = ArxivSource(max_results=5, lookback_days=7)
        empty_feed = (
            '<?xml version="1.0" encoding="UTF-8"?>'
            '<feed xmlns="http://www.w3.org/2005/Atom"></feed>'
        )
        source._request = AsyncMock(
            side_effect=[
                SimpleNamespace(text=empty_feed),
                httpx.ConnectError("report query unavailable"),
            ]
        )
        self.assertEqual(asyncio.run(source.fetch()), [])

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
