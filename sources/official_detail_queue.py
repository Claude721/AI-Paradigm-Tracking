"""Monotone official-detail work and public source results for a fixed window."""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict
from pathlib import Path

from paradigms.models import technical_evidence_from_dict
from research_watchlist import source_record
from runtime_clock import research_now


DETAIL_QUEUE_VERSION = 2
STAGE = "official_detail_queue"
TASK_STAGE = "official_detail_task"


def official_detail_policy():
    """Invalidate both source and detail receipts for parser/layout repairs."""
    root = Path(__file__).parent
    files = ("official_detail_queue.py", "priority_research_source.py", "qwen_catalog.py", "bibliography_resolver.py", "document_limits.py")
    return hashlib.sha256(b"".join(hashlib.sha256((root / name).read_bytes()).digest() for name in files)).hexdigest()


class OfficialDetailQueue:
    def __init__(self, index_url, lookback_days, stage_cache=None):
        self.index_url = index_url
        self.stage_cache = stage_cache
        # Policy changes reopen results, never hide tasks behind a new key.
        scope = [1, index_url, lookback_days, research_now().isoformat()]
        self.signature = hashlib.sha256(json.dumps(scope, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
        parser_policy = official_detail_policy()
        self.contract = hashlib.sha256(json.dumps([DETAIL_QUEUE_VERSION, source_record(index_url), parser_policy], sort_keys=True, ensure_ascii=False).encode()).hexdigest()
        self.contract_changed = False
        self._legacy_tasks = False
        saved = stage_cache.cache_get(STAGE, self.signature) if stage_cache else None
        self.tasks = {}
        if saved is not None:
            self._restore(saved)

    def task_signature(self, url):
        return hashlib.sha256(json.dumps([self.signature, url]).encode()).hexdigest()

    def _restore(self, saved):
        if not isinstance(saved, dict) or saved.get("version") not in {1, DETAIL_QUEUE_VERSION} or not isinstance(saved.get("tasks"), dict):
            raise ValueError("Invalid official detail manifest")
        from sources.priority_research_source import _ResearchLink
        from paradigms.reputation import source_link_allowed
        if not isinstance(saved.get("contract"), str) or len(saved["contract"]) != 64:
            raise ValueError("Official detail input contract is missing")
        self.contract_changed = saved["contract"] != self.contract
        self._legacy_tasks = saved["version"] == 1
        signatures = [self.task_signature(url) for url in saved["tasks"]]
        batch_get = getattr(self.stage_cache, "cache_get_many", None)
        receipts = (batch_get(TASK_STAGE, signatures) if callable(batch_get) else
                    {key: self.stage_cache.cache_get(TASK_STAGE, key) for key in signatures})
        for url, entry in saved["tasks"].items():
            if not isinstance(entry, dict) or not isinstance(entry.get("link"), dict):
                raise ValueError("Invalid official detail task")
            link = _ResearchLink(**entry["link"])
            if (link.url != url or not all(isinstance(value, str) for value in asdict(link).values())
                    or not source_link_allowed(self.index_url, url)):
                raise ValueError("Official detail identity mismatch")
            task = entry if saved["version"] == 1 else receipts.get(self.task_signature(url))
            if task is None:
                task = {"link": entry["link"], "status": "pending", "attempts": 0}
            elif saved["version"] != 1 and (not isinstance(task, dict) or not isinstance(task.get("contract"), str) or len(task["contract"]) != 64):
                raise ValueError("Official detail execution contract is missing")
            if (not isinstance(task, dict) or not isinstance(task.get("link"), dict)
                    or task["link"].get("url") != url or task.get("status") not in {"pending", "completed"}
                    or type(task.get("attempts")) is not int or task["attempts"] < 0):
                raise ValueError("Invalid official detail execution receipt")
            self._validate_result(url, task)
            stale = self.contract_changed or (saved["version"] != 1 and task.get("contract") not in (None, self.contract))
            self.tasks[url] = ({"link": entry["link"], "status": "pending", "attempts": task["attempts"]}
                               if stale else {**task, "link": entry["link"]})

    def _validate_result(self, url, task):
        if task["status"] == "pending":
            if task.get("evidence") or task.get("detail_parsed") or task.get("detail_outside_window"):
                raise ValueError("Pending official detail contains a completion receipt")
            return
        records = task.get("evidence")
        outside = task.get("detail_outside_window")
        if (not isinstance(records, list) or len(records) > 1 or type(outside) is not int
                or outside not in (0, 1) or type(task.get("detail_parsed")) is not int
                or task["detail_parsed"] != 1 or len(records) + outside != 1):
            raise ValueError("Official detail completion cardinality mismatch")
        for record in records:
            evidence = technical_evidence_from_dict(record)
            if (evidence.url != url or evidence.source != "priority-research-page"
                    or evidence.raw.get("research_index_url") != self.index_url):
                raise ValueError("Official detail cached source identity mismatch")

    def merge(self, links):
        for link in links:
            self.tasks.setdefault(link.url, {"link": asdict(link), "status": "pending", "attempts": 0})
            if self.contract_changed:
                self.tasks[link.url]["link"] = asdict(link)
        # Only identities are rewritten; potentially large public body results
        # have independent rows. This also commits seeds before any HTTP I/O.
        self.save()

    def pending_links(self):
        from sources.priority_research_source import _ResearchLink
        tasks = sorted((task for task in self.tasks.values() if task["status"] == "pending"), key=lambda task: task["attempts"])
        return [_ResearchLink(**task["link"]) for task in tasks]

    def _save_task(self, url, task):
        if self.stage_cache:
            self.stage_cache.cache_save(TASK_STAGE, self.task_signature(url), {**task, "contract": self.contract})

    def begin_attempt(self, link):
        task = {**self.tasks[link.url], "attempts": self.tasks[link.url]["attempts"] + 1}
        self._save_task(link.url, task)
        self.tasks[link.url] = task

    def reopen(self, url):
        task = self.tasks.get(url)
        if task and task["status"] == "completed":
            self.tasks[url] = {"link": task["link"], "status": "pending", "attempts": task["attempts"]}
            self._save_task(url, self.tasks[url])

    @property
    def pending_count(self):
        return sum(task["status"] == "pending" for task in self.tasks.values())

    def cached(self):
        records, outside, parsed = [], 0, 0
        for task in self.tasks.values():
            if task["status"] == "completed":
                records.extend(technical_evidence_from_dict(value) for value in task["evidence"])
                outside += task["detail_outside_window"]
                parsed += task["detail_parsed"]
        return records, outside, parsed

    def completed_detail(self, url):
        task = self.tasks.get(url)
        if not task or task["status"] != "completed":
            return None
        records = task["evidence"]
        return (technical_evidence_from_dict(records[0]) if records else None, bool(task["detail_outside_window"]))

    def complete(self, link, evidence, *, outside):
        task = {"link": self.tasks[link.url]["link"], "status": "completed",
                "attempts": self.tasks[link.url]["attempts"],
                "evidence": [evidence.to_dict()] if evidence is not None else [],
                "detail_parsed": 1, "detail_outside_window": int(outside)}
        self._validate_result(link.url, task)
        # A source result and its completion receipt are one atomic child row.
        # Never expose completion before the durable downstream save succeeds.
        self._save_task(link.url, task)
        self.tasks[link.url] = task

    def save(self):
        if self.stage_cache:
            if self._legacy_tasks:
                # All old inline attempts/results must survive before replacing
                # the parent with its lightweight manifest. A failed child save
                # leaves the original parent readable and migration retryable.
                for url, task in self.tasks.items():
                    self._save_task(url, task)
            self.stage_cache.cache_save(STAGE, self.signature, {
                "version": DETAIL_QUEUE_VERSION,
                "tasks": {url: {"link": task["link"]} for url, task in self.tasks.items()},
                "contract": self.contract,
            })
            self._legacy_tasks = False
