"""Explicit NYU/Tavily acceptance only; no production state, LLM or SMTP."""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import logging
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ["AI_RADAR_SKIP_DOTENV"] = "true"

import config
from healthcheck import configuration_syntax_checks
from source_audit import NYU_BIBLIOGRAPHY_URL, SourceAudit, build_entries, render_summary
from runtime_provenance import runtime_provenance


class InterfacePreflightError(ValueError):
    """Fixed public reason codes, never external response/exception text."""
    def __init__(self, reason):
        super().__init__(reason)
        self.reason = reason


def select_entries(manifest, scope):
    if scope not in {"nyu", "tavily", "both"}:
        raise InterfacePreflightError("unknown_scope")
    selected = []
    for key in (["nyu", "tavily"] if scope == "both" else [scope]):
        matches = [entry for entry in manifest if (
            entry.kind == "official" and entry.url == NYU_BIBLIOGRAPHY_URL
            if key == "nyu" else entry.key == "tavily" and entry.kind == "separate-acceptance")]
        if len(matches) != 1 or not matches[0].enabled:
            raise InterfacePreflightError("selected_interface_missing_duplicated_or_disabled")
        selected.append(matches[0])
    return selected


async def execute(scope, bibliography_requests, *, transport=None, reference_time=None, sleeper=None):
    # Validate the ENTIRE requested plan before ANY HTTP, including Tavily if
    # the other selected interface is misconfigured. Never load local .env.
    if type(bibliography_requests) is not int or not 1 <= bibliography_requests <= 8:
        raise InterfacePreflightError("invalid_bibliography_limit")
    errors = [check.note for check in configuration_syntax_checks() if check.status == "missing"]
    if errors:
        raise InterfacePreflightError("configuration_syntax_invalid")
    manifest = build_entries()
    selected = select_entries(manifest, scope)
    needs_nyu = scope in {"nyu", "both"}
    needs_tavily = scope in {"tavily", "both"}
    if needs_nyu and not config.OPENALEX_API_KEY:
        raise InterfacePreflightError("OPENALEX_API_KEY_not_configured")
    if needs_tavily and (not config.TAVILY_API_KEY or config.TAVILY_MONTHLY_CREDIT_LIMIT <= 0):
        raise InterfacePreflightError("TAVILY_API_KEY_or_monthly_guard_not_configured")
    result = await SourceAudit(transport=transport, include_authenticated=needs_nyu,
        include_platforms=needs_tavily, bibliography_requests=bibliography_requests if needs_nyu else None,
        tavily_included_only=needs_tavily, timeout=120, budget=300, max_requests=14,
        concurrency=1, sleeper=sleeper).run(selected, reference_time=reference_time)
    result.update(interface_contract_version=1, scope=scope, selected_entries=[entry.key for entry in selected],
        full_manifest=[entry.public() for entry in manifest],
        full_manifest_hash=hashlib.sha256(json.dumps([entry.public() for entry in manifest], sort_keys=True).encode()).hexdigest(),
        full_manifest_scope="current_interface_invocation",
        production_manifest_verified=False,
        untested_entry_keys=[entry.key for entry in manifest if entry not in selected],
        bibliography_request_limit=bibliography_requests if needs_nyu else 0,
        tavily_search_limit=1 if needs_tavily else 0,
        selected_protocols_passed=(not result["code_changed_during_run"] and
            len(result["entries"]) == len(selected) and all(row["status"] == "passed" for row in result["entries"])),
        acceptance_complete=False, campaign_coverage_complete=False, production_v0_accepted=False)
    result["boundary"] = "只验明确授权的 NYU/Tavily 子集；清单仅对应当前独立验收进程配置，不替代生产清单。其余入口未测试、书目尾项仍待办，不证明整批研究或 V0。"
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scope", required=True, choices=("nyu", "tavily", "both"))
    parser.add_argument("--bibliography-requests", type=int, default=8)
    parser.add_argument("--live-authorized", action="store_true")
    parser.add_argument("--list", action="store_true", help="No HTTP; list selected manifest identities")
    parser.add_argument("--output-dir", type=Path, default=ROOT / "logs" / "source_interfaces")
    args = parser.parse_args(argv)
    if not 1 <= args.bibliography_requests <= 8:
        parser.error("Bibliography request limit must be 1-8")
    if args.list:
        print(json.dumps([entry.public() for entry in select_entries(build_entries(), args.scope)], ensure_ascii=False))
        return 0
    if not args.live_authorized:
        parser.error("Explicit live authorization required; no requests made")
    logging.getLogger("sources").setLevel(logging.CRITICAL)
    args.output_dir.mkdir(parents=True, exist_ok=True)
    try:
        result = asyncio.run(execute(args.scope, args.bibliography_requests))
    except Exception as exc:
        # Do not expose exception text, provider response or secrets.
        result = {"interface_contract_version": 1, "scope": args.scope, "status": "failed", "error_type": type(exc).__name__,
            "runtime_provenance": runtime_provenance(),
            "reason": exc.reason if isinstance(exc, InterfacePreflightError) else "execution_error_usage_unknown",
            "request_count": 0 if isinstance(exc, InterfacePreflightError) else None,
            "acceptance_complete": False, "campaign_coverage_complete": False,
            "production_v0_accepted": False, "selected_protocols_passed": False}
    (args.output_dir / "source_interface_latest.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    summary = render_summary(result) if "entries" in result else f"接口验收未完成：{result['reason']}。无生产 V0 完成声明。\n"
    (args.output_dir / "source_interface_latest.md").write_text(summary, encoding="utf-8")
    if os.getenv("GITHUB_STEP_SUMMARY"):
        with Path(os.environ["GITHUB_STEP_SUMMARY"]).open("a", encoding="utf-8") as handle:
            handle.write(summary)
    print(json.dumps({key: result.get(key) for key in (
        "scope", "counts", "request_count", "selected_protocols_passed", "error_type", "production_v0_accepted")}, ensure_ascii=False))
    return 0 if result["selected_protocols_passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
