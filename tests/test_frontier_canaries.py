from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from database.paradigm_store import ParadigmStore
from paradigms.clustering import cluster_extractions, is_priority_review
from paradigms.landscape import (
    arxiv_priority_author_query_plan,
    arxiv_query_plan,
    classify_frontier_domains,
    coverage_report,
    load_landscape,
)
from paradigms.publication import classify_publication
from paradigms.models import (
    EvidenceType,
    ParadigmCandidate,
    ParadigmExtraction,
    ResearcherProfile,
    TechnicalEvidence,
)
from paradigms.rubric import evaluate_rubric, objective_answers
from paradigms.scoring import is_reportable, score_candidate
from sources.arxiv_document_source import parse_arxiv_html, parse_project_page
from sources.arxiv_source import ArxivSource, TECHNICAL_REPORT_QUERY
from sources.researcher_profile_source import _seed_profiles
from sources.social_web_search_source import _parse_results


T_REX_ATOM = """<?xml version="1.0" encoding="utf-8"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <entry>
    <id>https://arxiv.org/abs/2606.17055v2</id>
    <updated>2026-06-18T17:59:59Z</updated>
    <published>2026-06-15T17:59:59Z</published>
    <title>T-Rex: Tactile-Reactive Dexterous Manipulation</title>
    <summary>We introduce a vision-language-action system with asynchronous tactile feedback for dexterous robot manipulation.</summary>
    <author><name>Dantong Niu</name></author>
    <author><name>Zhuoyang Liu</name></author>
    <author><name>Zekai Wang</name></author>
    <author><name>Fei-Fei Li</name></author>
    <category term="cs.RO"/>
    <category term="cs.AI"/>
    <link href="https://arxiv.org/abs/2606.17055v2" type="text/html"/>
  </entry>
</feed>
"""

KIMI_K3_ATOM = """<?xml version="1.0" encoding="utf-8"?>
<feed xmlns="http://www.w3.org/2005/Atom"
      xmlns:arxiv="http://arxiv.org/schemas/atom">
  <entry>
    <id>https://arxiv.org/abs/2607.24653v1</id>
    <updated>2026-07-27T16:49:54Z</updated>
    <published>2026-07-27T16:49:54Z</published>
    <title>Kimi K3: Open Frontier Intelligence</title>
    <summary>We introduce Kimi K3, a 2.8T-parameter native multimodal
    Mixture-of-Experts language model with Kimi Delta Attention, a one
    million token context window, post-training reinforcement learning,
    algorithm-system co-design, deployment innovations, extensive
    evaluations, and released model weights.</summary>
    <author><name>Kimi Team</name></author>
    <category term="cs.CL"/>
    <category term="cs.LG"/>
    <arxiv:comment>K3 tech report</arxiv:comment>
    <link href="https://arxiv.org/abs/2607.24653v1" type="text/html"/>
  </entry>
</feed>
"""


def _max_assessment(types: list[str], stage: str) -> dict:
    definition = load_landscape()
    del definition
    from paradigms.rubric import load_rubric

    rubric = load_rubric()
    criteria = list(rubric["common_criteria"])
    for innovation_type in types:
        criteria.extend(rubric["type_criteria"][innovation_type])
    answers = [
        {
            "criterion_id": criterion["id"],
            "answer": max(criterion["options"], key=criterion["options"].get),
            "evidence": f"{criterion['id']} 的离线黄金样例证据",
        }
        for criterion in criteria
    ]
    return evaluate_rubric(
        stage=stage,
        innovation_types=types,
        answers=answers,
    )


def _route_candidate(
    *,
    key: str,
    route_family: str,
    mechanism: str,
    keywords: list[str],
    stars: int = 0,
) -> ParadigmCandidate:
    evidence = TechnicalEvidence(
        source="arxiv",
        evidence_type=EvidenceType.PRIMARY_PAPER,
        title="Tactile control paper",
        url=f"https://arxiv.org/abs/{key}",
        summary=mechanism,
        identifiers={"arxiv": key},
        keywords=["cs.RO"],
        raw={"frontier_domains": ["embodied_robotics"]},
    )
    if stars:
        evidence.metrics["stars"] = stars
    assessment = _max_assessment(["embodiment"], "final")
    return ParadigmCandidate(
        key=key,
        name="Tactile-reactive dexterous control",
        route_family=route_family,
        thesis="把触觉从附加模态变成快速控制状态。",
        problem_shift="从慢速视觉动作块转向接触事件驱动的快速修正。",
        mechanism=mechanism,
        innovation_types=["embodiment"],
        keywords=keywords,
        evidence=[evidence],
        screening_rubric=_max_assessment(["embodiment"], "screening"),
        rubric_assessment=assessment,
    )


