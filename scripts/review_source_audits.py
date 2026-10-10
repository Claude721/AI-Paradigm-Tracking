"""Read-only review of per-entry observations; never calls providers or models."""
from __future__ import annotations

import argparse
import json
import os
import statistics
import sys
from collections import Counter, defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ["AI_RADAR_SKIP_DOTENV"] = "true"
from source_audit import diagnose_entry, safe_url


def review(paths):
    observations = defaultdict(list)
    runs = []
    for index, path in enumerate(paths):
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        rows = data.get("entries")
        if not isinstance(rows, list) or len(rows) != data.get("planned_entries") or len(rows) != data.get("returned_entries"):
            raise ValueError("Audit manifest cardinality is incomplete")
        identities = set()
        for row in rows:
            if not isinstance(row, dict) or not isinstance(row.get("key"), str) or not isinstance(row.get("scope_hash"), str):
                raise ValueError("Audit identity is missing")
            identity = (row["key"], row["scope_hash"])
            if identity in identities:
                raise ValueError("Duplicate audit identity")
            identities.add(identity)
            observations[identity].append((index, row))
        runs.append({"index": index, "reference_time": data.get("reference_time"),
                     "manifest_hash": data.get("input_manifest_hash"),
                     "source_fingerprint": data.get("runtime_provenance", {}).get("source_fingerprint"),
                     "counts": dict(Counter(row["status"] for row in rows)),
                     "request_count": data.get("request_count")})
    entries = []
    for (key, scope), values in observations.items():
        latest_index, latest = values[-1]
        # An entry removed from a later manifest is not silently completed.
        absent = [index for index in range(len(runs)) if index not in {i for i, _ in values}]
        timings = [row["elapsed_seconds"] for _, row in values if type(row.get("elapsed_seconds")) in (int, float) and 0 <= row["elapsed_seconds"] < float("inf")]
        entries.append({"key": key, "scope_hash": scope, "url": safe_url(latest.get("url", "")),
                        "observations": len(values), "status_counts": dict(Counter(row["status"] for _, row in values)),
                        "latest_run_index": latest_index, "latest_status": latest["status"],
                        "absent_run_indices": absent, "diagnosis": diagnose_entry(latest),
                        "entry_seconds": {"samples": len(timings), "min": min(timings) if timings else None,
                                          "median": statistics.median(timings) if timings else None,
                                          "max": max(timings) if timings else None}})
    return {"read_only": True, "runs": runs, "entries": entries,
            "latest_status_counts": dict(Counter(row["latest_status"] for row in entries)),
            "long_term_stability_proven": False,
            "boundary": "输入文件顺序为先旧后新；仅汇总观测，不把重试当独立样本，不合并不同 scope。缺席保留，缺少耗时为未知。不估计 P95 或长期可用率；代码或配置换版前后的观测不能冒充同版本稳定性实验。"}


def render(result):
    lines = ["# 逐入口验收复核", "", result["boundary"], "",
             f"观测轮次：{len(result['runs'])}；最新状态：{result['latest_status_counts']}", "",
             "| 入口 | 最新状态 | 观测次数 | 归因 | 处理建议 |", "|---|---|---:|---|---|"]
    for row in result["entries"]:
        diagnosis = row["diagnosis"]
        lines.append(f"| {row['key']} | {row['latest_status']} | {row['observations']} | {diagnosis['category']} | {diagnosis['action']} |")
    return "\n".join(lines) + "\n"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("audits", nargs="+", type=Path, help="Oldest to newest; no network requests")
    parser.add_argument("--markdown", action="store_true")
    args = parser.parse_args()
    result = review(args.audits)
    print(render(result) if args.markdown else json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
