"""Regression cases from the first production memo; no credentials/network."""
import copy
import json
import unittest
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx
import config
from paradigms.analyzer import ParadigmAnalyzer
from paradigms.enrichment import EvidenceEnricher, _official_repository_evidence
from paradigms.evidence_association import countable_evidence, references_origin
from paradigms.models import EvidenceType, ResearcherProfile, TechnicalEvidence, candidate_from_dict
from paradigms.rubric import RubricContractError, validate_model_answers
from paradigms.scoring import _admission_gate, _momentum_score, score_candidate
from reports.paradigm_generator import _has_internal_evidence_labels, _momentum_evidence
from runtime_clock import research_window
from sources.arxiv_document_source import parse_arxiv_html
from sources.openreview_source import OpenReviewSource
from sources.researcher_profile_source import ResearcherProfileClient
from sources.social_web_search_source import SocialWebSearchClient
from sources.official_repository_release_source import _linked_primary_material
from tests.test_paradigm_pipeline import candidate, rubric_assessment


def primary():
    return TechnicalEvidence(source="arxiv", evidence_type=EvidenceType.PRIMARY_PAPER,
        title="TacitVLA: Implicit Reasoning for Robot Actions", url="https://arxiv.org/abs/2610.17540",
        identifiers={"arxiv": "2610.17540"}, authors=["Ada Researcher"], organization="Unknown Lab")


class EvidenceRepairTests(unittest.TestCase):
    def test_exact_paper_citation_in_an_awesome_list_is_not_an_implementation(self):
        item = candidate([primary()])
        aggregate = TechnicalEvidence(source="official-repository-release", evidence_type=EvidenceType.IMPLEMENTATION,
            title="TacitVLA: Implicit Reasoning for Robot Actions", url="https://github.com/VerifiedLab/awesome-vla",
            summary="A curated list of robot papers including 2610.17540.", metrics={"stars": 5000},
            raw={"relationship": "official_release_repository", "independence": "publisher"})
        self.assertTrue(references_origin(item, aggregate))
        self.assertFalse(countable_evidence(item, aggregate))
        EvidenceEnricher._attach_support(item, [aggregate])
        self.assertEqual(len(item.evidence), 1)
    def test_corporate_legal_page_is_not_an_external_technical_origin(self):
        self.assertEqual(_linked_primary_material("microsoft", "[Trademarks](https://www.microsoft.com/en-us/legal/intellectualproperty/trademarks)", ""), "")

    def test_generic_keywords_and_foreign_publishers_do_not_lend_adoption(self):
        item = candidate([primary()])
        item.researchers = []
        unrelated = TechnicalEvidence(source="official-repository-release", evidence_type=EvidenceType.IMPLEMENTATION,
            title="Deep GEMM action generation memory", url="https://github.com/ForeignLab/gemm",
            organization="OpenAI", metrics={"stars": 500, "forks": 49}, raw={"publisher_tier": "established", "independence": "official", "relationship": "official_release_repository"})
        before = _momentum_score(item)
        self.assertFalse(references_origin(item, unrelated))
        EvidenceEnricher._attach_support(item, [unrelated])
        self.assertEqual(len(item.evidence), 1)
        item.evidence.append(unrelated)  # Simulate old saved pollution.
        score_candidate(item)
        self.assertEqual(item.publisher_tier, "unknown")
        self.assertTrue(_admission_gate(item)[0])  # No audience veto; adoption facts remain unverified.
        self.assertEqual(_momentum_score(item), before)
        self.assertEqual(_momentum_evidence(item), [])

    def test_typed_ids_and_versions_associate_but_cross_namespace_does_not(self):
        item = candidate([primary()])
        related = copy.deepcopy(primary())
        related.identifiers = {"arxiv": "2610.17540v2"}
        related.title, related.summary = "Other index label", ""
        self.assertTrue(references_origin(item, related))
        related.identifiers = {"doi": "2610.17540"}
        self.assertFalse(references_origin(item, related))

    def test_paper_code_needs_current_work_anchor_not_html_chrome(self):
        item = candidate([primary()])
        official = _official_repository_evidence(item.evidence[0], "Lab/tacit-vla")
        self.assertTrue(countable_evidence(item, official))
        official.raw["origin_fingerprint"] = "another-work"
        official.raw["origin_work_ids"] = {}
        self.assertFalse(countable_evidence(item, official))
        chrome = _official_repository_evidence(item.evidence[0], "arXiv/html_feedback/issues")
        self.assertFalse(countable_evidence(item, chrome))
        legacy = copy.deepcopy(official)
        legacy.raw = {"relationship": "paper_linked_repository", "independence": "official"}
        self.assertFalse(countable_evidence(item, legacy))

    def test_arxiv_navigation_references_and_dependencies_are_not_paper_code(self):
        parsed = parse_arxiv_html('<nav><a href="https://github.com/arXiv/html_feedback/issues">Code</a></nav>'
            '<article><p>Our <a href="https://github.com/Lab/tacit-vla">Code</a>.</p>'
            '<p>We simulate with <a href="https://github.com/NVlabs/alpasim">Alpasim</a>.</p>'
            '<section class="ltx_bibliography"><a href="https://github.com/Other/project">Code</a></section>'
            '<p>Final boundary remains visible.</p></article>'
            '<footer><a href="https://github.com/brucemiller/LaTeXML">Code</a></footer>', base_url=primary().url)
        self.assertEqual(parsed["github_repositories"], ["https://github.com/Lab/tacit-vla"])
        self.assertIn("Final boundary", parsed["document_excerpt"])

    def test_complete_unknown_answers_are_valid_but_omissions_duplicates_are_not(self):
        answers = [{"criterion_id": row["criterion_id"], "answer": "unknown", "evidence": "材料未披露"}
            for row in rubric_assessment(stage="screening")["answers"]]
        validate_model_answers("screening", ["architecture"], answers)
        for bad in (answers[:-1], answers + answers[:1], None):
            with self.assertRaises(RubricContractError):
                validate_model_answers("screening", ["architecture"], bad)

    def test_internal_labels_are_not_public_prose(self):
        for text in ("事实（source_fact）", "`unknown`", "解释 interpretive_compression"):
            self.assertTrue(_has_internal_evidence_labels(text))
        self.assertFalse(_has_internal_evidence_labels("原文没有披露这个接口；这里只是用于理解的简化实例。"))


