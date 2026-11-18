"""System C: Scout + search + Playwright (wait/scroll) + write_output_file.

P3 complementary policy retained. One explainable Scout loop string change
(NO_TOOL_NUDGE tool-agnostic) lives in vendor scout.py.
"""

from __future__ import annotations

import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from harness.scout import Scout, ScoutConfig
from harness.systems import SystemSpec, build_searcher
from harness.tools import SearchTool

from scout_browser.browser_tools import BrowserSession, system_c_browser_tools
from scout_browser.odysseys2 import Odysseys2Task
from scout_browser.output_tools import default_output_tools
from scout_browser.paths import RESULTS
from scout_browser.policy import (
    CountingBrowserTool,
    NudgeSearchTool,
    build_system_c_prompt,
    build_user_prompt_c,
    count_tool_calls,
    initial_policy_meta,
    policy_triggered,
)
from scout_browser.system_a import (
    BASELINE_JUDGE_RESERVE_USD,
    BASELINE_TOTAL_BUDGET_USD,
    DEBUG_MAX_COST_USD,
    DEBUG_MAX_ROUNDS,
    DEBUG_TIMEOUT_S,
    _scores_from_grade,
    resolve_system_a,
    write_json,
)
from scout_browser.system_b import collect_output_files, collect_screenshot_manifest

VARIANT = "search_plus_browser_plus_files"
BROWSER_TOOL_NAMES = [
    "browser_navigate",
    "browser_snapshot",
    "browser_click",
    "browser_type",
    "browser_extract",
    "browser_wait",
    "browser_scroll",
]
SYSTEM_C_TOOLS = ["search", *BROWSER_TOOL_NAMES, "write_output_file", "submit_final_result"]
DEBUG_ROOT = RESULTS / "system-c-debug"

# Baseline (match System A/B mentor caps); P3 complementary + wait/scroll/files.
BASELINE_TIMEOUT_S = 1800.0
BASELINE_MAX_COST_USD = 1.75
BASELINE_ROOT = RESULTS / "system-c-baseline"
SYSTEM_A_COMPLETED_IDS = RESULTS / "system-a-baseline" / "completed_task_ids.json"

POLICY_VERIFY_TIMEOUT_S = 900.0
POLICY_VERIFY_MAX_COST_USD = 1.0
POLICY_VERIFY_MAX_ROUNDS = 15


def make_run_dir(task_id: str, *, root: Path | None = None) -> Path:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    run_dir = (root or DEBUG_ROOT) / f"{task_id}-{stamp}"
    run_dir.mkdir(parents=True, exist_ok=False)
    return run_dir


def make_baseline_run_dir(task_id: str, *, root: Path | None = None) -> Path:
    """Stable per-task dir for resume-safe System C baseline runs."""
    run_dir = (root or BASELINE_ROOT) / task_id
    run_dir.mkdir(parents=True, exist_ok=True)
    return run_dir


def is_baseline_complete(task_id: str, *, root: Path | None = None) -> bool:
    """Completed = grade.json present (resume-safe skip)."""
    return ((root or BASELINE_ROOT) / task_id / "grade.json").is_file()


