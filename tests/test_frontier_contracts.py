from __future__ import annotations

import asyncio
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

from database.paradigm_store import ParadigmStore
from paradigms.clustering import cluster_extractions, is_priority_review
from paradigms.discovery import _merge_origins, _raw_to_origin
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
from paradigms.rubric import evaluate_rubric
from paradigms.scoring import is_reportable, score_candidate
from sources.arxiv_document_source import parse_arxiv_html, parse_project_page
from sources.arxiv_source import ArxivSource, TECHNICAL_REPORT_QUERY
from sources.base import RawProject
from sources.researcher_profile_source import _seed_profiles
from sources.social_web_search_source import _parse_results


OLD_RESEARCH_PAPER_ATOM = """<?xml version="1.0" encoding="utf-8"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <entry>
    <id>https://arxiv.org/abs/2601.00001v2</id>
    <updated>2026-01-18T17:59:59Z</updated>
    <published>2026-01-15T17:59:59Z</published>
    <title>Event-Synchronous Skill Weaving</title>
    <summary>We present a new computational process for learning reusable behaviors.</summary>
    <author><name>Alex Chen</name></author>
    <author><name>Blair Singh</name></author>
    <author><name>Casey Park</name></author>
    <author><name>Fei-Fei Li</name></author>
    <category term="cs.RO"/>
    <category term="cs.AI"/>
    <link href="https://arxiv.org/abs/2601.00001v2" type="text/html"/>
  </entry>
</feed>
"""

SYSTEM_REPORT_ATOM = """<?xml version="1.0" encoding="utf-8"?>
<feed xmlns="http://www.w3.org/2005/Atom"
      xmlns:arxiv="http://arxiv.org/schemas/atom">
  <entry>
    <id>https://arxiv.org/abs/2601.00002v1</id>
    <updated>2026-01-27T16:49:54Z</updated>
    <published>2026-01-27T16:49:54Z</published>
    <title>Project Atlas: Open Multimodal Intelligence</title>
    <summary>We introduce Project Atlas, a native multimodal
    Mixture-of-Experts language model with a new attention mechanism,
    a long context window, post-training reinforcement learning,
    algorithm-system co-design, deployment innovations, extensive
    evaluations, and released model weights.</summary>
    <author><name>Atlas Research Team</name></author>
    <category term="cs.CL"/>
    <category term="cs.LG"/>
    <arxiv:comment>Technical report</arxiv:comment>
    <link href="https://arxiv.org/abs/2601.00002v1" type="text/html"/>
  </entry>
</feed>
"""

FIXTURE_REFERENCE_TIME = datetime(2026, 1, 30, tzinfo=timezone.utc)


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
            "evidence": f"{criterion['id']} 的离线契约证据",
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


