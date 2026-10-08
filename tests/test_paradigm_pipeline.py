from __future__ import annotations

import asyncio
import json
import tempfile
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import httpx

import config
from paradigms.completion import ResearchNotCompleteError
from tests.completion_fixtures import COMPLETED_RESEARCH
from agents.paradigm_orchestrator import ParadigmOrchestrator, _apply_safety_limit
from agents.llm_utils import parse_json_object
from database.paradigm_store import ParadigmStore
from paradigms.analyzer import (
    ParadigmAnalyzer,
    ParadigmSynthesizer,
    _author_prompt_summary,
    _bounded_synthesis_evidence,
    _validate_mental_model,
)
from paradigms.clustering import cluster_extractions
from paradigms.discovery import ParadigmDiscovery
from paradigms.enrichment import EvidenceEnricher
from paradigms.models import (
    EvidenceType,
    ParadigmCandidate,
    ParadigmExtraction,
    ResearcherProfile,
    TechnicalEvidence,
    assess_candidate_freshness,
    primary_material_url,
)
from paradigms.rubric import evaluate_rubric, load_rubric
from paradigms.scoring import is_reportable, score_candidate
from reports.paradigm_generator import (
    ParadigmReportGenerator,
    _attach_primary_source_index,
    _attach_research_scope_boundary,
    _attach_researcher_index,
    _candidate_dossier,
    _compact_route_dossier,
    _editorial_advisories,
    _editorial_violations,
    _momentum_evidence,
    _route_draft_violations,
    _route_fragment_key,
    _valid_editorial_report,
)
from research_watchlist import RESEARCH_SOURCES
from skills.loader import SkillLoader
from sources.paradigm_evidence_source import (
    CommunityEvidenceClient,
    _is_relevant_repository,
)
from sources.base import RawProject
from sources.arxiv_document_source import (
    ArxivDocumentClient,
    _distributed_text_excerpt,
    _normalize_external_href,
)
from sources.arxiv_source import ArxivSource
from sources.openalex_source import OpenAlexSource
from sources.openreview_source import OpenReviewSource
from sources.official_repository_release_source import (
    OfficialRepositoryReleaseSource,
    _linked_primary_material,
    _primary_publication_date,
)
from sources.priority_research_source import (
    PriorityResearchPageSource,
    _ArticleParser,
    _discover_index_links,
    _download_url,
)
from sources.reddit_evidence_source import RedditEvidenceClient
from sources.researcher_profile_source import ResearcherProfileClient
from sources.semantic_scholar_source import SemanticScholarClient
from sources.social_web_search_source import (
    SocialWebSearchClient,
    _parse_results as parse_tavily_results,
)


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


def rubric_answers(
    innovation_types: list[str] | None = None,
    *,
    weak_ids: set[str] | None = None,
) -> list[dict]:
    rubric = load_rubric()
    selected = innovation_types or ["architecture"]
    criteria = list(rubric["common_criteria"])
    for innovation_type in selected:
        criteria.extend(rubric["type_criteria"][innovation_type])
    weak = weak_ids or set()
    answers = []
    for criterion in criteria:
        if criterion["id"] in weak:
            answer = min(criterion["options"], key=criterion["options"].get)
        else:
            answer = max(criterion["options"], key=criterion["options"].get)
        answers.append(
            {
                "criterion_id": criterion["id"],
                "answer": answer,
                "evidence": f"{criterion['id']} 的模拟可核验证据",
            }
        )
    return answers


def rubric_assessment(
    innovation_types: list[str] | None = None,
    *,
    stage: str = "final",
    weak_ids: set[str] | None = None,
) -> dict:
    return evaluate_rubric(
        stage=stage,
        innovation_types=innovation_types or ["architecture"],
        answers=rubric_answers(innovation_types, weak_ids=weak_ids),
    )


def candidate(evidence: list[TechnicalEvidence] | None = None) -> ParadigmCandidate:
    screening = rubric_assessment(["architecture"], stage="screening")
    final = rubric_assessment(["architecture"], stage="final")
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
        mental_model={
            "observation_axis": "沿状态表征到未来状态预测的世界模型流程观察。",
            "low_resolution_model": (
                "旧方法预测像素却缺少动作变量；新方法把帧间变化压成潜在动作，"
                "再用它约束未来状态预测。"
            ),
            "decisive_intervention": "在状态转移接口引入可学习的离散潜在动作。",
            "resolution_ladder": [
                {
                    "question": "潜在动作究竟是什么？",
                    "answer": "由帧间状态变化学习出的离散变量。",
                    "evidence_status": "interpretive_compression",
                    "model_update": "它不是机器人控制量，而是预测状态转移的中间表示。",
                },
                {
                    "question": "什么信号迫使它保留动作信息？",
                    "answer": "未来状态预测误差。",
                    "evidence_status": "source_fact",
                    "model_update": "只有能解释后续变化的信息会被保留。",
                },
            ],
            "training_causal_chain": [
                "视频片段进入编码器，状态变化经预测误差被压缩为潜在动作。"
            ],
            "runtime_causal_chain": [
                "当前状态与候选潜在动作进入动力学模型，得到未来状态。"
            ],
            "minimal_simulation": "用两帧杯子移动的视频表示一次状态变化。",
            "counterfactual_and_boundary": "拿掉动作条件后只能预测平均未来。",
            "unresolved_interfaces": ["潜在动作如何与真实机器人控制量对齐。"],
        },
        application_value="让机器人从互联网视频中获得可迁移的动态先验。",
        innovation_types=["architecture"],
        lineage_parent="video prediction world models",
        lineage_path=["video prediction", "latent-action world models"],
        keywords=["latent action", "world model", "video prediction"],
        evidence=evidence or [paper()],
        screening_rubric=screening,
        rubric_assessment=final,
        novelty_score=9,
        solidity_score=8,
        scope_score=9,
        incremental_penalty=0,
    )


def verified_researcher(name: str = "A. Researcher") -> ResearcherProfile:
    return ResearcherProfile(
        name=name,
        current_affiliation="Example Lab",
        background_summary="长期研究世界模型与机器人学习。",
        contact_search_notes=[
            "当前论文题目与 OpenAlex 作者实体交叉核验通过",
            "已检索公开主页，未发现额外职业联系方式",
        ],
    )


