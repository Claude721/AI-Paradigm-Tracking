"""Pre-research opportunity selection, independent of technical validity.

Uses bounded work queries, free direction context and the existing cheap
qualification call. Queries are shared with deep work, not duplicated.
No signal means watch, not a negative technical judgment or a completed study.
"""
from __future__ import annotations

import copy
import json
import re
import asyncio
import sqlite3
import time
from datetime import timedelta

from database.origin_state import source_signature
from paradigms.models import ORIGIN_EVIDENCE_TYPES, EvidenceType, ParadigmCandidate, nonnegative_number, _evidence_datetime
from paradigms.evidence_association import countable_evidence, references_origin
from paradigms.momentum import momentum_assessment
from paradigms.propagation import certify_diffusion
from runtime_clock import research_now, research_days
from sources.direction_signal_source import context_for_report, REGISTRY

VERSION = "research-focus-v2"


def benefit_receipt(value, origin):
    """A model's benefit classification needs literal, quantitative source text.

    This is an author-reported comparison, not independently verified efficacy.
    Missing/malformed benefit data never invalidates the technical prefilter.
    """
    if not isinstance(value, dict) or value.get("kind") != "measured_comparison":
        return {}
    baseline, result = value.get("baseline_quote"), value.get("result_quote")
    text = origin.summary[:2400]
    if (not isinstance(baseline, str) or not isinstance(result, str)
            or not 8 <= len(baseline) <= 300 or not 8 <= len(result) <= 300
            or baseline == result or baseline not in text or result not in text
            or not re.search(r"\d", result)):
        return {}
    return {"kind": "author_reported_measured_comparison", "baseline_quote": baseline,
            "result_quote": result, "url": origin.url,
            "source_revision": source_signature(origin),
            "boundary": "摘要声称的可定位收益对照，尚待深挖验证，不是独立验证或生产收益保证"}


def probe(origin):
    return ParadigmCandidate(key="focus-" + origin.fingerprint, name=origin.title,
        thesis="", problem_shift="", mechanism="", why_now="", evidence=[copy.deepcopy(origin)])


def receipt_current(origin):
    value = origin.raw.get("research_focus", {})
    return (isinstance(value, dict) and value.get("version") == VERSION
            and value.get("selection_policy") == json.loads(REGISTRY.read_text(encoding="utf-8"))["version"]
            and value.get("source_revision") == source_signature(origin)
            and value.get("reference_time") == research_now().isoformat()
            and value.get("window_days") == research_days())


def admitted(candidate):
    """No receipt is legacy/unknown, not an implicit current admission."""
    return any(receipt_current(item) and item.raw["research_focus"].get("decision") == "research"
               for item in candidate.evidence if item.evidence_type in ORIGIN_EVIDENCE_TYPES)


