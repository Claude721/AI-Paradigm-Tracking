"""Bounded free-source acceptance only: never model calls, mail or state writes."""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path

os.environ["AI_RADAR_SKIP_DOTENV"] = "true"
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sources.direction_signal_source import (DirectionSignalClient, topics,
    parse_capital_feed, parse_jobs, parse_cohort, cohort_query)
from runtime_clock import research_now


async def inspect(topic_id):
    collector = DirectionSignalClient()
    now = research_now()
    report = {"purpose": "free_source_protocol_check_not_research", "sources": []}
    async with collector.client_factory(timeout=8, follow_redirects=False,
            headers={"User-Agent": "AI-Paradigm-Radar/4.1 (free source acceptance)"}) as client:
        for source in [*collector.registry["media_feeds"], *collector.registry["job_boards"]]:
            record = {"source": source["name"], "url": source["url"]}
            try:
                response = await collector._get(client, source)
                rows = (parse_capital_feed(response.text, source, now=now) if source in collector.registry["media_feeds"]
                        else parse_jobs(response.json(), source))
                record.update(status="parsed", matching_records=len(rows), scope="latest public slice, not a whole-week census")
            except Exception as exc:
                record.update(status="failed", reason=type(exc).__name__)
            report["sources"].append(record)
        topic = next(item for item in topics(collector.registry) if item["id"] == topic_id)
        query, *_ = cohort_query(topic, now=now, days=collector.registry["bounds"]["comparison_days"])
        try:
            response = await collector.arxiv._get_with_retry(client, {"search_query": query, "start": 0,
                "max_results": collector.registry["bounds"]["arxiv_members"], "sortBy": "submittedDate", "sortOrder": "descending"})
            cohort = parse_cohort(response.text, topic, now=now, days=collector.registry["bounds"]["comparison_days"], query=query)
            report["paper_sample"] = {key: cohort[key] for key in ("topic", "status", "previous_count", "current_count", "index_total", "windows")}
        except Exception as exc:
            report["paper_sample"] = {"topic": topic["label"], "status": "failed", "reason": type(exc).__name__}
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--topic", default="diffusion_language")
    args = parser.parse_args()
    print(json.dumps(asyncio.run(inspect(args.topic)), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