class FrontierCoverageTests(unittest.TestCase):
    def test_landscape_covers_full_ai_technical_stack(self) -> None:
        landscape = load_landscape()
        domain_ids = {item["id"] for item in landscape["domains"]}
        self.assertEqual(
            set(landscape["required_domain_ids"]),
            domain_ids,
        )
        queries = " ".join(item["query"] for item in arxiv_query_plan()).casefold()
        for marker in (
            "tactile robotics",
            "dexterous manipulation",
            "world model",
            "materials discovery",
            "neural operator",
            "hardware software co-design",
            "mechanistic interpretability",
        ):
            self.assertIn(marker, queries)
        self.assertIn('co:"tech report"', TECHNICAL_REPORT_QUERY.casefold())

    def test_trex_is_recalled_in_bootstrap_window_and_prioritized(self) -> None:
        source = ArxivSource(lookback_days=60, seed_arxiv_ids=[])
        parsed = source._parse_atom_feed(
            T_REX_ATOM,
            query_group="physical_intelligence",
            domain_ids=["world_spatial_models", "embodied_robotics"],
        )
        self.assertEqual(len(parsed), 1)
        item = parsed[0]
        self.assertIn("embodied_robotics", item.extra["frontier_domains"])
        self.assertEqual(item.extra["origin_priority"], 2)
        self.assertEqual(item.extra["updated_at"], "2026-06-18T17:59:59Z")

    def test_kimi_k3_is_recalled_as_multi_mechanism_system_report(self) -> None:
        source = ArxivSource(lookback_days=14, seed_arxiv_ids=[])
        parsed = source._parse_atom_feed(
            KIMI_K3_ATOM,
            query_group="core_models",
            domain_ids=[
                "foundation_models",
                "reasoning_agents",
                "multimodal_generation",
            ],
        )
        self.assertEqual(len(parsed), 1)
        item = parsed[0]
        self.assertEqual(item.extra["origin_kind"], "technical_report")
        self.assertEqual(item.extra["origin_priority"], 3)
        self.assertEqual(
            item.extra["origin_classification_reason"],
            "explicit_document_metadata:tech report",
        )
        self.assertEqual(item.extra["arxiv_comment"], "K3 tech report")
        self.assertEqual(item.extra["organization"], "Moonshot AI")
        self.assertEqual(item.extra["publisher_tier"], "established")
        self.assertIn("foundation_models", item.extra["frontier_domains"])
        self.assertIn("reasoning_agents", item.extra["frontier_domains"])
        self.assertIn("multimodal_generation", item.extra["frontier_domains"])

    def test_system_report_fallback_does_not_require_report_in_title(self) -> None:
        without_comment = KIMI_K3_ATOM.replace(
            "<arxiv:comment>K3 tech report</arxiv:comment>",
            "",
        )
        item = ArxivSource(
            lookback_days=14,
            seed_arxiv_ids=[],
        )._parse_atom_feed(without_comment)[0]
        self.assertEqual(item.extra["origin_kind"], "technical_report")
        self.assertEqual(
            item.extra["origin_classification_reason"],
            "inferred_system_scope_report",
        )

    def test_priority_researcher_lane_does_not_depend_on_known_technical_terms(self) -> None:
        plans = arxiv_priority_author_query_plan(
            ["Fei-Fei Li", "Yann LeCun"],
            chunk_size=1,
        )
        self.assertEqual(len(plans), 2)
        self.assertIn('au:"Fei-Fei Li"', plans[0]["query"])
        self.assertNotIn("world model", plans[0]["query"].casefold())

        novel_atom = T_REX_ATOM.replace(
            "T-Rex: Tactile-Reactive Dexterous Manipulation",
            "Project Banyan: Event-Synchronous Skill Weaving",
        ).replace(
            "We introduce a vision-language-action system with asynchronous tactile feedback for dexterous robot manipulation.",
            "We present a new computational process for learning reusable behaviors.",
        )
        item = ArxivSource(
            lookback_days=60,
            seed_arxiv_ids=[],
        )._parse_atom_feed(
            novel_atom,
            query_group="priority_researchers",
        )[0]
        self.assertEqual(item.extra["frontier_domains"], [])
        self.assertEqual(item.extra["origin_priority"], 2)

    def test_report_search_hit_does_not_force_an_ordinary_paper_into_report_mode(self) -> None:
        ordinary = T_REX_ATOM.replace(
            "T-Rex: Tactile-Reactive Dexterous Manipulation",
            "Calibration for Small Robot Policies",
        ).replace(
            "We introduce a vision-language-action system with asynchronous tactile feedback for dexterous robot manipulation.",
            "We compare against a technical report and improve calibration on one benchmark.",
        ).replace(
            "<author><name>Zhuoyang Liu</name></author>\n"
            "    <author><name>Zekai Wang</name></author>\n"
            "    <author><name>Fei-Fei Li</name></author>",
            "",
        )
        item = ArxivSource(
            lookback_days=60,
            seed_arxiv_ids=[],
        )._parse_atom_feed(
            ordinary,
            force_technical_report=True,
            query_group="technical_reports",
        )[0]
        self.assertEqual(item.extra["origin_kind"], "research_paper")
        self.assertEqual(
            item.extra["origin_classification_reason"],
            "report_query_unconfirmed",
        )

    def test_brand_named_official_document_uses_system_scope_not_title_suffix(self) -> None:
        body = " ".join(
            [
                "We release Project Banyan, a foundation model with model weights.",
                "Architecture and attention routing.",
                "Pretraining data mixture and post-training reinforcement learning.",
                "Infrastructure deployment and serving.",
                "Evaluation benchmark capabilities and parameter context length.",
            ]
        ) * 80
        result = classify_publication(
            title="Project Banyan",
            url="https://lab.example/publications/project-banyan.pdf",
            summary=body,
            official=True,
        )
        self.assertEqual(result.origin_kind, "technical_report")
        self.assertEqual(result.reason, "official_document_with_system_scope")

    def test_kimi_k3_canary_crosses_publisher_and_admission_logic(self) -> None:
        raw = ArxivSource(
            lookback_days=14,
            seed_arxiv_ids=[],
        )._parse_atom_feed(
            KIMI_K3_ATOM,
            query_group="core_models",
            domain_ids=["foundation_models"],
        )[0]
        evidence = TechnicalEvidence(
            source="arxiv",
            evidence_type=EvidenceType.PRIMARY_PAPER,
            title=raw.name,
            url=raw.url,
            summary=raw.readme_summary,
            published_at=raw.created_at,
            authors=raw.extra["all_authors"],
            organization=raw.extra["organization"],
            identifiers={"arxiv": "2607.24653"},
            raw=raw.extra,
        )
        assessment = _max_assessment(["architecture", "systems"], "final")
        candidate = ParadigmCandidate(
            key="kda-attnres",
            name="线性注意力与深层残差路由协同扩展",
            route_family="超大稀疏模型的稳定高效扩展",
            thesis="同时重写注意力状态更新和深层残差聚合。",
            problem_shift="从单独扩大 MoE 转向注意力、残差和系统协同设计。",
            mechanism="KDA、AttnRes 与 Stable LatentMoE 共同稳定超大规模训练。",
            innovation_types=["architecture", "systems"],
            evidence=[evidence],
            screening_rubric=_max_assessment(
                ["architecture", "systems"],
                "screening",
            ),
            rubric_assessment=assessment,
        )
        score_candidate(candidate)
        self.assertEqual(candidate.publisher_tier, "established")
        self.assertTrue(candidate.is_formal_technical_report)
        self.assertTrue(is_reportable(candidate))
        self.assertIn("优先解读", candidate.admission_reason)

    def test_trex_falls_outside_normal_week_but_exact_seed_can_backfill(self) -> None:
        weekly = ArxivSource(lookback_days=7, seed_arxiv_ids=[])
        self.assertEqual(weekly._parse_atom_feed(T_REX_ATOM), [])
        seeded = weekly._parse_atom_feed(
            T_REX_ATOM,
            query_group="explicit_seed",
            ignore_lookback=True,
        )
        self.assertEqual(len(seeded), 1)
        self.assertTrue(seeded[0].extra["explicit_seed"])

    def test_recent_revision_of_old_paper_is_recalled(self) -> None:
        xml = T_REX_ATOM.replace(
            "2026-06-18T17:59:59Z", "2026-07-25T17:59:59Z"
        )
        source = ArxivSource(lookback_days=7, seed_arxiv_ids=[])
        self.assertEqual(len(source._parse_atom_feed(xml)), 1)

    def test_arxiv_html_hydration_extracts_document_people_and_project(self) -> None:
        html = """
        <html><head>
          <meta name="citation_author" content="Dantong Niu">
          <meta name="citation_author" content="Zhuoyang Liu">
          <meta name="citation_author" content="Zekai Wang">
          <meta name="citation_author_institution" content="UC Berkeley">
        </head><body>
          <div class="ltx_authors">
            <span class="ltx_personname"><a href="https://dantong.example">Dantong Niu</a></span>
          </div>
          <span class="ltx_note">Dantong Niu, Zhuoyang Liu and Zekai Wang contributed equally.</span>
          <article><p>The fast tactile expert reacts asynchronously to contact while the slow expert predicts action chunks.</p>
          <a href="https://tactile-reactive-dexterous.github.io/">Project page</a>
          <a href="https://github.com/ZhuoyangLiu2005/T-Rex">Code</a></article>
        </body></html>
        """
        parsed = parse_arxiv_html(html, base_url="https://arxiv.org/html/2606.17055")
        self.assertIn("fast tactile expert", parsed["document_excerpt"])
        self.assertIn("UC Berkeley", parsed["affiliations"])
        self.assertEqual(
            parsed["author_profile_urls"]["Dantong Niu"],
            "https://dantong.example",
        )
        self.assertIn(
            "https://github.com/ZhuoyangLiu2005/T-Rex",
            parsed["github_repositories"],
        )
        self.assertEqual(parsed["author_roles"]["Dantong Niu"], "共同第一作者")

    def test_researcher_selection_keeps_three_leads_last_and_priority_people(self) -> None:
        evidence = TechnicalEvidence(
            source="arxiv",
            evidence_type=EvidenceType.PRIMARY_PAPER,
            title="T-Rex",
            url="https://arxiv.org/abs/2606.17055",
            authors=[
                "Dantong Niu",
                "Zhuoyang Liu",
                "Zekai Wang",
                "Other Author",
                "Fei-Fei Li",
                "Trevor Darrell",
            ],
            organization="UC Berkeley; NVIDIA; Stanford",
            raw={
                "author_roles": {
                    "Dantong Niu": "共同第一作者",
                    "Zhuoyang Liu": "共同第一作者",
                    "Zekai Wang": "共同第一作者",
                }
            },
        )
        profiles = _seed_profiles(evidence, [], 6)
        by_name = {profile.name: profile for profile in profiles}
        self.assertTrue(
            {"Dantong Niu", "Zhuoyang Liu", "Zekai Wang", "Fei-Fei Li", "Trevor Darrell"}
            <= set(by_name)
        )
        self.assertEqual(by_name["Zekai Wang"].role, "共同第一作者")

    def test_collective_team_author_is_publisher_not_person_profile(self) -> None:
        evidence = TechnicalEvidence(
            source="arxiv",
            evidence_type=EvidenceType.PRIMARY_PAPER,
            title="Frontier System Report",
            url="https://arxiv.org/abs/2607.24653",
            authors=[
                "Kimi Team",
                "Tongtong Bai",
                "Yifan Bai",
                "Yiping Bao",
                "Senior Author",
            ],
            organization="Moonshot AI",
        )
        profiles = _seed_profiles(evidence, [], 5)
        by_name = {profile.name: profile for profile in profiles}
        self.assertNotIn("Kimi Team", by_name)
        self.assertNotIn("Senior Author", by_name)
        self.assertIn("Tongtong Bai", by_name)
        self.assertEqual(
            by_name["Tongtong Bai"].role,
            "第一位具名作者/贡献角色待核验",
        )

    def test_project_page_maps_author_profiles_roles_and_public_email(self) -> None:
        parsed = parse_project_page(
            """
            <a href="https://dantong.example">Dantong Niu*</a>
            <a href="https://zhuoyang.example">Zhuoyang Liu*</a>
            <a href="mailto:zekai@university.edu">Zekai Wang*</a>
            <p>* Equal Contribution</p>
            """,
            base_url="https://tactile-reactive-dexterous.github.io/",
            author_names=["Dantong Niu", "Zhuoyang Liu", "Zekai Wang"],
        )
        self.assertEqual(
            parsed["author_profile_urls"]["Dantong Niu"],
            "https://dantong.example",
        )
        self.assertEqual(
            parsed["author_public_emails"]["Zekai Wang"],
            "zekai@university.edu",
        )
        self.assertEqual(parsed["author_roles"]["Zhuoyang Liu"], "共同第一作者")

    def test_trex_canary_crosses_full_post_recall_logic(self) -> None:
        raw = ArxivSource(lookback_days=60, seed_arxiv_ids=[])._parse_atom_feed(
            T_REX_ATOM,
            query_group="physical_intelligence",
            domain_ids=["embodied_robotics"],
        )[0]
        evidence = TechnicalEvidence(
            source="arxiv",
            evidence_type=EvidenceType.PRIMARY_PAPER,
            title=raw.name,
            url=raw.url,
            summary=raw.readme_summary,
            published_at=raw.created_at,
            authors=raw.extra["all_authors"],
            identifiers={"arxiv": "2606.17055"},
            raw=raw.extra,
        )
        screening = _max_assessment(["embodiment", "data"], "screening")
        extraction = ParadigmExtraction(
            evidence=evidence,
            is_candidate=True,
            canonical_name="异步触觉反应式灵巧控制",
            route_family="高频触觉闭环的机器人基础策略",
            thesis="触觉成为快速控制状态。",
            problem_shift="从视觉动作块转向接触事件驱动修正。",
            mechanism="慢速动作专家与高频触觉专家异步协同。",
            innovation_types=["embodiment", "data"],
            keywords=["tactile", "asynchronous control", "dexterous manipulation"],
            rubric_assessment=screening,
        )
        candidates = cluster_extractions([extraction])
        self.assertEqual(len(candidates), 1)
        item = candidates[0]
        item.rubric_assessment = _max_assessment(
            ["embodiment", "data"], "final"
        )
        item.researchers = [
            ResearcherProfile(
                name="Dantong Niu",
                role="共同第一作者",
                current_affiliation="Stanford University",
                representative_works=[
                    {"title": "Prior tactile work"},
                    {"title": "Prior robot learning work"},
                ],
                profile_urls={"homepage": "https://dantong.example"},
                identifiers={"openalex": "https://openalex.org/A1"},
                trajectory_consistency=8,
            ),
            ResearcherProfile(
                name="Fei-Fei Li",
                role="共同作者",
                current_affiliation="Stanford University",
                profile_urls={"homepage": "https://profiles.stanford.edu/fei-fei-li"},
                identifiers={"openalex": "https://openalex.org/A2"},
            ),
        ]
        item.evidence.append(
            TechnicalEvidence(
                source="github",
                evidence_type=EvidenceType.IMPLEMENTATION,
                title="ZhuoyangLiu2005/T-Rex",
                url="https://github.com/ZhuoyangLiu2005/T-Rex",
                metrics={"stars": 179, "forks": 20},
                identifiers={"github": "ZhuoyangLiu2005/T-Rex"},
                raw={
                    "relationship": "paper_linked_repository",
                    "independence": "official",
                },
            )
        )
        score_candidate(item)
        self.assertEqual(item.publisher_tier, "verified")
        self.assertTrue(is_reportable(item))
        answers = {
            answer["criterion_id"]: answer["answer"]
            for answer in objective_answers(item)
        }
        self.assertEqual(
            answers["independent_validation"],
            "official_implementation_uptake",
        )
        self.assertEqual(answers["secondary_discussion"], "none_or_unsearched")

    def test_high_potential_observe_candidate_gets_review_not_auto_report(self) -> None:
        raw = ArxivSource(lookback_days=60, seed_arxiv_ids=[])._parse_atom_feed(
            T_REX_ATOM,
            query_group="physical_intelligence",
            domain_ids=["embodied_robotics"],
        )[0]
        evidence = TechnicalEvidence(
            source="arxiv",
            evidence_type=EvidenceType.PRIMARY_PAPER,
            title=raw.name,
            url=raw.url,
            summary=raw.readme_summary,
            authors=raw.extra["all_authors"],
            identifiers={"arxiv": "2606.17055"},
            raw=raw.extra,
        )
        assessment = _max_assessment(["embodiment"], "screening")
        assessment["decision"] = "observe"
        extraction = ParadigmExtraction(
            evidence=evidence,
            is_candidate=True,
            canonical_name="异步触觉反应式灵巧控制",
            route_family="高频触觉闭环",
            thesis="触觉从附加观测变成可以异步触发动作修正的控制状态。",
            problem_shift="让接触事件可以即时修正慢速动作块。",
            mechanism="慢速动作专家与高频触觉专家异步协同。",
            innovation_types=["embodiment"],
            rubric_assessment=assessment,
        )
        self.assertTrue(is_priority_review(extraction))
        self.assertEqual(len(cluster_extractions([extraction])), 1)
        # 复核通道只让它进入深挖；最终报告仍必须重新通过 final Rubric。
        candidate = cluster_extractions([extraction])[0]
        candidate.rubric_assessment = assessment
        score_candidate(candidate)
        self.assertFalse(is_reportable(candidate))

    def test_unknown_official_repo_cannot_replace_publisher_or_independent_evidence(self) -> None:
        item = _route_candidate(
            key="2606.1",
            route_family="high frequency tactile control",
            mechanism="asynchronous tactile feedback for dexterous manipulation",
            keywords=["tactile", "dexterous", "asynchronous"],
        )
        item.evidence.append(
            TechnicalEvidence(
                source="github",
                evidence_type=EvidenceType.IMPLEMENTATION,
                title="author/project",
                url="https://github.com/author/project",
                metrics={"stars": 500, "forks": 30},
                raw={
                    "relationship": "paper_linked_repository",
                    "independence": "official",
                },
            )
        )
        score_candidate(item)
        self.assertFalse(is_reportable(item))
        self.assertIn("发布者背景未核验", item.admission_reason)

    def test_multiple_verified_frontier_researchers_trigger_deep_editorial_attention(self) -> None:
        item = _route_candidate(
            key="2606.2",
            route_family="tactile robot control",
            mechanism="asynchronous tactile feedback",
            keywords=["tactile", "robot", "feedback"],
        )
        item.researchers = [
            ResearcherProfile(
                name="Fei-Fei Li",
                profile_urls={"homepage": "https://profiles.example/fei-fei"},
                identifiers={"openalex": "A1"},
            ),
            ResearcherProfile(
                name="Pieter Abbeel",
                profile_urls={"homepage": "https://people.example/pieter"},
                identifiers={"openalex": "A2"},
            ),
        ]
        score_candidate(item)
        self.assertTrue(is_reportable(item))
        self.assertIn("多位长期前沿研究者", item.admission_reason)

    def test_cross_week_route_is_reconciled_without_exact_name(self) -> None:
        historical = _route_candidate(
            key="tactile-reactive-policy",
            route_family="high frequency tactile control for dexterous robotics",
            mechanism="asynchronous tactile feedback corrects manipulation actions",
            keywords=["tactile", "dexterous", "asynchronous", "manipulation"],
        )
        current = _route_candidate(
            key="multirate-touch-vla",
            route_family="multirate tactile control for dexterous robotics",
            mechanism="asynchronous tactile feedback updates manipulation policy",
            keywords=["tactile", "dexterous", "asynchronous", "manipulation"],
        )
        with tempfile.TemporaryDirectory() as directory:
            store = ParadigmStore(Path(directory) / "radar.db")
            store.mark_evidence(historical.evidence)
            store.save_candidates([historical])
            reconciled = store.attach_history([current])
        self.assertEqual(len(reconciled), 1)
        self.assertEqual(reconciled[0].key, "tactile-reactive-policy")
        self.assertTrue(
            any(item.raw.get("historical") for item in reconciled[0].evidence)
        )

    def test_rejected_route_cannot_capture_future_route_by_lexical_similarity(self) -> None:
        rejected = _route_candidate(
            key="rejected-tactile-route",
            route_family="high frequency tactile control for dexterous robotics",
            mechanism="asynchronous tactile feedback corrects manipulation actions",
            keywords=["tactile", "dexterous", "asynchronous", "manipulation"],
        )
        rejected.status = "rejected"
        current = _route_candidate(
            key="new-tactile-route",
            route_family="multirate tactile control for dexterous robotics",
            mechanism="asynchronous tactile feedback updates manipulation policy",
            keywords=["tactile", "dexterous", "asynchronous", "manipulation"],
        )
        with tempfile.TemporaryDirectory() as directory:
            store = ParadigmStore(Path(directory) / "radar.db")
            store.mark_evidence(rejected.evidence)
            store.save_candidates([rejected])
            reconciled = store.attach_history([current])
        self.assertEqual(reconciled[0].key, "new-tactile-route")

    def test_local_database_rebuilds_baseline_when_landscape_version_changes(self) -> None:
        item = _route_candidate(
            key="existing-route",
            route_family="tactile control",
            mechanism="tactile feedback",
            keywords=["tactile", "feedback"],
        )
        with tempfile.TemporaryDirectory() as directory:
            store = ParadigmStore(Path(directory) / "radar.db")
            store.mark_evidence(item.evidence, analyzed=True)
            store.save_candidates([item])
            self.assertTrue(store.is_bootstrap_required())
            store.mark_landscape_version("older-map")
            self.assertTrue(store.is_bootstrap_required())
            store.mark_landscape_version()
            self.assertFalse(store.is_bootstrap_required())

    def test_material_metric_threshold_triggers_update_without_plus_one_spam(self) -> None:
        item = _route_candidate(
            key="metric-route",
            route_family="tactile control",
            mechanism="tactile feedback",
            keywords=["tactile", "feedback"],
        )
        item.evidence[0].metrics = {"stars": 49}
        before = item.report_signature
        item.evidence[0].metrics = {"stars": 50}
        threshold = item.report_signature
        item.evidence[0].metrics = {"stars": 51}
        plus_one = item.report_signature
        self.assertNotEqual(before, threshold)
        self.assertEqual(threshold, plus_one)

    def test_coverage_audit_distinguishes_zero_hit_and_failed_query(self) -> None:
        evidence = TechnicalEvidence(
            source="arxiv",
            evidence_type=EvidenceType.PRIMARY_PAPER,
            title="Tactile robot policy",
            url="https://arxiv.org/abs/1",
            raw={"frontier_domains": ["embodied_robotics"]},
        )
        report = coverage_report(
            [evidence],
            executed_groups={"physical_intelligence", "science"},
            failed_groups={"trust"},
        )
        self.assertEqual(
            report["domains"]["embodied_robotics"]["status"],
            "covered",
        )
        self.assertEqual(
            report["domains"]["ai4science"]["status"],
            "searched_zero_hits",
        )
        self.assertEqual(
            report["domains"]["trust_alignment"]["status"],
            "query_failed",
        )

    def test_tavily_webwide_result_is_discovery_only(self) -> None:
        item = _route_candidate(
            key="2606.17055",
            route_family="tactile control",
            mechanism="tactile feedback",
            keywords=["tactile", "feedback"],
        )
        item.evidence[0].title = "T-Rex: Tactile-Reactive Dexterous Manipulation"
        found = _parse_results(
            {
                "request_id": "req-web",
                "results": [
                    {
                        "title": "T-Rex paper notes",
                        "url": "https://researcher.github.io/posts/t-rex-paper-notes/",
                        "content": "T-Rex: Tactile-Reactive Dexterous Manipulation analysis",
                        "score": 0.9,
                    }
                ],
            },
            item,
            item.evidence[0].title,
        )
        self.assertEqual(found[0].source, "tavily-web")
        self.assertTrue(found[0].raw["indexed_discovery_only"])

    def test_domain_classifier_maps_representative_industries(self) -> None:
        self.assertIn(
            "embodied_robotics",
            classify_frontier_domains("Tactile dexterous robot manipulation"),
        )
        self.assertIn(
            "ai4science",
            classify_frontier_domains("Neural operator for materials discovery"),
        )
        self.assertIn(
            "ml_systems_hardware",
            classify_frontier_domains("Hardware software co-design for AI accelerator"),
        )
        self.assertIn(
            "multimodal_generation",
            classify_frontier_domains("Unified speech language model for audio generation"),
        )
        self.assertIn(
            "embodied_robotics",
            classify_frontier_domains("Foundation policy for autonomous driving"),
        )
        self.assertNotIn(
            "reasoning_agents",
            classify_frontier_domains("A chemical reagent discovery benchmark"),
        )


if __name__ == "__main__":
    unittest.main()