def assess(provisional, *, benefit=None, source_health=None, origin=None):
    origin = origin or next(item for item in provisional.evidence if item.evidence_type in ORIGIN_EVIDENCE_TYPES
                            and countable_evidence(provisional, item))
    policy = json.loads(REGISTRY.read_text(encoding="utf-8"))["research_selection"]
    basis = []
    certify_diffusion(provisional)
    ledger = momentum_assessment(provisional, record_limit=1000)
    current = []
    cutoff = research_now() - timedelta(days=research_days())
    for record in ledger["records"]:
        date = _evidence_datetime(record["published_at"])
        if date and cutoff <= date <= research_now():
            current.append(record)
    for record in current:
        metrics = record["metrics"]
        if record["kind"] == "external_action":
            basis.append({"kind": "external_action", "url": record["url"]})
        elif record["kind"] in {"public_attention", "publisher_exposure"} and max(
                (nonnegative_number(metrics.get(key)) for key in
                 ("score", "upvotes", "comments", "likes", "retweets", "reposts", "replies")), default=0) >= policy["attention_metric_min"]:
            basis.append({"kind": "measured_attention", "url": record["url"], "metrics": metrics})
    public = [record for record in current if record["kind"] == "public_attention"]
    actors = {actor.strip().casefold() for record in public for actor in record["actors"] if actor.strip()}
    if len(public) >= policy["public_posts_min"] and len(actors) >= policy["public_posts_min"]:
        basis.append({"kind": "multiple_public_posts", "urls": [record["url"] for record in public]})
    for item in provisional.evidence:
        date = _evidence_datetime(item.published_at)
        if (item.evidence_type == EvidenceType.ORIGINAL_IMPLEMENTATION
                and item.source == "official-repository-release" and countable_evidence(provisional, item)
                and (nonnegative_number(item.metrics.get("stars")) >= policy["repository_stars_min"]
                     or nonnegative_number(item.metrics.get("forks")) >= policy["repository_forks_min"])):
            basis.append({"kind": "original_implementation_interest", "url": item.url,
                          "scope": "官方原生实现的关注，不代表独立复现"})
        if (item.source == "huggingface-papers" and item.evidence_type == EvidenceType.COMMUNITY_DISCUSSION
                and references_origin(provisional, item) and countable_evidence(provisional, item)
                and date and cutoff <= date <= research_now()
                and max(nonnegative_number(item.metrics.get("upvotes")), nonnegative_number(item.metrics.get("comments")))
                    >= policy["attention_metric_min"]):
            basis.append({"kind": "paper_platform_attention", "url": item.url,
                "metrics": {key: item.metrics[key] for key in ("upvotes", "comments") if key in item.metrics},
                "scope": "平台收藏/评论，不是独立技术验证；同步收录本身不算势能"})
    # Official code activity is useful, but mere existence/brand is not interest.
    for record in ledger["records"]:
        if record["kind"] in {"publisher_activity", "external_action"} and (
                nonnegative_number(record["metrics"].get("stars")) >= policy["repository_stars_min"]
                or nonnegative_number(record["metrics"].get("forks")) >= policy["repository_forks_min"]):
            basis.append({"kind": "repository_interest", "url": record["url"], "metrics": record["metrics"]})
    direction = context_for_report(provisional)
    for topic in direction.get("topics", []):
        if topic.get("selection_scope") != "specific_direction":
            continue  # Generic AI-domain financing cannot admit every paper.
        activity = topic.get("paper_activity", {})
        growing = (activity.get("status") == "complete" and type(activity.get("current_count")) is int
                   and type(activity.get("previous_count")) is int
                   and activity["current_count"] - activity["previous_count"] >= policy["paper_absolute_increase_min"]
                   and activity["current_count"] >= max(activity["previous_count"] * policy["paper_ratio_min"], policy["paper_absolute_increase_min"]))
        # Corroboration across resource/activity dimensions, not one broad job.
        dimensions = [kind for kind in ("media", "jobs") if topic.get(kind)]
        if growing:
            dimensions.append("paper_activity")
        if len(dimensions) >= policy["direction_dimensions_min"]:
            basis.append({"kind": "direction_resource_activity", "topic": topic["topic"],
                "dimensions": dimensions, "scope": "方向级筛选线索，不是本工作受到关注或机制获验证",
                "urls": [x["url"] for kind in ("media", "jobs") for x in topic.get(kind, [])]
                    + ([activity["url"]] if growing else [])})
    if benefit and benefit.get("source_revision") == source_signature(origin):
        basis.append(benefit)
    decision = "research" if basis else "watch"
    return {"version": VERSION, "selection_policy": json.loads(REGISTRY.read_text(encoding="utf-8"))["version"],
        "source_revision": source_signature(origin),
        "reference_time": research_now().isoformat(), "window_days": research_days(),
        "decision": decision, "basis": basis,
        "source_health": source_health or direction.get("source_health", {}),
        "reason": "已有可核验关注/投入或可定位收益对照，进入技术研究" if basis
            else "本轮尚无足够关注/投入或可定位收益对照；保留观察，非技术淘汰；缺失/异常不等于没有势能"}


