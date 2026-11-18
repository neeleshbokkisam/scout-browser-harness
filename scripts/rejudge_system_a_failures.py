#!/usr/bin/env python3
"""Re-judge System A baseline failures from existing result.json (no Scout reruns).

Example:
  cd vendor/exa-benchmarks
  set -a; source ../../.env; set +a
  PYTHONPATH=../../src:. uv run python ../../scripts/rejudge_system_a_failures.py
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

from scout_browser.judge_odysseys2 import grade_result  # noqa: E402
from scout_browser.odysseys2 import load_tasks  # noqa: E402
from scout_browser.system_a import BASELINE_ROOT, write_json  # noqa: E402


def _env_set(name: str) -> bool:
    return bool(os.environ.get(name))


def _full_score(grade: dict[str, Any] | None) -> Any:
    if not grade:
        return None
    scores = grade.get("scores") or {}
    return scores.get("full_score", scores.get("score"))


def is_grade_failure(grade: dict[str, Any] | None, *, grade_exists: bool) -> bool:
    if not grade_exists or grade is None:
        return True
    if grade.get("error"):
        return True
    return _full_score(grade) is None


def scan_failures(*, root: Path = BASELINE_ROOT) -> list[str]:
    failed: list[str] = []
    if not root.is_dir():
        return failed
    for child in sorted(root.iterdir()):
        if not child.is_dir() or not child.name.startswith("cmub-"):
            continue
        result_path = child / "result.json"
        grade_path = child / "grade.json"
        if not result_path.is_file():
            # No agent result → cannot rejudge; leave for separate handling.
            continue
        grade = None
        grade_exists = grade_path.is_file()
        if grade_exists:
            try:
                grade = json.loads(grade_path.read_text())
            except json.JSONDecodeError:
                grade = {"error": "grade.json decode error"}
        if is_grade_failure(grade, grade_exists=grade_exists):
            failed.append(child.name)
    return failed


def patch_summary(run_dir: Path, grade: dict[str, Any]) -> dict[str, Any]:
    summary_path = run_dir / "summary.json"
    summary: dict[str, Any] = {}
    if summary_path.is_file():
        try:
            summary = json.loads(summary_path.read_text())
        except json.JSONDecodeError:
            summary = {}
    scores = grade.get("scores") or {}
    full = scores.get("full_score", scores.get("score"))
    assessable = scores.get("assessable_only")
    judge_usd = grade.get("judge_cost_usd")
    agent_usd = summary.get("agent_cost_usd", summary.get("total_cost_usd"))
    combined = None
    if agent_usd is not None or judge_usd is not None:
        combined = float(agent_usd or 0.0) + float(judge_usd or 0.0)
    summary.update(
        {
            "task_id": summary.get("task_id") or run_dir.name,
            "run_dir": str(run_dir),
            "score": full,
            "full_score": full,
            "assessable_only": assessable,
            "item_credit": scores.get("item_credit"),
            "judge_cost_usd": judge_usd,
            "combined_cost_usd": combined,
            "error": grade.get("error") or None,
            "rejudged": True,
        }
    )
    # Clear stale judge-only error when scores are present.
    if full is not None and not grade.get("error"):
        summary["error"] = None
    write_json(summary_path, summary)
    return summary


async def rejudge_one(
    task_id: str,
    *,
    root: Path,
    model: str,
    judge_reasoning_effort: str | None,
) -> dict[str, Any]:
    run_dir = root / task_id
    result = json.loads((run_dir / "result.json").read_text())
    tasks = {t.id: t for t in load_tasks()}
    task = tasks.get(task_id)
    if task is None:
        raise KeyError(f"unknown task id {task_id}")
    grade = await grade_result(
        task,
        result,
        model=model,
        reasoning_effort=judge_reasoning_effort,
    )
    write_json(run_dir / "grade.json", grade)
    summary = patch_summary(run_dir, grade)
    return {
        "task_id": task_id,
        "full_score": summary.get("full_score"),
        "assessable_only": summary.get("assessable_only"),
        "judge_cost_usd": summary.get("judge_cost_usd"),
        "error": summary.get("error"),
    }


async def main_async(args: argparse.Namespace) -> int:
    missing = [k for k in ("OPENAI_BASE_URL", "OPENAI_API_KEY") if not _env_set(k)]
    if missing:
        print(f"MISSING env: {', '.join(missing)}", file=sys.stderr)
        return 2

    root = Path(args.root)
    failed_before = scan_failures(root=root)
    if args.only:
        wanted = set(args.only)
        failed_before = [t for t in failed_before if t in wanted] or [
            t for t in args.only if (root / t / "result.json").is_file()
        ]

    print(
        json.dumps({"phase": "scan", "failed_ids_before": failed_before, "n": len(failed_before)}),
        flush=True,
    )
    if not failed_before:
        print(json.dumps({"phase": "done", "failed_ids_after": [], "note": "nothing_to_rejudge"}), flush=True)
        return 0

    model = os.environ.get("JUDGE_MODEL") or os.environ.get("SCOUT_MODEL") or "openai/gpt-6-luna"
    results: list[dict[str, Any]] = []
    for task_id in failed_before:
        print(json.dumps({"phase": "rejudge_start", "task_id": task_id}), flush=True)
        try:
            row = await rejudge_one(
                task_id,
                root=root,
                model=model,
                judge_reasoning_effort=args.reasoning_effort,
            )
        except Exception as exc:  # noqa: BLE001
            row = {
                "task_id": task_id,
                "full_score": None,
                "assessable_only": None,
                "judge_cost_usd": None,
                "error": f"{type(exc).__name__}: {exc}",
            }
        results.append(row)
        print(json.dumps({"phase": "rejudge_done", **row}, default=str), flush=True)

    failed_after = scan_failures(root=root)
    print(
        json.dumps(
            {
                "phase": "done",
                "failed_ids_before": failed_before,
                "failed_ids_after": failed_after,
                "results": results,
                "baseline_grades_complete": len(failed_after) == 0,
            },
            default=str,
        ),
        flush=True,
    )
    return 0 if not failed_after else 1


def main() -> int:
    parser = argparse.ArgumentParser(description="Re-judge System A baseline grade failures")
    parser.add_argument("--root", default=str(BASELINE_ROOT))
    parser.add_argument("--only", nargs="*", help="Optional task id allowlist")
    parser.add_argument("--reasoning-effort", default="high")
    parser.add_argument("--scan-only", action="store_true")
    args = parser.parse_args()
    if args.scan_only:
        failed = scan_failures(root=Path(args.root))
        print(json.dumps({"failed_ids": failed, "n": len(failed)}))
        return 0
    return asyncio.run(main_async(args))


if __name__ == "__main__":
    raise SystemExit(main())