def _scout_settings(spec: SystemSpec) -> tuple[dict[str, Any], int]:
    settings = dict(spec.settings)
    num_results = int(settings.pop("num_results", 10))
    settings.pop("enrich_exa_contents", None)
    return settings, num_results


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
    headless: bool = True,
    cdp_url: str | None = None,
    enable_mid_run_nudge: bool = True,
) -> dict[str, Any]:
    """Run one Odysseys2 task under System C; always write whatever artifacts exist."""
    from scout_browser.judge_odysseys2 import grade_result

    run_dir = run_dir or make_run_dir(task.id)
    policy_meta = initial_policy_meta(task, enable_mid_run_nudge=enable_mid_run_nudge)
    policy_meta["variant"] = VARIANT
    prompt = build_user_prompt_c(task, interaction_heavy=bool(policy_meta["interaction_heavy"]))
    (run_dir / "prompt.txt").write_text(prompt)
    screenshot_dir = run_dir / "screenshots"
    screenshot_dir.mkdir(parents=True, exist_ok=True)
    output_root = run_dir / "output_files"
    output_root.mkdir(parents=True, exist_ok=True)

    agent_cap = max_cost_usd
    if judge:
        agent_cap = min(agent_cap, BASELINE_TOTAL_BUDGET_USD - BASELINE_JUDGE_RESERVE_USD)

    budgets = {
        "timeout_s": timeout_s,
        "max_cost_usd": agent_cap,
        "max_rounds": max_rounds,
        "concurrency": concurrency_note if concurrency_note is not None else 1,
        "browser": True,
        "tools": list(SYSTEM_C_TOOLS),
        "variant": VARIANT,
        "total_budget_usd": BASELINE_TOTAL_BUDGET_USD,
        "judge_reserve_usd": BASELINE_JUDGE_RESERVE_USD,
        "cdp": bool(cdp_url),
        "policy": policy_meta["name"],
        "enable_mid_run_nudge": enable_mid_run_nudge,
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
            "policy": policy_meta,
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
        "variant": VARIANT,
        "policy": policy_meta,
    }
    grade: dict[str, Any] | None = None
    wall_s: float | None = None
    screenshots: list[dict[str, Any]] = []
    output_files: list[dict[str, Any]] = []
    tool_counts = {"search": 0, "browser": 0, "write_output_file": 0}
    session: BrowserSession | None = None
    searcher = None
    t0 = time.monotonic()
    try:
        if spec.searcher is None:
            raise ValueError("System C requires an Exa searcher catalog entry")
        options = {k: v for k, v in spec.searcher.items() if k not in ("name", "provider")}
        searcher = build_searcher(spec.searcher["provider"], options)
        scout_kwargs, num_results = _scout_settings(spec)
        scout_kwargs["system_prompt"] = build_system_c_prompt()
        session = BrowserSession(
            headless=headless,
            screenshot_dir=screenshot_dir,
            cdp_url=cdp_url,
        )
        await session.start()
        browser_counter = {"browser": 0}
        search_tool = NudgeSearchTool(
            SearchTool(searcher, num_results=num_results),
            interaction_heavy=bool(policy_meta["interaction_heavy"]),
            enable_mid_run_nudge=enable_mid_run_nudge,
            after_searches=int(policy_meta["mid_run_nudge_after_searches"]),
            policy_meta=policy_meta,
            browser_call_counter=browser_counter,
        )
        browser_tools = [
            CountingBrowserTool(t, browser_counter) for t in system_c_browser_tools(session)
        ]
        output_tools = default_output_tools(output_root)
        tools = [search_tool, *browser_tools, *output_tools]
        assert spec.model is not None
        scout = Scout(ScoutConfig(model=spec.model, **scout_kwargs), tools=tools)
        raw = (await scout.run(prompt)).to_dict()
        wall_s = time.monotonic() - t0
        tool_counts = count_tool_calls(raw.get("messages"))
        screenshots = collect_screenshot_manifest(
            run_dir=run_dir,
            session=session,
            tool_history=raw.get("tool_history"),
        )
        output_files = collect_output_files(run_dir)
        result = {
            **raw,
            "task_id": task.id,
            "wall_time_s": wall_s,
            "budgets": budgets,
            "variant": VARIANT,
            "error": None,
            "screenshots": screenshots,
            "output_files": [{"path": f["path"], "bytes": None} for f in output_files],
            "policy": dict(policy_meta),
            "tool_counts": tool_counts,
        }
        write_json(run_dir / "result.json", result)
        write_json(run_dir / "screenshots.json", screenshots)
        write_json(run_dir / "policy.json", policy_meta)

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
                        "screenshots": f"{len(screenshots)} captured",
                        "output_files": f"{len(output_files)} files",
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
                        screenshots=screenshots,
                        output_files=output_files,
                    )
                    write_json(run_dir / "grade.json", grade)
                except Exception as exc:  # noqa: BLE001
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
                            "screenshots": f"{len(screenshots)} captured",
                            "output_files": f"{len(output_files)} files",
                            "bias": "Judge failed; agent artifacts still written.",
                        },
                    }
                    write_json(run_dir / "grade.json", grade)
    except Exception as exc:  # noqa: BLE001
        wall_s = time.monotonic() - t0
        result["error"] = f"{type(exc).__name__}: {exc}"
        result["wall_time_s"] = wall_s
        result["latency_ms"] = wall_s * 1000
        result["policy"] = dict(policy_meta)
        if session is not None:
            screenshots = collect_screenshot_manifest(
                run_dir=run_dir, session=session, tool_history=result.get("tool_history")
            )
            result["screenshots"] = screenshots
        output_files = collect_output_files(run_dir)
        result["output_files"] = [{"path": f["path"], "bytes": None} for f in output_files]
        write_json(run_dir / "result.json", result)
        write_json(run_dir / "policy.json", policy_meta)
    finally:
        if session is not None:
            try:
                await session.close()
            except Exception:  # noqa: BLE001
                pass
        if searcher is not None:
            try:
                await searcher.close()
            except Exception:  # noqa: BLE001
                pass

    score_bits = _scores_from_grade(grade)
    agent_usd = result.get("total_cost_usd")
    judge_usd = (grade or {}).get("judge_cost_usd") if grade else None
    combined = None
    if agent_usd is not None or judge_usd is not None:
        combined = float(agent_usd or 0.0) + float(judge_usd or 0.0)

    shot_note = f"{len(screenshots)} files" if screenshots else "empty"
    files_note = f"{len(output_files)} files" if output_files else "empty"
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
        "screenshots": shot_note,
        "screenshot_count": len(screenshots),
        "output_files": files_note,
        "n_output_files": len(output_files),
        "variant": VARIANT,
        "policy": dict(policy_meta),
        "policy_triggered": policy_triggered(policy_meta),
        "tool_counts": tool_counts,
        "n_search": tool_counts.get("search", 0),
        "n_browser": tool_counts.get("browser", 0),
        "n_write_output_file": tool_counts.get("write_output_file", 0),
    }
    write_json(run_dir / "summary.json", summary)
    return summary
