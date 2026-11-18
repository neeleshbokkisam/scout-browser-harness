#!/usr/bin/env python3
"""Re-judge existing result.json with optional screenshot images (no Scout rerun).

Writes ONLY under --out-root. Never overwrites source result.json / grade.json.

Example (smoke):
  cd vendor/exa-benchmarks
  set -a; source ../../.env; set +a
  PYTHONPATH=../../src:. uv run python ../../scripts/rejudge_multimodal.py \\
    --root ../../results/system-b-baseline \\
    --only cmub-001 cmub-002 \\
    --multimodal \\
    --out-root ../../results/rejudge-multimodal/system-b-baseline
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


def _env_set(name: str) -> bool:
    return bool(os.environ.get(name))


def write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, default=str) + "\n")


def _full_score(grade: dict[str, Any] | None) -> Any:
    if not grade:
        return None
    scores = grade.get("scores") or {}
    return scores.get("full_score", scores.get("score"))


def list_task_dirs(root: Path) -> list[Path]:
    if not root.is_dir():
        return []
    return sorted(
        p
        for p in root.iterdir()
        if p.is_dir() and (p.name.startswith("cmub-") or "-cmub-" in p.name)
        and (p / "result.json").is_file()
    )


def load_source_grade(run_dir: Path) -> dict[str, Any] | None:
    path = run_dir / "grade.json"
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text())
    except json.JSONDecodeError:
        return {"error": "grade.json decode error"}


def task_id_from_dir(run_dir: Path) -> str:
    name = run_dir.name
    if name.startswith("cmub-") and len(name) >= 8:
        # baseline: cmub-001 ; debug: cmub-001-2026... → cmub-001
        parts = name.split("-")
        if len(parts) >= 2 and parts[0] == "cmub" and parts[1].isdigit():
            return f"cmub-{parts[1]}"
    # fallback: read result
    try:
        result = json.loads((run_dir / "result.json").read_text())
        tid = result.get("task_id")
        if tid:
            return str(tid)
    except (OSError, json.JSONDecodeError):
        pass
    return name


async def rejudge_one(
    run_dir: Path,
    *,
    out_dir: Path,
    model: str,
    judge_reasoning_effort: str | None,
    multimodal: bool,
    max_images: int | None,
) -> dict[str, Any]:
    task_id = task_id_from_dir(run_dir)
    result = json.loads((run_dir / "result.json").read_text())
    tasks = {t.id: t for t in load_tasks()}
    task = tasks.get(task_id)
    if task is None:
        raise KeyError(f"unknown task id {task_id} (from {run_dir.name})")

    source_grade = load_source_grade(run_dir)
    text_score = _full_score(source_grade)
    text_judge_cost = (source_grade or {}).get("judge_cost_usd")

    grade = await grade_result(
        task,
        result,
        model=model,
        reasoning_effort=judge_reasoning_effort,
        screenshots=result.get("screenshots") if isinstance(result.get("screenshots"), list) else None,
        multimodal=multimodal,
        max_images=max_images,
        run_dir=run_dir,
    )
    write_json(out_dir / "grade.json", grade)

    mm = grade.get("multimodal") or {}
    mm_score = _full_score(grade)
    compare = {
        "task_id": task_id,
        "source_dir": str(run_dir),
        "out_dir": str(out_dir),
        "text_score": text_score,
        "mm_score": mm_score if multimodal else None,
        "score": mm_score,
        "assessable_only": (grade.get("scores") or {}).get("assessable_only"),
        "images_sent": mm.get("images_sent", 0),
        "image_paths": mm.get("image_paths") or [],
        "vision_error": mm.get("vision_error"),
        "fallback": mm.get("fallback"),
        "judge_cost_usd": grade.get("judge_cost_usd"),
        "text_judge_cost_usd": text_judge_cost,
        "judge_model": model,
        "judge_reasoning_effort": judge_reasoning_effort,
        "multimodal": multimodal,
        "error": grade.get("error"),
    }
    write_json(out_dir / "compare.json", compare)

    # Lightweight summary for the parallel tree only (do not touch source summary.json).
    write_json(
        out_dir / "summary.json",
        {
            "task_id": task_id,
            "source_dir": str(run_dir),
            "score": mm_score,
            "full_score": mm_score,
            "assessable_only": compare["assessable_only"],
            "judge_cost_usd": grade.get("judge_cost_usd"),
            "images_sent": compare["images_sent"],
            "multimodal": multimodal,
            "judge_reasoning_effort": judge_reasoning_effort,
            "error": grade.get("error"),
        },
    )
    return compare


async def main_async(args: argparse.Namespace) -> int:
    missing = [k for k in ("OPENAI_BASE_URL", "OPENAI_API_KEY") if not _env_set(k)]
    if missing:
        print(f"MISSING env: {', '.join(missing)}", file=sys.stderr)
        return 2

    root = Path(args.root).resolve()
    out_root = Path(args.out_root).resolve() if args.out_root else (
        ROOT / "results" / "rejudge-multimodal" / root.name
    ).resolve()

    dirs = list_task_dirs(root)
    if args.only:
        wanted = set(args.only)
        dirs = [
            d
            for d in dirs
            if d.name in wanted
            or task_id_from_dir(d) in wanted
            or any(d.name.startswith(f"{w}-") for w in wanted)
        ]

    # Prefer dirs that already have PNGs when scanning for smoke.
    if args.require_screenshots:
        dirs = [
            d
            for d in dirs
            if (d / "screenshots").is_dir() and any((d / "screenshots").glob("*.png"))
        ]

    print(
        json.dumps(
            {
                "phase": "scan",
                "root": str(root),
                "out_root": str(out_root),
                "n": len(dirs),
                "task_dirs": [d.name for d in dirs],
                "multimodal": args.multimodal,
                "reasoning_effort": args.reasoning_effort,
            }
        ),
        flush=True,
    )
    if args.scan_only:
        return 0
    if not dirs:
        print(json.dumps({"phase": "done", "note": "nothing_to_rejudge"}), flush=True)
        return 1

    model = os.environ.get("JUDGE_MODEL") or os.environ.get("SCOUT_MODEL") or "openai/gpt-6-luna"
    rows: list[dict[str, Any]] = []
    for run_dir in dirs:
        out_dir = out_root / run_dir.name
        print(
            json.dumps({"phase": "rejudge_start", "task_dir": run_dir.name, "out_dir": str(out_dir)}),
            flush=True,
        )
        try:
            row = await rejudge_one(
                run_dir,
                out_dir=out_dir,
                model=model,
                judge_reasoning_effort=args.reasoning_effort,
                multimodal=args.multimodal,
                max_images=args.max_images,
            )
        except Exception as exc:  # noqa: BLE001
            row = {
                "task_id": task_id_from_dir(run_dir),
                "source_dir": str(run_dir),
                "text_score": _full_score(load_source_grade(run_dir)),
                "mm_score": None,
                "images_sent": 0,
                "judge_cost_usd": None,
                "error": f"{type(exc).__name__}: {exc}",
                "multimodal": args.multimodal,
                "judge_reasoning_effort": args.reasoning_effort,
            }
        rows.append(row)
        print(json.dumps({"phase": "rejudge_done", **row}, default=str), flush=True)

    # Smoke / summary table
    print("", flush=True)
    print(
        f"{'task_id':<12} {'text_score':>10} {'mm_score':>10} {'#img':>5} {'judge$':>10} {'effort':<8} notes",
        flush=True,
    )
    print("-" * 80, flush=True)
    for row in rows:
        notes = row.get("error") or row.get("fallback") or row.get("vision_error") or ""
        print(
            f"{str(row.get('task_id')):<12} "
            f"{_fmt(row.get('text_score')):>10} "
            f"{_fmt(row.get('mm_score') if row.get('multimodal') else row.get('score')):>10} "
            f"{int(row.get('images_sent') or 0):>5} "
            f"{_fmt_cost(row.get('judge_cost_usd')):>10} "
            f"{str(row.get('judge_reasoning_effort') or ''):<8} "
            f"{notes}",
            flush=True,
        )

    write_json(out_root / "batch_compare.json", {"rows": rows, "multimodal": args.multimodal})
    ok = all(r.get("error") is None and (r.get("mm_score") is not None or r.get("score") is not None) for r in rows)
    vision_ok = args.multimodal and any(int(r.get("images_sent") or 0) > 0 for r in rows) and not any(
        r.get("fallback") == "text_only" and r.get("vision_error") for r in rows if int(r.get("images_sent") or 0) == 0 and r.get("vision_error")
    )
    # Vision supported if at least one task sent images and got a score without vision fallback.
    vision_supported = False
    if args.multimodal:
        for r in rows:
            if int(r.get("images_sent") or 0) > 0 and r.get("mm_score") is not None and not r.get("error"):
                vision_supported = True
                break
            if r.get("fallback") == "text_only" and r.get("vision_error"):
                vision_supported = False
                break

    print(
        json.dumps(
            {
                "phase": "done",
                "n": len(rows),
                "out_root": str(out_root),
                "vision_supported": vision_supported,
                "all_scored": ok,
                "vision_ok_hint": vision_ok,
            },
            default=str,
        ),
        flush=True,
    )
    return 0 if ok else 1


def _fmt(v: Any) -> str:
    if v is None:
        return "-"
    try:
        return f"{float(v):.2f}"
    except (TypeError, ValueError):
        return str(v)[:10]


def _fmt_cost(v: Any) -> str:
    if v is None:
        return "-"
    try:
        return f"{float(v):.4f}"
    except (TypeError, ValueError):
        return str(v)[:10]


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Side-by-side multimodal rejudge (does not touch source grades)"
    )
    parser.add_argument("--root", required=True, help="Source results root (e.g. results/system-b-baseline)")
    parser.add_argument(
        "--out-root",
        default=None,
        help="Parallel output root (default: results/rejudge-multimodal/<root.name>)",
    )
    parser.add_argument("--only", nargs="*", help="Task id or run-dir name allowlist")
    parser.add_argument("--reasoning-effort", default="high")
    parser.add_argument("--max-images", type=int, default=None)
    parser.add_argument(
        "--multimodal",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="Attach screenshot images (default: JUDGE_MULTIMODAL env, else off)",
    )
    parser.add_argument(
        "--require-screenshots",
        action="store_true",
        help="Only rejudge task dirs that already contain screenshots/*.png",
    )
    parser.add_argument("--scan-only", action="store_true")
    args = parser.parse_args()
    if args.multimodal is None:
        from scout_browser.judge_images import resolve_multimodal

        args.multimodal = resolve_multimodal(None)
    return asyncio.run(main_async(args))


if __name__ == "__main__":
    raise SystemExit(main())
