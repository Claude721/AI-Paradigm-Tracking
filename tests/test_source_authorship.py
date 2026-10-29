"""Source bylines are deliverable facts, independent of optional biography."""
import copy
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import httpx

from paradigms.authorship import source_authorship, candidate_authorship
from paradigms.models import (EvidenceType, TechnicalEvidence, candidate_from_dict,
                              key_researcher_profiles)
from reports.paradigm_generator import (_attach_researcher_index, _attribution_boundary,
                                        _candidate_dossier, _compact_route_dossier,
                                        _ungrounded_url_violations)
from sources.arxiv_document_source import ArxivDocumentClient, parse_arxiv_html
from sources.researcher_profile_source import _seed_profiles
from tests.test_paradigm_pipeline import candidate
from tests import test_v0_delivery as fixture
from database.paradigm_store import _deep_checkpoint_input_signature


def paper(**kwargs):
    return TechnicalEvidence(source="arxiv", evidence_type=EvidenceType.PRIMARY_PAPER,
        title="Mechanism Paper", url="https://arxiv.org/abs/2610.00001",
        identifiers={"arxiv": "2610.00001"}, **kwargs)


class AuthorshipParsingTests(unittest.TestCase):
    def test_explicit_author_groups_preserve_individual_affiliations(self):
        html = '''<article>
        <div class="ltx_creator ltx_role_author"><span class="ltx_personname">Alice Chen</span>
        <span class="ltx_role_affiliation">Example University</span></div>
        <div class="ltx_creator ltx_role_author"><span class="ltx_personname">Bob Lee</span>
        <span class="ltx_role_affiliation">Startup.ai</span></div>
        <section class="ltx_bibliography"><span class="ltx_personname">Unrelated Author</span></section>
        <p>Technical text.</p></article>'''
        parsed = parse_arxiv_html(html, base_url="https://arxiv.org/html/2610.00001")
        self.assertEqual(parsed["authors"], ["Alice Chen", "Bob Lee"])
        self.assertEqual(parsed["author_affiliations"], {"Alice Chen": ["Example University"], "Bob Lee": ["Startup.ai"]})

    def test_numbered_refs_do_not_broadcast_every_institution_to_every_author(self):
        html = '''<article><div class="ltx_authors"><div class="ltx_creator">
        <span class="ltx_personname">Alice Chen<sup><a href="#aff1">1</a></sup></span>
        <span class="ltx_personname">Bob Lee<sup><a href="#aff2">2</a></sup></span>
        <div class="ltx_role_affiliation" id="aff1">Example University</div>
        <div class="ltx_role_affiliation" id="aff2">Startup.ai</div>
        </div></div></article>'''
        parsed = parse_arxiv_html(html, base_url="https://arxiv.org/html/2610.00001")
        self.assertEqual(parsed["authors"], ["Alice Chen", "Bob Lee"])
        self.assertEqual(parsed["author_affiliations"], {"Alice Chen": ["Example University"], "Bob Lee": ["Startup.ai"]})

    def test_comma_joined_names_and_shared_affiliation_are_structural_not_one_person(self):
        html = '''<article><div class="ltx_creator"><span class="ltx_personname">
        Alice Chen *, Bob Lee *, Carol Park</span>
        <span class="ltx_role_affiliation">Startup.ai</span></div>
        <p>* These authors contributed equally.</p></article>'''
        parsed = parse_arxiv_html(html, base_url="https://arxiv.org/html/2610.00001")
        self.assertEqual(parsed["authors"], ["Alice Chen", "Bob Lee", "Carol Park"])
        self.assertEqual(parsed["author_roles"]["Alice Chen"], "共同第一作者")
        self.assertEqual(parsed["author_roles"]["Bob Lee"], "共同第一作者")
        self.assertEqual(parsed["author_affiliations"]["Alice Chen"], ["Startup.ai"])

    def test_citation_metadata_repeats_author_to_bind_institutions(self):
        html = '''<meta name="citation_author" content="Alice Chen">
        <meta name="citation_author_institution" content="Example University">
        <meta name="citation_author" content="Bob Lee">
        <meta name="citation_author_institution" content="Startup.ai">'''
        parsed = parse_arxiv_html(html, base_url="https://arxiv.org/html/2610.00001")
        self.assertEqual(parsed["author_affiliations"]["Bob Lee"], ["Startup.ai"])

    def test_bibliography_alone_never_supplies_a_byline(self):
        parsed = parse_arxiv_html('<article><section class="ltx_bibliography"><span class="ltx_personname">Unrelated Author</span></section></article>',
                                  base_url="https://arxiv.org/html/2610.00001")
        self.assertEqual(parsed["authors"], [])
        self.assertEqual(parsed["author_roles"], {})

    def test_collective_signature_position_survives_html_hydration(self):
        html = '''<article><div class="ltx_creator"><span class="ltx_personname">Atlas Research Team</span></div>
        <div class="ltx_creator"><span class="ltx_personname">Alice Chen</span></div>
        <div class="ltx_creator"><span class="ltx_personname">Bob Lee</span></div></article>'''
        parsed = parse_arxiv_html(html, base_url="https://arxiv.org/html/2610.00001")
        self.assertEqual(parsed["authors"], ["Atlas Research Team", "Alice Chen", "Bob Lee"])
        original = paper(authors=parsed["authors"], raw={"author_roles": parsed["author_roles"]})
        self.assertEqual(source_authorship(original)[0].role, "第一位具名作者/贡献角色待核验")
        self.assertNotIn("Atlas Research Team", [p.name for p in _seed_profiles(original, [], 3)])

    def test_metadata_author_order_precedes_partial_html_name_detection(self):
        html = '''<meta name="citation_author" content="Alice Chen"><meta name="citation_author" content="Bob Lee">
        <meta name="citation_author" content="Carol Park"><article><span class="ltx_personname">Alice Chen</span>
        <span class="ltx_personname">Carol Park</span></article>'''
        parsed = parse_arxiv_html(html, base_url="https://arxiv.org/html/2610.00001")
        self.assertEqual(parsed["authors"], ["Alice Chen", "Bob Lee", "Carol Park"])

    def test_unrelated_equal_contribution_phrase_in_body_does_not_define_author_roles(self):
        html = '''<article><span class="ltx_personname">Alice Chen *</span><span class="ltx_personname">Bob Lee *</span>
        <h2>Abstract</h2><p>A cited work uses equal contribution.</p></article>'''
        parsed = parse_arxiv_html(html, base_url="https://arxiv.org/html/2610.00001")
        self.assertNotEqual(parsed["author_roles"]["Alice Chen"], "共同第一作者")

    def test_old_excerpt_recovery_is_work_scoped_pure_and_not_employment(self):
        original = paper(authors=["Alice Chen", "Bob Lee"], organization="Publishing Lab", raw={
            "document_excerpt": "Mechanism Paper Alice Chen Affiliation: Example University Email: a@example.edu Bob Lee Affiliation: Startup.ai Abstract Technical text.",
            "author_roles": {"Alice Chen": "第一作者", "Bob Lee": "末位作者/资深作者线索"},
        })
        before = copy.deepcopy(original.to_dict())
        facts = source_authorship(original)
        self.assertEqual(facts[0].affiliations, ("Example University",))
        self.assertEqual(facts[1].affiliations, ("Startup.ai",))
        self.assertEqual(facts[0].role, "署名首位作者")
        seeds = _seed_profiles(original, [], 3)
        self.assertEqual(key_researcher_profiles(seeds, 1)[0].name, "Alice Chen")
        self.assertTrue(all(not person.current_affiliation for person in seeds))
        self.assertTrue(all(not person.prior_affiliations for person in seeds))
        self.assertEqual(original.to_dict(), before)

    def test_collectives_and_model_prose_cannot_create_authors(self):
        original = paper(authors=["Example Visual Generation Team"], organization="Publishing Lab",
            summary="Alice Chen leads this technology.", raw={"author_roles": {"Example Visual Generation Team": "第一作者"}})
        self.assertEqual(source_authorship(original), [])
        self.assertEqual(_seed_profiles(original, [], 3), [])
        self.assertEqual(source_authorship(paper(authors=["Google DeepMind"])), [])

    def test_old_comma_joined_role_recovers_names_but_not_group_employment(self):
        original = paper(raw={"author_roles": {"Alice Chen * , Bob Lee † \\dagger": "第一作者"}})
        self.assertEqual([fact.name for fact in source_authorship(original)], ["Alice Chen", "Bob Lee"])
        self.assertTrue(all(not fact.affiliations for fact in source_authorship(original)))

    def test_unbound_flat_affiliations_stay_unknown(self):
        original = paper(authors=["Alice Chen", "Bob Lee"], organization="Publishing Lab",
                         raw={"affiliations": ["Example University", "Startup.ai"]})
        self.assertTrue(all(not fact.affiliations for fact in source_authorship(original)))

    def test_alphabetically_sorted_old_roles_do_not_reorder_the_paper_byline(self):
        original = paper(raw={"author_roles": {"Alpha Lee": "末位作者/资深作者线索", "Zoe Chen": "第一作者"},
                              "document_excerpt": "Mechanism Paper Zoe Chen Affiliation: Startup.ai Alpha Lee Affiliation: Example University Abstract Body."})
        facts = source_authorship(original)
        self.assertEqual(facts[0].name, "Zoe Chen")
        self.assertEqual(facts[0].role, "署名首位作者")
        self.assertEqual(facts[0].affiliations, ("Startup.ai",))

    def test_numbered_old_excerpt_only_binds_explicit_indices(self):
        original = paper(authors=["Alice Chen", "Bob Lee"], raw={
            "document_excerpt": "Paper Alice Chen 1* Bob Lee 2* Affiliation: 1 Example University 2 Startup Research * These authors contributed equally. Abstract Body."})
        facts = source_authorship(original)
        self.assertEqual(facts[0].affiliations, ("Example University",))
        self.assertEqual(facts[1].affiliations, ("Startup Research",))
        self.assertEqual(facts[0].role, "共同第一作者")

    def test_embedded_digits_are_not_silently_deleted_to_manufacture_a_person(self):
        self.assertEqual(source_authorship(paper(authors=["font12size author"])), [])

    def test_role_only_old_snapshot_does_not_absorb_an_unlisted_authors_institution(self):
        original = paper(raw={"author_roles": {"Alice Chen": "第一作者", "Zoe Park": "末位作者/资深作者线索"},
            "document_excerpt": "Paper Alice Chen Affiliation: Example University Bob Lee Affiliation: Wrong Lab Zoe Park Affiliation: Final University Abstract Body."})
        facts = source_authorship(original)
        self.assertEqual(facts[0].affiliations, ("Example University",))
        self.assertNotIn("Wrong Lab", str(facts))