async def assess_candidates(candidates, supporting, *, enricher, analyzer, deadline, stats):
    """Use actual route identities and retained evidence, isolating bad peers.

    This phase only decides opportunity. No final Rubric or completion receipt
    is manufactured when a query, candidate, or assessment is incomplete.
    """
    from research_archive import review_archive
    from run_audit import run_audit
    from paradigms.async_utils import gather_scoped
    import config
    health = {} if config.PARADIGM_DIRECTION_SIGNALS_ENABLED else {"direction_context": "disabled"}
    remaining = deadline - time.monotonic()
    if config.PARADIGM_DIRECTION_SIGNALS_ENABLED and remaining > 0:
        try:
            await asyncio.wait_for(enricher.direction_signals.attach(candidates), timeout=min(45, remaining))
        except Exception as exc:
            health["direction_context"] = "unknown_" + type(exc).__name__
    benefits = getattr(analyzer, "origin_benefits", {})
    query_deadline = min(deadline, time.monotonic() + json.loads(REGISTRY.read_text(encoding="utf-8"))["bounds"]["community_preflight_seconds"])
    semaphore = asyncio.Semaphore(max(getattr(enricher, "concurrency", 4), 1))

    async def one(candidate):
        try:
            async with semaphore:
                enricher._attach_support(candidate, supporting)
                prepare = getattr(enricher, "preflight_community", None)
                if callable(prepare):
                    try:
                        await prepare(candidate, query_deadline)
                    except asyncio.TimeoutError:
                        from paradigms.search_receipts import record_search
                        record_search(candidate, "community_preflight", "failed", reason="preflight_timeout")
                for origin in candidate.evidence:
                    if origin.evidence_type not in ORIGIN_EVIDENCE_TYPES or not countable_evidence(candidate, origin):
                        continue
                    receipt = assess(candidate, origin=origin, benefit=benefits.get(origin.fingerprint), source_health=health)
                    origin.raw["research_focus"] = receipt
                    review_archive.judgment("research_focus", {"origins": [origin]}, receipt)
                return "research" if admitted(candidate) else "watch"
        except (sqlite3.DatabaseError, OSError):
            raise
        except Exception as exc:
            run_audit.event("research_focus_candidate", "failed", candidate.key + ": " + type(exc).__name__)
            return "pending"  # Not watch/reject/completed research.

    outcomes = await gather_scoped(*(one(candidate) for candidate in candidates))
    counts = {name: outcomes.count(name) for name in ("research", "watch", "pending")}
    stats["research_focus"] = {"version": VERSION, **counts, "input_count": len(candidates),
        "position": "before_full_extraction_and_main", "technical_rejection_count": 0}
    run_audit.event("research_focus", "selected", f"研究取材 {counts['research']}/{len(candidates)}；"
        f"观察 {counts['watch']}，执行待重试 {counts['pending']}；同工作取证复用，不作技术否定")
    return outcomes


async def select(origins, supporting, *, enricher, analyzer, deadline, stats):
    """Select before full extraction/document hydration/MAIN, never after prose."""
    from sources.work_evidence_cache import CHECKPOINT
    probes = [probe(item) for item in origins]
    outcomes = await assess_candidates(probes, supporting, enricher=enricher,
        analyzer=analyzer, deadline=deadline, stats=stats)
    for origin, provisional, outcome in zip(origins, probes, outcomes, strict=True):
        for key in ("research_focus", CHECKPOINT):
            if key in provisional.evidence[0].raw:
                origin.raw[key] = copy.deepcopy(provisional.evidence[0].raw[key])
        if outcome == "pending":
            origin.raw.pop("research_focus", None)
    return [origin for origin, outcome in zip(origins, outcomes, strict=True) if outcome == "research"]
