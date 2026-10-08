"""Offline contract: discovery leads cannot become route-level uptake."""

from __future__ import annotations

import unittest
import tempfile
import copy
from pathlib import Path
from unittest.mock import patch

from database.paradigm_store import ParadigmStore
from paradigms.analyzer import ParadigmSynthesizer
from paradigms.enrichment import EvidenceEnricher, _dedupe_evidence
from paradigms.models import (
    EvidenceType, ParadigmCandidate, TechnicalEvidence,
    is_verified_substantive_discussion,
)
from paradigms.rubric import objective_answers, substantive_secondary
from paradigms.scoring import _admission_gate, _momentum_score
from reports.paradigm_generator import _candidate_dossier, _compact_route_dossier


def route() -> ParadigmCandidate:
    return ParadigmCandidate(
        key="route-evidence", name="Latent action state transitions",
        thesis="test", problem_shift="test", mechanism="latent action update",
        evidence=[TechnicalEvidence(
            source="arxiv", evidence_type=EvidenceType.PRIMARY_PAPER,
            title="Latent action state transitions",
            url="https://arxiv.org/abs/2609.00001",
            authors=["Alice Researcher"], organization="Example Lab",
        )],
    )


def forum_record(
    *, source="reddit", author="Bob Reviewer", body="Detailed analysis of latent action state updates and what changes during training.",
) -> TechnicalEvidence:
    return TechnicalEvidence(
        source=source,
        evidence_type=EvidenceType.COMMUNITY_DISCUSSION,
        title="Latent action state transitions discussion",
        url=(
            "https://www.reddit.com/r/MachineLearning/comments/abc/"
            if source == "reddit"
            else "https://news.ycombinator.com/item?id=123"
        ),
        summary=body,
        authors=[author],
        metrics={"score": 200, "comments": 40},
        raw={"relationship": "official_reddit_exact_work_search"},
    )


def model_selects(candidate: ParadigmCandidate, index: int = 1) -> None:
    with patch("paradigms.analyzer._validate_mental_model"), patch(
        "paradigms.analyzer.evaluate_rubric",
        return_value={"decision": "report", "innovation_types": ["architecture"]},
    ):
        ParadigmSynthesizer._apply_synthesis_payload(
            candidate,
            {
                "innovation_types": ["architecture"],
                "mental_model": {"observation_axis": "状态转移"},
                "substantive_uptake_evidence_indices": [index],
            },
            visible_evidence_indices={index},
        )


