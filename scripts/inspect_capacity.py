"""Read-only capacity triage of an extracted state + same-run audit. No APIs or migration."""
from __future__ import annotations

import argparse
import json
import math
import sqlite3
from collections import Counter, defaultdict
from pathlib import Path


def inspect_capacity(database: Path, audit_path: Path) -> dict:
    audit = json.loads(audit_path.read_text(encoding="utf-8"))
    stats, calls = audit.get("pipeline_stats"), audit.get("llm_calls")
    if not isinstance(stats, dict) or not isinstance(calls, list) or any(not isinstance(row, dict) for row in calls):
        raise ValueError("Audit is missing pipeline_stats/llm_calls")
    stages = defaultdict(list)
    for call in calls:
        stages[str(call.get("stage", "unknown"))].append(call)
    summary = {}
    for stage, rows in stages.items():
        timings = [row['elapsed_seconds'] for row in rows if type(row.get('elapsed_seconds')) in (int,float) and math.isfinite(row['elapsed_seconds']) and row['elapsed_seconds'] >= 0]
        value = {"calls": len(rows), "failures": sum(row.get("status") == "failed" for row in rows),
                 "unknown_usage": sum(row.get("usage_reported") is not True for row in rows),
                 "request_seconds": round(sum(timings),3) if timings else None,
                 "timing_recorded_calls": len(timings)}
        for field in ("prompt_tokens", "completion_tokens", "reasoning_tokens", "total_tokens"):
            values = [row.get(field,0) for row in rows]
            if any(type(v) is not int or v < 0 for v in values):
                raise ValueError("Audit token counter is invalid")
            value[field] = sum(values)
        summary[stage] = value
    with sqlite3.connect(database.resolve().as_uri() + '?mode=ro', uri=True) as conn:
        conn.execute('PRAGMA query_only=ON')
        if conn.execute('PRAGMA quick_check').fetchone() != ('ok',):
            raise ValueError("State is not a healthy SQLite database")
        tables = ('evidence_state','origin_research_state','paradigms','report_deliveries',
                  'campaign_work','research_attempts','research_stage_cache')
        cardinality = {name: conn.execute('SELECT count(*) FROM '+name).fetchone()[0] for name in tables}
        work = [dict(zip(('kind','status','count'),row)) for row in conn.execute('SELECT kind,status,count(*) FROM campaign_work GROUP BY kind,status')]
        pending_origins = dict(conn.execute("SELECT o.baseline_status,count(*) FROM campaign_work w JOIN origin_research_state o ON o.fingerprint=w.object_key WHERE w.kind='origin' AND w.status='pending' GROUP BY o.baseline_status"))
        source_counts = [dict(zip(('source','type','count','analyzed'),row)) for row in conn.execute('SELECT source,evidence_type,count(*),sum(last_analyzed_at IS NOT NULL) FROM evidence_state GROUP BY source,evidence_type')]
        attempts = [dict(zip(('kind','outcome','count'),row)) for row in conn.execute('SELECT kind,outcome,count(*) FROM research_attempts GROUP BY kind,outcome')]
        campaigns = [dict(zip(('campaign_id','reference_time','status','attempt_count'),row)) for row in conn.execute("SELECT campaign_id,reference_time,status,attempt_count FROM research_campaigns WHERE status != 'delivered'")]
    return {"read_only": True, "runtime": audit.get('runtime',{}), "reference_time": stats.get('reference_time'),
            "cardinality": cardinality, "active_campaigns": campaigns, "work_manifest": work,
            "pending_origin_baselines": pending_origins, "evidence_by_source": source_counts,
            "attempt_outcomes": attempts, "llm_by_stage": summary,
            "research_service": stats.get('research_service'),
            "boundary": "Counts are not completion receipts. Request-seconds sum overlaps concurrency; missing timings are unknown, not zero. Old audits may omit cancelled requests; recorded usage alone cannot prove complete billing."}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--database', required=True, type=Path)
    parser.add_argument('--audit', required=True, type=Path)
    args = parser.parse_args()
    print(json.dumps(inspect_capacity(args.database, args.audit), ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
