from __future__ import annotations

import unittest

import config
from paradigms.models import ParadigmCandidate, ResearcherProfile
from paradigms.reputation import (
    resolve_organization,
    source_identity,
    source_link_allowed,
    verified_priority_researcher,
)
from paradigms.scoring import _assess_publisher
from research_watchlist import (
    ESTABLISHED_ORGANIZATION_IDS,
    ORGANIZATIONS_BY_ID,
    RESEARCH_SOURCES,
)
from sources.priority_research_source import _looks_like_research_link, _origin_kind


class ResearchWatchlistTest(unittest.TestCase):
    def test_catalog_references_are_consistent(self) -> None:
        self.assertTrue(ESTABLISHED_ORGANIZATION_IDS <= ORGANIZATIONS_BY_ID.keys())
        source_urls = {item["url"] for item in RESEARCH_SOURCES}
        self.assertIn("https://wayve.ai/science/", source_urls)
        self.assertIn("https://www.tri.global/publications", source_urls)
        self.assertTrue(all(item["owner"] in ORGANIZATIONS_BY_ID for item in RESEARCH_SOURCES))

    def test_required_domestic_and_global_organizations_exist(self) -> None:
        for organization_id in (
            "openai",
            "anthropic",
            "google-deepmind",
            "google-research",
            "meta-fair",
            "baidu",
            "tencent-hunyuan",
            "bytedance-seed",
            "alibaba-qwen",
            "moonshot",
            "zhipu",
            "modelbest",
            "stepfun",
            "360-ai",
            "tsinghua-tsail",
            "tsinghua-evar",
            "stanford-svl",
            "nyu-cilvr",
        ):
            self.assertIn(organization_id, ORGANIZATIONS_BY_ID)

    def test_alias_resolution_is_exact_not_substring(self) -> None:
        self.assertEqual(resolve_organization("月之暗面")["id"], "moonshot")
        self.assertEqual(resolve_organization("Zhipu AI / Z.ai")["id"], "zhipu")
        self.assertEqual(resolve_organization("Tencent ARC Lab")["id"], "tencent-hunyuan")
        self.assertIsNone(resolve_organization("OpenAI competitor"))
        self.assertIsNone(resolve_organization("unfair research group"))
        self.assertIsNone(resolve_organization("清华大学"))

    def test_short_alias_only_matches_complete_field(self) -> None:
        self.assertEqual(resolve_organization("1X")["id"], "one-x")
        self.assertIsNone(resolve_organization("1x faster robotics"))

    def test_monitored_organization_does_not_become_established(self) -> None:
        self.assertEqual(resolve_organization("AgiBot")["tier"], "monitored")
        self.assertEqual(resolve_organization("OpenAI")["tier"], "established")

    def test_priority_researcher_requires_public_identity(self) -> None:
        bare = ResearcherProfile(name="Fei-Fei Li")
        verified = ResearcherProfile(
            name="Fei-Fei Li",
            profile_urls={"homepage": "https://profiles.stanford.edu/fei-fei-li"},
        )
        self.assertIsNone(verified_priority_researcher(bare))
        self.assertEqual(verified_priority_researcher(verified)["name"], "Fei-Fei Li")

    def test_priority_researcher_only_produces_verified_tier(self) -> None:
        candidate = ParadigmCandidate(
            key="jepa",
            name="JEPA",
            thesis="预测抽象状态",
            problem_shift="从像素生成转向表征预测",
            mechanism="联合嵌入预测",
            researchers=[
                ResearcherProfile(
                    name="Yann LeCun",
                    profile_urls={"homepage": "https://cds.nyu.edu/team/yann-lecun/"},
                )
            ],
        )
        _assess_publisher(candidate)
        self.assertEqual(candidate.publisher_tier, "verified")
        self.assertTrue(any("重点研究者身份" in item for item in candidate.publisher_evidence))

    def test_builtin_source_has_owner_but_custom_source_does_not(self) -> None:
        _, owner, tier = source_identity("https://www.kimi.com/blog/")
        self.assertEqual(owner["id"], "moonshot")
        self.assertEqual(tier, "established")
        _, owner, tier = source_identity("https://www.moonshot.ai/")
        self.assertEqual(owner["id"], "moonshot")
        self.assertEqual(tier, "verified")
        source, owner, tier = source_identity("https://example.com/research")
        self.assertIsNone(source)
        self.assertIsNone(owner)
        self.assertEqual(tier, "verified")

    def test_priority_source_link_domain_boundary(self) -> None:
        index = "https://www.anthropic.com/research"
        self.assertTrue(source_link_allowed(index, "https://www.anthropic.com/research/test"))
        self.assertTrue(source_link_allowed(index, "https://arxiv.org/abs/2607.00001"))
        self.assertFalse(source_link_allowed(index, "https://marketing.example/test"))
        custom = "https://custom.example/research"
        self.assertTrue(source_link_allowed(custom, "https://custom.example/report"))
        self.assertFalse(source_link_allowed(custom, "https://arxiv.org/abs/2607.00001"))
        kimi = "https://www.kimi.com/blog/"
        self.assertTrue(source_link_allowed(kimi, "https://github.com/MoonshotAI/Kimi-K3"))
        self.assertTrue(source_link_allowed(kimi, "https://arxiv.org/abs/2607.24653"))

    def test_chinese_research_links_and_report_kind(self) -> None:
        self.assertTrue(
            _looks_like_research_link(
                "新一代具身世界模型技术报告",
                "https://example.cn/research/report-2026",
            )
        )
        self.assertEqual(_origin_kind("新一代系统技术报告", "https://example.cn/a"), "technical_report")

    def test_merge_mode_keeps_defaults_and_deduplicates(self) -> None:
        self.assertEqual(
            config._merge_watchlist(["OpenAI", "Anthropic"], ["OpenAI", "新实验室"], "merge"),
            ["OpenAI", "Anthropic", "新实验室"],
        )
        self.assertEqual(
            config._merge_watchlist(["OpenAI"], ["自定义实验室"], "replace"),
            ["自定义实验室"],
        )


if __name__ == "__main__":
    unittest.main()