class EvidenceValidationTests(unittest.TestCase):
    def test_title_only_forum_hit_cannot_pass_admission(self):
        candidate = route()
        baseline_score = _momentum_score(candidate)
        lead = forum_record(body="")
        candidate.evidence.append(lead)
        self.assertEqual(substantive_secondary(candidate)[0], 0)
        self.assertFalse(_admission_gate(candidate)[0])
        self.assertEqual(_momentum_score(candidate), baseline_score)
        model_selects(candidate)
        self.assertFalse(is_verified_substantive_discussion(lead, route_key=candidate.key))

    def test_full_body_model_audit_can_certify_independent_forum_discussion(self):
        candidate = route()
        discussion = forum_record()
        candidate.evidence.append(discussion)
        self.assertEqual(substantive_secondary(candidate)[0], 0)
        model_selects(candidate)
        self.assertTrue(is_verified_substantive_discussion(discussion, route_key=candidate.key))
        self.assertEqual(discussion.raw["substantive_uptake_source"], "synthesis-v1")
        self.assertTrue(_admission_gate(candidate)[0])
        self.assertEqual(substantive_secondary(candidate)[0], 1)

    def test_shared_support_audit_is_bound_to_one_route(self):
        first = route()
        second = route()
        second.key = "different-mechanism-route"
        shared = forum_record()
        EvidenceEnricher._attach_support(first, [shared])
        EvidenceEnricher._attach_support(second, [shared])
        self.assertIsNot(first.evidence[1], second.evidence[1])
        self.assertIsNot(first.evidence[1], shared)
        model_selects(first)
        self.assertTrue(is_verified_substantive_discussion(
            first.evidence[1], route_key=first.key
        ))
        self.assertFalse(is_verified_substantive_discussion(
            first.evidence[1], route_key=second.key
        ))
        self.assertFalse(is_verified_substantive_discussion(
            second.evidence[1], route_key=second.key
        ))
        self.assertEqual(substantive_secondary(second)[0], 0)

    def test_previously_certified_evidence_does_not_certify_second_route(self):
        first = route()
        first.evidence.append(forum_record())
        model_selects(first)
        second = route()
        second.key = "different-mechanism-route"
        second.evidence.append(first.evidence[1])
        self.assertFalse(is_verified_substantive_discussion(
            second.evidence[1], route_key=second.key
        ))
        self.assertFalse(_admission_gate(second)[0])
        self.assertEqual(substantive_secondary(second)[0], 0)

    def test_route_binding_survives_sqlite_snapshot(self):
        first = route()
        first.evidence.append(forum_record())
        model_selects(first)
        first.status = "pending_deep"
        second = route()
        second.key = "different-mechanism-route"
        second.evidence.append(first.evidence[1])
        second.status = "pending_deep"
        with tempfile.TemporaryDirectory() as directory:
            store = ParadigmStore(Path(directory) / "state.db")
            store.save_candidates([first, second])
            loaded = {item.key: item for item in store.load_pending_deep_candidates()}
        self.assertEqual(set(loaded), {first.key, second.key})
        self.assertEqual(substantive_secondary(loaded[first.key])[0], 1)
        self.assertEqual(substantive_secondary(loaded[second.key])[0], 0)

    def test_route_certification_is_a_report_signature_event(self):
        candidate = route()
        discussion = forum_record()
        discussion.raw.update({
            "relationship": "independent_mechanism_analysis",
            "independence": "independent",
        })
        candidate.evidence.append(discussion)
        lead_signature = candidate.report_signature
        discussion.raw.update({
            "substantive_uptake": True,
            "substantive_uptake_source": "synthesis-v1",
            "substantive_uptake_route_key": "another-route",
        })
        self.assertEqual(candidate.report_signature, lead_signature)
        discussion.raw["substantive_uptake_route_key"] = candidate.key
        self.assertNotEqual(candidate.report_signature, lead_signature)

    def test_author_self_post_and_search_index_cannot_be_certified(self):
        self_post = route()
        self_post.evidence.append(forum_record(author="Alice Researcher"))
        model_selects(self_post)
        self.assertFalse(is_verified_substantive_discussion(self_post.evidence[1], route_key=self_post.key))

        indexed = route()
        search_hit = forum_record()
        search_hit.source = "tavily-reddit"
        search_hit.raw["indexed_discovery_only"] = True
        indexed.evidence.append(search_hit)
        model_selects(indexed)
        self.assertFalse(is_verified_substantive_discussion(search_hit, route_key=indexed.key))

    def test_curated_label_without_attributable_article_is_only_a_lead(self):
        candidate = route()
        candidate.evidence.append(TechnicalEvidence(
            source="curated-kol-blog",
            evidence_type=EvidenceType.SECONDARY_INTERPRETATION,
            title="Third-party analysis",
            url="https://example.org/commentary",
            summary="This essay carefully analyzes latent action updates, training feedback, and limits.",
            authors=[],
            raw={
                "independence": "independent",
                "relationship": "independent_commentary",
            },
        ))
        model_selects(candidate)
        self.assertFalse(is_verified_substantive_discussion(
            candidate.evidence[1], route_key=candidate.key
        ))

    def test_model_cannot_certify_compact_or_foreign_host_record(self):
        compact = route()
        compact.evidence.append(forum_record())
        with patch("paradigms.analyzer._validate_mental_model"), patch(
            "paradigms.analyzer.evaluate_rubric",
            return_value={"decision": "report", "innovation_types": ["architecture"]},
        ):
            ParadigmSynthesizer._apply_synthesis_payload(
                compact,
                {"innovation_types": ["architecture"],
                 "mental_model": {"observation_axis": "状态转移"},
                 "substantive_uptake_evidence_indices": [1]},
                visible_evidence_indices=set(),
            )
        self.assertFalse(is_verified_substantive_discussion(compact.evidence[1], route_key=compact.key))

        foreign = route()
        record = forum_record()
        record.url = "https://example.net/search-result"
        foreign.evidence.append(record)
        model_selects(foreign)
        self.assertFalse(is_verified_substantive_discussion(record, route_key=foreign.key))

        index_page = route()
        record = forum_record()
        record.url = "https://www.reddit.com/search/?q=latent-action"
        index_page.evidence.append(record)
        model_selects(index_page)
        self.assertFalse(is_verified_substantive_discussion(
            record, route_key=index_page.key
        ))

    def test_unverified_replication_is_not_independent_validation(self):
        candidate = route()
        candidate.evidence.append(TechnicalEvidence(
            source="github", evidence_type=EvidenceType.INDEPENDENT_REPLICATION,
            title="same-name hit", url="https://github.com/example/unrelated",
            raw={"independence": "unverified"},
        ))
        self.assertEqual(substantive_secondary(candidate)[0], 0)
        validation = {
            row["criterion_id"]: row["answer"]
            for row in objective_answers(candidate)
        }
        self.assertNotEqual(
            validation["independent_validation"],
            "independent_replication_or_adoption",
        )

    def test_legacy_boolean_without_audit_provenance_is_not_substantive(self):
        candidate = route()
        legacy = forum_record()
        legacy.raw.update({
            "relationship": "independent_discussion",
            "independence": "independent",
            "substantive_uptake": True,
        })
        candidate.evidence.append(legacy)
        self.assertFalse(is_verified_substantive_discussion(legacy, route_key=candidate.key))
        self.assertEqual(substantive_secondary(candidate)[0], 0)
        self.assertFalse(_admission_gate(candidate)[0])

    def test_github_name_match_and_stars_do_not_create_implementation_uptake(self):
        candidate = route()
        baseline_score = _momentum_score(candidate)
        candidate.evidence.append(TechnicalEvidence(
            source="github", evidence_type=EvidenceType.IMPLEMENTATION,
            title="same-name-repository",
            url="https://github.com/example/same-name-repository",
            metrics={"stars": 500, "forks": 80},
            raw={
                "relationship": "name_and_mechanism_match",
                "independence": "unverified",
            },
        ))
        self.assertEqual(_momentum_score(candidate), baseline_score)
        self.assertFalse(_admission_gate(candidate)[0])
        validation = {
            row["criterion_id"]: row["answer"]
            for row in objective_answers(candidate)
        }
        self.assertEqual(validation["independent_validation"], "primary_claim_only")

    def test_scrubbed_prior_audit_survives_same_route_but_not_new_mechanism(self):
        candidate = route()
        prior = forum_record()
        prior.summary = ""
        prior.authors = []
        prior.raw = {
            "relationship": "independent_mechanism_analysis",
            "independence": "independent",
            "substantive_uptake": True,
            "substantive_uptake_source": "synthesis-v1",
            "substantive_uptake_route_key": candidate.key,
            "content_scrubbed": True,
        }
        candidate.evidence.append(prior)

        def resynthesize(mechanism: str | None = None) -> None:
            payload = {
                "innovation_types": ["architecture"],
                "mental_model": {"observation_axis": "状态转移"},
                "substantive_uptake_evidence_indices": [],
            }
            if mechanism is not None:
                payload["mechanism"] = mechanism
            with patch("paradigms.analyzer._validate_mental_model"), patch(
                "paradigms.analyzer.evaluate_rubric",
                return_value={"decision": "report", "innovation_types": ["architecture"]},
            ):
                ParadigmSynthesizer._apply_synthesis_payload(
                    candidate, payload, visible_evidence_indices=set()
                )

        resynthesize()
        self.assertTrue(is_verified_substantive_discussion(prior, route_key=candidate.key))
        resynthesize("different mechanism")
        self.assertFalse(is_verified_substantive_discussion(prior, route_key=candidate.key))

    def test_empty_repeat_does_not_erase_scrubbed_route_audit(self):
        candidate = route()
        prior = forum_record()
        candidate.evidence.append(prior)
        model_selects(candidate)
        prior.summary = ""
        prior.raw["content_scrubbed"] = True
        repeated = copy.deepcopy(prior)
        repeated.raw = {"relationship": "official_reddit_exact_work_search"}
        candidate.evidence = _dedupe_evidence(
            [candidate.evidence[0], prior, repeated], route_key=candidate.key
        )
        retained = candidate.evidence[1]
        self.assertTrue(retained.raw["content_scrubbed"])
        with patch("paradigms.analyzer._validate_mental_model"), patch(
            "paradigms.analyzer.evaluate_rubric",
            return_value={"decision": "report", "innovation_types": ["architecture"]},
        ):
            ParadigmSynthesizer._apply_synthesis_payload(
                candidate,
                {"innovation_types": ["architecture"],
                 "mental_model": {"observation_axis": "状态转移"},
                 "substantive_uptake_evidence_indices": []},
                visible_evidence_indices=set(),
            )
        self.assertTrue(is_verified_substantive_discussion(
            retained, route_key=candidate.key
        ))

        self_release = copy.deepcopy(repeated)
        self_release.raw = {
            "relationship": "author_self_release",
            "independence": "author",
        }
        downgraded = _dedupe_evidence(
            [prior, self_release], route_key=candidate.key
        )[0]
        self.assertFalse(is_verified_substantive_discussion(
            downgraded, route_key=candidate.key
        ))

    def test_report_input_downgrades_unsupported_community_claims(self):
        candidate = route()
        candidate.objective_momentum_signals = ["已在三个平台广泛扩散"]
        candidate.secondary_discussion_summary = "社区已经完成多个独立复现。"
        candidate.trend_interpretation = "跨平台热议，处于多团队扩散。"
        candidate.evidence.append(forum_record(body=""))
        candidate.evidence.append(TechnicalEvidence(
            source="github", evidence_type=EvidenceType.IMPLEMENTATION,
            title="Official code", url="https://github.com/lab/code",
            metrics={"stars": 55, "forks": 2},
            raw={
                "relationship": "paper_linked_repository",
                "independence": "official",
            },
        ))
        for dossier in (_candidate_dossier(candidate), _compact_route_dossier(candidate)):
            serialized = str(dossier)
            self.assertNotIn("广泛扩散", serialized)
            self.assertNotIn("多个独立复现", serialized)
            self.assertNotIn("跨平台热议", serialized)
            self.assertNotIn("/comments/abc/", serialized)
            self.assertIn("仅说明采用势能", serialized)
            self.assertIn("尚未核验到非作者主体", serialized)
        self.assertEqual(candidate.trend_interpretation, "跨平台热议，处于多团队扩散。")


if __name__ == "__main__":
    unittest.main()