class AuthorshipDeliveryTests(unittest.TestCase):
    def test_no_lookup_still_renders_source_byline_and_institution_with_safe_links(self):
        original = paper(authors=["Alice Chen"], raw={"author_affiliations": {"Alice Chen": ["Example University"]}})
        value = candidate([original]); value.researchers = []
        restored = candidate_from_dict(value.to_dict())
        content = _attach_researcher_index("## 本期研究 Memo\n\n正文。", [restored])
        self.assertIn("Alice Chen", content)
        self.assertIn("Example University", content)
        self.assertIn("本工作署名机构", content)
        self.assertIn("未执行或未完成", content)
        self.assertNotIn("现有材料未能可靠归因到关键自然人", content)
        self.assertEqual(_ungrounded_url_violations(content, [restored]), [])
        for dossier in [_candidate_dossier(restored), _compact_route_dossier(restored)]:
            self.assertEqual(dossier["source_authorship"][0]["name"], "Alice Chen")
            self.assertEqual(dossier["source_authorship"][0]["affiliations"], ("Example University",))

    def test_excluded_material_never_lends_a_name_or_institution(self):
        value = candidate([paper(authors=["Alice Chen"])])
        unrelated = paper(authors=["Unrelated Person"], raw={"author_affiliations": {"Unrelated Person": ["Wrong Lab"]}})
        value.excluded_evidence = [unrelated]
        self.assertEqual([fact.name for fact in candidate_authorship(value)], ["Alice Chen"])
        self.assertNotIn("Wrong Lab", _attach_researcher_index("正文。", [value]))

    def test_anonymous_material_remains_explicitly_unknown(self):
        value = candidate([paper()]); value.researchers = []
        self.assertIn("未能可靠归因到关键自然人", _attribution_boundary(value))


