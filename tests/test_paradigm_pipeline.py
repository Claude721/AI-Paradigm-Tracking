from __future__ import annotations

import asyncio
import json
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import httpx

import config
from agents.paradigm_orchestrator import _apply_safety_limit
from agents.llm_utils import parse_json_object
from database.paradigm_store import ParadigmStore
from paradigms.analyzer import (
    ParadigmAnalyzer,
    ParadigmSynthesizer,
    _author_prompt_summary,
    _validate_mental_model,
)
from paradigms.clustering import cluster_extractions
from paradigms.enrichment import EvidenceEnricher
from paradigms.models import (
    EvidenceType,
    ParadigmCandidate,
    ParadigmExtraction,
    ResearcherProfile,
    TechnicalEvidence,
)
from paradigms.rubric import evaluate_rubric, load_rubric
from paradigms.scoring import is_reportable, score_candidate
from reports.paradigm_generator import (
    ParadigmReportGenerator,
    _candidate_dossier,
    _valid_editorial_report,
)
from skills.loader import SkillLoader
from sources.paradigm_evidence_source import (
    CommunityEvidenceClient,
    _is_relevant_repository,
)
from sources.base import RawProject
from sources.arxiv_document_source import (
    ArxivDocumentClient,
    _distributed_text_excerpt,
)
from sources.arxiv_source import ArxivSource
from sources.openalex_source import OpenAlexSource
from sources.openreview_source import OpenReviewSource
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


class ParadigmPipelineTests(unittest.TestCase):
    def test_zero_safety_limit_never_truncates_dynamic_volume(self) -> None:
        items = list(range(275))
        selected, deferred = _apply_safety_limit(items, 0)
        self.assertEqual(selected, items)
        self.assertEqual(deferred, [])

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
        self.assertFalse(is_reportable(item))
        self.assertIn("范式综合失败", item.rejection_reason)

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

    def test_empty_report_discloses_failed_recall_lane_and_official_page(self) -> None:
        content = ParadigmReportGenerator._empty_report(
            "2026-07-28",
            {
                "origin_count": 0,
                "frontier_coverage": {
                    "domains": {},
                    "recall_lanes": {
                        "priority_researchers_1": {
                            "status": "query_failed",
                            "hits": 0,
                        }
                    },
                    "academic_indexes": {
                        "openreview": {
                            "status": "partial",
                            "queries": 4,
                            "completed_queries": 3,
                            "failed_queries": 1,
                            "requests": 8,
                            "rate_limited_requests": 2,
                            "results": 5,
                        }
                    },
                    "official_pages": {
                        "total_pages": 2,
                        "checked_pages": 2,
                        "request_failed": 1,
                        "parse_zero_links": 0,
                        "detail_failures": 0,
                    },
                },
            },
        )
        self.assertIn("运行不完整的空报告", content)
        self.assertIn("priority_researchers_1", content)
        self.assertIn("openreview=partial", content)
        self.assertIn("官方入口", content)

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
        <a href="https://www.kimi.com/blog/kimi-k3">2026-07-14 Kimi K3</a>
        <a href="/research">Research</a>
        """
        links = _discover_index_links(html, "https://www.moonshot.ai/")
        self.assertEqual(len(links), 1)
        self.assertEqual(links[0].title, "Kimi K3")
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

    def test_arxiv_exact_seed_runs_before_broad_recall_and_survives_failures(
        self,
    ) -> None:
        source = ArxivSource(
            max_results=None,
            lookback_days=7,
            seed_arxiv_ids=["2607.24653"],
        )
        seeded = RawProject(
            source="arxiv",
            name="Seeded report",
            url="https://arxiv.org/abs/2607.24653",
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
        authors = ["Kimi Team", *[f"Researcher {index}" for index in range(400)]]
        summary = _author_prompt_summary(authors)
        self.assertIn("共 401 位作者", summary)
        self.assertIn("Kimi Team", summary)
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
        self.assertEqual(call.kwargs["params"]["query"], "world model")
        self.assertEqual(
            call.kwargs["params"]["venueid"], "ICLR.cc/2026/Conference"
        )

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
