#!/usr/bin/env python3
"""System B debug: Odysseys2 task(s), Scout + Exa search + Playwright browser.

Example:
  cd vendor/exa-benchmarks
  set -a; source ../../.env; set +a
  PYTHONPATH=../../src:. uv run python ../../scripts/run_system_b_debug.py --task cmub-002
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
from scout_browser.system_b import (  # noqa: E402
    POLICY_VERIFY_MAX_COST_USD,
    POLICY_VERIFY_MAX_ROUNDS,
    POLICY_VERIFY_TIMEOUT_S,
    run_task,
)


def _env_set(name: str) -> bool:
    return bool(os.environ.get(name))


def main() -> int:
    parser = argparse.ArgumentParser(description="System B Odysseys2 debug (search + browser)")
    parser.add_argument(
        "--task",
        action="append",
        dest="tasks",
        help="Odysseys2 task id (repeatable). Default: cmub-002",
    )
    parser.add_argument("--timeout-s", type=float, default=POLICY_VERIFY_TIMEOUT_S)
    parser.add_argument("--max-cost-usd", type=float, default=POLICY_VERIFY_MAX_COST_USD)
    parser.add_argument("--max-rounds", type=int, default=POLICY_VERIFY_MAX_ROUNDS)
    parser.add_argument("--no-judge", action="store_true")
    parser.add_argument("--headed", action="store_true", help="Show browser window")
    parser.add_argument(
        "--no-mid-run-nudge",
        action="store_true",
        help="Disable soft mid-run search-result nudge (prompt policy still applies)",
    )
    parser.add_argument(
        "--cdp-url",
        default=None,
        help="Optional CDP endpoint (defaults to BROWSER_USE_CDP_URL if set)",
    )
    args = parser.parse_args()

    missing = [k for k in ("OPENAI_BASE_URL", "OPENAI_API_KEY", "EXA_API_KEY") if not _env_set(k)]
    if missing:
        print(f"MISSING env: {', '.join(missing)}", file=sys.stderr)
        return 2

    task_ids = args.tasks or ["cmub-002"]
    tasks = filter_tasks(load_tasks(), ids=task_ids)
    by_id = {t.id: t for t in tasks}
    missing_ids = [tid for tid in task_ids if tid not in by_id]
    if missing_ids:
        print(f"unknown task id(s): {missing_ids}", file=sys.stderr)
        return 2

    model = os.environ.get("SCOUT_MODEL") or None
    judge_model = os.environ.get("JUDGE_MODEL") or model or "openai/gpt-6-luna"
    cdp_url = args.cdp_url or os.environ.get("BROWSER_USE_CDP_URL") or None
    enable_mid_run_nudge = not args.no_mid_run_nudge

    summaries: list[dict] = []
    exit_code = 0
    for tid in task_ids:
        task = by_id[tid]
        print(
            json.dumps(
                {
                    "phase": "start",
                    "variant": "search_plus_browser",
                    "policy": "complementary_s_to_b",
                    "task_id": task.id,
                    "title": task.title,
                    "timeout_s": args.timeout_s,
                    "max_cost_usd": args.max_cost_usd,
                    "max_rounds": args.max_rounds,
                    "judge": not args.no_judge,
                    "headless": not args.headed,
                    "cdp": bool(cdp_url),
                    "enable_mid_run_nudge": enable_mid_run_nudge,
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
                headless=not args.headed,
                cdp_url=cdp_url,
                enable_mid_run_nudge=enable_mid_run_nudge,
            )
        )
        print(json.dumps({"phase": "done", **summary}, indent=2, default=str), flush=True)
        summaries.append(summary)
        if summary.get("error"):
            exit_code = 1

    if len(summaries) > 1:
        print(
            json.dumps(
                {
                    "phase": "batch_done",
                    "n": len(summaries),
                    "rows": [
                        {
                            "task_id": s.get("task_id"),
                            "policy_triggered": s.get("policy_triggered"),
                            "n_search": s.get("n_search"),
                            "n_browser": s.get("n_browser"),
                            "screenshot_count": s.get("screenshot_count"),
                            "full_score": s.get("full_score"),
                            "assessable_only": s.get("assessable_only"),
                            "combined_cost_usd": s.get("combined_cost_usd"),
                            "stop_reason": s.get("stop_reason"),
                            "run_dir": s.get("run_dir"),
                            "error": s.get("error"),
                        }
                        for s in summaries
                    ],
                },
                indent=2,
                default=str,
            ),
            flush=True,
        )
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
