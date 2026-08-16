from __future__ import annotations

import unittest
from datetime import datetime, timezone

from paradigms.models import EvidenceType, ORIGIN_EVIDENCE_TYPES
from sources.curated_intelligence_source import _looks_like_short_concept, _parse_feed
from sources.official_repository_release_source import (
    _repository_native_origin,
    _repository_origin_qualification,
)


def rss_item(*, title: str, body: str, author: str = "Lilian Weng") -> str:
    now = datetime.now(timezone.utc).strftime("%a, %d %b %Y %H:%M:%S +0000")
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<rss xmlns:dc="http://purl.org/dc/elements/1.1/" version="2.0"><channel>
  <item><title>{title}</title><link>https://example.com/post</link>
  <description><![CDATA[{body}]]></description><dc:creator>{author}</dc:creator>
  <pubDate>{now}</pubDate></item>
</channel></rss>"""


class CuratedIntelligenceSourceTests(unittest.TestCase):
    def test_verified_kol_feed_can_seed_concept_origin_and_profiles(self) -> None:
        record = {
            "id": "lilian-weng",
            "name": "Lilian Weng",
            "aliases": ("Lilian Weng",),
            "homepage": "https://lilianweng.github.io/",
            "x_handle": "lilianweng",
            "origin_policy": "concept_origin",
        }
        items, filtered = _parse_feed(
            rss_item(
                title="A new memory architecture for language model agents",
                body=(
                    "We describe a training algorithm, memory representation, "
                    "inference protocol, experiment and falsifiable mechanism."
                ),
            ),
            feed_url="https://lilianweng.github.io/index.xml",
            lookback_days=7,
            record=record,
            forum=None,
        )
        self.assertEqual(filtered, 0)
        self.assertEqual(items[0].evidence_type, EvidenceType.CONCEPT_ESSAY)
        self.assertTrue(items[0].raw["priority_researcher_match"])
        self.assertEqual(
            items[0].raw["author_public_profiles"]["Lilian Weng"]["x"],
            "https://x.com/lilianweng",
        )

    def test_secondary_newsletter_never_becomes_origin(self) -> None:
        record = {
            "id": "commentary",
            "name": "Commentator",
            "aliases": ("Commentator",),
            "homepage": "https://example.com/",
            "origin_policy": "secondary_only",
        }
        items, _ = _parse_feed(
            rss_item(
                title="Language model reasoning architecture discussion",
                body="Analysis of a new training algorithm and inference mechanism.",
                author="Commentator",
            ),
            feed_url="https://example.com/feed",
            lookback_days=7,
            record=record,
            forum=None,
        )
        self.assertEqual(items[0].evidence_type, EvidenceType.SECONDARY_INTERPRETATION)
        self.assertNotIn(items[0].evidence_type, ORIGIN_EVIDENCE_TYPES)
        self.assertEqual(items[0].raw["publisher_tier"], "unknown")
        self.assertEqual(items[0].raw["commentator_tier"], "verified")

    def test_lesswrong_threshold_is_lower_bound_not_exact_metric(self) -> None:
        forum = {
            "forum": "lesswrong",
            "view": "frontpage",
            "selection": "frontpage_karma_gte_30",
            "karma_lower_bound": 30,
        }
        items, _ = _parse_feed(
            rss_item(
                title="Recursive self-improvement for AI agents",
                body="A testable learning algorithm with a feedback protocol and evaluation.",
                author="Forum Author",
            ),
            feed_url="https://www.lesswrong.com/feed.xml",
            lookback_days=7,
            record=None,
            forum=forum,
        )
        self.assertEqual(items[0].metrics, {"karma_lower_bound": 30})
        self.assertIn("not exact", items[0].raw["metric_boundary"])
        self.assertEqual(items[0].raw["publisher_tier"], "unknown")

    def test_lesswrong_alignment_crosspost_has_one_stable_identity(self) -> None:
        forum = {
            "forum": "lesswrong",
            "view": "curated",
            "selection": "curated",
            "karma_lower_bound": 0,
        }
        xml = rss_item(
            title="A language model learning mechanism",
            body="A training algorithm changes the memory representation during inference.",
        ).replace("https://example.com/post", "https://www.lesswrong.com/posts/abc123/a-post")
        lesswrong, _ = _parse_feed(
            xml,
            feed_url="https://www.lesswrong.com/feed.xml",
            lookback_days=7,
            record=None,
            forum=forum,
        )
        alignment_xml = xml.replace("www.lesswrong.com", "www.alignmentforum.org")
        alignment, _ = _parse_feed(
            alignment_xml,
            feed_url="https://www.alignmentforum.org/feed.xml",
            lookback_days=7,
            record=None,
            forum={**forum, "forum": "alignment-forum"},
        )
        self.assertEqual(lesswrong[0].fingerprint, alignment[0].fingerprint)

    def test_non_ai_post_is_filtered_before_model_tokens(self) -> None:
        items, filtered = _parse_feed(
            rss_item(
                title="A recipe for sourdough",
                body="A detailed kitchen method and experiment with flour.",
            ),
            feed_url="https://example.com/feed",
            lookback_days=7,
            record={
                "id": "writer",
                "name": "Writer",
                "homepage": "https://example.com/",
                "origin_policy": "concept_origin",
            },
            forum=None,
        )
        self.assertEqual(items, [])
        self.assertEqual(filtered, 1)

    def test_x_short_concept_requires_ai_object_and_intervention(self) -> None:
        self.assertFalse(_looks_like_short_concept("RSI will change everything."))
        self.assertTrue(
            _looks_like_short_concept(
                "A self-improving language model agent can treat failed tool calls as "
                "training feedback: update a verifier-backed memory after each episode, "
                "then use that representation during the next inference pass."
            )
        )

    def test_repository_native_origin_requires_structured_mechanism(self) -> None:
        repository = {
            "name": "self-improving-agent",
            "full_name": "lab/self-improving-agent",
            "description": "A language model agent learning from execution feedback",
            "topics": ["llm", "reinforcement-learning"],
            "html_url": "https://github.com/lab/self-improving-agent",
            "created_at": "2026-08-15T00:00:00Z",
            "stargazers_count": 70,
            "forks_count": 5,
        }
        readme = "# Architecture\n## Training\n" + (
            "The language model agent uses a training algorithm, feedback objective, "
            "memory representation and inference protocol. " * 20
        )
        qualified, reason = _repository_origin_qualification(repository, readme)
        self.assertTrue(qualified, reason)
        origin = _repository_native_origin(
            configured={"login": "lab", "owner": "lab"},
            repository=repository,
            readme=readme,
            organization="Lab",
            publisher_tier="verified",
            reason=reason,
        )
        self.assertEqual(origin.evidence_type, EvidenceType.ORIGINAL_IMPLEMENTATION)
        self.assertEqual(origin.raw["independence"], "publisher")

    def test_awesome_repository_is_not_an_origin(self) -> None:
        qualified, reason = _repository_origin_qualification(
            {"name": "awesome-agents", "description": "AI agent list", "topics": []},
            "# Awesome agents\n" + "curated list of language model papers " * 100,
        )
        self.assertFalse(qualified)
        self.assertIn("列表", reason)


if __name__ == "__main__":
    unittest.main()
