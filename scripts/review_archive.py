"""Local JSON archive merge/feedback and emailed ZIPs; no research APIs."""
from __future__ import annotations

import argparse
import asyncio
import json
import os
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import config
from research_archive import ArchiveError, ArchiveStore, SCHEMA_VERSION, _no_symlinks, _read, restore_bundle


def _private_bytes(path, value):
    _no_symlinks(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "wb") as handle:
        handle.write(value)


def label_view(store):
    feedback_path = store.directory / "feedback.json"
    feedback = _read(feedback_path) if feedback_path.exists() else {"annotations": {}}
    rows = []
    for path in sorted((store.directory / "origins").glob("*.json")):
        origin = _read(path)
        judgments = sorted([_read(store.directory / "judgments" / (key + ".json")) for key in origin["judgment_ids"]],
                           key=lambda row: (row["created_at"], row["judgment_id"]))
        relevant = [row for row in judgments if row["stage"] in {"candidate_final", "origin_screening", "extraction_validated"}]
        latest_run = relevant[-1]["run_id"] if relevant else None
        finals, screening = {}, []
        for judgment in judgments:
            if judgment["run_id"] != latest_run:
                continue
            if judgment["stage"] == "candidate_final":
                finals[judgment["candidate_key"]] = judgment["result"].get("system_retention")
            if judgment["stage"] in {"origin_screening", "extraction_validated"}:
                screening.append(judgment["result"].get("system_retention"))
        labels = list(finals.values())
        model = (True if True in labels else None if None in labels else False) if labels else (
            False if screening and all(value is False for value in screening) else None)
        human = feedback["annotations"].get(origin["origin_id"], {})
        version = next(reversed(origin["versions"]))
        if relevant:
            version = next(ref["content_version"] for ref in relevant[-1]["origin_refs"] if ref["origin_id"] == origin["origin_id"])
        rows.append({"origin_id": origin["origin_id"], "identity": origin["identity"],
            "title": origin["versions"][version]["title"],
            "system_retention": model, "human_feedback": human, "label_run_id": latest_run,
            "screening_labels": screening, "final_candidate_labels": finals,
            "effective_dataset_label": human.get("retain") if human.get("retain") is not None else model,
            "label_scope": "offline dataset preference only; never a production filter",
            "call_ids": origin["call_ids"], "judgment_ids": origin["judgment_ids"]})
    return {"schema_version": SCHEMA_VERSION, "origins": rows}


async def mail_archive(directory, output):
    if not config.RESEARCH_ARCHIVE_ENABLED:
        print("Review retention explicitly disabled; no data email sent.")
        return 0
    prefix = (os.environ["GITHUB_RUN_ID"] + ":" + os.getenv("GITHUB_RUN_ATTEMPT", "1") + ":") if os.getenv("GITHUB_RUN_ID") else ""
    if not directory.exists():
        raise ArchiveError("No review archive directory exists; data retention was not confirmed")
    store = ArchiveStore(directory)
    files = store.export(output, run_prefix=prefix)
    summary = {"origins": sum(name.startswith("origins/") for name in files),
               "calls": sum(name.startswith("calls/") for name in files),
               "judgments": sum(name.startswith("judgments/") for name in files),
               "capture_errors": sum(len(row["errors"]) for row in files.get("capture-status.json", {}).get("runs", {}).values())}
    summary["capture_status_known"] = bool(files.get("capture-status.json", {}).get("runs"))
    from notifications.email_notifier import send_review_archive_email
    await send_review_archive_email(output, summary)
    if summary["capture_errors"] or not summary["capture_status_known"]:
        raise ArchiveError("Available review data mailed, but capture errors or unknown capture status require inspection")
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(description="Local review JSON retention, not training or model execution")
    parser.add_argument("--directory", type=Path, default=config.RESEARCH_ARCHIVE_DIR)
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("pack", "view", "mail"):
        sub.add_parser(name).add_argument("--output", type=Path, required=True)
    for name in ("unpack", "merge", "feedback-import"):
        sub.add_parser(name).add_argument("--input", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        if args.command == "mail":
            return asyncio.run(mail_archive(args.directory, args.output))
        if args.command == "unpack":
            restore_bundle(args.input.read_bytes(), args.directory)
        else:
            store = ArchiveStore(args.directory)
            if args.command == "view":
                _private_bytes(args.output, json.dumps(label_view(store), ensure_ascii=False, indent=2).encode())
            elif args.command == "feedback-import":
                store.import_feedback(_read(args.input))
            elif args.command == "merge":
                store.merge(args.input.read_bytes())
            else:
                store.export(args.output)
        print("Local review archive operation completed; no research API or email invoked.")
        return 0
    except (ArchiveError, OSError, ValueError, KeyError, TypeError, AttributeError, RuntimeError) as exc:
        print("Review archive operation failed: " + (str(exc) if isinstance(exc, ArchiveError) else type(exc).__name__), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