def _observe_extraction(raw: dict) -> ParadigmExtraction:
    evidence = TechnicalEvidence(
        source="arxiv",
        evidence_type=EvidenceType.PRIMARY_PAPER,
        title="Event-Synchronous Skill Weaving",
        url="https://arxiv.org/abs/2601.00001",
        summary="A new computational process for reusable robot behaviors.",
        authors=["Alex Chen", "Blair Singh"],
        identifiers={"arxiv": "2601.00001"},
        raw={"frontier_domains": ["embodied_robotics"], **raw},
    )
    assessment = _max_assessment(["embodiment"], "screening")
    assessment["decision"] = "observe"
    return ParadigmExtraction(
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


class FrontierContractTests(unittest.TestCase):
    def test_aggregator_recall_keeps_direct_arxiv_primary_url(self) -> None:
        item = _raw_to_origin(
            RawProject(
                source="huggingface-papers",
                name="Event-Synchronous Skill Weaving",
                url="https://huggingface.co/papers/2601.00001",
                description="Aggregated abstract",
                extra={"arxiv_id": "2601.00001"},
            )
        )
        self.assertEqual(item.url, "https://arxiv.org/abs/2601.00001")
        self.assertEqual(
            item.raw["discovery_url"],
            "https://huggingface.co/papers/2601.00001",
        )

    def test_origin_merge_cannot_downgrade_primary_url_or_identity(self) -> None:
        direct = TechnicalEvidence(
            source="arxiv",
            evidence_type=EvidenceType.PRIMARY_PAPER,
            title="Event-Synchronous Skill Weaving",
            url="https://arxiv.org/abs/2601.00001",
            summary="short",
            identifiers={"arxiv": "2601.00001"},
            raw={
                "origin_priority": 3,
                "origin_kind": "technical_report",
                "origin_classification_reason": "explicit_document_metadata",
                "publisher_tier": "established",
                "publisher_evidence": "verified official source",
                "priority_researcher_match": True,
                "frontier_domains": ["embodied_robotics"],
            },
        )
        aggregator = TechnicalEvidence(
            source="huggingface-papers",
            evidence_type=EvidenceType.PRIMARY_PAPER,
            title=direct.title,
            url="https://huggingface.co/papers/2601.00001",
            summary="a much longer aggregated description",
            identifiers={"arxiv": "2601.00001"},
            raw={
                "origin_priority": 0,
                "origin_kind": "research_paper",
                "publisher_tier": "unknown",
                "frontier_domains": ["world_spatial_models"],
            },
        )
        merged = _merge_origins([direct, aggregator])[0]
        self.assertEqual(merged.url, direct.url)
        self.assertEqual(merged.summary, aggregator.summary)
        self.assertEqual(merged.raw["origin_priority"], 3)
        self.assertEqual(merged.raw["origin_kind"], "technical_report")
        self.assertEqual(merged.raw["publisher_tier"], "established")
        self.assertEqual(
            merged.raw["origin_classification_reason"],
            "explicit_document_metadata",
        )
        self.assertTrue(merged.raw["priority_researcher_match"])
        self.assertEqual(
            set(merged.raw["frontier_domains"]),
            {"embodied_robotics", "world_spatial_models"},
        )

    def test_generic_titles_cannot_merge_unrelated_primary_materials(self) -> None:
        first = TechnicalEvidence(
            source="lab-a",
            evidence_type=EvidenceType.TECHNICAL_BLOG,
            title="Technical Report",
            url="https://lab-a.example/report",
        )
        second = TechnicalEvidence(
            source="lab-b",
            evidence_type=EvidenceType.TECHNICAL_BLOG,
            title="Technical Report",
            url="https://lab-b.example/report",
        )
        self.assertEqual(len(_merge_origins([first, second])), 2)

    def test_landscape_covers_full_ai_technical_stack(self) -> None:
        landscape = load_landscape()
        domain_ids = {item["id"] for item in landscape["domains"]}
        self.assertEqual(
            set(landscape["required_domain_ids"]),
            domain_ids,
        )
        queries = " ".join(item["query"] for item in arxiv_query_plan()).casefold()
        self.assertNotIn('all:"', queries)
        self.assertIn('ti:"world model"', queries)
        self.assertIn('abs:"world model"', queries)
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

    def test_system_report_fallback_does_not_require_report_in_title(self) -> None:
        without_comment = SYSTEM_REPORT_ATOM.replace(
            "<arxiv:comment>Technical report</arxiv:comment>",
            "",
        )
        item = ArxivSource(
            lookback_days=14,
            seed_arxiv_ids=[],
            reference_time=FIXTURE_REFERENCE_TIME,
        )._parse_atom_feed(without_comment)[0]
        self.assertEqual(item.extra["origin_kind"], "technical_report")
        self.assertEqual(
            item.extra["origin_classification_reason"],
            "inferred_system_scope_report",
        )

    def test_explicit_report_metadata_sets_document_priority(self) -> None:
        item = ArxivSource(
            lookback_days=14,
            seed_arxiv_ids=[],
            reference_time=FIXTURE_REFERENCE_TIME,
        )._parse_atom_feed(SYSTEM_REPORT_ATOM)[0]
        self.assertEqual(item.extra["origin_kind"], "technical_report")
        self.assertEqual(item.extra["origin_priority"], 3)
        self.assertEqual(
            item.extra["origin_classification_reason"],
            "explicit_document_metadata:technical report",
        )

    def test_priority_researcher_lane_does_not_depend_on_known_technical_terms(self) -> None:
        plans = arxiv_priority_author_query_plan(
            ["Fei-Fei Li", "Yann LeCun"],
            chunk_size=1,
        )
        self.assertEqual(len(plans), 2)
        self.assertIn('au:"Fei-Fei Li"', plans[0]["query"])
        self.assertNotIn("world model", plans[0]["query"].casefold())

        item = ArxivSource(
            lookback_days=60,
            seed_arxiv_ids=[],
            reference_time=FIXTURE_REFERENCE_TIME,
        )._parse_atom_feed(
            OLD_RESEARCH_PAPER_ATOM,
            query_group="priority_researchers",
        )[0]
        self.assertEqual(item.extra["frontier_domains"], [])
        self.assertEqual(item.extra["origin_priority"], 2)
        self.assertTrue(item.extra["priority_researcher_match"])

    def test_report_search_hit_does_not_force_an_ordinary_paper_into_report_mode(self) -> None:
        ordinary = OLD_RESEARCH_PAPER_ATOM.replace(
            "Event-Synchronous Skill Weaving",
            "Calibration for Small Robot Policies",
        ).replace(
            "We present a new computational process for learning reusable behaviors.",
            "We compare against a technical report and improve calibration on one benchmark.",
        ).replace(
            "<author><name>Blair Singh</name></author>\n"
            "    <author><name>Casey Park</name></author>\n"
            "    <author><name>Fei-Fei Li</name></author>",
            "",
        )
        item = ArxivSource(
            lookback_days=60,
            seed_arxiv_ids=[],
            reference_time=FIXTURE_REFERENCE_TIME,
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

    def test_report_recall_lane_discards_unverified_query_false_positive(self) -> None:
        ordinary = OLD_RESEARCH_PAPER_ATOM.replace(
            "Event-Synchronous Skill Weaving",
            "Calibration for Small Robot Policies",
        ).replace(
            "We present a new computational process for learning reusable behaviors.",
            "We compare against a technical report and improve calibration on one benchmark.",
        )
        source = ArxivSource(
            lookback_days=7,
            high_signal_lookback_days=60,
            seed_arxiv_ids=[],
            reference_time=FIXTURE_REFERENCE_TIME,
        )
        source._request = AsyncMock(return_value=MagicMock(text=ordinary))

        received = asyncio.run(
            source._fetch_query(
                MagicMock(),
                "technical report",
                force_technical_report=True,
                query_group="technical_reports",
                lookback_days=60,
            )
        )

        self.assertEqual(received, [])
        self.assertEqual(source.technical_query_false_positives, 1)

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

    def test_old_item_falls_outside_normal_week_but_exact_seed_can_backfill(self) -> None:
        ordinary_author_atom = OLD_RESEARCH_PAPER_ATOM.replace(
            "<author><name>Fei-Fei Li</name></author>",
            "<author><name>Drew Morgan</name></author>",
        )
        weekly = ArxivSource(
            lookback_days=7,
            seed_arxiv_ids=[],
            reference_time=FIXTURE_REFERENCE_TIME,
        )
        self.assertEqual(weekly._parse_atom_feed(ordinary_author_atom), [])
        seeded = weekly._parse_atom_feed(
            ordinary_author_atom,
            query_group="explicit_seed",
            ignore_lookback=True,
        )
        self.assertEqual(len(seeded), 1)
        self.assertTrue(seeded[0].extra["explicit_seed"])
        # 精确补录只改变可发现性，不伪造重点研究者身份或编辑资格。
        self.assertFalse(seeded[0].extra["priority_researcher_match"])

    def test_recent_revision_of_old_paper_is_recalled(self) -> None:
        recent_revision = (
            FIXTURE_REFERENCE_TIME - timedelta(days=1)
        ).strftime("%Y-%m-%dT%H:%M:%SZ")
        xml = OLD_RESEARCH_PAPER_ATOM.replace(
            "2026-01-18T17:59:59Z", recent_revision
        )
        source = ArxivSource(
            lookback_days=7,
            seed_arxiv_ids=[],
            reference_time=FIXTURE_REFERENCE_TIME,
        )
        self.assertEqual(len(source._parse_atom_feed(xml)), 1)

    def test_arxiv_html_hydration_extracts_document_people_and_project(self) -> None:
        html = """
        <html><head>
          <meta name="citation_author" content="Alex Chen">
          <meta name="citation_author" content="Blair Singh">
          <meta name="citation_author" content="Casey Park">
          <meta name="citation_author_institution" content="Example University">
        </head><body>
          <div class="ltx_authors">
            <span class="ltx_personname"><a href="https://alex.example">Alex Chen</a></span>
          </div>
          <span class="ltx_note">Alex Chen, Blair Singh and Casey Park contributed equally.</span>
          <article><p>The fast controller reacts asynchronously while the slow controller predicts action chunks.</p>
          <a href="https://project-atlas.example/">Project page</a>
          <a href="https://github.com/example-lab/project-atlas">Code</a></article>
        </body></html>
        """
        parsed = parse_arxiv_html(html, base_url="https://arxiv.org/html/2601.00001")
        self.assertIn("fast controller", parsed["document_excerpt"])
        self.assertIn("Example University", parsed["affiliations"])
        self.assertEqual(
            parsed["author_profile_urls"]["Alex Chen"],
            "https://alex.example",
        )
        self.assertIn(
            "https://github.com/example-lab/project-atlas",
            parsed["github_repositories"],
        )
        self.assertEqual(parsed["author_roles"]["Alex Chen"], "共同第一作者")

    def test_researcher_selection_keeps_three_leads_last_and_priority_people(self) -> None:
        evidence = TechnicalEvidence(
            source="arxiv",
            evidence_type=EvidenceType.PRIMARY_PAPER,
            title="Event-Synchronous Skill Weaving",
            url="https://arxiv.org/abs/2601.00001",
            authors=[
                "Alex Chen",
                "Blair Singh",
                "Casey Park",
                "Other Author",
                "Fei-Fei Li",
                "Trevor Darrell",
            ],
            organization="Example University; Stanford",
            raw={
                "author_roles": {
                    "Alex Chen": "共同第一作者",
                    "Blair Singh": "共同第一作者",
                    "Casey Park": "共同第一作者",
                }
            },
        )
        profiles = _seed_profiles(evidence, [], 6)
        by_name = {profile.name: profile for profile in profiles}
        self.assertTrue(
            {"Alex Chen", "Blair Singh", "Casey Park", "Fei-Fei Li", "Trevor Darrell"}
            <= set(by_name)
        )
        self.assertEqual(by_name["Casey Park"].role, "共同第一作者")
        focused = _seed_profiles(evidence, [], 3)
        self.assertEqual(
            [profile.name for profile in focused],
            ["Alex Chen", "Fei-Fei Li", "Trevor Darrell"],
        )

    def test_collective_team_author_is_publisher_not_person_profile(self) -> None:
        evidence = TechnicalEvidence(
            source="arxiv",
            evidence_type=EvidenceType.PRIMARY_PAPER,
            title="Frontier System Report",
            url="https://arxiv.org/abs/2601.00002",
            authors=[
                "Atlas Research Team",
                "Alex Chen",
                "Blair Singh",
                "Casey Park",
                "Senior Author",
            ],
            organization="Example Research Lab",
        )
        profiles = _seed_profiles(evidence, [], 5)
        by_name = {profile.name: profile for profile in profiles}
        self.assertNotIn("Atlas Research Team", by_name)
        self.assertNotIn("Senior Author", by_name)
        self.assertIn("Alex Chen", by_name)
        self.assertEqual(
            by_name["Alex Chen"].role,
            "第一位具名作者/贡献角色待核验",
        )

    def test_project_page_maps_author_profiles_roles_and_public_email(self) -> None:
        parsed = parse_project_page(
            """
            <a href="https://alex.example">Alex Chen*</a>
            <a href="https://blair.example">Blair Singh*</a>
            <a href="mailto:casey@university.edu">Casey Park*</a>
            <p>* Equal Contribution</p>
            """,
            base_url="https://project-atlas.example/",
            author_names=["Alex Chen", "Blair Singh", "Casey Park"],
        )
        self.assertEqual(
            parsed["author_profile_urls"]["Alex Chen"],
            "https://alex.example",
        )
        self.assertEqual(
            parsed["author_public_emails"]["Casey Park"],
            "casey@university.edu",
        )
        self.assertEqual(parsed["author_roles"]["Blair Singh"], "共同第一作者")

    def test_high_potential_observe_candidate_gets_review_not_auto_report(self) -> None:
        extraction = _observe_extraction(
            {
                "origin_priority": 2,
                "priority_researcher_match": True,
            }
        )
        self.assertTrue(is_priority_review(extraction))
        self.assertEqual(len(cluster_extractions([extraction])), 1)
        # 复核通道只让它进入深挖；最终报告仍必须重新通过 final Rubric。
        candidate = cluster_extractions([extraction])[0]
        candidate.rubric_assessment = extraction.rubric_assessment
        score_candidate(candidate)
        self.assertFalse(is_reportable(candidate))

    def test_manual_seed_is_recall_only_not_editorial_priority(self) -> None:
        extraction = _observe_extraction(
            {
                "origin_priority": 3,
                "explicit_seed": True,
                "origin_kind": "research_paper",
                "publisher_tier": "unknown",
            }
        )
        self.assertFalse(is_priority_review(extraction))
        self.assertEqual(cluster_extractions([extraction]), [])

    def test_unknown_technical_report_does_not_inherit_review_priority(self) -> None:
        extraction = _observe_extraction(
            {
                "origin_priority": 3,
                "origin_kind": "technical_report",
                "publisher_tier": "unknown",
            }
        )
        self.assertFalse(is_priority_review(extraction))
        self.assertEqual(cluster_extractions([extraction]), [])

    def test_established_official_report_gets_bounded_re_review(self) -> None:
        extraction = _observe_extraction(
            {
                "origin_priority": 3,
                "origin_kind": "technical_report",
                "publisher_tier": "established",
            }
        )
        self.assertTrue(is_priority_review(extraction))
        self.assertEqual(len(cluster_extractions([extraction])), 1)

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
            key="2601.00001",
            route_family="tactile control",
            mechanism="tactile feedback",
            keywords=["tactile", "feedback"],
        )
        item.evidence[0].title = "Event-Synchronous Skill Weaving"
        found = _parse_results(
            {
                "request_id": "req-web",
                "results": [
                    {
                        "title": "Event-Synchronous Skill Weaving paper notes",
                        "url": "https://researcher.example/posts/event-synchronous-notes/",
                        "content": "Event-Synchronous Skill Weaving analysis",
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
