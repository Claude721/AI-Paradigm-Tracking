"""Run the repository regression suite in a hermetic, network-denied process.

This is the CI release gate. It deliberately does not inherit production API
keys or mutable research configuration, and it writes a compact machine-readable
failure summary for the workflow notification step.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
LOG_DIR = ROOT / "logs"
LOG_PATH = LOG_DIR / "offline_checks.log"
SUMMARY_PATH = LOG_DIR / "offline_checks.json"


def _safe_environment() -> dict[str, str]:
    env = os.environ.copy()
    # Production Variables/Secrets must never alter release-test semantics.
    configuration_prefixes = (
        "LLM_",
        "SUB_AGENT_",
        "MAIN_AGENT_",
        "PARADIGM_",
        "OPENALEX_",
        "OPENREVIEW_",
        "SEMANTIC_SCHOLAR_",
        "TAVILY_",
        "REDDIT_",
        "TWITTER_",
        "SMTP_",
        "EMAIL_",
        "RESEARCH_",
        "PRIORITY_",
        "ESTABLISHED_",
        "MONITORED_",
        "FOLLOW_BUILDERS_",
        "FRONTIER_",
        "SOURCING_",
        "WECHAT_",
        "PRODUCTHUNT_",
    )
    for key in list(env):
        if key.startswith(configuration_prefixes):
            env.pop(key, None)
    env.update(
        {
            "AI_RADAR_SKIP_DOTENV": "true",
            "PIPELINE_MODE": "paradigm",
            "PYTHONHASHSEED": "0",
            "TZ": "UTC",
            "LLM_PROVIDER": "dashscope",
            "LLM_MODEL": "qwen3.7-plus",
            "LLM_API_KEY": "offline-test-only",
            "SUB_AGENT_PROVIDER": "dashscope",
            "SUB_AGENT_MODEL": "qwen3.7-plus",
            "SUB_AGENT_API_KEY": "offline-test-only",
            "MAIN_AGENT_PROVIDER": "dashscope",
            "MAIN_AGENT_MODEL": "qwen3.7-plus",
            "MAIN_AGENT_API_KEY": "offline-test-only",
            "OPENALEX_API_KEY": "",
            "SEMANTIC_SCHOLAR_ENABLED": "false",
            "SEMANTIC_SCHOLAR_API_KEY": "",
            "GITHUB_TOKEN": "",
            "TWITTER_BEARER_TOKEN": "",
            "TAVILY_API_KEY": "",
            "REDDIT_CLIENT_ID": "",
            "REDDIT_CLIENT_SECRET": "",
            "REDDIT_API_ACCESS_APPROVED": "false",
            "REDDIT_USER_AGENT": "",
            "EMAIL_PUSH_ENABLED": "false",
            "SMTP_HOST": "",
            "SMTP_USERNAME": "",
            "SMTP_PASSWORD": "",
            "SMTP_FROM": "",
            "SMTP_TO": "",
            "RESEARCH_WATCHLIST_MODE": "merge",
            "PRIORITY_RESEARCH_PAGES": "",
            "ESTABLISHED_RESEARCH_ORGANIZATIONS": "",
            "MONITORED_RESEARCH_ORGANIZATIONS": "",
            "PRIORITY_RESEARCHERS": "",
            "PARADIGM_RUN_BUDGET_SECONDS": "3600",
            "PARADIGM_STAGE_RESERVE_SECONDS": "1200",
            "PARADIGM_DISCOVERY_SOURCE_TIMEOUT_SECONDS": "600",
            "PARADIGM_REPORT_TIMEOUT_SECONDS": "1200",
            "PARADIGM_REPORT_REQUEST_TIMEOUT_SECONDS": "360",
            "PARADIGM_REPORT_ROUTE_CONCURRENCY": "2",
            # Any accidental real HTTP request must fail locally and quickly.
            "HTTP_PROXY": "http://127.0.0.1:9",
            "HTTPS_PROXY": "http://127.0.0.1:9",
            "ALL_PROXY": "http://127.0.0.1:9",
            "NO_PROXY": "",
        }
    )
    return env


def _summarize_failure(text: str, stage: str, exit_code: int) -> str:
    cases = re.findall(r"(?m)^(?:ERROR|FAIL):\s+([^\n]+)", text)
    suite = re.findall(r"(?m)^FAILED\s+\(([^\n]+)\)", text)
    if cases:
        detail = "；".join(dict.fromkeys(cases[:6]))
        suffix = f"；{suite[-1]}" if suite else ""
        return f"{stage} 失败（exit {exit_code}）：{detail}{suffix}"
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    tail = " | ".join(lines[-6:])
    return f"{stage} 失败（exit {exit_code}）：{tail[:800]}"


def _run_stage(
    name: str,
    command: list[str],
    *,
    env: dict[str, str],
    log,
) -> tuple[int, str]:
    header = f"\n=== {name} ===\n"
    print(header, end="", flush=True)
    log.write(header)
    log.flush()
    process = subprocess.Popen(
        command,
        cwd=ROOT,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    captured: list[str] = []
    assert process.stdout is not None
    for line in process.stdout:
        print(line, end="", flush=True)
        log.write(line)
        captured.append(line)
    exit_code = process.wait()
    log.flush()
    return exit_code, "".join(captured)


def main() -> int:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    started_at = datetime.now(timezone.utc).isoformat()
    env = _safe_environment()
    stages = [
        (
            "unit_tests",
            [
                sys.executable,
                "-m",
                "unittest",
                "discover",
                "-s",
                "tests",
                "-v",
            ],
        ),
        (
            "stdlib_failure_notifier",
            [
                sys.executable,
                "-S",
                "main.py",
                "--notify-failure",
            ],
        ),
        (
            "manual_probe_imports",
            [sys.executable, "-c", "import test_hf, test_hf_spaces"],
        ),
        (
            "compileall",
            [
                sys.executable,
                "-m",
                "compileall",
                "-q",
                "agents",
                "database",
                "notifications",
                "paradigms",
                "reports",
                "scripts",
                "skills",
                "sources",
                "main.py",
                "config.py",
                "run_audit.py",
                "healthcheck.py",
                "smokecheck.py",
                "setup_env.py",
                "runtime_clock.py",
                "test_hf.py",
                "test_hf_spaces.py",
            ],
        ),
    ]
    summary: dict[str, object] = {
        "status": "passed",
        "started_at": started_at,
        "finished_at": "",
        "failed_stage": "",
        "failure_summary": "",
        "commit_sha": os.getenv("GITHUB_SHA", ""),
        "network_policy": "proxy-denied",
        "configuration_policy": "hermetic-offline-fixture",
    }
    exit_code = 0
    with LOG_PATH.open("w", encoding="utf-8") as log:
        for name, command in stages:
            exit_code, output = _run_stage(name, command, env=env, log=log)
            if exit_code:
                summary.update(
                    {
                        "status": "failed",
                        "failed_stage": name,
                        "failure_summary": _summarize_failure(
                            output, name, exit_code
                        ),
                    }
                )
                break
    summary["finished_at"] = datetime.now(timezone.utc).isoformat()
    SUMMARY_PATH.write_text(
        json.dumps(summary, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
