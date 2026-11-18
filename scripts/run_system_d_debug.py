#!/usr/bin/env python3
"""System D debug: Scout browser-only (C belt minus Exa search).

Example:
  cd vendor/exa-benchmarks
  set -a; source ../../.env; set +a
  PYTHONPATH=../../src:. uv run python ../../scripts/run_system_d_debug.py --task cmub-002

Default slice (n=6): cmub-001 002 009 074 053 036
Writes only under results/system-d-browser-only/ (never system-{a,b,c}-*).
Concurrency is always serial (1) so it can run beside a C baseline.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path
from typing import Any

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
    pass

from scout_browser.odysseys2 import filter_tasks, load_tasks  # noqa: E402
from scout_browser.paths import RESULTS  # noqa: E402
from scout_browser.system_a import write_json  # noqa: E402
from scout_browser.system_d import (  # noqa: E402
    DEBUG_ROOT,
    DEFAULT_SLICE,
    POLICY_VERIFY_MAX_COST_USD,
    POLICY_VERIFY_MAX_ROUNDS,
    POLICY_VERIFY_TIMEOUT_S,
    run_task,
)


def _env_set(name: str) -> bool:
    return bool(os.environ.get(name))


def _full_score_from_grade(grade: dict[str, Any] | None) -> float | None:
    if not grade:
        return None
    scores = grade.get("scores") or {}
    val = scores.get("full_score", scores.get("score"))
    return float(val) if val is not None else None


def _latest_c_debug_grade(task_id: str) -> float | None:
    root = RESULTS / "system-c-debug"
    if not root.is_dir():
        return None
    candidates = sorted(
        [p for p in root.glob(f"{task_id}-*") if (p / "grade.json").is_file()],
        key=lambda p: p.name,
    )
    if not candidates:
        return None
    grade = json.loads((candidates[-1] / "grade.json").read_text())
    return _full_score_from_grade(grade)


def _score_a(task_id: str) -> float | None:
    """Prefer pre-clipfix A grade for apples-to-apples vs B."""
    base = RESULTS / "system-a-baseline" / task_id
    pre = base / "grade.pre_clipfix.json"
    cur = base / "grade.json"
    path = pre if pre.is_file() else cur
    if not path.is_file():
        return None
    return _full_score_from_grade(json.loads(path.read_text()))


def _score_b(task_id: str) -> float | None:
    path = RESULTS / "system-b-baseline" / task_id / "grade.json"
    if not path.is_file():
        return None
    return _full_score_from_grade(json.loads(path.read_text()))


def _score_c(task_id: str) -> float | None:
    base = RESULTS / "system-c-baseline" / task_id / "grade.json"
    if base.is_file():
        return _full_score_from_grade(json.loads(base.read_text()))
    return _latest_c_debug_grade(task_id)


def write_batch_summary(summaries: list[dict[str, Any]], *, root: Path) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    rows = []
    for s in summaries:
        tid = str(s.get("task_id"))
        rows.append(
            {
                "task_id": tid,
                "D_full_score": s.get("full_score"),
                "A_full_score": _score_a(tid),
                "B_full_score": _score_b(tid),
                "C_full_score": _score_c(tid),
                "n_search": s.get("n_search"),
                "n_browser": s.get("n_browser"),
                "n_write_output_file": s.get("n_write_output_file"),
                "screenshot_count": s.get("screenshot_count"),
                "n_output_files": s.get("n_output_files"),
                "combined_cost_usd": s.get("combined_cost_usd"),
                "wall_time_s": s.get("wall_time_s"),
                "stop_reason": s.get("stop_reason"),
                "run_dir": s.get("run_dir"),
                "error": s.get("error"),
            }
        )
    scored = [r["D_full_score"] for r in rows if r["D_full_score"] is not None]
    payload = {
        "variant": "browser_only_plus_files",
        "n": len(rows),
        "mean_D_full_score": (sum(scored) / len(scored)) if scored else None,
        "rows": rows,
        "notes": (
            "A uses grade.pre_clipfix.json when present; C prefers "
            "system-c-baseline then latest system-c-debug. NA=0 full scores."
        ),
    }
    out = root / "batch_summary.json"
    write_json(out, payload)
    return out


def main() -> int:
    parser = argparse.ArgumentParser(
        description="System D Odysseys2 debug (browser-only = C minus search)"
    )
    parser.add_argument(
        "--task",
        action="append",
        dest="tasks",
        help="Odysseys2 task id (repeatable). Default: 6-task D slice",
    )
    parser.add_argument("--timeout-s", type=float, default=POLICY_VERIFY_TIMEOUT_S)
    parser.add_argument("--max-cost-usd", type=float, default=POLICY_VERIFY_MAX_COST_USD)
    parser.add_argument("--max-rounds", type=int, default=POLICY_VERIFY_MAX_ROUNDS)
    parser.add_argument("--no-judge", action="store_true")
    parser.add_argument("--headed", action="store_true", help="Show browser window")
    parser.add_argument(
        "--cdp-url",
        default=None,
        help="Optional CDP endpoint (explicit only; does not inherit BROWSER_USE_CDP_URL)",
    )
    args = parser.parse_args()

    # EXA_API_KEY not required — no SearchTool.
    missing = [k for k in ("OPENAI_BASE_URL", "OPENAI_API_KEY") if not _env_set(k)]
    if missing:
        print(f"MISSING env: {', '.join(missing)}", file=sys.stderr)
        return 2

    if args.tasks:
        task_ids = args.tasks
    else:
        task_ids = list(DEFAULT_SLICE)

    tasks = filter_tasks(load_tasks(), ids=task_ids)
    by_id = {t.id: t for t in tasks}
    missing_ids = [tid for tid in task_ids if tid not in by_id]
    if missing_ids:
        print(f"unknown task id(s): {missing_ids}", file=sys.stderr)
        return 2

    model = os.environ.get("SCOUT_MODEL") or None
    judge_model = os.environ.get("JUDGE_MODEL") or model or "openai/gpt-6-luna"
    # Do not inherit BROWSER_USE_CDP_URL — keep D on local Chromium so we
    # do not contend with a concurrent System C baseline on CDP.
    cdp_url = args.cdp_url or None

    DEBUG_ROOT.mkdir(parents=True, exist_ok=True)
    log_path = DEBUG_ROOT / "batch.log"
    summaries: list[dict] = []
    exit_code = 0

    def _log(obj: dict[str, Any]) -> None:
        line = json.dumps(obj, indent=2, default=str)
        print(line, flush=True)
        with log_path.open("a", encoding="utf-8") as fh:
            fh.write(line + "\n")

    for tid in task_ids:
        # Soft stop: D-only STOP_QUEUE file under D root.
        if (DEBUG_ROOT / "STOP_QUEUE").exists():
            _log({"phase": "stopped", "reason": "STOP_QUEUE", "remaining": tid})
            exit_code = 1
            break
        task = by_id[tid]
        _log(
            {
                "phase": "start",
                "variant": "browser_only_plus_files",
                "policy": "browser_only",
                "task_id": task.id,
                "title": task.title,
                "timeout_s": args.timeout_s,
                "max_cost_usd": args.max_cost_usd,
                "max_rounds": args.max_rounds,
                "judge": not args.no_judge,
                "headless": not args.headed,
                "cdp": bool(cdp_url),
                "concurrency": 1,
                "search": False,
            }
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
                concurrency_note=1,
            )
        )
        _log({"phase": "done", **summary})
        summaries.append(summary)
        if summary.get("error"):
            exit_code = 1
        # Confirm ablation: search must be zero.
        if int(summary.get("n_search") or 0) != 0:
            _log({"phase": "warn", "msg": "n_search != 0", "task_id": tid})
            exit_code = 1

    if summaries:
        out = write_batch_summary(summaries, root=DEBUG_ROOT)
        _log(
            {
                "phase": "batch_done",
                "n": len(summaries),
                "batch_summary": str(out),
                "rows": json.loads(out.read_text()).get("rows"),
            }
        )
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
