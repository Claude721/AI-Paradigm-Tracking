"""Identity and cross-week profile preservation without live lookups."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

import config
from database.paradigm_store import ParadigmStore
from paradigms.enrichment import _merge_profiles
from paradigms.models import (
    EvidenceType, ParadigmCandidate, ResearcherProfile, TechnicalEvidence,
    delivery_researcher_profiles, key_researcher_profiles,
)
from paradigms.researcher_identity import merge_researcher_profiles
from reports.paradigm_generator import _attach_researcher_index
from sources.researcher_profile_source import ResearcherProfileClient, _seed_profiles


def origin(name: str, *, authors: list[str] | None = None) -> TechnicalEvidence:
    return TechnicalEvidence(
        source="arxiv", evidence_type=EvidenceType.PRIMARY_PAPER,
        title=name, url=f"https://arxiv.org/abs/{name}",
        authors=authors or [],
    )


class ResearcherIdentityTests(unittest.TestCase):
    def test_same_name_is_not_identity_and_distinct_profiles_survive(self) -> None:
        old = ResearcherProfile(
            name="Alex Lee", identifiers={"orcid": "0000-0001"},
            profile_urls={"orcid": "https://orcid.org/0000-0001"},
            contact_search_notes=["已检索公开主页"],
        )
        new = ResearcherProfile(
            name="Alex Lee", identifiers={"orcid": "0000-0002"},
        )
        merged = merge_researcher_profiles([old], [new])
        self.assertEqual(len(merged), 2)
        self.assertEqual(len(_merge_profiles([old], [new])), 2)
        self.assertEqual(old.contact_search_notes, ["已检索公开主页"])

    def test_matching_public_identifier_preserves_contact_search(self) -> None:
        old = ResearcherProfile(
            name="Alex Lee", identifiers={"orcid": "0000-0001"},
            public_email="alex@example.edu",
            public_email_source="https://example.edu/people/alex",
            contact_search_notes=["已检索公开主页"],
        )
        new = ResearcherProfile(
            name="A. Lee",
            profile_urls={"orcid": "https://orcid.org/0000-0001"},
            role="通讯作者",
        )
        merged = merge_researcher_profiles([old], [new])
        self.assertEqual(len(merged), 1)
        self.assertEqual(merged[0].public_contacts["email"], "alex@example.edu")
        self.assertEqual(merged[0].role, "通讯作者")
        self.assertTrue(merged[0].contact_lookup_completed)

    def test_shared_team_mailbox_does_not_prove_person_identity(self) -> None:
        contacts = {
            "public_email": "team@example.edu",
            "public_email_source": "https://example.edu/team",
        }
        left = ResearcherProfile(name="Alex Lee", **contacts)
        right = ResearcherProfile(name="Casey Park", **contacts)
        self.assertEqual(len(merge_researcher_profiles([left], [right])), 2)
        same_name = ResearcherProfile(name="Alex Lee", **contacts)
        self.assertEqual(len(merge_researcher_profiles([left], [same_name])), 2)

    def test_author_seeding_keeps_ambiguous_historical_person(self) -> None:
        evidence = origin("paper", authors=["Alex Lee"])
        evidence.raw["author_openalex_map"] = {
            "Alex Lee": "https://openalex.org/A2"
        }
        old = ResearcherProfile(
            name="Alex Lee", identifiers={"openalex": "https://openalex.org/A1"},
            contact_search_notes=["已检索公开主页"],
        )
        profiles = _seed_profiles(evidence, [old], 1)
        self.assertEqual(len(profiles), 2)
        self.assertEqual(
            {profile.identifiers.get("openalex") for profile in profiles},
            {"https://openalex.org/A1", "https://openalex.org/A2"},
        )

    def test_lookup_limit_does_not_erase_prior_route_lead(self) -> None:
        evidence = origin("paper", authors=["New Author"])
        old = ResearcherProfile(
            name="Prior Lead", identifiers={"orcid": "0000-0001"},
            contact_search_notes=["已检索公开主页"],
        )
        profiles = _seed_profiles(evidence, [old], 1)
        self.assertEqual(
            [profile.name for profile in profiles],
            ["New Author", "Prior Lead"],
        )

    def test_same_key_history_keeps_verified_profile_and_lineage(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = ParadigmStore(Path(directory) / "radar.db")
            first = origin("first")
            second = origin("second")
            store.mark_evidence([first, second], analyzed=False)
            old = ParadigmCandidate(
                key="route-a", name="Route A", thesis="", problem_shift="",
                mechanism="", evidence=[first], lineage_path=["early"],
                researchers=[ResearcherProfile(
                    name="Alex Lee", identifiers={"orcid": "0000-0001"},
                    contact_search_notes=["已检索公开主页"],
                )],
            )
            store.save_candidates([old])
            current = ParadigmCandidate(
                key="route-a", name="Route A", thesis="", problem_shift="",
                mechanism="", evidence=[second], lineage_path=["later"],
                researchers=[ResearcherProfile(
                    name="Alex Lee", identifiers={"orcid": "0000-0002"},
                )],
            )
            result = store.attach_history([current])[0]
            self.assertEqual({"early", "later"}, set(result.lineage_path))
            self.assertEqual(len(result.researchers), 2)
            self.assertEqual(len(result.evidence), 2)
            self.assertTrue(any(
                profile.contact_lookup_completed
                for profile in result.researchers
            ))

    def test_distinct_same_name_leads_survive_key_selection_and_index(self) -> None:
        profiles = [
            ResearcherProfile(
                name="Alex Lee", role="第一作者",
                current_affiliation=f"Lab {index}",
                identifiers={"orcid": f"0000-000{index}"},
                profile_urls={"orcid": f"https://orcid.org/0000-000{index}"},
            )
            for index in (1, 2)
        ]
        self.assertEqual(len(key_researcher_profiles(profiles, 3)), 2)
        self.assertEqual(len(delivery_researcher_profiles(profiles, 3)), 2)
        route = ParadigmCandidate(
            key="route-a", name="Route A", route_family="Route A",
            thesis="", problem_shift="", mechanism="",
            evidence=[origin("paper")], researchers=profiles,
        )
        report = _attach_researcher_index("## 本期研究 Memo\n\n正文。", [route])
        self.assertEqual(report.count("**Alex Lee**"), 2)
        self.assertIn("https://orcid.org/0000-0001", report)
        self.assertIn("https://orcid.org/0000-0002", report)


class ResearcherActiveLookupTests(unittest.IsolatedAsyncioTestCase):
    async def test_same_name_historical_profile_is_not_reidentified(self) -> None:
        evidence = origin("paper", authors=["Alex Lee"])
        evidence.raw["author_openalex_map"] = {
            "Alex Lee": "https://openalex.org/A2"
        }
        old = ResearcherProfile(
            name="Alex Lee", identifiers={"openalex": "https://openalex.org/A1"},
            public_email="old@example.edu",
            public_email_source="https://example.edu/old",
        )
        client = ResearcherProfileClient()
        looked_up = []

        async def homepage(_http, profile):
            looked_up.append(profile.identifiers.get("openalex"))

        client._homepage_contacts = AsyncMock(side_effect=homepage)
        with patch.object(config, "OPENALEX_API_KEY", ""):
            profiles = await client.enrich(evidence, [old], limit=1)
        self.assertEqual(looked_up, ["https://openalex.org/A2"])
        self.assertEqual(len(profiles), 2)
        self.assertEqual(old.public_email, "old@example.edu")

    async def test_matching_raw_author_id_waits_for_current_work_check(self) -> None:
        evidence = origin("paper", authors=["Alex Lee"])
        evidence.raw["author_openalex_map"] = {
            "Alex Lee": "https://openalex.org/A1"
        }
        old = ResearcherProfile(
            name="Alex Lee", identifiers={"openalex": "https://openalex.org/A1"},
            public_email="old@example.edu",
            public_email_source="https://example.edu/old",
        )
        client = ResearcherProfileClient()
        client._homepage_contacts = AsyncMock()
        with patch.object(config, "OPENALEX_API_KEY", ""):
            profiles = await client.enrich(evidence, [old], limit=1)
        self.assertEqual(len(profiles), 2)
        self.assertEqual(profiles[0].public_email, "")

    async def test_verified_current_work_id_can_inherit_old_contact(self) -> None:
        evidence = origin("paper", authors=["Alex Lee"])
        evidence.raw["author_openalex_map"] = {
            "Alex Lee": "https://openalex.org/A1"
        }
        old = ResearcherProfile(
            name="Alex Lee", identifiers={"openalex": "https://openalex.org/A1"},
            public_email="old@example.edu",
            public_email_source="https://example.edu/old",
        )
        client = ResearcherProfileClient()

        async def verify(_http, profile, _evidence):
            profile.contact_search_notes.append(
                "当前论文题目与 OpenAlex 作者实体交叉核验通过"
            )

        client._openalex = AsyncMock(side_effect=verify)
        client._homepage_contacts = AsyncMock()
        with patch.object(config, "OPENALEX_API_KEY", "offline-test"):
            profiles = await client.enrich(evidence, [old], limit=1)
        self.assertEqual(len(profiles), 1)
        self.assertEqual(profiles[0].public_contacts["email"], "old@example.edu")


if __name__ == "__main__":
    unittest.main()