class ParadigmPipelineTests(unittest.TestCase):
    def test_priority_watchlist_uses_current_arc_and_isomorphic_indexes(self) -> None:
        urls = {record["url"] for record in RESEARCH_SOURCES}
        self.assertIn("https://arcinstitute.org/news", urls)
        self.assertIn("https://www.isomorphiclabs.com/news", urls)
        self.assertNotIn("https://arcinstitute.org/publications", urls)
        self.assertNotIn("https://www.isomorphiclabs.com/articles", urls)

    def test_arxiv_project_link_repairs_tex_escaped_punctuation(self) -> None:
        value = (
            "http://papers.nips.cc/paper\\_files/paper/2023/hash/"
            "abc-Abstract-Datasets\\_and\\_Benchmarks.html"
        )
        normalized = _normalize_external_href(value)
        self.assertEqual(
            normalized,
            "http://papers.nips.cc/paper_files/paper/2023/hash/"
            "abc-Abstract-Datasets_and_Benchmarks.html",
        )
        self.assertEqual(_normalize_external_href("https://example.com/a\\q"), "")

    def test_zero_safety_limit_never_truncates_dynamic_volume(self) -> None:
        items = list(range(275))
        selected, deferred = _apply_safety_limit(items, 0)
        self.assertEqual(selected, items)
        self.assertEqual(deferred, [])

    def test_discovery_limit_does_not_drop_already_fetched_cross_source_items(
        self,
    ) -> None:
        with patch.object(config, "PARADIGM_DISCOVERY_SAFETY_LIMIT", 1):
            discovery = ParadigmDiscovery(lookback_days=7)
        fetched = [
            RawProject(
                source="arxiv",
                name=f"Fetched origin {index}",
                url=f"https://arxiv.org/abs/2608.000{index}",
                created_at="2026-08-15T00:00:00Z",
                extra={"arxiv_id": f"2608.000{index}"},
            )
            for index in (1, 2)
        ]

        async def bounded(source):
            values = fetched if source is discovery.arxiv else []
            return values, {
                "source": source.source_name,
                "status": "completed",
                "error_type": "",
                "results": len(values),
                "elapsed_seconds": 0.0,
                "timeout_seconds": 1,
            }

        discovery._bounded_fetch = bounded
        batch = asyncio.run(discovery.run())

        self.assertEqual(len(batch.origins), 2)
        self.assertEqual(
            {item.identifiers["arxiv"] for item in batch.origins},
            {"2608.0001", "2608.0002"},
        )

    def test_rubric_uses_type_specific_questions(self) -> None:
        architecture = rubric_assessment(["architecture"], stage="screening")
        algorithm = rubric_assessment(["algorithm"], stage="screening")
        architecture_ids = {
            item["criterion_id"] for item in architecture["answers"]
        }
        algorithm_ids = {
            item["criterion_id"] for item in algorithm["answers"]
        }
        self.assertIn("architecture_computation_change", architecture_ids)
        self.assertNotIn("architecture_computation_change", algorithm_ids)
        self.assertIn("algorithm_credit_assignment", algorithm_ids)

    def test_incomplete_rubric_requires_retry_instead_of_merit_rejection(self) -> None:
        assessment = evaluate_rubric(
            stage="screening",
            innovation_types=["architecture"],
            answers=[
                {
                    "criterion_id": "problem_is_material",
                    "answer": "yes",
                    "evidence": "存在跨任务瓶颈。",
                }
            ],
        )
        self.assertEqual(assessment["decision"], "incomplete")
        self.assertIn("应重试而不是据此淘汰", assessment["decision_reason"])

    def test_invalid_llm_json_log_does_not_dump_model_content(self) -> None:
        sensitive = '{"field":"DO_NOT_LOG_THIS", BROKEN}'
        with self.assertLogs("agents.llm_utils", level="WARNING") as captured:
            with self.assertRaises(json.JSONDecodeError):
                parse_json_object(sensitive)
        self.assertNotIn("DO_NOT_LOG_THIS", "\n".join(captured.output))

    def test_unknown_low_volume_work_stays_in_observation_pool(self) -> None:
        item = candidate()
        score_candidate(item)
        self.assertFalse(is_reportable(item))
        self.assertIn("发布者背景未核验", item.admission_reason)
        self.assertEqual(item.rubric_assessment["decision"], "observe")

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

    def test_title_alone_cannot_upgrade_an_ordinary_paper_to_formal_report(self) -> None:
        evidence = paper()
        evidence.title = "A Technical Report on One Benchmark"
        evidence.organization = "Moonshot AI"
        evidence.raw = {
            "origin_kind": "research_paper",
            "publisher_tier": "established",
            "publisher_evidence": "verified publication metadata",
        }
        item = candidate([evidence])
        score_candidate(item)
        self.assertFalse(item.is_formal_technical_report)
        self.assertNotIn("正式 Technical Report", item.admission_reason)

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
        score_candidate(item)
        self.assertFalse(is_reportable(item))
        self.assertIn("缺少实质二次讨论", item.admission_reason)

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
        self.assertIn("缺少实质二次讨论", item.admission_reason)

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
        self.assertIn("缺少实质二次讨论", item.admission_reason)

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
        self.assertIn(
            "命中 1 条", item.community_coverage["reddit_official"]
        )

    def test_community_query_failure_is_visible_in_candidate_coverage(self) -> None:
        item = candidate()
        response = SimpleNamespace(status_code=503)
        client = SimpleNamespace(get=AsyncMock(return_value=response))
        found = asyncio.run(
            CommunityEvidenceClient()._hackernews(client, item)
        )
        self.assertEqual(found, [])
        self.assertIn("HTTP 503", item.community_coverage["hackernews"])

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

    def test_independent_repository_uptake_can_validate_unknown_publisher(self) -> None:
        item = candidate()
        item.evidence.append(
            TechnicalEvidence(
                source="github",
                evidence_type=EvidenceType.IMPLEMENTATION,
                title="community/latent-action-world-models",
                url="https://github.com/community/latent-action-world-models",
                metrics={"stars": 60, "forks": 8},
                raw={
                    "relationship": "name_and_mechanism_match",
                    "independence": "independent",
                },
            )
        )
        score_candidate(item)
        self.assertTrue(is_reportable(item))
        self.assertIn("独立讨论或承接", item.admission_reason)

    def test_weak_rubric_answers_override_model_like_numeric_scores(self) -> None:
        item = candidate()
        item.novelty_score = 10
        item.scope_score = 10
        item.solidity_score = 10
        all_ids = {
            value["id"] for value in load_rubric()["common_criteria"]
        } | {
            value["id"]
            for value in load_rubric()["type_criteria"]["architecture"]
        }
        item.rubric_assessment = rubric_assessment(
            ["architecture"], stage="final", weak_ids=all_ids
        )
        score_candidate(item)
        self.assertFalse(is_reportable(item))
        self.assertEqual(item.rubric_assessment["decision"], "reject")
        self.assertLess(item.total_score, 35)

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
            innovation_types=["architecture"],
            rubric_assessment=rubric_assessment(
                ["architecture"], stage="screening"
            ),
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
            innovation_types=["architecture"],
            rubric_assessment=rubric_assessment(
                ["architecture"], stage="screening"
            ),
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
        index_payload = {
            "mechanisms": [
                {
                    "canonical_name": "Sparse attention routing",
                    "route_family": "Efficient frontier architectures",
                    "thesis": "t1",
                    "problem_shift": "p1",
                    "mechanism": "m1",
                    "keywords": ["sparse attention"],
                    "innovation_types": ["architecture"],
                    "source_evidence": ["Architecture section"],
                },
                {
                    "canonical_name": "Residual expert composition",
                    "route_family": "Efficient frontier architectures",
                    "thesis": "t2",
                    "problem_shift": "p2",
                    "mechanism": "m2",
                    "keywords": ["expert composition"],
                    "innovation_types": ["architecture"],
                    "source_evidence": ["Residual section"],
                },
            ]
        }
        index_response = SimpleNamespace(
            choices=[
                SimpleNamespace(
                    message=SimpleNamespace(content=json.dumps(index_payload))
                )
            ]
        )
        assessment_response = SimpleNamespace(
            choices=[
                SimpleNamespace(
                    message=SimpleNamespace(
                        content=json.dumps(
                            {
                                "assessment": {
                                    "innovation_types": ["architecture"],
                                    "rubric_answers": rubric_answers(
                                        ["architecture"]
                                    ),
                                }
                            }
                        )
                    )
                )
            ]
        )
        client = SimpleNamespace(
            chat=SimpleNamespace(
                completions=SimpleNamespace(
                    create=AsyncMock(
                        side_effect=[
                            index_response,
                            assessment_response,
                            assessment_response,
                        ]
                    )
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
        self.assertEqual(client.chat.completions.create.await_count, 3)

    def test_technical_report_mechanism_count_is_not_silently_capped_at_six(self) -> None:
        evidence = paper()
        evidence.raw = {"origin_kind": "technical_report"}
        mechanisms = [
            {
                "canonical_name": f"Mechanism {index}",
                "route_family": "Frontier systems",
                "thesis": "t",
                "problem_shift": "p",
                "mechanism": "m",
                "keywords": [f"mechanism-{index}"],
                "innovation_types": ["architecture"],
                "source_evidence": [f"Section {index}"],
            }
            for index in range(1, 8)
        ]
        index_response = SimpleNamespace(
            choices=[
                SimpleNamespace(
                    message=SimpleNamespace(
                        content=json.dumps(
                            {
                                "report_disposition": "mechanisms_found",
                                "disposition_reason": "存在七项独立机制。",
                                "mechanisms": mechanisms,
                            }
                        )
                    )
                )
            ]
        )
        assessment_response = SimpleNamespace(
            choices=[
                SimpleNamespace(
                    message=SimpleNamespace(
                        content=json.dumps(
                            {
                                "assessment": {
                                    "innovation_types": ["architecture"],
                                    "rubric_answers": rubric_answers(
                                        ["architecture"]
                                    ),
                                }
                            }
                        )
                    )
                )
            ]
        )
        client = SimpleNamespace(
            chat=SimpleNamespace(
                completions=SimpleNamespace(
                    create=AsyncMock(
                        side_effect=[
                            index_response,
                            *[assessment_response for _ in range(7)],
                        ]
                    )
                )
            )
        )
        extracted = asyncio.run(
            ParadigmAnalyzer(client=client, model="test").extract(evidence)
        )
        self.assertEqual(len(extracted), 7)

    def test_technical_report_mechanisms_resume_from_bounded_checkpoint(self) -> None:
        evidence = paper("checkpoint")
        evidence.raw = {"origin_kind": "technical_report"}
        mechanisms = [
            {
                "canonical_name": f"Mechanism {index}",
                "route_family": "Frontier systems",
                "thesis": "t",
                "problem_shift": "p",
                "mechanism": "m",
                "keywords": [f"mechanism-{index}"],
                "innovation_types": ["architecture"],
                "source_evidence": [f"Section {index}"],
            }
            for index in range(1, 4)
        ]
        index_response = SimpleNamespace(
            choices=[
                SimpleNamespace(
                    message=SimpleNamespace(
                        content=json.dumps({"mechanisms": mechanisms})
                    )
                )
            ]
        )
        assessment_response = SimpleNamespace(
            choices=[
                SimpleNamespace(
                    message=SimpleNamespace(
                        content=json.dumps(
                            {
                                "assessment": {
                                    "innovation_types": ["architecture"],
                                    "rubric_answers": rubric_answers(
                                        ["architecture"]
                                    ),
                                }
                            }
                        )
                    )
                )
            ]
        )
        client = SimpleNamespace(
            chat=SimpleNamespace(
                completions=SimpleNamespace(
                    create=AsyncMock(
                        side_effect=[
                            index_response,
                            assessment_response,
                            assessment_response,
                            assessment_response,
                        ]
                    )
                )
            )
        )
        analyzer = ParadigmAnalyzer(
            client=client,
            model="test",
            technical_report_mechanism_slice=2,
        )

        first = asyncio.run(analyzer.extract(evidence))
        self.assertEqual(
            len([item for item in first if item.canonical_name]), 2
        )
        self.assertTrue(evidence.raw["technical_report_slice_pending"])
        self.assertEqual(client.chat.completions.create.await_count, 3)

        second = asyncio.run(analyzer.extract(evidence))
        self.assertEqual(
            {item.canonical_name for item in second},
            {"Mechanism 3"},
        )
        self.assertNotIn("technical_report_slice_pending", evidence.raw)
        self.assertEqual(client.chat.completions.create.await_count, 4)

    def test_pending_technical_report_restores_mechanism_checkpoint(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = ParadigmStore(Path(directory) / "radar.db")
            evidence = paper("restore")
            evidence.raw = {
                "origin_kind": "technical_report",
                "technical_report_checkpoint_version": (
                    "technical-report-checkpoint-v1"
                ),
                "technical_report_mechanism_seeds": [
                    {"canonical_name": "Mechanism A"}
                ],
                "technical_report_completed_mechanisms": {"key": {"x": 1}},
                "technical_report_mechanism_failure_counts": {"key": 2},
            }
            store.mark_evidence([evidence], analyzed=False)

            rediscovered = paper("restore")
            selected, _ = store.plan_origins([rediscovered])

            self.assertEqual(len(selected), 1)
            self.assertEqual(
                rediscovered.raw["technical_report_mechanism_failure_counts"],
                {"key": 2},
            )

    def test_batch_origin_eligibility_screens_only_explicit_rejection(self) -> None:
        survey = paper("survey")
        mechanism = paper("mechanism")
        triage_response = SimpleNamespace(
            choices=[
                SimpleNamespace(
                    message=SimpleNamespace(
                        content=json.dumps(
                            {
                                "decisions": [
                                    {
                                        "fingerprint": survey.fingerprint,
                                        "decision": "screen_out",
                                        "reason": "摘要明确说明这是无新机制的综述。",
                                    },
                                    {
                                        "fingerprint": mechanism.fingerprint,
                                        "decision": "full_review",
                                        "reason": "可能改变训练信号，需要完整核验。",
                                    },
                                ]
                            }
                        )
                    )
                )
            ]
        )
        extraction_response = SimpleNamespace(
            choices=[
                SimpleNamespace(
                    message=SimpleNamespace(
                        content=json.dumps(
                            {
                                "hypotheses": [
                                    {
                                        "canonical_name": "Mechanism route",
                                        "thesis": "t",
                                        "problem_shift": "p",
                                        "mechanism": "m",
                                        "innovation_types": ["architecture"],
                                        "rubric_answers": rubric_answers(
                                            ["architecture"]
                                        ),
                                    }
                                ]
                            }
                        )
                    )
                )
            ]
        )
        client = SimpleNamespace(
            chat=SimpleNamespace(
                completions=SimpleNamespace(
                    create=AsyncMock(
                        side_effect=[triage_response, extraction_response]
                    )
                )
            )
        )

        extracted = asyncio.run(
            ParadigmAnalyzer(
                client=client,
                model="test",
                enable_batch_prefilter=True,
            ).run([survey, mechanism])
        )

        by_fingerprint = {
            item.evidence.fingerprint: item for item in extracted
        }
        self.assertEqual(
            by_fingerprint[survey.fingerprint].rubric_assessment["version"],
            "origin-eligibility-v1",
        )
        self.assertEqual(
            by_fingerprint[mechanism.fingerprint].canonical_name,
            "Mechanism route",
        )
        self.assertEqual(client.chat.completions.create.await_count, 2)

    def test_technical_report_mechanism_failure_is_isolated(self) -> None:
        evidence = paper()
        evidence.raw = {"origin_kind": "technical_report"}
        mechanisms = [
            {
                "canonical_name": name,
                "route_family": "Frontier systems",
                "thesis": "t",
                "problem_shift": "p",
                "mechanism": "m",
                "keywords": [name],
                "innovation_types": ["architecture"],
                "source_evidence": ["Architecture section"],
            }
            for name in ("Mechanism A", "Mechanism B")
        ]
        index_response = SimpleNamespace(
            choices=[
                SimpleNamespace(
                    message=SimpleNamespace(
                        content=json.dumps({"mechanisms": mechanisms})
                    )
                )
            ]
        )
        valid_assessment = SimpleNamespace(
            choices=[
                SimpleNamespace(
                    message=SimpleNamespace(
                        content=json.dumps(
                            {
                                "assessment": {
                                    "innovation_types": ["architecture"],
                                    "rubric_answers": rubric_answers(
                                        ["architecture"]
                                    ),
                                }
                            }
                        )
                    )
                )
            ]
        )
        invalid_assessment = SimpleNamespace(
            choices=[
                SimpleNamespace(
                    message=SimpleNamespace(content='{"assessment": BROKEN}')
                )
            ]
        )
        client = SimpleNamespace(
            chat=SimpleNamespace(
                completions=SimpleNamespace(
                    create=AsyncMock(
                        side_effect=[
                            index_response,
                            valid_assessment,
                            invalid_assessment,
                            invalid_assessment,
                        ]
                    )
                )
            )
        )
        extracted = asyncio.run(
            ParadigmAnalyzer(client=client, model="test").extract(evidence)
        )
        self.assertEqual(
            [item.canonical_name for item in extracted if item.canonical_name],
            ["Mechanism A"],
        )
        self.assertTrue(
            any(
                item.rejection_reason.startswith(
                    "Technical Report 部分机制评估失败"
                )
                for item in extracted
            )
        )
        self.assertTrue(evidence.raw["technical_report_partial_failure"])

    def test_report_with_no_independent_mechanism_is_a_valid_research_result(self) -> None:
        evidence = paper()
        evidence.title = "Model Safety System Card"
        evidence.raw = {"origin_kind": "technical_report"}
        response = SimpleNamespace(
            choices=[
                SimpleNamespace(
                    message=SimpleNamespace(
                        content=json.dumps(
                            {
                                "report_disposition": "no_independent_mechanism",
                                "disposition_reason": (
                                    "正文只汇总既有安全评测，没有新的训练或推理机制。"
                                ),
                                "mechanisms": [],
                            }
                        )
                    )
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
            ParadigmAnalyzer(client=client, model="test").extract(evidence)
        )
        self.assertEqual(len(extracted), 1)
        self.assertFalse(extracted[0].is_candidate)
        self.assertEqual(
            extracted[0].rubric_assessment["decision"],
            "reject",
        )
        self.assertNotIn("索引失败", extracted[0].rejection_reason)

    def test_synthesis_builds_internal_mental_model_for_deep_candidates(self) -> None:
        payload = {
            "innovation_types": ["architecture"],
            "rubric_answers": rubric_answers(["architecture"]),
            "substantive_uptake_evidence_indices": [1],
            "mental_model": {
                "observation_axis": "沿世界模型的状态转移预测流程观察。",
                "low_resolution_model": (
                    "旧模型预测画面但没有动作变量；新方法把帧间变化压成潜在动作，"
                    "再以它为条件预测未来状态。"
                ),
                "decisive_intervention": "在状态转移接口引入离散潜在动作。",
                "resolution_ladder": [
                    {
                        "question": "潜在动作在计算图中是什么？",
                        "answer": "状态转移的离散表示。",
                        "evidence_status": "interpretive_compression",
                        "model_update": "它先解释视频变化，还不是机器人控制量。",
                    },
                    {
                        "question": "什么信号使它保留可预测变化？",
                        "answer": "未来状态预测误差。",
                        "evidence_status": "source_fact",
                        "model_update": "预测目标把动作表示和未来状态绑定起来。",
                    },
                ],
                "training_causal_chain": [
                    "编码相邻视频帧",
                    "用未来状态预测误差学习潜在动作",
                ],
                "runtime_causal_chain": [
                    "读取当前状态",
                    "预测候选动作后的未来状态",
                ],
                "minimal_simulation": "杯子从桌面左侧移动到右侧。",
                "misconception_corrections": [
                    {
                        "hypothesis": "潜在动作等于真实控制量。",
                        "correction": "它首先是从视频变化学习的中间变量。",
                        "basis": "尚未看到与机器人控制接口的对齐证据。",
                    }
                ],
                "counterfactual_and_boundary": "没有动作条件时只能学习平均变化。",
                "unresolved_interfaces": ["潜在动作如何与机器人控制量对齐"],
            }
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
        item = candidate()
        item.mental_model = {}
        item.evidence[0].summary = "机制细节" * 1000
        item.evidence.append(
            TechnicalEvidence(
                source="curated-kol-blog",
                evidence_type=EvidenceType.SECONDARY_INTERPRETATION,
                title="Direct mechanism analysis",
                url="https://example.net/direct-analysis",
                summary="逐项分析训练信号、机制边界和反事实。" * 30,
                authors=["Independent Analyst"],
                raw={
                    "relationship": "independent_commentary",
                    "independence": "independent",
                },
            )
        )
        asyncio.run(
            ParadigmSynthesizer(client=client, model="test").run([item])
        )
        self.assertEqual(
            item.mental_model["observation_axis"],
            "沿世界模型的状态转移预测流程观察。",
        )
        self.assertIn(
            "潜在动作如何与机器人控制量对齐",
            item.mental_model["unresolved_interfaces"],
        )
        prompt = client.chat.completions.create.await_args.kwargs["messages"][0][
            "content"
        ]
        self.assertGreater(len(prompt), 2400)
        self.assertIn("先建立低分辨率运行图", prompt)
        self.assertIn("interpretive_compression", prompt)
        self.assertEqual(
            _candidate_dossier(item)["mental_model"], item.mental_model
        )
        self.assertTrue(item.evidence[1].raw["substantive_uptake"])
        self.assertEqual(
            item.evidence[1].raw["substantive_uptake_source"],
            "synthesis-v1",
        )

    def test_synthesis_evidence_budget_is_explicit_not_a_fixed_top_k(self) -> None:
        tiny = [paper(str(index)) for index in range(30)]
        for index, value in enumerate(tiny):
            value.title = f"Compact evidence {index}"
            value.url = f"https://example.org/paper/{index}"
            value.summary = "短证据"
        represented = _bounded_synthesis_evidence(tiny)
        self.assertGreater(len(represented["records"]), 24)
        self.assertEqual(represented["overflow"]["count"], 0)

        large = [paper(str(index)) for index in range(80)]
        for index, value in enumerate(large):
            value.title = f"Large evidence {index}"
            value.url = f"https://example.org/large/{index}"
            value.summary = "机制正文" * 2000
            value.raw["document_excerpt"] = "完整报告" * 6000
        bounded = _bounded_synthesis_evidence(large)
        serialized = json.dumps(bounded, ensure_ascii=False)
        self.assertLess(len(serialized), 60_000)
        self.assertGreater(bounded["overflow"]["count"], 0)
        self.assertIn("不代表被 Rubric 淘汰", bounded["overflow"]["meaning"])

    def test_synthesis_repairs_only_invalid_structure_after_valid_json(self) -> None:
        first_payload = {
            "innovation_types": ["architecture"],
            "rubric_answers": rubric_answers(["architecture"]),
            "mental_model": {
                "observation_axis": "沿状态转移观察。",
                "low_resolution_model": "旧模型预测像素，新模型预测状态。",
                "decisive_intervention": "改变状态转移接口。",
                "resolution_ladder": [
                    {
                        "question": "改变了什么？",
                        "answer": "状态接口。",
                        "evidence_status": "source_fact",
                        "model_update": "不再只看像素。",
                    },
                    {
                        "question": "信号来自哪里？",
                        "answer": "预测误差。",
                        "evidence_status": "source_fact",
                        "model_update": "闭合训练信号。",
                    },
                ],
                "training_causal_chain": ["状态进入模型并由预测误差更新参数。"],
                "runtime_causal_chain": ["当前状态映射为未来状态。"],
                "minimal_simulation": "一个杯子从左向右移动。",
            },
        }
        repair_payload = {
            "mental_model": {
                "counterfactual_and_boundary": "移除状态接口后退回平均像素预测。"
            }
        }
        responses = [
            SimpleNamespace(
                choices=[
                    SimpleNamespace(
                        message=SimpleNamespace(content=json.dumps(first_payload))
                    )
                ]
            ),
            SimpleNamespace(
                choices=[
                    SimpleNamespace(
                        message=SimpleNamespace(content=json.dumps(repair_payload))
                    )
                ]
            ),
        ]
        client = SimpleNamespace(
            chat=SimpleNamespace(
                completions=SimpleNamespace(
                    create=AsyncMock(side_effect=responses)
                )
            )
        )
        item = candidate()
        item.mental_model = {}
        item.evidence[0].raw["document_excerpt"] = "很长的报告正文" * 4000
        asyncio.run(ParadigmSynthesizer(client=client, model="test").run([item]))
        calls = client.chat.completions.create.await_args_list
        first_prompt = calls[0].kwargs["messages"][0]["content"]
        repair_prompt = calls[1].kwargs["messages"][0]["content"]
        self.assertIn("counterfactual_and_boundary", repair_prompt)
        self.assertLess(len(repair_prompt), len(first_prompt))
        self.assertEqual(
            item.mental_model["counterfactual_and_boundary"],
            "移除状态接口后退回平均像素预测。",
        )

    def test_mental_model_rejects_module_dump_without_resolution_ladder(self) -> None:
        with self.assertRaisesRegex(ValueError, "observation_axis"):
            _validate_mental_model(
                {
                    "system_objects": ["Encoder", "SCM", "U-Net"],
                    "training_flow": ["联合训练全部模块"],
                    "inference_flow": ["模型输出图像"],
                    "minimal_example": "编辑年龄。",
                    "counterfactual_and_boundary": "可能泛化不足。",
                }
            )

    def test_mental_model_normalizes_semantic_status_aliases(self) -> None:
        mental_model = {
            "observation_axis": "沿训练信息流观察。",
            "low_resolution_model": "先编码再预测。",
            "decisive_intervention": "改写训练信号。",
            "minimal_simulation": "一个样本进入编码器。",
            "counterfactual_and_boundary": "移除信号后能力消失。",
            "resolution_ladder": [
                {
                    "question": "训练事实是什么？",
                    "answer": "使用预测损失。",
                    "evidence_status": "source factual",
                    "model_update": "确认训练对象。",
                },
                {
                    "question": "为何可能泛化？",
                    "answer": "这是对机制的压缩解释。",
                    "evidence_status": "interpretation",
                    "model_update": "补上迁移直觉。",
                },
            ],
            "training_causal_chain": ["样本 → 编码 → 预测损失 → 参数更新"],
            "runtime_causal_chain": [],
            "unresolved_interfaces": [],
        }
        _validate_mental_model(mental_model)
        self.assertEqual(
            [item["evidence_status"] for item in mental_model["resolution_ladder"]],
            ["source_fact", "interpretive_compression"],
        )

    def test_technical_mental_model_skill_requires_progressive_correction(self) -> None:
        method = SkillLoader().load("technical-mental-model")
        self.assertIn("先建立低分辨率运行图", method)
        self.assertIn("选择一个主导观察坐标", method)
        self.assertIn("条件注入不等于改写采样动力学", method)
        self.assertIn("interpretive_compression", method)
        case = (
            Path("skills/technical-mental-model/references/"
                 "cidiffuser-calibration.md")
            .read_text(encoding="utf-8")
        )
        self.assertIn("不是“因果图直接定义了噪声轨迹”", case)
        self.assertIn("顺畅但错误的解释", case)

    def test_synthesis_failure_cannot_fall_back_into_report(self) -> None:
        response = SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content="{}"))]
        )
        client = SimpleNamespace(
            chat=SimpleNamespace(
                completions=SimpleNamespace(
                    create=AsyncMock(return_value=response)
                )
            )
        )
        item = candidate()
        item.mental_model = {}
        asyncio.run(
            ParadigmSynthesizer(client=client, model="test").run([item])
        )
        score_candidate(item)
        self.assertEqual(client.chat.completions.create.await_count, 2)
        self.assertEqual(item.rubric_assessment["decision"], "incomplete")
        self.assertEqual(item.status, "pending_deep")
        self.assertFalse(is_reportable(item))
        self.assertIn("范式综合失败", item.rejection_reason)

    def test_malformed_external_metrics_cannot_crash_candidate_scoring(self) -> None:
        item = candidate()
        item.evidence.extend(
            [
                TechnicalEvidence(
                    source="openreview",
                    evidence_type=EvidenceType.PEER_REVIEW,
                    title="Review discussion",
                    url="https://openreview.net/forum?id=fixture",
                    metrics={"review_replies": "N/A", "score": {"bad": 1}},
                ),
                TechnicalEvidence(
                    source="github",
                    evidence_type=EvidenceType.IMPLEMENTATION,
                    title="Implementation",
                    url="https://github.com/example/implementation",
                    metrics={"forks": "unknown", "stars": None},
                    raw={
                        "relationship": "official_release_repository",
                        "independence": "official",
                    },
                ),
            ]
        )

        scored = score_candidate(item)

        self.assertIs(scored, item)
        self.assertGreaterEqual(item.momentum_score, 0.0)

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

    def test_first_seen_historical_reactivation_is_labeled_update(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = ParadigmStore(Path(directory) / "radar.db")
            item = candidate()
            item.freshness_assessment = {
                "classification": "historical_reactivated",
                "decision": "include",
            }
            selected = store.prepare_report([item])
            self.assertEqual(selected[0].report_kind, "update")

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
        route = (
            "### **技术路线**正在形成新的能力边界\n\n"
            f"{body} **关键机制**仍需独立复现。A. Researcher 是关键作者，"
            "公开入口：[ORCID](https://orcid.org/0000-0000-0000-0001)，"
            "并可[查看原文](https://arxiv.org/abs/2607.00001)。\n\n"
            "**讨论势能判断：** 当前仍是单点提出，尚未看到独立复现；"
            "本轮社区覆盖有限，因此暂不判断为扩散。"
        )
        frame = (
            "# AI 技术范式雷达\n\n## 本期研究 Memo\n\n"
            f"{memo}\n\n"
            "## 接下来真正值得盯的信号\n\n观察独立复现与有内容的二次讨论。"
        )
        responses = [
            SimpleNamespace(
                choices=[SimpleNamespace(message=SimpleNamespace(content=value))]
            )
            for value in (route, frame)
        ]
        client = SimpleNamespace(
            chat=SimpleNamespace(
                completions=SimpleNamespace(
                    create=AsyncMock(side_effect=responses)
                )
            )
        )
        with tempfile.TemporaryDirectory() as directory:
            path = asyncio.run(
                ParadigmReportGenerator(directory, client=client, model="test").generate(
                    [item], {**COMPLETED_RESEARCH, "origin_count": 1}
                )
            )
            content = path.read_text(encoding="utf-8")
        self.assertIn("https://orcid.org/0000-0000-0000-0001", content)
        self.assertIn("## 原文与一手资料", content)
        self.assertIn("https://arxiv.org/abs/2607.00001", content)
        self.assertNotIn("@example", content)
        self.assertIn("## 本期研究 Memo", content)
        self.assertNotIn("评分拆解", content)
        self.assertNotIn("| 新颖性 |", content)

    def test_deterministic_person_index_prevents_organization_only_retry_loop(
        self,
    ) -> None:
        """Regression for Actions runs 31943920436/32440480145/33172597536."""

        item = candidate()
        item.researchers = [verified_researcher("A. Researcher")]
        item.publisher_tier = "established"
        item.is_formal_technical_report = True
        item.evidence[0].organization = "Example Research Lab"
        body = (
            "旧系统把所有变化混在同一个表示里，新方法只改写决定状态转移的接口。"
            "训练时预测误差沿这个接口更新参数，运行时当前状态先形成中间表示，"
            "再决定下一状态；拿掉这一接口后，系统会退回平均预测。"
        ) * 8
        route = (
            "### 世界模型开始把变化压成可行动状态\n\n"
            f"{body} 该路线由 Example Research Lab 发布，"
            "[查看原文](https://arxiv.org/abs/2607.00001)。\n\n"
            "**讨论势能判断：** 当前仍是单点提出，尚未看到独立复现；"
            "本轮社区覆盖有限，因此暂不判断为扩散。"
        )
        self.assertEqual(_route_draft_violations(route, item), [])
        memo = (
            "本期工作把状态转移接口从像素生成中拆出，使训练信号和运行时信息流"
            "围绕可行动变化重新组织。现有证据仍主要来自发布团队，外部复现不足，"
            "因此应把它视为需要继续验证的技术路线，而不是成熟共识。"
        ) * 4
        frame = (
            "# AI 技术范式雷达\n\n## 本期研究 Memo\n\n"
            f"{memo}\n\n## 接下来真正值得盯的信号\n\n"
            "观察独立复现能否确认状态接口跨任务迁移。"
        )
        client = SimpleNamespace(
            chat=SimpleNamespace(
                completions=SimpleNamespace(
                    create=AsyncMock(
                        side_effect=[
                            SimpleNamespace(
                                choices=[
                                    SimpleNamespace(
                                        message=SimpleNamespace(content=route)
                                    )
                                ]
                            ),
                            SimpleNamespace(
                                choices=[
                                    SimpleNamespace(
                                        message=SimpleNamespace(content=frame)
                                    )
                                ]
                            ),
                        ]
                    )
                )
            )
        )

        with tempfile.TemporaryDirectory() as directory:
            path = asyncio.run(
                ParadigmReportGenerator(
                    directory, client=client, model="test"
                ).generate([item], {**COMPLETED_RESEARCH, "origin_count": 1})
            )
            content = path.read_text(encoding="utf-8")

        self.assertIn("Example Research Lab", content)
        self.assertIn("A. Researcher", content)
        self.assertIn("## 关键人物与公开联系入口", content)
        self.assertEqual(client.chat.completions.create.await_count, 2)

    def test_multi_route_report_is_bounded_and_persists_each_route(self) -> None:
        first = candidate([paper("1")])
        first.researchers = [verified_researcher("A. Researcher")]
        second = candidate([paper("2")])
        second.key = "second-route"
        second.name = "Second route"
        second.route_family = "Reasoning-time memory"
        second.researchers = [verified_researcher("B. Researcher")]
        for route_index, item in enumerate((first, second), 1):
            for evidence_index in range(25):
                item.evidence.append(
                    TechnicalEvidence(
                        source="reddit",
                        evidence_type=EvidenceType.COMMUNITY_DISCUSSION,
                        title=f"discussion-{route_index}-{evidence_index}",
                        url=(
                            "https://reddit.com/r/MachineLearning/comments/"
                            f"{route_index}-{evidence_index}"
                        ),
                        summary="社区对机制边界的中文讨论" * 800,
                        raw={
                            "relationship": "independent_discussion",
                            "independence": "independent",
                            "substantive_uptake": True,
                            "substantive_uptake_source": "synthesis-v1",
                            "substantive_uptake_route_key": item.key,
                        },
                    )
                )

        route_body = (
            "旧系统把所有变化混在同一个表示里，新方法只改写决定状态转移的接口。"
            "训练时预测误差沿这个接口更新参数，运行时当前状态先形成中间表示，"
            "再决定下一状态；拿掉这一接口后，系统会退回平均预测。"
        ) * 8
        route_one = (
            "### 世界模型开始把变化压成可行动状态\n\n"
            f"{route_body} A. Researcher 延续了此前的世界模型研究。"
            "[查看原文](https://arxiv.org/abs/2607.00001)。\n\n"
            "**讨论势能判断：** 当前仍是单点提出，社区开始讨论状态接口，"
            "但尚无独立复现；本轮只覆盖到部分公开 Reddit 页面。"
        )
        route_two = (
            "### 推理时记忆开始进入状态更新闭环\n\n"
            f"{route_body} B. Researcher 的连续工作聚焦推理时状态。"
            "[查看原文](https://arxiv.org/abs/2607.00002)。\n\n"
            "**讨论势能判断：** 当前出现少量机制解读，尚未形成多团队承接；"
            "X 与小红书没有完整平台覆盖。"
        )
        memo = (
            "本期两条路线表面分属世界模型与推理系统，实质都在重写状态如何被保留和更新。"
            "旧系统把上下文当作一次性输入，新工作则尝试把可行动变化或推理记忆放进运行闭环。"
            "这并不等于两条路线已经形成统一范式，但它们共同说明研究重心正从扩大输入规模，"
            "转向设计可持续更新的内部状态。当前证据仍以原始工作和少量机制讨论为主，"
            "独立复现与跨团队采用还不足，因此更适合作为需要验证的技术方向，而非成熟共识。"
        ) * 2
        frame = (
            "# AI 技术范式雷达\n\n## 本期研究 Memo\n\n"
            f"{memo}\n\n## 接下来真正值得盯的信号\n\n"
            "观察独立复现是否证明这些状态接口能跨任务迁移。"
        )
        responses = [
            SimpleNamespace(
                choices=[SimpleNamespace(message=SimpleNamespace(content=value))]
            )
            for value in (route_one, route_two, frame)
        ]
        client = SimpleNamespace(
            chat=SimpleNamespace(
                completions=SimpleNamespace(
                    create=AsyncMock(side_effect=responses)
                )
            )
        )
        fragments = {}
        with tempfile.TemporaryDirectory() as directory:
            path = asyncio.run(
                ParadigmReportGenerator(
                    directory, client=client, model="test"
                ).generate(
                    [first, second],
                    {**COMPLETED_RESEARCH, "origin_count": 52},
                    save_route_fragment=fragments.__setitem__,
                )
            )
            content = path.read_text(encoding="utf-8")

        self.assertEqual(len(fragments), 2)
        self.assertIn(_route_fragment_key(first), fragments)
        self.assertIn(_route_fragment_key(second), fragments)
        self.assertIn(route_one.strip(), content)
        self.assertIn(route_two.strip(), content)
        self.assertEqual(content.count("**讨论势能判断：**"), 2)
        self.assertEqual(client.chat.completions.create.await_count, 3)
        first_dossier = _compact_route_dossier(first)
        self.assertGreater(
            first_dossier["evidence_overflow"]["momentum_evidence"]["count"],
            0,
        )
        prompt_lengths = [
            len(call.kwargs["messages"][0]["content"])
            for call in client.chat.completions.create.await_args_list
        ]
        self.assertLess(max(prompt_lengths), 45_000)

    def test_one_route_failure_does_not_discard_peer_route_checkpoints(self) -> None:
        first = candidate([paper("1")])
        second = candidate([paper("2")])
        second.key = "failing-route"
        third = candidate([paper("3")])
        third.key = "third-route"
        generator = ParadigmReportGenerator(client=False)

        async def draft(_date, item, **_kwargs):
            if item.key == "failing-route":
                raise TimeoutError("one route timed out")
            return f"### {item.name}\n\n已通过路线级质量闸门"

        generator._draft_one_route = draft
        saved = {}
        with self.assertRaisesRegex(RuntimeError, "1 条路线草稿未完成"):
            asyncio.run(
                generator._draft_routes(
                    "2026-08-10",
                    [first, second, third],
                    cached={},
                    save_fragment=saved.__setitem__,
                )
            )

        self.assertEqual(len(saved), 2)
        self.assertIn(_route_fragment_key(first), saved)
        self.assertIn(_route_fragment_key(third), saved)

    def test_researcher_index_keeps_completed_search_trace_when_no_contact_exists(self) -> None:
        item = candidate()
        item.researchers = [
            ResearcherProfile(
                name="A. Researcher",
                current_affiliation="Example Lab",
                background_summary="长期研究世界模型。",
                contact_search_notes=[
                    "当前论文题目与 OpenAlex 作者实体交叉核验通过"
                ],
            )
        ]
        content = _attach_researcher_index(
            "# Radar\n\n## 接下来真正值得盯的信号\n\n继续观察。",
            [item],
        )
        self.assertIn("未找到可核验的公开联系入口", content)
        self.assertIn("检索记录", content)
        self.assertIn("当前论文题目", content)

    def test_researcher_index_discloses_team_attribution_boundary(self) -> None:
        item = candidate()
        item.researchers = []
        item.publisher_tier = "established"
        item.is_formal_technical_report = True
        item.evidence[0].organization = "Example Research Lab"
        content = _attach_researcher_index(
            "# Radar\n\n## 接下来真正值得盯的信号\n\n继续观察。",
            [item],
        )
        self.assertIn("Example Research Lab", content)
        self.assertIn("不猜测负责人", content)
        self.assertIn(item.evidence[0].url, content)

    def test_momentum_dossier_excludes_self_release_and_search_index_noise(self) -> None:
        item = candidate()
        item.evidence.extend(
            [
                TechnicalEvidence(
                    source="reddit",
                    evidence_type=EvidenceType.COMMUNITY_DISCUSSION,
                    title="Independent technical discussion",
                    url="https://reddit.com/r/MachineLearning/comments/independent",
                    metrics={"score": 42, "comments": 13},
                    raw={
                        "relationship": "independent_discussion",
                        "independence": "independent",
                        "substantive_uptake": True,
                        "substantive_uptake_source": "synthesis-v1",
                        "substantive_uptake_route_key": item.key,
                    },
                ),
                TechnicalEvidence(
                    source="x-title-search",
                    evidence_type=EvidenceType.SECONDARY_INTERPRETATION,
                    title="Author announcement",
                    url="https://x.com/author/status/1",
                    raw={"relationship": "author_self_release"},
                ),
                TechnicalEvidence(
                    source="tavily-social-web",
                    evidence_type=EvidenceType.COMMUNITY_DISCUSSION,
                    title="Indexed result only",
                    url="https://example.com/indexed",
                    raw={"indexed_discovery_only": True},
                ),
                TechnicalEvidence(
                    source="hackernews",
                    evidence_type=EvidenceType.COMMUNITY_DISCUSSION,
                    title="Title-only search hit",
                    url="https://news.ycombinator.com/item?id=123",
                    metrics={"score": 500},
                    raw={"relationship": "exact_or_mechanism_title_match"},
                ),
            ]
        )

        selected = _momentum_evidence(item)
        self.assertEqual(
            [value.title for value in selected],
            ["Independent technical discussion"],
        )
        dossier = _candidate_dossier(item)
        self.assertEqual(len(dossier["momentum_evidence"]), 1)
        self.assertEqual(dossier["momentum_evidence"][0]["metrics"]["score"], 42)

    def test_execution_failure_metadata_is_not_exposed_to_report_writer(
        self,
    ) -> None:
        item = candidate()
        item.execution_failure_count = 3
        item.last_execution_failure_at = "2026-08-15T00:00:00Z"

        full_dossier = _candidate_dossier(item)
        compact_dossier = _compact_route_dossier(item)

        self.assertNotIn("execution_failure_count", full_dossier)
        self.assertNotIn("last_execution_failure_at", full_dossier)
        self.assertNotIn("execution_failure_count", compact_dossier)
        self.assertNotIn("last_execution_failure_at", compact_dossier)

    def test_editorial_gate_rejects_scores_and_tables(self) -> None:
        item = candidate()
        item.researchers = [verified_researcher()]
        memo = "本期技术路线从旧方法的边界出发，解释设计思想如何落到训练与推理。" * 18
        body = "技术部分继续分析问题、机制、证据和应用价值。" * 25
        report = (
            "# AI 技术范式雷达\n\n## 本期研究 Memo\n\n"
            f"{memo}\n\n## **技术路线**开始改变能力边界\n\n"
            f"{body} **关键机制**值得继续验证。A. Researcher 是本期关键作者。\n\n"
            "**讨论势能判断：** 目前出现少量有内容的社区讨论，但尚无独立复现；"
            "本轮覆盖不足以判断跨平台扩散。\n\n"
            "## 接下来真正值得盯的信号\n\n观察独立复现与有内容的二次讨论。"
        )
        report = _attach_researcher_index(report, [item])
        report = _attach_primary_source_index(report, [item])
        self.assertTrue(_valid_editorial_report(report, [item]))
        self.assertFalse(
            _valid_editorial_report(
                report.replace("**讨论势能判断：**", "**传播情况：**"),
                [item],
            )
        )
        self.assertFalse(_valid_editorial_report(report + "\n\n总分：92", [item]))
        self.assertFalse(
            _valid_editorial_report(report + "\n\n| 项目 | 数据 |\n|---|---|", [item])
        )
        self.assertFalse(
            _valid_editorial_report(
                report + "\n\n这是真正意义上的新范式。",
                [item],
            )
        )
        english = (
            "The method treats optimization as a recursive process where every new task "
            "must preserve all previously acquired capabilities while adapting to a changing "
            "distribution through a carefully designed verification loop."
        )
        self.assertFalse(_valid_editorial_report(report + f"\n\n{english}", [item]))

    def test_incomplete_memo_is_rejected_instead_of_getting_scope_disclaimer(self) -> None:
        content = "# AI 技术范式雷达\n\n## 本期研究 Memo\n\n研究结论。"
        with self.assertRaises(ResearchNotCompleteError):
            _attach_research_scope_boundary(
                content, {**COMPLETED_RESEARCH, "pending_work_count": 88}
            )
        self.assertTrue(any(
            "阶段性" in value for value in _editorial_violations(
                content + "\n\n> **阶段性研究范围：** 本轮只完成部分研究。", []
            )
        ))
    def test_report_gate_parses_memo_before_level_three_route_heading(self) -> None:
        item = candidate()
        memo = "本期从旧系统的运行边界出发，解释新机制改变了什么关键接口以及为什么值得继续观察。" * 14
        body = "这条路线继续说明训练信号、推理路径、客观证据与应用边界。" * 28
        report = (
            "# AI 技术范式雷达\n\n## 本期研究 Memo\n\n"
            f"{memo}\n\n### 路线从状态预测转向可行动表示\n\n"
            f"{body} **关键机制**需要复现。A. Researcher 是关键作者。\n\n"
            "## 接下来真正值得盯的信号\n\n继续观察独立承接。"
        )
        item.researchers = [verified_researcher()]
        report = _attach_researcher_index(report, [item])
        report = _attach_primary_source_index(report, [item])
        violations = _editorial_violations(report, [item])
        self.assertFalse(
            any("Memo 中文长度" in value for value in violations), violations
        )

    def test_original_source_index_is_deterministic_and_hard_required(self) -> None:
        first = candidate([paper("1")])
        second = candidate([paper("2")])
        second.key = "second-route"
        second.name = "Second route"
        second.route_family = "A second technical route"
        memo = "本期技术路线从旧方法边界出发，解释新设计如何改变训练与推理接口。" * 16
        body = "技术部分继续分析问题、机制、证据和应用价值。" * 28
        draft = (
            "# AI 技术范式雷达\n\n## 本期研究 Memo\n\n"
            f"{memo}\n\n## 路线分析\n\n{body}\n\n"
            "## 接下来真正值得盯的信号\n\n观察独立复现。"
        )
        without_index = _editorial_violations(draft, [first, second])
        self.assertIn("缺少确定性的原文与一手资料索引", without_index)

        report = _attach_primary_source_index(draft, [first, second])
        self.assertIn("## 原文与一手资料", report)
        self.assertIn("https://arxiv.org/abs/2607.00001", report)
        self.assertIn("https://arxiv.org/abs/2607.00002", report)
        self.assertLess(
            report.index("## 原文与一手资料"),
            report.index("## 接下来真正值得盯的信号"),
        )
        self.assertFalse(
            any("一手材料" in value or "原文" in value for value in _editorial_violations(report, [first, second]))
        )

    def test_route_gate_rejects_extra_plausible_but_ungrounded_url(self) -> None:
        item = candidate()
        body = "这条路线从旧系统的能力边界出发，解释训练信号与推理接口如何变化。" * 24
        draft = (
            "### 从状态预测走向可行动表示\n\n"
            f"{body}\n\n"
            "[论文原文](https://arxiv.org/abs/2607.00001)\n\n"
            "[看似合理但不存在的官方页面](https://deepseek.example/nonexistent)\n\n"
            "**讨论势能判断：** 已出现少量独立讨论，但仍需等待复现。"
        )

        violations = _route_draft_violations(draft, item)

        self.assertTrue(
            any("未由证据或人物档案提供的链接" in value for value in violations),
            violations,
        )

    def test_historical_update_route_must_link_current_uptake_evidence(self) -> None:
        item = candidate()
        uptake_url = "https://example.net/current-independent-analysis"
        item.evidence.append(
            TechnicalEvidence(
                source="independent-analysis",
                evidence_type=EvidenceType.SECONDARY_INTERPRETATION,
                title="Current independent analysis",
                url=uptake_url,
                raw={
                    "relationship": "independent_mechanism_analysis",
                    "independence": "independent",
                    "substantive_uptake": True,
                    "substantive_uptake_source": "synthesis-v1",
                    "substantive_uptake_route_key": item.key,
                },
            )
        )
        item.freshness_assessment = {
            "classification": "historical_reactivated",
            "decision": "include",
            "current_uptake_urls": [uptake_url],
        }
        body = "这条路线解释训练信号、推理接口与能力边界之间的因果关系。" * 28
        without_uptake = (
            "### 一条重新进入观察的技术路线\n\n"
            f"{body}\n\n"
            "[原始论文](https://arxiv.org/abs/2607.00001)\n\n"
            "**讨论势能判断：** 本期存在独立分析，但仍未形成复现。"
        )
        self.assertTrue(
            any(
                "本期实质承接证据" in value
                for value in _route_draft_violations(without_uptake, item)
            )
        )

        with_uptake = without_uptake.replace(
            "**讨论势能判断：**",
            f"[本期独立分析]({uptake_url})。\n\n**讨论势能判断：**",
        )
        self.assertFalse(
            any(
                "本期实质承接证据" in value
                for value in _route_draft_violations(with_uptake, item)
            )
        )

    def test_url_grounding_preserves_case_sensitive_path(self) -> None:
        item = candidate()
        item.evidence[0].url = "https://research.example/Reports/ModelV1"
        body = "这条路线解释训练信号、推理接口与能力边界之间的因果关系。" * 28
        draft = (
            "### 一条可复核的技术路线\n\n"
            f"{body}\n\n"
            "[原文](https://research.example/reports/modelv1)\n\n"
            "**讨论势能判断：** 当前只有有限的独立承接，仍需继续观察。"
        )

        violations = _route_draft_violations(draft, item)

        self.assertTrue(
            any("未由证据或人物档案提供的链接" in value for value in violations),
            violations,
        )

    def test_missing_primary_url_stops_report_even_when_model_draft_is_valid(self) -> None:
        item = candidate(
            [
                TechnicalEvidence(
                    source="arxiv",
                    evidence_type=EvidenceType.PRIMARY_PAPER,
                    title="Missing URL paper",
                    url="",
                )
            ]
        )
        item.researchers = [verified_researcher()]
        memo = "本期从旧系统的能力边界出发，解释技术设计如何改变训练与推理。" * 18
        body = "这条路线的背景、机制、客观证据与应用边界需要放在一起理解。" * 26
        editorial = (
            "# AI 技术范式雷达\n\n## 本期研究 Memo\n\n"
            f"{memo}\n\n## **技术路线**改变能力边界\n\n"
            f"{body} **关键机制**需要复现。A. Researcher 是关键作者。\n\n"
            "## 接下来真正值得盯的信号\n\n观察独立承接。"
        )
        response = SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content=editorial))]
        )
        client = SimpleNamespace(
            chat=SimpleNamespace(
                completions=SimpleNamespace(create=AsyncMock(return_value=response))
            )
        )
        with tempfile.TemporaryDirectory() as directory:
            generator = ParadigmReportGenerator(directory, client=client, model="test")
            with self.assertRaises(RuntimeError):
                asyncio.run(generator.generate([item], {**COMPLETED_RESEARCH, "origin_count": 1}))
            self.assertEqual(list(Path(directory).glob("*.md")), [])

    def test_presentation_target_is_advisory_without_full_rewrite(self) -> None:
        memo = "本期先建立低分辨率运行图，再沿关键接口逐步提高理解分辨率。" * 11
        report = (
            "# AI 技术范式雷达\n\n## 本期研究 Memo\n\n"
            f"{memo}\n\n## 路线分析\n\n" + "这条路线解释技术机制与边界。" * 50
            + "A. Researcher 是这条路线的关键研究者。"
            + "\n\n**讨论势能判断：** 当前证据不足，尚不能判断形成独立承接。"
            + "\n\n## 接下来真正值得盯的信号\n\n观察独立复现。"
        )
        self.assertTrue(_editorial_advisories(report))
        self.assertFalse(
            any("行内重点强调" in value for value in _editorial_violations(report, [], require_primary_sources=False))
        )
        route = (
            "### 路线分析\n\n"
            + "这条路线解释技术机制与边界。" * 50
            + "A. Researcher 是这条路线的关键研究者。"
            + "[查看原文](https://arxiv.org/abs/2607.00001)。"
            + "\n\n**讨论势能判断：** 当前证据不足，尚不能判断形成独立承接。"
        )
        frame = (
            "# AI 技术范式雷达\n\n## 本期研究 Memo\n\n"
            f"{memo}\n\n## 接下来真正值得盯的信号\n\n观察独立复现。"
        )
        responses = [
            SimpleNamespace(
                choices=[SimpleNamespace(message=SimpleNamespace(content=value))]
            )
            for value in (route, frame)
        ]
        client = SimpleNamespace(
            chat=SimpleNamespace(
                completions=SimpleNamespace(create=AsyncMock(side_effect=responses))
            )
        )
        item = candidate()
        item.researchers = [verified_researcher()]
        with tempfile.TemporaryDirectory() as directory:
            path = asyncio.run(
                ParadigmReportGenerator(
                    directory, client=client, model="test"
                ).generate([item], {**COMPLETED_RESEARCH, "origin_count": 1})
            )
            content = path.read_text(encoding="utf-8")
        self.assertEqual(client.chat.completions.create.await_count, 2)
        self.assertIn("## 原文与一手资料", content)

    def test_report_generation_fails_instead_of_sending_raw_fallback(self) -> None:
        item = candidate()
        item.researchers = [verified_researcher()]
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
                asyncio.run(generator.generate([item], {**COMPLETED_RESEARCH, "origin_count": 1}))
            self.assertEqual(list(Path(directory).glob("*.md")), [])
        self.assertEqual(client.chat.completions.create.await_count, 2)

    def test_empty_report_rejects_failed_recall_lane_and_official_page(self) -> None:
        for frontier in (
            {"recall_lanes": {"priority_researchers_1": {"status": "query_failed"}}},
            {"academic_indexes": {"openreview": {"status": "partial"}}},
            {"official_pages": {"total_pages": 2, "checked_pages": 2, "request_failed": 1}},
        ):
            with self.subTest(frontier=frontier), self.assertRaises(ResearchNotCompleteError):
                ParadigmReportGenerator._empty_report(
                    "2026-07-28", {**COMPLETED_RESEARCH, "frontier_coverage": frontier}
                )
    def test_discovery_assigns_long_window_only_to_high_signal_lanes(self) -> None:
        discovery = ParadigmDiscovery(
            broad_lookback_days=7,
            high_signal_lookback_days=60,
        )

        self.assertEqual(discovery.arxiv.lookback_days, 7)
        self.assertEqual(discovery.arxiv.high_signal_lookback_days, 60)
        self.assertEqual(discovery.hf.lookback_days, 7)
        self.assertEqual(discovery.openalex.lookback_days, 7)
        self.assertEqual(discovery.openreview.lookback_days, 7)
        self.assertEqual(discovery.priority_pages.lookback_days, 60)
        self.assertEqual(discovery.evidence_sources[2].lookback_days, 60)

    def test_database_reset_does_not_expand_ordinary_discovery_window(self) -> None:
        store = MagicMock()
        store.is_bootstrap_required.return_value = True
        with (
            patch(
                "agents.paradigm_orchestrator.ParadigmStore",
                return_value=store,
            ),
            patch(
                "agents.paradigm_orchestrator.ParadigmDiscovery"
            ) as discovery_class,
            patch("agents.paradigm_orchestrator.ParadigmAnalyzer"),
            patch("agents.paradigm_orchestrator.EvidenceEnricher"),
            patch("agents.paradigm_orchestrator.ParadigmSynthesizer"),
            patch("agents.paradigm_orchestrator.ResearcherTrajectoryAnalyzer"),
            patch.object(config, "SOURCING_LOOKBACK_DAYS", 7),
            patch.object(config, "PARADIGM_BOOTSTRAP_LOOKBACK_DAYS", 60),
        ):
            orchestrator = ParadigmOrchestrator()

        self.assertTrue(orchestrator.bootstrap_mode)
        self.assertEqual(
            discovery_class.call_args.kwargs,
            {
                "broad_lookback_days": 7,
                "high_signal_lookback_days": 60,
            },
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
            origin_kind="technical_report",
            frontier_domains=["world_spatial_models"],
            publisher_context={"organization": "Lab"},
            rubric_definition="{}",
        )
        self.assertIn('"hypotheses"', prompt)
        self.assertIn('"canonical_name"', prompt)
        self.assertIn('"route_family"', prompt)
        self.assertIn('"design_philosophy"', prompt)
        self.assertIn('"rubric_answers"', prompt)
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
            mental_model="{}",
            innovation_types='["architecture"]',
            screening_rubric="{}",
            rubric_definition="{}",
            mental_model_method="先建立低分辨率运行图，再逐层提高分辨率。",
            lineage_parent="video prediction",
            evidence="[]",
        )
        self.assertIn('"trend_interpretation"', synthesis)
        self.assertIn('"excluded_evidence_indices"', synthesis)
        self.assertIn('"mental_model"', synthesis)
        self.assertIn('"observation_axis"', synthesis)
        self.assertIn('"resolution_ladder"', synthesis)
        self.assertIn('"rubric_answers"', synthesis)
        self.assertIn('证据列表：[]', synthesis)
        self.assertIn("先建立低分辨率运行图", synthesis)

        editorial = SkillLoader().render(
            "weekly_research_memo",
            date="2026-07-18",
            lookback_days=7,
            stats="{}",
            candidate_dossiers="[]",
            mental_model_method="先建立低分辨率运行图，再逐层提高分辨率。",
        )
        self.assertIn("约 450 到 650 个中文字", editorial)
        self.assertIn("不展示总分", editorial)
        self.assertIn("最小实例", editorial)
        self.assertIn("训练过程与推理过程必须分开", editorial)
        self.assertIn("先建立低分辨率运行图", editorial)
        self.assertIn("primary_sources", editorial)
        self.assertIn("完整且原样的 URL", editorial)

        revision = SkillLoader().render(
            "weekly_memo_revision",
            date="2026-07-18",
            lookback_days=7,
            violations="出现英文原文",
            candidate_dossiers="[]",
            previous_draft="draft",
            mental_model_method="先建立低分辨率运行图，再逐层提高分辨率。",
        )
        self.assertIn("严禁复制英文摘要", revision)
        self.assertIn("primary_sources", revision)
        self.assertIn("不得凭记忆改写或猜测任何 URL", revision)

    def test_skill_loader_keeps_embedded_json_valid(self) -> None:
        prompt = SkillLoader().render(
            "weekly_research_memo",
            date="2026-07-18",
            lookback_days=7,
            stats='{"origin_count": 3}',
            candidate_dossiers='[{"name": "route"}]',
            mental_model_method="低分辨率方法",
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
        <a href="https://lab.example/blog/project-atlas">2026-07-14 Project Atlas</a>
        <a href="/research">Research</a>
        """
        links = _discover_index_links(html, "https://lab.example/")
        self.assertEqual(len(links), 1)
        self.assertEqual(links[0].title, "Project Atlas")
        self.assertEqual(links[0].published_at, "2026-07-14")

    def test_priority_page_recovers_card_date_from_hydration_data(self) -> None:
        html = r"""
        <a href="/blog/frontier-model" aria-label="Frontier Model"></a>
        <script>
        self.__next_f.push("{\\"title\\":\\"Frontier Model\\",
        \\"href\\":\\"/blog/frontier-model\\",\\"date\\":\\"2026/07/27\\"}")
        </script>
        """
        links = _discover_index_links(html, "https://research.example/blog/")
        self.assertEqual(len(links), 1)
        self.assertEqual(links[0].title, "Frontier Model")
        self.assertEqual(links[0].published_at, "2026-07-27")

    def test_priority_page_discovers_unknown_brand_name_by_publication_structure(self) -> None:
        html = '<a href="/research/project-banyan">Project Banyan</a>'
        links = _discover_index_links(html, "https://lab.example/research/")
        self.assertEqual(len(links), 1)
        self.assertEqual(links[0].title, "Project Banyan")

    def test_priority_page_reads_jsonld_without_visible_anchor_or_keyword(self) -> None:
        html = """
        <script type="application/ld+json">
        {
          "@type": "ScholarlyArticle",
          "headline": "Project Cedar",
          "url": "/research/project-cedar",
          "datePublished": "2026-07-27"
        }
        </script>
        """
        links = _discover_index_links(html, "https://lab.example/research/")
        self.assertEqual(len(links), 1)
        self.assertEqual(links[0].title, "Project Cedar")
        self.assertEqual(links[0].published_at, "2026-07-27")

    def test_article_parser_exposes_linked_full_report_and_modified_date(self) -> None:
        parser = _ArticleParser("https://lab.example/news/project-cedar")
        parser.feed(
            """
            <meta property="article:published_time" content="2026-06-01">
            <meta property="article:modified_time" content="2026-07-27">
            <meta name="citation_pdf_url" content="/reports/cedar.pdf">
            <a href="/reports/cedar-system-card.pdf">Read the full report</a>
            """
        )
        self.assertEqual(parser.modified_at, "2026-07-27")
        self.assertIn(
            ("citation PDF", "https://lab.example/reports/cedar.pdf"),
            parser.links,
        )
        self.assertIn(
            (
                "Read the full report",
                "https://lab.example/reports/cedar-system-card.pdf",
            ),
            parser.links,
        )

    def test_priority_source_coverage_distinguishes_page_parse_failure(self) -> None:
        source = PriorityResearchPageSource(
            pages=["https://lab.example/research"],
        )
        source.page_coverage = {
            "https://lab.example/research": {
                "status": "parse_zero_links",
                "detail_failures": 0,
                "evidence": 0,
            }
        }
        coverage = source.coverage()
        self.assertEqual(coverage["parse_zero_links"], 1)
        self.assertEqual(coverage["request_failed"], 0)

    def test_priority_source_distinguishes_smoke_sampling_from_user_safety_limit(
        self,
    ) -> None:
        with patch.object(config, "PRIORITY_RESEARCH_LINK_SAFETY_LIMIT", 0):
            sampled = PriorityResearchPageSource(
                per_page=2,
                pages=["https://lab.example/research"],
            )
        with patch.object(config, "PRIORITY_RESEARCH_LINK_SAFETY_LIMIT", 3):
            configured = PriorityResearchPageSource(
                pages=["https://lab.example/research"],
            )

        self.assertEqual(sampled.limit_origin, "caller_sample")
        self.assertEqual(configured.limit_origin, "configured_safety_limit")

    def test_primary_material_uses_durable_url_not_signed_hf_redirect(self) -> None:
        evidence = TechnicalEvidence(
            source="priority-research-page",
            evidence_type=EvidenceType.TECHNICAL_BLOG,
            title="DeepSeek Technical Report",
            url=(
                "https://us.aws.cdn.hf.co/xet-bridge-us/file.pdf"
                "?Expires=1786289003&Signature=temporary"
            ),
            raw={
                "canonical_source_url": (
                    "https://huggingface.co/deepseek-ai/report/"
                    "blob/main/technical-report.pdf"
                )
            },
        )
        self.assertEqual(
            primary_material_url(evidence),
            "https://huggingface.co/deepseek-ai/report/blob/main/technical-report.pdf",
        )
        draft = (
            "# AI 技术范式雷达\n\n## 本期研究 Memo\n\n占位\n\n"
            "## 接下来真正值得盯的信号\n\n占位"
        )
        report_candidate = candidate([evidence])
        report = _attach_primary_source_index(draft, [report_candidate])
        self.assertIn("/blob/main/technical-report.pdf", report)
        self.assertNotIn("xet-bridge", report)
        self.assertNotIn("Expires=", report)
        evidence.raw = {}
        self.assertEqual(primary_material_url(evidence), "")

    def test_priority_source_never_promotes_modified_time_to_publish_time(self) -> None:
        today = datetime.now(timezone.utc).date().isoformat()
        index_url = "https://lab.example/research"

        async def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/research":
                return httpx.Response(
                    200,
                    text='<a href="/research/old-system">Old System Report</a>',
                    request=request,
                )
            return httpx.Response(
                200,
                text=(
                    '<meta property="article:published_time" content="2025-05-14">'
                    f'<meta property="article:modified_time" content="{today}">'
                    '<meta property="og:title" content="Old System Report">'
                    + "Architecture and training details. " * 20
                ),
                request=request,
            )

        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        source = PriorityResearchPageSource(lookback_days=30, pages=[index_url])
        with patch(
            "sources.priority_research_source.httpx.AsyncClient",
            return_value=client,
        ):
            received = asyncio.run(source.fetch())
        self.assertEqual(received, [])

    def test_dated_official_changelog_section_is_recalled_without_detail_link(self) -> None:
        today = datetime.now(timezone.utc).date().isoformat()
        page_url = "https://api-docs.deepseek.com/updates/"
        body = (
            f"<html><head><title>API Updates</title></head><body>"
            f"<h2>{today}</h2><h3>Project Redwood model update</h3>"
            + "We introduce a model release with architecture, inference, "
            "tool-use and benchmark details. " * 12
            + "</body></html>"
        )

        async def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, text=body, request=request)

        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        source = PriorityResearchPageSource(
            lookback_days=30,
            pages=[page_url],
        )
        with patch(
            "sources.priority_research_source.httpx.AsyncClient",
            return_value=client,
        ):
            received = asyncio.run(source.fetch())
        self.assertEqual(len(received), 1)
        self.assertEqual(received[0].url, page_url)
        self.assertTrue(received[0].published_at.startswith(today))
        self.assertIn("Project Redwood", received[0].summary)

    def test_freshness_distinguishes_current_release_from_old_reactivation(self) -> None:
        reference = datetime(2026, 8, 15, tzinfo=timezone.utc)
        old = candidate(
            [
                TechnicalEvidence(
                    source="official",
                    evidence_type=EvidenceType.TECHNICAL_BLOG,
                    title="AlphaEvolve",
                    url="https://deepmind.google/blog/alphaevolve/",
                    published_at="2025-05-14T00:00:00+00:00",
                )
            ]
        )
        stale = assess_candidate_freshness(
            old, reference_time=reference, window_days=30
        )
        self.assertEqual(stale["decision"], "defer")
        self.assertEqual(
            stale["classification"], "historical_without_current_uptake"
        )

        old.evidence.append(
            TechnicalEvidence(
                source="independent-lab",
                evidence_type=EvidenceType.PRODUCT_ADOPTION,
                title="Independent adoption",
                url="https://example.org/adoption",
                published_at="2026-08-12T00:00:00+00:00",
                raw={"independence": "independent"},
            )
        )
        reactivated = assess_candidate_freshness(
            old, reference_time=reference, window_days=30
        )
        self.assertEqual(reactivated["decision"], "include")
        self.assertEqual(
            reactivated["classification"], "historical_reactivated"
        )

        old.evidence[-1] = TechnicalEvidence(
            source="official-repository-release",
            evidence_type=EvidenceType.IMPLEMENTATION,
            title="Publisher repository",
            url="https://github.com/example/release",
            published_at="2026-08-12T00:00:00+00:00",
            raw={
                "relationship": "official_release_repository",
                "independence": "publisher",
            },
        )
        publisher_only = assess_candidate_freshness(
            old, reference_time=reference, window_days=30
        )
        self.assertEqual(publisher_only["decision"], "defer")
        self.assertEqual(
            publisher_only["classification"],
            "historical_without_current_uptake",
        )

    def test_generic_recent_discussion_cannot_reactivate_old_route(self) -> None:
        reference = datetime(2026, 8, 15, tzinfo=timezone.utc)
        item = candidate(
            [
                TechnicalEvidence(
                    source="official",
                    evidence_type=EvidenceType.TECHNICAL_BLOG,
                    title="Old mechanism",
                    url="https://example.org/old-mechanism",
                    published_at="2025-05-14T00:00:00+00:00",
                ),
                TechnicalEvidence(
                    source="curated-kol-blog",
                    evidence_type=EvidenceType.SECONDARY_INTERPRETATION,
                    title="A broad discussion of adjacent agents",
                    url="https://example.net/adjacent-discussion",
                    summary="A long but generic discussion of agent systems." * 20,
                    published_at="2026-08-12T00:00:00+00:00",
                    raw={
                        "relationship": "independent_commentary",
                        "independence": "independent",
                    },
                ),
            ]
        )

        assessment = assess_candidate_freshness(
            item, reference_time=reference, window_days=30
        )

        self.assertEqual(assessment["decision"], "defer")
        self.assertEqual(assessment["current_uptake_urls"], [])

    def test_audited_substantive_discussion_reactivates_and_exposes_source(self) -> None:
        reference = datetime(2026, 8, 15, tzinfo=timezone.utc)
        uptake_url = "https://example.net/mechanism-analysis"
        item = candidate(
            [
                TechnicalEvidence(
                    source="official",
                    evidence_type=EvidenceType.TECHNICAL_BLOG,
                    title="Old mechanism",
                    url="https://example.org/old-mechanism",
                    published_at="2025-05-14T00:00:00+00:00",
                ),
                TechnicalEvidence(
                    source="curated-kol-blog",
                    evidence_type=EvidenceType.SECONDARY_INTERPRETATION,
                    title="Direct mechanism analysis",
                    url=uptake_url,
                    summary="The analysis directly tests the mechanism boundary.",
                    published_at="2026-08-12T00:00:00+00:00",
                    raw={
                        "relationship": "independent_mechanism_analysis",
                        "independence": "independent",
                        "substantive_uptake": True,
                        "substantive_uptake_source": "synthesis-v1",
                        "substantive_uptake_route_key": "latent-action-world-models",
                    },
                ),
            ]
        )

        assessment = assess_candidate_freshness(
            item, reference_time=reference, window_days=30
        )

        self.assertEqual(assessment["decision"], "include")
        self.assertEqual(assessment["current_uptake_urls"], [uptake_url])
        self.assertEqual(
            assessment["current_uptake_evidence"][0]["qualification_reason"],
            "独立且直接关联本机制的实质讨论",
        )

    def test_small_counter_change_does_not_reactivate_old_route(self) -> None:
        reference = datetime(2026, 8, 15, tzinfo=timezone.utc)
        primary = TechnicalEvidence(
            source="arxiv",
            evidence_type=EvidenceType.PRIMARY_PAPER,
            title="Old paper",
            url="https://arxiv.org/abs/2501.00001",
            published_at="2025-01-02T00:00:00+00:00",
        )
        citation = TechnicalEvidence(
            source="semantic-scholar",
            evidence_type=EvidenceType.CITATION,
            title="Old paper",
            url="https://www.semanticscholar.org/paper/example",
            published_at="2025-01-02T00:00:00+00:00",
            raw={
                "metric_delta": {"citations": 1},
                "metric_delta_observed_at": "2026-08-14T00:00:00+00:00",
            },
        )
        item = candidate([primary, citation])

        small = assess_candidate_freshness(
            item, reference_time=reference, window_days=30
        )
        self.assertEqual(small["decision"], "defer")

        citation.raw["metric_delta"] = {"citations": 3}
        material = assess_candidate_freshness(
            item, reference_time=reference, window_days=30
        )
        self.assertEqual(material["decision"], "include")

        # A stored old delta is not a new event when another source changes.
        citation.raw["metric_delta_observed_at"] = "2026-07-01T00:00:00+00:00"
        stale_delta = assess_candidate_freshness(
            item, reference_time=reference, window_days=30
        )
        self.assertEqual(stale_delta["decision"], "defer")
        citation.raw.pop("metric_delta_observed_at")
        legacy_delta = assess_candidate_freshness(
            item, reference_time=reference, window_days=30
        )
        self.assertEqual(legacy_delta["decision"], "defer")

    def test_official_repository_requires_external_primary_material(self) -> None:
        now = datetime.now(timezone.utc).isoformat()
        repositories = [
            {
                "name": "deepseek-harness",
                "full_name": "deepseek-ai/deepseek-harness",
                "html_url": "https://github.com/deepseek-ai/deepseek-harness",
                "homepage": "https://www.deepseek.com/harness/",
                "description": "A composable agent harness",
                "created_at": now,
                "fork": False,
                "archived": False,
                "stargazers_count": 12000,
                "forks_count": 900,
                "topics": ["agents"],
            }
        ]

        async def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path.endswith("/repos"):
                return httpx.Response(200, json=repositories, request=request)
            return httpx.Response(
                200,
                text=(
                    "DeepSeek Harness introduces a plugin architecture and "
                    "training-time composability. https://www.deepseek.com/harness/ "
                )
                * 10,
                request=request,
            )

        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        source = OfficialRepositoryReleaseSource(
            lookback_days=30,
            organizations=[{"login": "deepseek-ai", "owner": "deepseek"}],
        )
        with (
            patch.object(config, "GITHUB_TOKEN", "configured"),
            patch(
                "sources.official_repository_release_source.httpx.AsyncClient",
                return_value=client,
            ),
        ):
            evidence = asyncio.run(source.fetch())

        origins = [
            item
            for item in evidence
            if item.evidence_type in {
                EvidenceType.PRIMARY_PAPER,
                EvidenceType.TECHNICAL_BLOG,
            }
        ]
        supporting = [
            item
            for item in evidence
            if item.evidence_type == EvidenceType.IMPLEMENTATION
        ]
        self.assertEqual([item.url for item in origins], ["https://www.deepseek.com/harness/"])
        self.assertEqual(len(supporting), 1)
        self.assertNotEqual(origins[0].url, supporting[0].url)
        self.assertEqual(origins[0].published_at, "")
        self.assertEqual(origins[0].raw["release_event_at"], now)
        self.assertEqual(
            origins[0].raw["date_basis"], "external_primary_date_unknown"
        )

    def test_official_repository_uses_external_material_date_not_repo_date(self) -> None:
        repository_created = "2026-08-14T10:00:00+00:00"
        repository = {
            "name": "old-paper-release",
            "full_name": "deepseek-ai/old-paper-release",
            "html_url": "https://github.com/deepseek-ai/old-paper-release",
            "homepage": "https://www.deepseek.com/research/old-paper",
            "created_at": repository_created,
            "fork": False,
            "archived": False,
        }

        async def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path.endswith("/repos"):
                return httpx.Response(200, json=[repository], request=request)
            if request.url.path.endswith("/readme"):
                return httpx.Response(
                    200,
                    text="See https://www.deepseek.com/research/old-paper",
                    request=request,
                )
            return httpx.Response(
                200,
                text=(
                    '<meta property="article:published_time" '
                    'content="2025-05-14T00:00:00Z">'
                    + "Architecture and evaluation details. " * 20
                ),
                request=request,
            )

        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        source = OfficialRepositoryReleaseSource(
            organizations=[{"login": "deepseek-ai", "owner": "deepseek"}],
            reference_time=datetime(2026, 8, 15, tzinfo=timezone.utc),
        )
        with (
            patch.object(config, "GITHUB_TOKEN", "configured"),
            patch(
                "sources.official_repository_release_source.httpx.AsyncClient",
                return_value=client,
            ),
        ):
            evidence = asyncio.run(source.fetch())
        origin = next(
            item
            for item in evidence
            if item.evidence_type == EvidenceType.TECHNICAL_BLOG
        )
        self.assertEqual(origin.published_at, "2025-05-14T00:00:00+00:00")
        self.assertEqual(origin.raw["release_event_at"], repository_created)

    def test_repository_only_release_stays_supporting_and_auditable(self) -> None:
        now = datetime.now(timezone.utc).isoformat()
        repository = {
            "name": "repository-only",
            "full_name": "deepseek-ai/repository-only",
            "html_url": "https://github.com/deepseek-ai/repository-only",
            "homepage": "",
            "created_at": now,
            "fork": False,
            "archived": False,
        }

        async def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path.endswith("/repos"):
                return httpx.Response(200, json=[repository], request=request)
            return httpx.Response(200, text="Architecture details only.", request=request)

        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        source = OfficialRepositoryReleaseSource(
            organizations=[{"login": "deepseek-ai", "owner": "deepseek"}]
        )
        with (
            patch.object(config, "GITHUB_TOKEN", "configured"),
            patch(
                "sources.official_repository_release_source.httpx.AsyncClient",
                return_value=client,
            ),
        ):
            evidence = asyncio.run(source.fetch())
        self.assertEqual(
            [item.evidence_type for item in evidence],
            [EvidenceType.IMPLEMENTATION],
        )
        self.assertEqual(
            source.coverage()["repository_only_releases"],
            ["deepseek-ai/repository-only"],
        )

    def test_minor_official_repository_watchlist_failure_is_audited_not_fatal(self) -> None:
        source = OfficialRepositoryReleaseSource(
            organizations=[
                {"login": f"organization-{index}", "owner": "deepseek"}
                for index in range(10)
            ]
        )

        async def fake_fetch():
            source._coverage = {
                "configured_organizations": 10,
                "checked_organizations": 9,
                "failed_organizations": ["organization-9:HTTPStatusError"],
            }
            return []

        source.fetch = AsyncMock(side_effect=fake_fetch)
        with patch.object(config, "GITHUB_TOKEN", "configured"):
            self.assertEqual(asyncio.run(source.safe_fetch()), [])
        self.assertEqual(source.fetch_status, "completed_with_warnings")
        self.assertEqual(source.fetch_error, "MinorOrganizationFailures")

    def test_official_repository_pages_until_it_reaches_window_boundary(self) -> None:
        now = datetime.now(timezone.utc).isoformat()
        first_page = [
            {
                "name": f"repo-{index}",
                "full_name": f"deepseek-ai/repo-{index}",
                "html_url": f"https://github.com/deepseek-ai/repo-{index}",
                "created_at": now,
                # Forks do not become evidence, but still occupy the GitHub
                # organization listing and therefore exercise pagination.
                "fork": True,
                "archived": False,
            }
            for index in range(100)
        ]
        requested_pages: list[int] = []

        async def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path.endswith("/repos"):
                page = int(request.url.params.get("page", "1"))
                requested_pages.append(page)
                return httpx.Response(
                    200,
                    json=first_page if page == 1 else [],
                    request=request,
                )
            raise AssertionError(f"unexpected request: {request.url}")

        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        source = OfficialRepositoryReleaseSource(
            organizations=[{"login": "deepseek-ai", "owner": "deepseek"}]
        )
        with (
            patch.object(config, "GITHUB_TOKEN", "configured"),
            patch(
                "sources.official_repository_release_source.httpx.AsyncClient",
                return_value=client,
            ),
        ):
            evidence = asyncio.run(source.fetch())
        self.assertEqual(evidence, [])
        self.assertEqual(requested_pages, [1, 2])
        self.assertEqual(
            source.coverage()["repository_pages_by_organization"],
            {"deepseek-ai": 2},
        )

    def test_official_repository_partial_pagination_is_not_a_zero_hit(self) -> None:
        source = OfficialRepositoryReleaseSource(
            organizations=[
                {"login": f"organization-{index}", "owner": "deepseek"}
                for index in range(10)
            ]
        )

        async def fake_fetch():
            source._coverage = {
                "configured_organizations": 10,
                "checked_organizations": 10,
                "failed_organizations": [],
                "repository_page_failures": [
                    "organization-0:page_2:HTTPStatusError"
                ],
                "external_primary_targets": 0,
                "external_primary_validation_failures": 0,
            }
            return []

        source.fetch = AsyncMock(side_effect=fake_fetch)
        with patch.object(config, "GITHUB_TOKEN", "configured"):
            self.assertEqual(asyncio.run(source.safe_fetch()), [])
        self.assertEqual(source.fetch_status, "partial")
        self.assertEqual(
            source.fetch_error, "MaterialRepositoryPaginationLoss"
        )

    def test_material_official_primary_validation_loss_marks_source_partial(self) -> None:
        source = OfficialRepositoryReleaseSource(
            organizations=[{"login": "deepseek-ai", "owner": "deepseek"}]
        )

        async def fake_fetch():
            source._coverage = {
                "configured_organizations": 1,
                "checked_organizations": 1,
                "failed_organizations": [],
                "external_primary_targets": 1,
                "external_primary_validation_failures": 1,
            }
            return []

        source.fetch = AsyncMock(side_effect=fake_fetch)
        with patch.object(config, "GITHUB_TOKEN", "configured"):
            self.assertEqual(asyncio.run(source.safe_fetch()), [])
        self.assertEqual(source.fetch_status, "partial")
        self.assertEqual(source.fetch_error, "MaterialPrimaryValidationLoss")

    def test_repository_readme_does_not_pick_an_arbitrary_cited_paper(self) -> None:
        readme = (
            "Related work: https://arxiv.org/abs/2501.00001 and "
            "https://arxiv.org/abs/2502.00002"
        )
        self.assertEqual(_linked_primary_material("deepseek", readme, ""), "")
        labeled = (
            readme
            + "\n[Technical report](https://arxiv.org/abs/2608.00003)"
        )
        self.assertEqual(
            _linked_primary_material("deepseek", labeled, ""),
            "https://arxiv.org/abs/2608.00003",
        )

    def test_repository_primary_date_prefers_exact_metadata_over_arxiv_month(self) -> None:
        self.assertEqual(
            _primary_publication_date(
                "https://arxiv.org/abs/2608.00003",
                '<meta name="citation_date" content="2026-08-27">',
            ),
            "2026-08-27T00:00:00+00:00",
        )

    def test_arxiv_report_query_failure_does_not_drop_regular_feed(self) -> None:
        source = ArxivSource(max_results=5, lookback_days=7)
        async def fetch_query(*args, **kwargs):
            if kwargs.get("force_technical_report"):
                raise httpx.ConnectError("report query unavailable")
            return []

        source._fetch_query = AsyncMock(side_effect=fetch_query)
        with patch.object(config, "PARADIGM_PRIORITY_AUTHOR_SWEEP_ENABLED", False):
            self.assertEqual(asyncio.run(source.fetch()), [])
        self.assertIn("technical_documents", source.failed_recall_lanes)
        self.assertTrue(source.executed_query_groups)

    def test_arxiv_recall_lanes_apply_their_own_windows(self) -> None:
        source = ArxivSource(
            max_results=None,
            lookback_days=7,
            high_signal_lookback_days=60,
            seed_arxiv_ids=[],
        )
        source._fetch_query = AsyncMock(return_value=[])
        with patch.object(config, "PARADIGM_PRIORITY_AUTHOR_SWEEP_ENABLED", False):
            asyncio.run(source.fetch())

        calls = source._fetch_query.await_args_list
        self.assertEqual(calls[0].kwargs["query_group"], "technical_reports")
        self.assertEqual(calls[0].kwargs["lookback_days"], 60)
        self.assertTrue(
            all(call.kwargs["lookback_days"] == 7 for call in calls[1:])
        )
        coverage = source.recall_coverage()
        self.assertEqual(
            coverage["technical_documents"]["lookback_days"],
            60,
        )
        landscape_lane = next(
            name for name in coverage if name.startswith("landscape:")
        )
        self.assertEqual(coverage[landscape_lane]["lookback_days"], 7)

    def test_arxiv_exact_seed_runs_before_broad_recall_and_survives_failures(
        self,
    ) -> None:
        source = ArxivSource(
            max_results=None,
            lookback_days=7,
            seed_arxiv_ids=["2601.00002"],
        )
        seeded = RawProject(
            source="arxiv",
            name="Seeded report",
            url="https://arxiv.org/abs/2601.00002",
            created_at="2026-07-29T00:00:00Z",
            extra={"origin_priority": 3},
        )
        events = []

        async def fetch_seed(*args, **kwargs):
            events.append("seed")
            return [seeded]

        async def fail_query(*args, **kwargs):
            events.append(
                "report" if kwargs.get("force_technical_report") else "other"
            )
            raise httpx.ConnectError("other recall unavailable")

        source._fetch_seed_ids = AsyncMock(side_effect=fetch_seed)
        source._fetch_query = AsyncMock(side_effect=fail_query)
        with patch.object(config, "PARADIGM_PRIORITY_AUTHOR_SWEEP_ENABLED", False):
            received = asyncio.run(source.fetch())

        self.assertEqual(received, [seeded])
        self.assertEqual(events[0], "seed")
        self.assertIn("explicit_seeds", source.executed_recall_lanes)

    def test_arxiv_shared_rate_limit_opens_circuit_after_one_retry(self) -> None:
        source = ArxivSource(max_results=None, lookback_days=7)
        request = httpx.Request("GET", "https://export.arxiv.org/api/query")
        client = SimpleNamespace(
            get=AsyncMock(
                side_effect=[
                    httpx.Response(429, request=request),
                    httpx.Response(429, request=request),
                ]
            )
        )
        with (
            patch(
                "sources.arxiv_source.asyncio.sleep",
                new=AsyncMock(),
            ),
            self.assertRaises(httpx.HTTPStatusError),
        ):
            asyncio.run(source._request(client, "all:model", 1))

        self.assertEqual(client.get.await_count, 2)
        self.assertEqual(source.request_count, 2)
        self.assertEqual(source.rate_limited_requests, 2)
        self.assertTrue(source._circuit_open)
        self.assertEqual(source.circuit_reason, "rate_limited")

    def test_arxiv_circuit_marks_remaining_lanes_not_executed(self) -> None:
        source = ArxivSource(
            max_results=None,
            lookback_days=7,
            seed_arxiv_ids=[],
        )

        async def rate_limited(*args, **kwargs):
            source._circuit_open = True
            source.circuit_reason = "rate_limited"
            raise httpx.HTTPStatusError(
                "rate limited",
                request=httpx.Request(
                    "GET", "https://export.arxiv.org/api/query"
                ),
                response=httpx.Response(
                    429,
                    request=httpx.Request(
                        "GET", "https://export.arxiv.org/api/query"
                    ),
                ),
            )

        source._fetch_query = AsyncMock(side_effect=rate_limited)
        received = asyncio.run(source.fetch())
        coverage = source.recall_coverage()

        self.assertEqual(received, [])
        self.assertEqual(source._fetch_query.await_count, 1)
        self.assertEqual(source.coverage()["status"], "rate_limited")
        self.assertEqual(
            coverage["technical_documents"]["status"],
            "query_failed",
        )
        self.assertTrue(
            any(
                value["status"] == "not_executed_rate_limited"
                for value in coverage.values()
            )
        )

    def test_arxiv_transport_timeout_retries_once(self) -> None:
        source = ArxivSource(max_results=5, lookback_days=7)
        request = httpx.Request("GET", "https://export.arxiv.org/api/query")
        response = httpx.Response(200, text="<feed/>", request=request)
        client = SimpleNamespace(
            get=AsyncMock(
                side_effect=[
                    httpx.ReadTimeout("temporary timeout", request=request),
                    response,
                ]
            )
        )
        with patch(
            "sources.arxiv_source.asyncio.sleep",
            new=AsyncMock(),
        ):
            received = asyncio.run(source._request(client, "all:model", 1))
        self.assertEqual(received.status_code, 200)
        self.assertEqual(client.get.await_count, 2)

    def test_large_system_report_author_prompt_is_bounded(self) -> None:
        authors = [
            "Atlas Research Team",
            *[f"Researcher {index}" for index in range(400)],
        ]
        summary = _author_prompt_summary(authors)
        self.assertIn("共 401 位作者", summary)
        self.assertIn("Atlas Research Team", summary)
        self.assertLess(len(summary), 500)

    def test_priority_arxiv_html_404_falls_back_to_official_pdf(self) -> None:
        evidence = paper()
        evidence.raw = {
            "origin_kind": "technical_report",
            "origin_priority": 3,
        }
        html_request = httpx.Request(
            "GET",
            "https://arxiv.org/html/2607.00001",
        )
        pdf_request = httpx.Request(
            "GET",
            "https://arxiv.org/pdf/2607.00001",
        )
        transport = SimpleNamespace(
            get=AsyncMock(
                side_effect=[
                    httpx.Response(404, request=html_request),
                    httpx.Response(200, content=b"%PDF-mock", request=pdf_request),
                ]
            )
        )
        context = MagicMock()
        context.__aenter__ = AsyncMock(return_value=transport)
        context.__aexit__ = AsyncMock(return_value=False)
        with (
            patch(
                "sources.arxiv_document_source.httpx.AsyncClient",
                return_value=context,
            ),
            patch(
                "sources.arxiv_document_source.parse_arxiv_pdf",
                return_value="系统报告正文，包含架构、训练与部署机制。",
            ),
        ):
            coverage = asyncio.run(ArxivDocumentClient().hydrate(evidence))
        self.assertEqual(
            evidence.raw["document_source_kind"],
            "arxiv_pdf_fallback",
        )
        self.assertIn("系统报告正文", evidence.raw["document_excerpt"])
        self.assertIn("官方 PDF", coverage["primary_document"])

    def test_official_article_linked_pdf_hydrates_before_screening(self) -> None:
        evidence = TechnicalEvidence(
            source="priority-research-page",
            evidence_type=EvidenceType.TECHNICAL_BLOG,
            title="Project Cedar",
            url="https://lab.example/research/project-cedar",
            summary="官方发布页。",
            raw={
                "origin_kind": "technical_report",
                "origin_priority": 3,
                "linked_research_documents": [
                    {
                        "title": "Full report",
                        "url": "https://lab.example/reports/cedar.pdf",
                    }
                ],
            },
        )
        request = httpx.Request(
            "GET",
            "https://lab.example/reports/cedar.pdf",
        )
        transport = SimpleNamespace(
            get=AsyncMock(
                return_value=httpx.Response(
                    200,
                    content=b"%PDF-mock",
                    headers={"content-type": "application/pdf"},
                    request=request,
                )
            )
        )
        context = MagicMock()
        context.__aenter__ = AsyncMock(return_value=transport)
        context.__aexit__ = AsyncMock(return_value=False)
        with (
            patch(
                "sources.arxiv_document_source.httpx.AsyncClient",
                return_value=context,
            ),
            patch(
                "sources.arxiv_document_source.parse_arxiv_pdf",
                return_value="完整系统报告正文，包含架构、训练与部署。",
            ),
        ):
            coverage = asyncio.run(ArxivDocumentClient().hydrate(evidence))
        self.assertEqual(
            evidence.raw["document_source_kind"],
            "official_linked_pdf",
        )
        self.assertIn("完整系统报告正文", evidence.raw["document_excerpt"])
        self.assertIn("官方发布页链接", coverage["primary_document"])

    def test_linked_brand_pdf_can_upgrade_release_after_full_text_is_read(self) -> None:
        evidence = TechnicalEvidence(
            source="priority-research-page",
            evidence_type=EvidenceType.TECHNICAL_BLOG,
            title="Project Cedar",
            url="https://lab.example/research/project-cedar",
            summary="We release Project Cedar.",
            authors=["Cedar Team"],
            raw={
                "origin_kind": "official_model_release",
                "origin_priority": 2,
                "linked_research_documents": [
                    {
                        "title": "Download PDF",
                        "url": "https://lab.example/files/cedar.pdf",
                    }
                ],
            },
        )
        request = httpx.Request("GET", "https://lab.example/files/cedar.pdf")
        transport = SimpleNamespace(
            get=AsyncMock(
                return_value=httpx.Response(
                    200,
                    content=b"%PDF-mock",
                    headers={"content-type": "application/pdf"},
                    request=request,
                )
            )
        )
        context = MagicMock()
        context.__aenter__ = AsyncMock(return_value=transport)
        context.__aexit__ = AsyncMock(return_value=False)
        report_text = (
            "We release a foundation model with model weights. "
            "Architecture attention experts. Pretraining training data. "
            "Post-training reinforcement learning. Infrastructure deployment serving. "
            "Evaluation benchmark capability. Parameter context length. "
        ) * 30
        with (
            patch(
                "sources.arxiv_document_source.httpx.AsyncClient",
                return_value=context,
            ),
            patch(
                "sources.arxiv_document_source.parse_arxiv_pdf",
                return_value=report_text,
            ),
        ):
            asyncio.run(ArxivDocumentClient().hydrate(evidence))
        self.assertEqual(evidence.raw["origin_kind"], "technical_report")
        self.assertEqual(evidence.raw["origin_priority"], 3)
        self.assertEqual(
            evidence.raw["origin_classification_reason"],
            "official_document_with_system_scope",
        )

    def test_long_document_excerpt_samples_late_sections(self) -> None:
        text = "HEAD " * 3000 + "MIDDLE " * 3000 + "TAIL_MECHANISM " * 1000
        excerpt = _distributed_text_excerpt(text, limit=12_000)
        self.assertLessEqual(len(excerpt), 12_000)
        self.assertIn("HEAD", excerpt)
        self.assertIn("TAIL_MECHANISM", excerpt)

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

    def test_semantic_scholar_without_approved_key_never_opens_client(self) -> None:
        with (
            patch.object(config, "SEMANTIC_SCHOLAR_ENABLED", False),
            patch.object(config, "SEMANTIC_SCHOLAR_API_KEY", ""),
            patch("sources.semantic_scholar_source.httpx.AsyncClient") as client_cls,
        ):
            result = asyncio.run(SemanticScholarClient().enrich_paper(paper()))
        self.assertEqual(result, (None, []))
        client_cls.assert_not_called()

    def test_openalex_orders_search_by_relevance_before_date(self) -> None:
        response = httpx.Response(
            200,
            json={"results": []},
            request=httpx.Request("GET", "https://api.openalex.org/works"),
        )
        transport = MagicMock()
        transport.get = AsyncMock(return_value=response)
        context = MagicMock()
        context.__aenter__ = AsyncMock(return_value=transport)
        context.__aexit__ = AsyncMock(return_value=False)
        with (
            patch.object(config, "OPENALEX_API_KEY", "configured"),
            patch("sources.openalex_source.httpx.AsyncClient", return_value=context),
        ):
            asyncio.run(
                OpenAlexSource(searches=['"world model"'], per_query=2).fetch()
            )
        params = transport.get.await_args.kwargs["params"]
        self.assertEqual(
            params["sort"], "relevance_score:desc,publication_date:desc"
        )
        self.assertEqual(params["search"], '"world model"')

    def test_openalex_stops_after_relevance_is_exhausted_not_fixed_top_k(self) -> None:
        def page(cursor: str) -> httpx.Response:
            return httpx.Response(
                200,
                json={
                    "results": [
                        {
                            "display_name": "Unrelated horticulture paper",
                            "primary_topic": {"display_name": "Botany"},
                        }
                    ],
                    "meta": {"next_cursor": cursor},
                },
                request=httpx.Request("GET", "https://api.openalex.org/works"),
            )

        transport = MagicMock()
        transport.get = AsyncMock(side_effect=[page("cursor-1"), page("cursor-2")])
        context = MagicMock()
        context.__aenter__ = AsyncMock(return_value=transport)
        context.__aexit__ = AsyncMock(return_value=False)
        with (
            patch.object(config, "OPENALEX_API_KEY", "configured"),
            patch("sources.openalex_source.httpx.AsyncClient", return_value=context),
        ):
            result = asyncio.run(
                OpenAlexSource(searches=['"world model"'], per_query=1).fetch()
            )
        self.assertEqual(result, [])
        self.assertEqual(transport.get.await_count, 2)

    def test_openalex_retries_429_and_reports_recovery(self) -> None:
        request = httpx.Request("GET", "https://api.openalex.org/works")
        rate_limited = httpx.Response(
            429, headers={"Retry-After": "0"}, request=request
        )
        recovered = httpx.Response(
            200,
            json={"results": [], "meta": {"next_cursor": ""}},
            request=request,
        )
        transport = MagicMock()
        transport.get = AsyncMock(side_effect=[rate_limited, recovered])
        context = MagicMock()
        context.__aenter__ = AsyncMock(return_value=transport)
        context.__aexit__ = AsyncMock(return_value=False)
        source = OpenAlexSource(searches=['"world model"'], concurrency=1)
        with (
            patch.object(config, "OPENALEX_API_KEY", "configured"),
            patch("sources.openalex_source.httpx.AsyncClient", return_value=context),
            patch("sources.openalex_source.asyncio.sleep", new=AsyncMock()),
        ):
            result = asyncio.run(source.fetch())

        self.assertEqual(result, [])
        self.assertEqual(transport.get.await_count, 2)
        self.assertEqual(source.coverage()["status"], "completed_after_retry")
        self.assertEqual(source.coverage()["rate_limited_requests"], 1)

    def test_openalex_known_route_lane_ignores_abstract_only_tail(self) -> None:
        def page(results: list[dict], cursor: str) -> httpx.Response:
            return httpx.Response(
                200,
                json={"results": results, "meta": {"next_cursor": cursor}},
                request=httpx.Request("GET", "https://api.openalex.org/works"),
            )

        strong = {
            "id": "https://openalex.org/W1",
            "display_name": "A World Model for Robot Planning",
            "publication_date": "2026-07-20",
            "primary_topic": {"display_name": "World Models"},
            "abstract_inverted_index": {"world": [0], "model": [1]},
        }
        weak = {
            "id": "https://openalex.org/W2",
            "display_name": "A Generic Prediction Method",
            "publication_date": "2026-07-20",
            "primary_topic": {"display_name": "Machine Learning"},
            # 全文 search 会返回只在摘要里弱命中的尾部结果；它不应让
            # OpenAlex 已知路线车道继续无界翻页。
            "abstract_inverted_index": {"world": [0], "model": [1]},
        }
        transport = MagicMock()
        transport.get = AsyncMock(
            side_effect=[
                page([strong], "cursor-1"),
                page([weak], "cursor-2"),
                page([weak], "cursor-3"),
            ]
        )
        context = MagicMock()
        context.__aenter__ = AsyncMock(return_value=transport)
        context.__aexit__ = AsyncMock(return_value=False)
        source = OpenAlexSource(searches=['"world model"'], per_query=1)
        with (
            patch.object(config, "OPENALEX_API_KEY", "configured"),
            patch("sources.openalex_source.httpx.AsyncClient", return_value=context),
        ):
            result = asyncio.run(source.fetch())

        self.assertEqual([item.title for item in result], [strong["display_name"]])
        self.assertEqual(transport.get.await_count, 3)
        self.assertEqual(source.coverage()["requests"], 3)
        self.assertEqual(source.coverage()["status"], "completed")

    def test_openalex_null_publication_date_is_normalized_before_storage(self) -> None:
        work = {
            "id": "https://openalex.org/W-null-date",
            "display_name": "A World Model with an Unknown Publication Date",
            "publication_date": None,
            "primary_topic": {"display_name": "World Models"},
            "abstract_inverted_index": {"world": [0], "model": [1]},
        }
        evidence = OpenAlexSource(searches=['"world model"'])._parse([work])

        self.assertEqual(len(evidence), 1)
        self.assertEqual(evidence[0].published_at, "")
        with tempfile.TemporaryDirectory() as directory:
            store = ParadigmStore(Path(directory) / "radar.db")
            checkpoint = store.mark_evidence(evidence, analyzed=False)
            restored = store.load_pending_origins()

        self.assertEqual(checkpoint.rejected_count, 0)
        self.assertEqual(len(restored), 1)
        self.assertEqual(restored[0].published_at, "")

    def test_openreview_uses_public_search_endpoint_instead_of_challenged_notes(self) -> None:
        response = httpx.Response(
            200,
            json={"notes": []},
            request=httpx.Request(
                "GET", "https://api2.openreview.net/notes/search"
            ),
        )
        transport = MagicMock()
        transport.get = AsyncMock(return_value=response)
        context = MagicMock()
        context.__aenter__ = AsyncMock(return_value=transport)
        context.__aexit__ = AsyncMock(return_value=False)
        with patch(
            "sources.openreview_source.httpx.AsyncClient", return_value=context
        ):
            asyncio.run(
                OpenReviewSource(
                    venues=["ICLR.cc/2026/Conference"],
                    searches=["world model"],
                    limit=2,
                ).fetch()
            )
        call = transport.get.await_args
        self.assertTrue(call.args[0].endswith("/notes/search"))
        self.assertEqual(call.kwargs["params"]["term"], "world model")
        self.assertEqual(
            call.kwargs["params"]["group"], "ICLR.cc/2026/Conference"
        )
        self.assertNotIn("sort", call.kwargs["params"])
        self.assertNotIn("details", call.kwargs["params"])
        self.assertNotIn("venueid", call.kwargs["params"])

    def test_openreview_uses_submission_date_and_filters_weak_search_hits(self) -> None:
        now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
        old_ms = int(
            (datetime.now(timezone.utc) - timedelta(days=30)).timestamp()
            * 1000
        )

        def note(note_id: str, title: str, abstract: str, cdate: int, tmdate: int):
            return {
                "id": note_id,
                "cdate": cdate,
                "tmdate": tmdate,
                "content": {
                    "title": {"value": title},
                    "abstract": {"value": abstract},
                    "authors": {"value": ["A. Researcher"]},
                    "authorids": {"value": ["~A_Researcher1"]},
                    "venueid": {"value": "ICLR.cc/2026/Conference"},
                },
                "details": {"replyCount": 1},
            }

        response = httpx.Response(
            200,
            json={
                "notes": [
                    note(
                        "old-discussion",
                        "A World Model from 2024",
                        "world model",
                        old_ms,
                        now_ms,
                    ),
                    note(
                        "weak-hit",
                        "Compiler Scheduling",
                        "An unrelated systems paper.",
                        now_ms,
                        now_ms,
                    ),
                    note(
                        "recent-match",
                        "Learning a World Model",
                        "A predictive world model for control.",
                        now_ms,
                        now_ms,
                    ),
                ]
            },
            request=httpx.Request("GET", "https://api2.openreview.net/notes/search"),
        )
        transport = MagicMock()
        transport.get = AsyncMock(return_value=response)
        context = MagicMock()
        context.__aenter__ = AsyncMock(return_value=transport)
        context.__aexit__ = AsyncMock(return_value=False)
        source = OpenReviewSource(
            lookback_days=7,
            venues=["ICLR.cc/2026/Conference"],
            searches=["world model"],
            limit=10,
        )
        with patch(
            "sources.openreview_source.httpx.AsyncClient",
            return_value=context,
        ):
            result = asyncio.run(source.fetch())

        self.assertEqual([item.identifiers["openreview"] for item in result], ["recent-match"])
        self.assertEqual(result[0].raw["origin_date_basis"], "submission_created_at")
        self.assertEqual(source.coverage()["relevance_filtered"], 1)

    def test_openreview_retries_429_and_exposes_degraded_coverage(self) -> None:
        request = httpx.Request(
            "GET", "https://api2.openreview.net/notes/search"
        )
        rate_limited = httpx.Response(
            429,
            headers={"Retry-After": "0"},
            request=request,
        )
        recovered = httpx.Response(
            200,
            json={"notes": []},
            request=request,
        )
        transport = MagicMock()
        transport.get = AsyncMock(side_effect=[rate_limited, recovered])
        context = MagicMock()
        context.__aenter__ = AsyncMock(return_value=transport)
        context.__aexit__ = AsyncMock(return_value=False)
        source = OpenReviewSource(
            venues=["ICLR.cc/2026/Conference"],
            searches=["world model"],
            limit=2,
            concurrency=1,
        )
        with (
            patch("sources.openreview_source.httpx.AsyncClient", return_value=context),
            patch(
                "sources.openreview_source.asyncio.sleep",
                new=AsyncMock(),
            ),
        ):
            result = asyncio.run(source.fetch())

        self.assertEqual(result, [])
        self.assertEqual(transport.get.await_count, 2)
        self.assertEqual(source.coverage()["rate_limited_requests"], 1)
        self.assertEqual(source.coverage()["status"], "completed_after_retry")

    def test_openreview_shared_circuit_stops_rate_limit_storm(self) -> None:
        request = httpx.Request(
            "GET", "https://api2.openreview.net/notes/search"
        )
        rate_limited = httpx.Response(
            429,
            headers={"Retry-After": "0"},
            request=request,
        )
        transport = MagicMock()
        transport.get = AsyncMock(return_value=rate_limited)
        context = MagicMock()
        context.__aenter__ = AsyncMock(return_value=transport)
        context.__aexit__ = AsyncMock(return_value=False)
        source = OpenReviewSource(
            venues=["ICLR.cc/2026/Conference", "NeurIPS.cc/2026/Conference"],
            searches=["world model", "reasoning model"],
            limit=2,
            concurrency=1,
        )
        with (
            patch("sources.openreview_source.httpx.AsyncClient", return_value=context),
            patch("sources.openreview_source.asyncio.sleep", new=AsyncMock()),
        ):
            result = asyncio.run(source.fetch())

        self.assertEqual(result, [])
        self.assertEqual(transport.get.await_count, 3)
        self.assertEqual(source.coverage()["status"], "rate_limited")
        self.assertEqual(source.coverage()["failed_queries"], 1)
        self.assertEqual(source.coverage()["not_executed_queries"], 3)

    def test_tavily_budget_is_shared_across_candidates(self) -> None:
        response = httpx.Response(
            200,
            json={"results": []},
            request=httpx.Request("POST", "https://api.tavily.com/search"),
        )
        transport = MagicMock()
        transport.post = AsyncMock(return_value=response)
        client = SocialWebSearchClient(max_requests=1)
        first, second = candidate(), candidate()
        with (
            patch.object(config, "TAVILY_SOCIAL_SEARCH_ENABLED", True),
            patch.object(config, "TAVILY_API_KEY", "configured"),
            patch.object(config, "TAVILY_SOCIAL_SEARCH_DOMAINS", ["reddit.com"]),
        ):
            asyncio.run(client.search(transport, first))
            asyncio.run(client.search(transport, second))
        self.assertEqual(transport.post.await_count, 1)
        self.assertEqual(client.requests_used, 1)
        self.assertIn("credit safety limit", second.community_coverage["tavily_social_web"])

    def test_unanalyzed_discovery_remains_eligible_next_run(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = ParadigmStore(Path(directory) / "radar.db")
            origin = paper()
            store.mark_evidence([origin], analyzed=False)
            selected, stats = store.plan_origins([origin])
            self.assertEqual(selected, [origin])
            self.assertEqual(stats["new"], 1)

            store.mark_evidence([origin], analyzed=True)
            selected, stats = store.plan_origins([origin])
            self.assertEqual(selected, [])
            self.assertEqual(stats["unchanged_skip"], 1)

    def test_unanalyzed_origin_survives_after_discovery_window_moves_on(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = ParadigmStore(Path(directory) / "radar.db")
            origin = paper()
            store.mark_evidence([origin], analyzed=False)
            backlog = store.load_pending_origins(exclude_fingerprints=set())
        self.assertEqual([item.fingerprint for item in backlog], [origin.fingerprint])

    def test_pending_deep_candidate_is_not_treated_as_discussion_refresh(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = ParadigmStore(Path(directory) / "radar.db")
            item = candidate()
            item.status = "pending_deep"
            store.mark_evidence(item.evidence, analyzed=True)
            store.save_candidates([item])
            pending = store.load_pending_deep_candidates()
            refresh = store.load_refresh_candidates()
        self.assertEqual([value.key for value in pending], [item.key])
        self.assertEqual(refresh, [])

    def test_huggingface_blob_pdf_uses_download_endpoint(self) -> None:
        self.assertEqual(
            _download_url(
                "https://huggingface.co/Qwen/Qwen3-Technical-Report/blob/main/report.pdf"
            ),
            "https://huggingface.co/Qwen/Qwen3-Technical-Report/resolve/main/report.pdf",
        )


if __name__ == "__main__":
    unittest.main()
