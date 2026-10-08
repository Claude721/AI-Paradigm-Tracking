"""Hermetic finite-campaign latency replay, with no real APIs or SMTP.

Outputs assumed latency and simulated seconds explicitly. This is a scheduler
and persistence experiment, never a prediction of provider/model quality.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--origins", type=int, default=10000)
    parser.add_argument("--latency-seconds", type=float, default=20)
    parser.add_argument("--budget-seconds", type=int, default=3600)
    parser.add_argument("--isolated", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args()
    if not 1 <= args.origins <= 20000 or not 0 < args.latency_seconds <= 120 or not 60 <= args.budget_seconds <= 7200:
        parser.error("origins=1..20000, latency=(0,120], budget=60..7200")
    if not args.isolated:
        from scripts.offline_checks import _safe_environment
        command = [sys.executable, str(Path(__file__).resolve()), "--isolated", "--origins", str(args.origins),
                   "--latency-seconds", str(args.latency_seconds), "--budget-seconds", str(args.budget_seconds)]
        raise SystemExit(subprocess.run(command, cwd=ROOT, env=_safe_environment()).returncode)
    if os.environ.get("AI_RADAR_SKIP_DOTENV") != "true" or os.environ.get("HTTP_PROXY") != "http://127.0.0.1:9":
        raise SystemExit("replay requires the hermetic, network-denied environment")
    from tests.test_research_campaign import pressure_replay
    result = asyncio.run(pressure_replay(count=args.origins, latency_seconds=args.latency_seconds, budget_seconds=args.budget_seconds))
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
