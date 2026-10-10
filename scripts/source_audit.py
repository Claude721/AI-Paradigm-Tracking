"""Independent source acceptance CLI; no LLM, SMTP or production-state writes."""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
# Explicitly ignore a local .env. Cloud environment Variables/Secrets remain
# available only to bounded source checks; model/mail settings are never used.
os.environ["AI_RADAR_SKIP_DOTENV"] = "true"

from source_audit import SourceAudit, build_entries, render_summary
from healthcheck import configuration_syntax_checks


def main():
    # Production adapters can log failing URLs. This audit emits only redacted
    # metadata, never query secrets or external response bodies.
    logging.getLogger("sources").setLevel(logging.CRITICAL)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--include-authenticated", action="store_true", help="Allow bounded OpenAlex/GitHub checks; never enable paid X/Tavily/Reddit calls")
    parser.add_argument("--include-platforms", action="store_true", help="Explicitly authorize one minimal Tavily/X/Semantic Scholar request and Reddit OAuth+search; may consume platform credits")
    parser.add_argument("--budget-seconds", type=int, default=900)
    parser.add_argument("--max-requests", type=int, default=300)
    parser.add_argument("--entry-timeout", type=int, default=45)
    parser.add_argument("--kind", action="append", help="Select input kind(s); output remains explicitly partial")
    parser.add_argument("--entry", action="append", help="Select exact manifest identity(s); remains partial")
    parser.add_argument("--list", action="store_true", help="List redacted manifest without network")
    parser.add_argument("--output-dir", type=Path, default=ROOT / "logs")
    args = parser.parse_args()
    if not 1 <= args.budget_seconds <= 1800 or not 1 <= args.max_requests <= 1000 or not 1 <= args.entry_timeout <= 120:
        parser.error("Acceptance limits are out of range")
    syntax_errors = [check.note for check in configuration_syntax_checks() if check.status == "missing"]
    if syntax_errors:
        parser.error("；".join(syntax_errors))
    entries = build_entries()
    if args.entry:
        if set(args.entry) - {entry.key for entry in entries}:
            parser.error("Unknown input identity")
        entries = [entry for entry in entries if entry.key in args.entry]
    if args.kind:
        known = {entry.kind for entry in entries}
        if set(args.kind) - known:
            parser.error("Unknown input kind")
        entries = [entry for entry in entries if entry.kind in args.kind]
    if args.list:
        print(json.dumps([entry.public() for entry in entries], ensure_ascii=False, indent=2))
        return 0
    result = asyncio.run(SourceAudit(include_authenticated=args.include_authenticated, include_platforms=args.include_platforms, budget=args.budget_seconds,
                                    max_requests=args.max_requests, timeout=args.entry_timeout).run(entries))
    if args.kind or args.entry:
        result["acceptance_complete"] = False
        result["selected_kinds"] = args.kind or []
        result["selected_entries"] = args.entry or []
    args.output_dir.mkdir(parents=True, exist_ok=True)
    (args.output_dir / "source_audit_latest.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    (args.output_dir / "source_audit_latest.md").write_text(render_summary(result), encoding="utf-8")
    print(json.dumps({key: result[key] for key in ("planned_entries", "counts", "request_count", "elapsed_seconds", "acceptance_complete")}, ensure_ascii=False))
    # Do not greenify an incomplete audit. Disabled/unconfigured optional
    # endpoints are declared exclusions, not passed source checks.
    return 0 if result["acceptance_complete"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
