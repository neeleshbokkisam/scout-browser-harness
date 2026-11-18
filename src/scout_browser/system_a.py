"""System A: Scout + Exa search only (browser OFF). Scout loop unchanged."""

from __future__ import annotations

import json
import time
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from harness.systems import Catalog, System, SystemSpec

from scout_browser.odysseys2 import Odysseys2Task
from scout_browser.paths import EXA_BENCHMARKS, RESULTS

# Debug budgets — tighter than mentor caps; do not raise silently.
DEBUG_TIMEOUT_S = 600.0
DEBUG_MAX_COST_USD = 0.50
DEBUG_MAX_ROUNDS = 10

# Baseline (mentor-locked): $2 agent+judge → agent ≈1.75; Scout rounds stay catalog default.
BASELINE_TIMEOUT_S = 1800.0
BASELINE_MAX_COST_USD = 1.75
BASELINE_TOTAL_BUDGET_USD = 2.0
BASELINE_JUDGE_RESERVE_USD = 0.25
BASELINE_ROOT = RESULTS / "system-a-baseline"
SYSTEM_NAME = "scout-exa-auto-highlights"
VARIANT = "search_only"


def load_catalog() -> Catalog:
    return Catalog.load(EXA_BENCHMARKS / "systems.toml")


def resolve_system_a(
    *,
    timeout_s: float = DEBUG_TIMEOUT_S,
    max_cost_usd: float = DEBUG_MAX_COST_USD,
    max_rounds: int | None = None,
    model: str | None = None,
) -> SystemSpec:
    """Resolve stock Scout+Exa with budget overrides (no loop changes).

    If max_rounds is None, keep the catalog/default Scout rounds.
    """
    spec = load_catalog().resolve(SYSTEM_NAME, model=model)
    settings = {
        **spec.settings,
        "timeout_s": timeout_s,
        "max_cost_usd": max_cost_usd,
    }
    if max_rounds is not None:
        settings["max_rounds"] = max_rounds
    return replace(spec, settings=settings)


def make_run_dir(task_id: str, *, root: Path | None = None) -> Path:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    run_dir = (root or RESULTS) / "system-a-debug" / f"{task_id}-{stamp}"
    run_dir.mkdir(parents=True, exist_ok=False)
    return run_dir


def make_baseline_run_dir(task_id: str, *, root: Path | None = None) -> Path:
    """Stable per-task dir for resume-safe baseline runs."""
    run_dir = (root or BASELINE_ROOT) / task_id
    run_dir.mkdir(parents=True, exist_ok=True)
    return run_dir


def is_baseline_complete(task_id: str, *, root: Path | None = None) -> bool:
    """Completed = summary.json present under the stable task dir."""
    return ((root or BASELINE_ROOT) / task_id / "summary.json").is_file()


def write_json(path: Path, payload: Any) -> None:
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False, default=str) + "\n")


def _scores_from_grade(grade: dict[str, Any] | None) -> dict[str, Any]:
    if not grade:
        return {"full_score": None, "assessable_only": None, "item_credit": None}
    scores = grade.get("scores") or {}
    scoring = grade.get("scoring") or {}
    full = scores.get("full_score", scores.get("score"))
    assessable = scores.get("assessable_only", scoring.get("assessable_score"))
    return {
        "full_score": full,
        "assessable_only": assessable,
        "item_credit": scores.get("item_credit", scoring.get("item_credit")),
    }


