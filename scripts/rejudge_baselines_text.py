#!/usr/bin/env python3
"""Re-judge A/B/C artifacts with the clipfix text-judge recipe (no agent reruns).

- Backs up existing grade.json → grade.pre_clipfix.json (once)
- Reloads screenshot labels + output_files previews from disk
- Forces multimodal=False (text judge primary)
- Patches summary.json scores only; leaves result.json untouched

Example:
  cd vendor/exa-benchmarks
  set -a; source ../../.env; set +a
  PYTHONPATH=../../src:. uv run python ../../scripts/rejudge_baselines_text.py --system a --concurrency 12
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
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

from scout_browser.judge_odysseys2 import CLIP_BUDGET_DOC, grade_result  # noqa: E402
from scout_browser.odysseys2 import load_tasks  # noqa: E402
from scout_browser.paths import RESULTS  # noqa: E402
from scout_browser.system_a import write_json  # noqa: E402
from scout_browser.system_b import collect_output_files  # noqa: E402

SYSTEM_ROOTS = {
    "a": RESULTS / "system-a-baseline",
    "b": RESULTS / "system-b-baseline",
    "c-debug": RESULTS / "system-c-debug",
}
BACKUP_NAME = "grade.pre_clipfix.json"


def _env_set(name: str) -> bool:
    return bool(os.environ.get(name))


def list_run_dirs(root: Path, *, system: str, only: list[str] | None) -> list[Path]:
    if not root.is_dir():
        return []
    dirs: list[Path] = []
    if system == "c-debug":
        # Prefer latest stamped dir per task id.
        by_id: dict[str, Path] = {}
        for child in sorted(root.iterdir()):
            if not child.is_dir() or not (child / "result.json").is_file():
                continue
            tid = child.name.split("-20")[0] if "-20" in child.name else child.name
            if only and tid not in only and child.name not in only:
                continue
            by_id[tid] = child  # sorted ascending → last wins
        dirs = [by_id[k] for k in sorted(by_id)]
        return dirs

    for child in sorted(root.iterdir()):
        if not child.is_dir() or not child.name.startswith("cmub-"):
            continue
        if only and child.name not in only:
            continue
        if (child / "result.json").is_file():
            dirs.append(child)
    return dirs


def load_screenshots(run_dir: Path, result: dict[str, Any]) -> list[dict[str, Any]] | None:
    path = run_dir / "screenshots.json"
    if path.is_file():
        try:
            data = json.loads(path.read_text())
            if isinstance(data, list):
                return data
        except json.JSONDecodeError:
            pass
    shots = result.get("screenshots")
    return shots if isinstance(shots, list) else None


def backup_grade(run_dir: Path) -> None:
    grade_path = run_dir / "grade.json"
    backup_path = run_dir / BACKUP_NAME
    if grade_path.is_file() and not backup_path.is_file():
        backup_path.write_text(grade_path.read_text())


def na_rate_from_grade(grade: dict[str, Any] | None) -> tuple[int, int]:
    if not grade:
        return 0, 0
    scoring = grade.get("scoring") or {}
    items = scoring.get("per_item") or []
    if not items:
        return 0, 0
    na = sum(1 for it in items if it.get("status") == "not_assessable")
    return na, len(items)


def full_score(grade: dict[str, Any] | None) -> float | None:
    if not grade:
        return None
    scores = grade.get("scores") or {}
    val = scores.get("full_score", scores.get("score"))
    try:
        return float(val) if val is not None else None
    except (TypeError, ValueError):
        return None


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
            "task_id": summary.get("task_id") or run_dir.name.split("-20")[0],
            "run_dir": str(run_dir),
            "score": full,
            "full_score": full,
            "assessable_only": assessable,
            "item_credit": scores.get("item_credit"),
            "judge_cost_usd": judge_usd,
            "combined_cost_usd": combined,
            "error": grade.get("error") or None,
            "rejudged_clipfix": True,
        }
    )
    if full is not None and not grade.get("error"):
        summary["error"] = None
    write_json(summary_path, summary)
    return summary


async def rejudge_one(
    run_dir: Path,
    *,
    tasks: dict[str, Any],
    model: str,
    reasoning_effort: str | None,
    sem: asyncio.Semaphore,
) -> dict[str, Any]:
    async with sem:
        tid = run_dir.name.split("-20")[0] if "-20" in run_dir.name else run_dir.name
        task = tasks.get(tid)
        if task is None:
            return {"task_id": tid, "run_dir": str(run_dir), "error": "unknown_task"}
        result = json.loads((run_dir / "result.json").read_text())
        old_grade = None
        backup = run_dir / BACKUP_NAME
        grade_path = run_dir / "grade.json"
        if backup.is_file():
            try:
                old_grade = json.loads(backup.read_text())
            except json.JSONDecodeError:
                old_grade = None
        elif grade_path.is_file():
            try:
                old_grade = json.loads(grade_path.read_text())
            except json.JSONDecodeError:
                old_grade = None

        backup_grade(run_dir)
        screenshots = load_screenshots(run_dir, result)
        output_files = collect_output_files(run_dir)
        t0 = time.monotonic()
        grade = await grade_result(
            task,
            result,
            model=model,
            reasoning_effort=reasoning_effort,
            screenshots=screenshots,
            output_files=output_files,
            multimodal=False,
            run_dir=run_dir,
        )
        write_json(run_dir / "grade.json", grade)
        summary = patch_summary(run_dir, grade)
        old_full = full_score(old_grade)
        new_full = full_score(grade)
        old_na, old_n = na_rate_from_grade(old_grade)
        new_na, new_n = na_rate_from_grade(grade)
        return {
            "task_id": tid,
            "run_dir": str(run_dir),
            "old_full": old_full,
            "new_full": new_full,
            "delta_full": None if old_full is None or new_full is None else new_full - old_full,
            "old_na_items": old_na,
            "old_items": old_n,
            "new_na_items": new_na,
            "new_items": new_n,
            "n_output_files": len(output_files),
            "judge_cost_usd": grade.get("judge_cost_usd"),
            "wall_s": round(time.monotonic() - t0, 1),
            "error": grade.get("error"),
            "assessable_only": summary.get("assessable_only"),
        }


def rewrite_batch_summary(root: Path) -> Path | None:
    rows: list[dict[str, Any]] = []
    for child in sorted(root.iterdir()):
        if not child.is_dir() or not child.name.startswith("cmub-"):
            continue
        sp = child / "summary.json"
        if not sp.is_file():
            continue
        try:
            s = json.loads(sp.read_text())
        except json.JSONDecodeError:
            continue
        rows.append(
            {
                "task_id": s.get("task_id") or child.name,
                "full_score": s.get("full_score", s.get("score")),
                "assessable_only": s.get("assessable_only"),
                "n_search": s.get("n_search"),
                "n_browser": s.get("n_browser"),
                "screenshot_count": s.get("screenshot_count"),
                "stop_reason": s.get("stop_reason"),
                "agent_$": s.get("agent_cost_usd", s.get("total_cost_usd")),
                "judge_$": s.get("judge_cost_usd"),
                "wall_s": s.get("wall_time_s"),
                "concurrency": s.get("concurrency") or (s.get("budgets") or {}).get("concurrency"),
                "rejudged_clipfix": s.get("rejudged_clipfix"),
            }
        )
    out = root / "batch_summary.json"
    # Preserve prior summary beside new one.
    legacy = root / "batch_summary.pre_clipfix.json"
    if out.is_file() and not legacy.is_file():
        legacy.write_text(out.read_text())
    write_json(out, {"rows": rows, "count": len(rows), "recipe": "clipfix_text"})
    return out


def aggregate(rows: list[dict[str, Any]]) -> dict[str, Any]:
    fulls_old = [r["old_full"] for r in rows if isinstance(r.get("old_full"), (int, float))]
    fulls_new = [r["new_full"] for r in rows if isinstance(r.get("new_full"), (int, float))]
    old_na = sum(int(r.get("old_na_items") or 0) for r in rows)
    old_items = sum(int(r.get("old_items") or 0) for r in rows)
    new_na = sum(int(r.get("new_na_items") or 0) for r in rows)
    new_items = sum(int(r.get("new_items") or 0) for r in rows)
    return {
        "n": len(rows),
        "old_full_mean": sum(fulls_old) / len(fulls_old) if fulls_old else None,
        "new_full_mean": sum(fulls_new) / len(fulls_new) if fulls_new else None,
        "delta_full_mean": (
            (sum(fulls_new) / len(fulls_new)) - (sum(fulls_old) / len(fulls_old))
            if fulls_old and fulls_new
            else None
        ),
        "old_na_pct": (100.0 * old_na / old_items) if old_items else None,
        "new_na_pct": (100.0 * new_na / new_items) if new_items else None,
        "errors": sum(1 for r in rows if r.get("error")),
    }


async def run_system(
    system: str,
    *,
    concurrency: int,
    only: list[str] | None,
    model: str,
    reasoning_effort: str | None,
) -> dict[str, Any]:
    root = SYSTEM_ROOTS[system]
    run_dirs = list_run_dirs(root, system=system, only=only)
    tasks = {t.id: t for t in load_tasks()}
    sem = asyncio.Semaphore(concurrency)
    print(
        json.dumps(
            {
                "phase": "kickoff",
                "system": system,
                "root": str(root),
                "n": len(run_dirs),
                "concurrency": concurrency,
                "clip_budgets": CLIP_BUDGET_DOC,
            },
            default=str,
        ),
        flush=True,
    )
    coros = [
        rejudge_one(
            d,
            tasks=tasks,
            model=model,
            reasoning_effort=reasoning_effort,
            sem=sem,
        )
        for d in run_dirs
    ]
    rows: list[dict[str, Any]] = []
    for fut in asyncio.as_completed(coros):
        row = await fut
        rows.append(row)
        print(json.dumps({"phase": "task_done", "system": system, **row}, default=str), flush=True)

    rows.sort(key=lambda r: str(r.get("task_id") or ""))
    stats = aggregate(rows)
    out = {
        "system": system,
        "stats": stats,
        "rows": rows,
        "clip_budgets": CLIP_BUDGET_DOC,
    }
    report_path = root / "rejudge_clipfix_report.json"
    write_json(report_path, out)
    if system in ("a", "b"):
        rewrite_batch_summary(root)
    print(json.dumps({"phase": "system_done", "system": system, "stats": stats, "report": str(report_path)}, default=str), flush=True)
    return out


def main() -> int:
    parser = argparse.ArgumentParser(description="Text rejudge with clipfix recipe")
    parser.add_argument(
        "--system",
        choices=["a", "b", "c-debug", "a,b", "all"],
        default="a,b",
    )
    parser.add_argument("--concurrency", type=int, default=12)
    parser.add_argument("--only", nargs="*", default=None)
    parser.add_argument("--reasoning-effort", default="high")
    args = parser.parse_args()

    missing = [k for k in ("OPENAI_BASE_URL", "OPENAI_API_KEY") if not _env_set(k)]
    if missing:
        print(f"MISSING env: {', '.join(missing)}", file=sys.stderr)
        return 2

    if args.system == "all":
        systems = ["a", "b", "c-debug"]
    elif args.system == "a,b":
        systems = ["a", "b"]
    else:
        systems = [args.system]

    model = os.environ.get("JUDGE_MODEL") or os.environ.get("SCOUT_MODEL") or "openai/gpt-6-luna"

    async def _run() -> list[dict[str, Any]]:
        outs = []
        for sys_name in systems:
            outs.append(
                await run_system(
                    sys_name,
                    concurrency=args.concurrency,
                    only=args.only,
                    model=model,
                    reasoning_effort=args.reasoning_effort,
                )
            )
        return outs

    outs = asyncio.run(_run())
    print(json.dumps({"phase": "final", "systems": [{k: o[k] for k in ("system", "stats") if k in o} for o in outs]}, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
