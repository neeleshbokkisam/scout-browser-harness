#!/usr/bin/env python3
"""System A debug: one Odysseys2 task, Scout+Exa search only, tight budgets.

Example:
  cd vendor/exa-benchmarks
  set -a; source ../../.env; set +a
  PYTHONPATH=../../src:. uv run python ../../scripts/run_system_a_debug.py --task cmub-002
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
EXA = ROOT / "vendor" / "exa-benchmarks"
for path in (SRC, EXA):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

try:
    from dotenv import load_dotenv

    load_dotenv(ROOT / ".env")
except ImportError:
    pass  # shell may have already sourced .env

from scout_browser.odysseys2 import filter_tasks, load_tasks  # noqa: E402
from scout_browser.system_a import (  # noqa: E402
    DEBUG_MAX_COST_USD,
    DEBUG_MAX_ROUNDS,
    DEBUG_TIMEOUT_S,
    run_task,
)


def _env_set(name: str) -> bool:
    return bool(os.environ.get(name))


def main() -> int:
    parser = argparse.ArgumentParser(description="System A Odysseys2 debug (browser OFF)")
    parser.add_argument("--task", default="cmub-002", help="Odysseys2 task id")
    parser.add_argument("--timeout-s", type=float, default=DEBUG_TIMEOUT_S)
    parser.add_argument("--max-cost-usd", type=float, default=DEBUG_MAX_COST_USD)
    parser.add_argument("--max-rounds", type=int, default=DEBUG_MAX_ROUNDS)
    parser.add_argument("--no-judge", action="store_true")
    args = parser.parse_args()

    missing = [k for k in ("OPENAI_BASE_URL", "OPENAI_API_KEY", "EXA_API_KEY") if not _env_set(k)]
    if missing:
        print(f"MISSING env: {', '.join(missing)}", file=sys.stderr)
        return 2

    tasks = filter_tasks(load_tasks(), ids=[args.task])
    if not tasks:
        print(f"unknown task id {args.task!r}", file=sys.stderr)
        return 2
    task = tasks[0]

    model = os.environ.get("SCOUT_MODEL") or None
    judge_model = os.environ.get("JUDGE_MODEL") or model or "openai/gpt-6-luna"

    print(
        json.dumps(
            {
                "phase": "start",
                "task_id": task.id,
                "title": task.title,
                "timeout_s": args.timeout_s,
                "max_cost_usd": args.max_cost_usd,
                "max_rounds": args.max_rounds,
                "judge": not args.no_judge,
            },
            indent=2,
        ),
        flush=True,
    )

    summary = asyncio.run(
        run_task(
            task,
            timeout_s=args.timeout_s,
            max_cost_usd=args.max_cost_usd,
            max_rounds=args.max_rounds,
            model=model,
            judge=not args.no_judge,
            judge_model=judge_model,
        )
    )
    print(json.dumps({"phase": "done", **summary}, indent=2, default=str), flush=True)
    return 0 if not summary.get("error") else 1


if __name__ == "__main__":
    raise SystemExit(main())