async def run_task(
    task: Odysseys2Task,
    *,
    run_dir: Path | None = None,
    timeout_s: float = DEBUG_TIMEOUT_S,
    max_cost_usd: float = DEBUG_MAX_COST_USD,
    max_rounds: int | None = DEBUG_MAX_ROUNDS,
    model: str | None = None,
    judge: bool = True,
    judge_model: str | None = None,
    judge_reasoning_effort: str | None = "high",
    concurrency_note: int | None = None,
) -> dict[str, Any]:
    """Run one Odysseys2 task under System A; always write whatever artifacts exist."""
    from scout_browser.judge_odysseys2 import grade_result

    run_dir = run_dir or make_run_dir(task.id)
    prompt = task.scout_query
    (run_dir / "prompt.txt").write_text(prompt)

    # Keep combined agent+judge under TOTAL when possible: shrink agent cap if needed.
    agent_cap = max_cost_usd
    if judge:
        agent_cap = min(agent_cap, BASELINE_TOTAL_BUDGET_USD - BASELINE_JUDGE_RESERVE_USD)

    budgets = {
        "timeout_s": timeout_s,
        "max_cost_usd": agent_cap,
        "max_rounds": max_rounds,
        "concurrency": concurrency_note if concurrency_note is not None else 1,
        "browser": False,
        "tools": ["search", "submit_final_result"],
        "variant": VARIANT,
        "total_budget_usd": BASELINE_TOTAL_BUDGET_USD,
        "judge_reserve_usd": BASELINE_JUDGE_RESERVE_USD,
    }
    spec = resolve_system_a(
        timeout_s=timeout_s,
        max_cost_usd=agent_cap,
        max_rounds=max_rounds,
        model=model,
    )
    write_json(
        run_dir / "config.json",
        {
            "task_id": task.id,
            "title": task.title,
            "category": task.category,
            "start_url": task.start_url,
            "system": spec.to_dict(),
            "budgets": budgets,
            "created_at": datetime.now(timezone.utc).isoformat(),
        },
    )

    result: dict[str, Any] = {
        "task_id": task.id,
        "answer": "",
        "stop_reason": "error",
        "error": None,
        "model_cost_usd": None,
        "search_cost_usd": None,
        "total_cost_usd": None,
        "latency_ms": None,
        "tool_history": [],
        "messages": [],
        "budgets": budgets,
    }
    grade: dict[str, Any] | None = None
    wall_s: float | None = None
    system: System | None = None
    t0 = time.monotonic()
    try:
        system = System(spec)
        raw = await system.answer(prompt)
        wall_s = time.monotonic() - t0
        result = {
            **raw,
            "task_id": task.id,
            "wall_time_s": wall_s,
            "budgets": budgets,
            "variant": VARIANT,
            "error": None,
        }
        write_json(run_dir / "result.json", result)

        if judge:
            agent_spent = float(result.get("total_cost_usd") or 0.0)
            remaining = BASELINE_TOTAL_BUDGET_USD - agent_spent
            if remaining <= 0:
                grade = {
                    "task_id": task.id,
                    "error": "skipped_judge_over_total_budget",
                    "scores": {
                        "score": None,
                        "item_credit": None,
                        "full_score": None,
                        "assessable_only": None,
                    },
                    "evidence_notes": {
                        "screenshots": "empty",
                        "output_files": "empty",
                        "bias": "Judge skipped: agent already at/over $2 total budget.",
                    },
                    "judge_cost_usd": 0.0,
                }
                write_json(run_dir / "grade.json", grade)
            else:
                jmodel = judge_model or model or "openai/gpt-6-luna"
                try:
                    grade = await grade_result(
                        task,
                        result,
                        model=jmodel,
                        reasoning_effort=judge_reasoning_effort,
                    )
                    write_json(run_dir / "grade.json", grade)
                except Exception as exc:  # noqa: BLE001 — still report agent result
                    grade = {
                        "task_id": task.id,
                        "error": f"{type(exc).__name__}: {exc}",
                        "scores": {
                            "score": None,
                            "item_credit": None,
                            "full_score": None,
                            "assessable_only": None,
                        },
                        "evidence_notes": {
                            "screenshots": "empty",
                            "output_files": "empty",
                            "bias": "Judge failed; agent artifacts still written.",
                        },
                    }
                    write_json(run_dir / "grade.json", grade)
    except Exception as exc:  # noqa: BLE001 — write partial result
        wall_s = time.monotonic() - t0
        result["error"] = f"{type(exc).__name__}: {exc}"
        result["wall_time_s"] = wall_s
        result["latency_ms"] = wall_s * 1000
        write_json(run_dir / "result.json", result)
    finally:
        if system is not None:
            await system.close()

    score_bits = _scores_from_grade(grade)
    agent_usd = result.get("total_cost_usd")
    judge_usd = (grade or {}).get("judge_cost_usd") if grade else None
    combined = None
    if agent_usd is not None or judge_usd is not None:
        combined = float(agent_usd or 0.0) + float(judge_usd or 0.0)

    summary = {
        "task_id": task.id,
        "run_dir": str(run_dir),
        "stop_reason": result.get("stop_reason"),
        "score": score_bits["full_score"],
        "full_score": score_bits["full_score"],
        "assessable_only": score_bits["assessable_only"],
        "item_credit": score_bits["item_credit"],
        "model_cost_usd": result.get("model_cost_usd"),
        "search_cost_usd": result.get("search_cost_usd"),
        "total_cost_usd": result.get("total_cost_usd"),
        "agent_cost_usd": result.get("total_cost_usd"),
        "judge_cost_usd": judge_usd,
        "combined_cost_usd": combined,
        "wall_time_s": wall_s if wall_s is not None else result.get("wall_time_s"),
        "budgets": budgets,
        "concurrency": budgets.get("concurrency"),
        "error": result.get("error") or ((grade or {}).get("error") if grade else None),
        "screenshots": "empty",
        "output_files": "empty",
    }
    write_json(run_dir / "summary.json", summary)
    return summary