class InterfaceRepairTests(unittest.IsolatedAsyncioTestCase):
    async def test_malformed_identity_payload_is_failed_not_completed_without_match(self):
        client = httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(200, json={})))
        with patch.object(config, "OPENALEX_API_KEY", "offline"), patch("sources.researcher_profile_source.httpx.AsyncClient", return_value=client):
            profiles = await ResearcherProfileClient().enrich(primary(), [], limit=1)
        self.assertEqual(profiles[0].identity_search_status, "failed")
        self.assertEqual(profiles[0].contact_search_status, "failed")
        self.assertFalse(profiles[0].contact_lookup_completed)

    async def test_openreview_long_first_query_cannot_starve_other_topics(self):
        calls = []
        def handler(request):
            query, offset = request.url.params["term"], int(request.url.params["offset"])
            calls.append((query, offset))
            return httpx.Response(200, json={"notes": [{"id": f"{query}-{offset}",
                "cdate": 1735689600000, "content": {"title": {"value": query}, "venueid": {"value": "Venue"}}}], "count": 10000})
        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        source = OpenReviewSource(venues=["Venue"], searches=["world model", "reasoning", "memory"], limit=1, max_pages_per_query=2)
        with research_window(datetime(2026, 10, 14, tzinfo=timezone.utc)), patch("sources.openreview_source.httpx.AsyncClient", return_value=client), patch("sources.openreview_source.OPENREVIEW_REQUEST_SPACING_SECONDS", 0):
            self.assertEqual(await source.safe_fetch(), [])
        self.assertEqual(len(calls), 6)
        self.assertEqual({query for query, _ in calls}, {"world model", "reasoning", "memory"})
        self.assertEqual(source.limited_queries, 3)
        self.assertEqual(source.coverage()["status"], "partial")
        self.assertTrue(all(row["status"] == "page_limit" for row in source.query_progress.values()))

    async def test_tavily_receipt_distinguishes_raw_hits_filtering_and_zero(self):
        observed = []
        def handler(request):
            observed.append(json.loads(request.content))
            return httpx.Response(200, json={"request_id": "test-request", "usage": {"credits": 1}, "results": [
                {"title": "TacitVLA: Implicit Reasoning for Robot Actions analysis", "url": "https://review.example/tacit", "content": "2610.17540"},
                {"title": "Unrelated work", "url": "https://review.example/other", "content": "Different subject"}]})
        item = candidate([primary()])
        with patch.object(config, "TAVILY_API_KEY", "offline"), patch.object(config, "TAVILY_SOCIAL_SEARCH_ENABLED", True), patch.object(config, "TAVILY_MONTHLY_CREDIT_LIMIT", 0):
            async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
                results = await SocialWebSearchClient().search(client, item)
        self.assertEqual(len(results), 1)
        self.assertFalse(observed[0]["exact_match"])
        self.assertNotIn('"TacitVLA: Implicit', observed[0]["query"])
        receipt = item.community_search_receipts["tavily_social_web"]
        self.assertEqual((receipt["raw_result_count"], receipt["retained_result_count"], receipt["filtered_result_count"]), (2, 1, 1))
        self.assertEqual(receipt["confirmed_credits"], 1)
        self.assertEqual(receipt["request_id"], "test-request")
        candidate_from_dict(item.to_dict())

    async def test_homepage_biography_without_meta_is_read_as_professional_background(self):
        profile = ResearcherProfile(name="Ada Researcher", profile_urls={"homepage": "https://ada.example"})
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(200,
            text="<nav>Unrelated</nav><p>I am a doctoral student researching robot learning and world models at Example Lab.</p>"))) as client:
            await ResearcherProfileClient()._homepage_contacts(client, profile)
        self.assertIn("doctoral student", profile.public_bio_excerpt)
        self.assertEqual(profile.contact_search_status, "completed")

    async def test_rubric_retry_names_missing_questions_instead_of_generic_error(self):
        answers = [{key: row[key] for key in ("criterion_id", "answer", "evidence")} for row in rubric_assessment(stage="screening")["answers"]]
        seed = {"canonical_name": "Mechanism", "problem_shift": "改变接口", "mechanism": "作用机制", "innovation_types": ["architecture"], "rubric_answers": answers}
        missing = copy.deepcopy(seed)
        missing["rubric_answers"] = answers[:-1]
        def response(value):
            return SimpleNamespace(choices=[SimpleNamespace(finish_reason="stop", message=SimpleNamespace(content=json.dumps({"hypotheses": [value]})))])
        create = AsyncMock(side_effect=[response(missing), response(seed)])
        client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
        result = await ParadigmAnalyzer(client=client, model="offline").extract(primary())
        self.assertEqual(len(result), 1)
        self.assertIn(answers[-1]["criterion_id"], create.call_args.kwargs["messages"][0]["content"])
        self.assertEqual(create.await_count, 2)
