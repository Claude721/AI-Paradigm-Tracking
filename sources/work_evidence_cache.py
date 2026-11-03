"""Work-bound community queries shared by selection and deep research.

The existing provider clients retain their billing, limits and failure ledger.
Only metadata is checkpointed on the origin; temporary post bodies stay in RAM.
"""
from __future__ import annotations

import asyncio
import copy
import hashlib
import json
import sqlite3
import time

from database.origin_state import source_signature
from paradigms.models import (ORIGIN_EVIDENCE_TYPES, EvidenceType, ParadigmCandidate,
    candidate_from_dict, scrub_ephemeral_evidence)
from paradigms.evidence_association import countable_evidence
from paradigms.propagation import certify_diffusion, transfer_work_visibility
from paradigms.search_receipts import search_signature, record_search

CHECKPOINT = "work_search_checkpoint"
PROVIDERS = ("github", "hackernews", "tavily_social_web", "reddit_official", "x_official")


def work_probe(origin):
    source = copy.deepcopy(origin)
    source.raw.pop(CHECKPOINT, None)
    source.raw.pop("research_focus", None)
    source.source_revision = origin.source_revision or source_signature(origin)
    return ParadigmCandidate(key="work-" + origin.fingerprint, name=origin.title,
        thesis="", problem_shift="", mechanism="", why_now="", evidence=[source])


def _signature(probe):
    return hashlib.sha256(json.dumps([1, probe.evidence[0].raw.get("discovery_lookback_days", 0),
                                     *[search_signature(probe, name) for name in PROVIDERS]],
                                    sort_keys=True).encode()).hexdigest()


def _export(probe, signature):
    records = []
    for item in copy.deepcopy(probe.evidence[1:]):
        if item.source in {"hackernews", "reddit", "x-title-search", "read-web-article"} or item.source.startswith("tavily"):
            item.raw["ephemeral_content"] = True
        scrub_ephemeral_evidence(item)
        # A read article's body/actor cannot be inferred from scrubbed metadata.
        # Restore it as an index for a later free page read, not a paid re-search.
        if item.source == "read-web-article" and item.raw.get("content_scrubbed"):
            item.source = "tavily-web"
            item.raw["indexed_discovery_only"] = True
        records.append(item.to_dict())
    return {"version": 1, "signature": signature, "evidence": records,
        "receipts": copy.deepcopy(probe.community_search_receipts),
        "coverage": copy.deepcopy(probe.community_coverage)}


def _restore(probe, checkpoint, signature):
    if not isinstance(checkpoint, dict) or checkpoint.get("version") != 1 or checkpoint.get("signature") != signature:
        return None
    try:
        payload = probe.to_dict()
        payload["evidence"] += checkpoint["evidence"]
        payload["community_search_receipts"] = checkpoint["receipts"]
        payload["community_coverage"] = checkpoint["coverage"]
        restored = candidate_from_dict(payload)
        if any(item.evidence_type in ORIGIN_EVIDENCE_TYPES for item in restored.evidence[1:]):
            return None
        return restored
    except (KeyError, TypeError, ValueError):
        return None  # Invalid cache is not a valid negative search result.


class WorkEvidenceCache:
    def __init__(self):
        self._values, self._locks = {}, {}

    async def attach(self, candidate, community, *, pages=None, deadline=float("inf")):
        """Query each active original once per source/window/configuration.

        Failures and partial results are shared too; no immediate retry storm.
        Cancellation preserves completed peer providers before propagating.
        """
        ledgers = []
        for origin in list(candidate.evidence):
            if origin.evidence_type not in ORIGIN_EVIDENCE_TYPES or not countable_evidence(candidate, origin):
                continue
            probe = work_probe(origin)
            signature = _signature(probe)
            async with self._locks.setdefault(signature, asyncio.Lock()):
                saved = self._values.get(signature)
                if saved is None:
                    restored = _restore(probe, origin.raw.get(CHECKPOINT), signature)
                    if restored is not None:
                        if set(PROVIDERS).issubset(restored.community_search_receipts) and all(row["status"] in {"completed", "disabled", "not_configured"}
                                for row in restored.community_search_receipts.values()):
                            saved = restored
                        else:
                            # Retry failed providers in a new process, retaining
                            # successful provider receipts and metadata.
                            probe = restored
                if saved is None:
                    if deadline <= time.monotonic():
                        record_search(candidate, "community_preflight", "not_executed", reason="preflight_time_budget")
                        continue
                    try:
                        operation = getattr(community, "search_work", community.search)
                        rows = await asyncio.wait_for(operation(probe), timeout=min(40, deadline-time.monotonic()))
                        by_id = {item.fingerprint: item for item in [*probe.evidence, *rows]}
                        probe.evidence = list(by_id.values())
                    except sqlite3.DatabaseError:
                        raise
                    except asyncio.CancelledError:
                        certify_diffusion(probe)
                        self._values[signature] = copy.deepcopy(probe)
                        origin.raw[CHECKPOINT] = _export(probe, signature)
                        raise
                    except Exception as exc:
                        record_search(probe, "community_runtime", "failed", reason=type(exc).__name__)
                    certify_diffusion(probe)
                    saved = self._values[signature] = copy.deepcopy(probe)
                else:
                    self._values.setdefault(signature, copy.deepcopy(saved))
                origin.raw[CHECKPOINT] = _export(saved, signature)
            existing = {item.fingerprint: index for index, item in enumerate(candidate.evidence)}
            for item in copy.deepcopy(saved.evidence[1:]):
                if not countable_evidence(candidate, item):
                    continue
                index = existing.get(item.fingerprint)
                if item.source == "github" and item.evidence_type == EvidenceType.IMPLEMENTATION:
                    links = {str(url).rstrip("/").casefold() for url in origin.raw.get("github_repositories", []) or []}
                    previous = candidate.evidence[index] if index is not None else None
                    linked = item.url.rstrip("/").casefold() in links
                    if linked or previous is not None and previous.raw.get("relationship") == "paper_linked_repository" and countable_evidence(candidate, previous):
                        # Hydration can reveal an official link after the search.
                        # Reuse its measured counters, not a zero-counter seed;
                        # official code is still NOT independent replication.
                        from paradigms.evidence_association import VERSION
                        item.raw.update(relationship="paper_linked_repository", independence="official",
                            association_version=VERSION, origin_fingerprint=origin.fingerprint,
                            origin_work_ids={key: value for key, value in origin.identifiers.items()
                                             if key in {"arxiv", "doi", "openreview"}})
                        if index is not None:
                            candidate.evidence[index] = item
                            continue
                if index is not None and (not candidate.evidence[index].raw.get("content_scrubbed") or not item.summary):
                    continue
                transfer_work_visibility(saved, candidate, item)
                if index is not None:
                    candidate.evidence[index] = item
                else:
                    existing[item.fingerprint] = len(candidate.evidence)
                    candidate.evidence.append(item)
            ledgers.append(saved)
            candidate.community_coverage.update(saved.community_coverage)
        for provider in {name for saved in ledgers for name in saved.community_search_receipts}:
            entries = [saved.community_search_receipts[provider] for saved in ledgers if provider in saved.community_search_receipts]
            merged = copy.deepcopy(next((row for row in entries if row["status"] not in {"completed", "disabled"}), entries[0]))
            ids = sorted({key for row in entries for key in row.get("evidence_fingerprints", [])})
            merged.update(input_signature=search_signature(candidate, provider), evidence_fingerprints=ids,
                          result_count=len(ids))
            candidate.community_search_receipts[provider] = merged
        if pages is not None:
            await pages.read(candidate)
        certify_diffusion(candidate)