class AuthorshipHydrationTests(unittest.IsolatedAsyncioTestCase):
    async def test_hydration_persists_authors_and_per_author_affiliations(self):
        original = paper()
        response = httpx.Response(200, text='''<article><div class="ltx_creator">
            <span class="ltx_personname">Alice Chen</span><span class="ltx_role_affiliation">Startup.ai</span>
            </div><p>Technical mechanism.</p></article>''', request=httpx.Request("GET", "https://arxiv.org/html/2610.00001"))
        context = MagicMock()
        context.__aenter__ = AsyncMock(return_value=SimpleNamespace(get=AsyncMock(return_value=response)))
        context.__aexit__ = AsyncMock(return_value=False)
        with patch("sources.arxiv_document_source.httpx.AsyncClient", return_value=context):
            await ArxivDocumentClient().hydrate(original)
        self.assertEqual(original.authors, ["Alice Chen"])
        self.assertEqual(original.raw["author_affiliations"], {"Alice Chen": ["Startup.ai"]})


class PeopleSchedulingTests(unittest.IsolatedAsyncioTestCase):
    setUp = fixture.V0DeliveryTests.setUp
    process = fixture.V0DeliveryTests.process

    async def test_unvisited_durable_dependency_does_not_veto_healthy_people_stage(self):
        process = self.process(accepted=True)
        process.delivery_contract_version = 5
        synthesize = process.synthesizer.run

        async def with_late_dependency(items):
            values = await synthesize(items)
            pending = copy.deepcopy(values[0])
            pending.key = "late-reconciled-upstream-pending"
            pending.status = "pending_deep"
            pending.deep_checkpoint_stage = ""
            pending.rubric_assessment = {}
            self.store.campaigns.include(process.campaign.campaign_id, "deep", [
                (pending.key, _deep_checkpoint_input_signature(pending), pending.to_dict())])
            return values

        process.synthesizer.run = with_late_dependency
        stats = await process.run(reference_time=fixture.NOW)
        self.assertGreater(stats["research_campaign"]["deep_unattempted"], 0)
        self.assertTrue(process.enricher.enrich_people.await_count)
        self.assertTrue(process.trajectory.run.await_count)
        self.assertTrue(stats["delivery_ready"])
        # Optional enrichment must not manufacture an attempt for the dependency.
        receipt = next(row for row in stats["research_campaign"]["execution_receipts"]
                       if row["key"] == "late-reconciled-upstream-pending")
        self.assertEqual(receipt["outcome"], "unattempted")
