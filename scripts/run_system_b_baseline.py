#!/usr/bin/env python3
"""System B baseline: Odysseys2 × Scout search+Playwright+P3, resume-safe queue.

Same task IDs as System A (results/system-a-baseline/completed_task_ids.json).
Browser ON, complementary S→B policy, mid-run nudge on.

Example:
  cd vendor/exa-benchmarks
  set -a; source ../../.env; set +a
  PYTHONPATH=../../src:. uv run python ../../scripts/run_system_b_baseline.py

Stop queue (drain in-flight):
  touch ../../results/system-b-baseline/STOP_QUEUE
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import random
import sys
import time
from collections import deque
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

from scout_browser.odysseys2 import Odysseys2Task, load_tasks  # noqa: E402
from scout_browser.system_b import (  # noqa: E402
    BASELINE_MAX_COST_USD,
    BASELINE_ROOT,
    BASELINE_TIMEOUT_S,
    SYSTEM_A_COMPLETED_IDS,
    is_baseline_complete,
    make_baseline_run_dir,
    run_task,
    write_json,
)

SEED = 42
CONCURRENCY_LEVELS = [4, 8, 12, 15, 20]
STORM_WINDOW = 8
STORM_THRESHOLD = 3
RAMP_STABLE_COMPLETIONS = 6
STATUS_LOG = BASELINE_ROOT / "status.jsonl"
STOP_FILE = BASELINE_ROOT / "STOP_QUEUE"
COMPLETED_IDS_PATH = BASELINE_ROOT / "completed_task_ids.json"
SUMMARY_TABLE_PATH = BASELINE_ROOT / "batch_summary.json"
QUEUE_STATE_PATH = BASELINE_ROOT / "queue_state.json"

_RATE_MARKERS = (
    "rate limit",
    "rate_limit",
    "ratelimit",
    "too many requests",
    "429",
    "resource_exhausted",
    "quota",
    "overloaded",
)
_BROWSER_CRASH_MARKERS = (
    "browser has been closed",
    "browser closed",
    "target closed",
    "target crashed",
    "page crashed",
    "playwright",
    "chromium",
    "browsercontext",
    "session closed",
    "websocket",
    "cdp",
    "navigation failed",
)
_OOM_MARKERS = (
    "out of memory",
    "oom",
    "cannot allocate",
    "memoryerror",
    "killed",
    "sigkill",
    "enomem",
)


def _env_set(name: str) -> bool:
    return bool(os.environ.get(name))


def load_system_a_task_ids(path: Path = SYSTEM_A_COMPLETED_IDS) -> list[str]:
    if not path.is_file():
        raise FileNotFoundError(f"System A completed IDs missing: {path}")
    payload = json.loads(path.read_text())
    ids = payload.get("task_ids") if isinstance(payload, dict) else payload
    if not isinstance(ids, list) or not ids:
        raise ValueError(f"No task_ids in {path}")
    return [str(x) for x in ids]


def seeded_order(tasks: list[Odysseys2Task], *, seed: int = SEED) -> list[Odysseys2Task]:
    ordered = list(tasks)
    rng = random.Random(seed)
    rng.shuffle(ordered)
    return ordered


def pending_tasks(tasks: list[Odysseys2Task], *, root: Path = BASELINE_ROOT) -> list[Odysseys2Task]:
    return [t for t in tasks if not is_baseline_complete(t.id, root=root)]


def classify_failure(error: str | None, stop_reason: str | None) -> str | None:
    """Return storm class for concurrency control, or None if healthy."""
    blob = f"{error or ''} {stop_reason or ''}".lower()
    if any(m in blob for m in _OOM_MARKERS):
        return "oom"
    if any(m in blob for m in _RATE_MARKERS):
        return "rate_limit"
    if any(m in blob for m in _BROWSER_CRASH_MARKERS) and (
        "error" in blob
        or "fail" in blob
        or "closed" in blob
        or "crash" in blob
        or stop_reason == "error"
    ):
        return "browser_crash"
    if stop_reason == "error" and error:
        # Generic hard errors also count toward storm window (not immediate).
        return "error"
    return None


def fmt_money(x: Any) -> str:
    if x is None:
        return "?"
    try:
        return f"${float(x):.3f}"
    except (TypeError, ValueError):
        return "?"


def fmt_score(x: Any) -> str:
    if x is None:
        return "na"
    try:
        return f"{float(x):.1f}"
    except (TypeError, ValueError):
        return "na"


def status_line(summary: dict[str, Any], *, concurrency: int, done: int, total: int) -> str:
    return (
        f"STATUS task={summary.get('task_id')} "
        f"full={fmt_score(summary.get('full_score'))} "
        f"assessable={fmt_score(summary.get('assessable_only'))} "
        f"search={summary.get('n_search', '?')} "
        f"browser={summary.get('n_browser', '?')} "
        f"screenshots={summary.get('screenshot_count', '?')} "
        f"agent={fmt_money(summary.get('agent_cost_usd') or summary.get('total_cost_usd'))} "
        f"judge={fmt_money(summary.get('judge_cost_usd'))} "
        f"wall_s={float(summary.get('wall_time_s') or 0):.0f} "
        f"stop={summary.get('stop_reason')} "
        f"concurrency={concurrency} "
        f"done={done}/{total}"
        + (f" err={summary.get('error')}" if summary.get("error") else "")
    )


def append_status(record: dict[str, Any]) -> None:
    BASELINE_ROOT.mkdir(parents=True, exist_ok=True)
    with STATUS_LOG.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")


def load_completed_summaries(*, root: Path = BASELINE_ROOT) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    if not root.is_dir():
        return rows
    for child in sorted(root.iterdir()):
        if not child.is_dir():
            continue
        summary_path = child / "summary.json"
        if summary_path.is_file():
            try:
                rows.append(json.loads(summary_path.read_text()))
            except json.JSONDecodeError:
                continue
    return rows


def write_stop_artifacts(*, root: Path = BASELINE_ROOT) -> dict[str, Any]:
    rows = load_completed_summaries(root=root)
    # Prefer grade.json presence for completed ID list (resume contract).
    ids: list[str] = []
    if root.is_dir():
        for child in sorted(root.iterdir()):
            if child.is_dir() and (child / "grade.json").is_file():
                ids.append(child.name)
    write_json(COMPLETED_IDS_PATH, {"task_ids": ids, "count": len(ids), "seed": SEED})
    by_id = {r.get("task_id"): r for r in rows if r.get("task_id")}
    table = []
    for tid in ids:
        r = by_id.get(tid, {"task_id": tid})
        table.append(
            {
                "task_id": tid,
                "full_score": r.get("full_score", r.get("score")),
                "assessable_only": r.get("assessable_only"),
                "n_search": r.get("n_search"),
                "n_browser": r.get("n_browser"),
                "screenshot_count": r.get("screenshot_count"),
                "stop_reason": r.get("stop_reason"),
                "agent_$": r.get("agent_cost_usd", r.get("total_cost_usd")),
                "judge_$": r.get("judge_cost_usd"),
                "wall_s": r.get("wall_time_s"),
                "concurrency": r.get("concurrency") or (r.get("budgets") or {}).get("concurrency"),
                "policy_triggered": r.get("policy_triggered"),
            }
        )
    write_json(SUMMARY_TABLE_PATH, {"rows": table, "count": len(table)})
    return {
        "completed_task_ids": str(COMPLETED_IDS_PATH),
        "batch_summary": str(SUMMARY_TABLE_PATH),
        "n": len(ids),
    }


class ConcurrencyController:
    """Ramp 4→8→12→15→20; immediate step-down on OOM / browser crash / rate storms."""

    def __init__(self, start: int = 4, levels: list[int] | None = None) -> None:
        self.levels = levels or CONCURRENCY_LEVELS
        self.idx = 0
        while self.idx < len(self.levels) - 1 and self.levels[self.idx] < start:
            self.idx += 1
        if start in self.levels:
            self.idx = self.levels.index(start)
        self.current = self.levels[self.idx]
        self._recent_storm: deque[bool] = deque(maxlen=STORM_WINDOW)
        self._stable_ok = 0
        self._lock = asyncio.Lock()
        self.highest_stable = self.current

    async def note(self, *, kind: str | None) -> int:
        async with self._lock:
            immediate = kind in ("oom", "browser_crash", "rate_limit")
            storm = kind is not None
            self._recent_storm.append(storm)
            if immediate and self.idx > 0:
                self.idx -= 1
                self.current = self.levels[self.idx]
                self._stable_ok = 0
                self._recent_storm.clear()
                print(
                    f"STATUS concurrency_down={self.current} reason={kind}_immediate "
                    f"highest_stable={self.highest_stable}",
                    flush=True,
                )
            elif storm:
                self._stable_ok = 0
                storms = sum(self._recent_storm)
                if storms >= STORM_THRESHOLD and self.idx > 0:
                    self.idx -= 1
                    self.current = self.levels[self.idx]
                    self._recent_storm.clear()
                    print(
                        f"STATUS concurrency_down={self.current} reason=error_storm "
                        f"highest_stable={self.highest_stable}",
                        flush=True,
                    )
            else:
                self._stable_ok += 1
                if self.current > self.highest_stable:
                    self.highest_stable = self.current
                if (
                    self._stable_ok >= RAMP_STABLE_COMPLETIONS
                    and self.idx < len(self.levels) - 1
                    and sum(self._recent_storm) == 0
                ):
                    self.idx += 1
                    self.current = self.levels[self.idx]
                    self._stable_ok = 0
                    print(
                        f"STATUS concurrency_up={self.current} reason=stable "
                        f"highest_stable={self.highest_stable}",
                        flush=True,
                    )
            return self.current


async def run_queue(
    tasks: list[Odysseys2Task],
    *,
    timeout_s: float,
    max_cost_usd: float,
    model: str | None,
    judge_model: str | None,
    start_concurrency: int,
    limit: int | None,
    headless: bool,
    cdp_url: str | None,
    enable_mid_run_nudge: bool,
) -> int:
    BASELINE_ROOT.mkdir(parents=True, exist_ok=True)
    if STOP_FILE.exists():
        STOP_FILE.unlink()

    queue = pending_tasks(tasks)
    if limit is not None:
        queue = queue[:limit]
    total_planned = len(queue)
    already = len(tasks) - len(pending_tasks(tasks))
    print(
        f"STATUS kickoff already_done={already} queued={total_planned} "
        f"seed={SEED} timeout_s={timeout_s} max_cost_usd={max_cost_usd} "
        f"browser=ON policy=complementary_s_to_b mid_run_nudge={int(enable_mid_run_nudge)} "
        f"concurrency_start={start_concurrency}",
        flush=True,
    )
    append_status(
        {
            "phase": "kickoff",
            "already_done": already,
            "queued": total_planned,
            "seed": SEED,
            "timeout_s": timeout_s,
            "max_cost_usd": max_cost_usd,
            "browser": True,
            "policy": "complementary_s_to_b",
            "enable_mid_run_nudge": enable_mid_run_nudge,
            "concurrency": start_concurrency,
        }
    )

    ctrl = ConcurrencyController(start=start_concurrency)
    stop_new = False
    in_flight: set[asyncio.Task[Any]] = set()
    completed_this_run = 0
    q_index = 0
    pending_notes: list[str | None] = []

    async def worker(task: Odysseys2Task, concurrency_at_start: int) -> dict[str, Any]:
        run_dir = make_baseline_run_dir(task.id)
        if is_baseline_complete(task.id):
            summary_path = run_dir / "summary.json"
            if summary_path.is_file():
                return json.loads(summary_path.read_text())
            return {"task_id": task.id, "stop_reason": "already_complete", "full_score": None}
        return await run_task(
            task,
            run_dir=run_dir,
            timeout_s=timeout_s,
            max_cost_usd=max_cost_usd,
            max_rounds=None,  # Scout catalog default
            model=model,
            judge=True,
            judge_model=judge_model,
            concurrency_note=concurrency_at_start,
            headless=headless,
            cdp_url=cdp_url,
            enable_mid_run_nudge=enable_mid_run_nudge,
        )

    def on_done(fut: asyncio.Task[Any]) -> None:
        nonlocal completed_this_run
        in_flight.discard(fut)
        try:
            summary = fut.result()
        except Exception as exc:  # noqa: BLE001
            summary = {
                "task_id": "?",
                "full_score": None,
                "assessable_only": None,
                "n_search": None,
                "n_browser": None,
                "screenshot_count": None,
                "stop_reason": "error",
                "error": f"{type(exc).__name__}: {exc}",
                "wall_time_s": None,
                "agent_cost_usd": None,
                "judge_cost_usd": None,
            }
        completed_this_run += 1
        done_total = already + completed_this_run
        line = status_line(
            summary,
            concurrency=ctrl.current,
            done=done_total,
            total=already + total_planned,
        )
        print(line, flush=True)
        print(
            f"STATUS concurrency_now={ctrl.current} highest_stable={ctrl.highest_stable}",
            flush=True,
        )
        append_status(
            {
                "phase": "task_done",
                "concurrency": ctrl.current,
                "highest_stable": ctrl.highest_stable,
                **summary,
            }
        )
        pending_notes.append(classify_failure(summary.get("error"), summary.get("stop_reason")))

    while q_index < len(queue) or in_flight:
        if STOP_FILE.exists() and not stop_new:
            stop_new = True
            print("STATUS stop_queue=1 draining_in_flight", flush=True)
            append_status({"phase": "stop_queue", "in_flight": len(in_flight)})

        while pending_notes:
            await ctrl.note(kind=pending_notes.pop(0))

        target = ctrl.current
        # If concurrency dropped below in-flight, just stop launching until slots free.
        while not stop_new and q_index < len(queue) and len(in_flight) < target:
            task = queue[q_index]
            q_index += 1
            write_json(
                QUEUE_STATE_PATH,
                {
                    "next_index": q_index,
                    "queued_remaining": max(0, len(queue) - q_index),
                    "in_flight": len(in_flight) + 1,
                    "concurrency": target,
                    "highest_stable": ctrl.highest_stable,
                    "stop_new": stop_new,
                    "updated_at": time.time(),
                },
            )
            fut = asyncio.create_task(worker(task, target))
            in_flight.add(fut)
            fut.add_done_callback(on_done)

        if not in_flight and (stop_new or q_index >= len(queue)):
            break

        if in_flight:
            await asyncio.wait(in_flight, return_when=asyncio.FIRST_COMPLETED)
        else:
            await asyncio.sleep(0.2)

    while pending_notes:
        await ctrl.note(kind=pending_notes.pop(0))

    artifacts = write_stop_artifacts()
    print(
        f"STATUS final completed={artifacts['n']} "
        f"highest_stable={ctrl.highest_stable} concurrency={ctrl.current} "
        f"completed_task_ids={artifacts['completed_task_ids']} "
        f"batch_summary={artifacts['batch_summary']}",
        flush=True,
    )
    append_status(
        {
            "phase": "final",
            "highest_stable": ctrl.highest_stable,
            "concurrency": ctrl.current,
            **artifacts,
        }
    )
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description="System B Odysseys2 baseline batch (search + browser + P3)"
    )
    parser.add_argument("--timeout-s", type=float, default=BASELINE_TIMEOUT_S)
    parser.add_argument("--max-cost-usd", type=float, default=BASELINE_MAX_COST_USD)
    parser.add_argument("--concurrency", type=int, default=4)
    parser.add_argument("--seed", type=int, default=SEED)
    parser.add_argument("--limit", type=int, default=None, help="Optional cap on new tasks this run")
    parser.add_argument("--headed", action="store_true")
    parser.add_argument("--no-mid-run-nudge", action="store_true")
    parser.add_argument("--cdp-url", default=None)
    parser.add_argument("--ids-from", type=Path, default=SYSTEM_A_COMPLETED_IDS)
    parser.add_argument("--write-stop-artifacts-only", action="store_true")
    args = parser.parse_args()

    if args.write_stop_artifacts_only:
        BASELINE_ROOT.mkdir(parents=True, exist_ok=True)
        artifacts = write_stop_artifacts()
        print(json.dumps(artifacts, indent=2))
        return 0

    missing = [k for k in ("OPENAI_BASE_URL", "OPENAI_API_KEY", "EXA_API_KEY") if not _env_set(k)]
    if missing:
        print(f"MISSING env: {', '.join(missing)}", file=sys.stderr)
        return 2

    a_ids = load_system_a_task_ids(args.ids_from)
    a_id_set = set(a_ids)
    all_loaded = load_tasks()
    by_id = {t.id: t for t in all_loaded}
    missing_ids = [tid for tid in a_ids if tid not in by_id]
    if missing_ids:
        print(f"unknown task id(s) from System A list: {missing_ids[:10]}...", file=sys.stderr)
        return 2

    # Same seed shuffle as A, restricted to A's completed IDs.
    tasks = seeded_order([by_id[tid] for tid in a_ids if tid in a_id_set], seed=args.seed)

    model = os.environ.get("SCOUT_MODEL") or None
    judge_model = os.environ.get("JUDGE_MODEL") or model or "openai/gpt-6-luna"
    cdp_url = args.cdp_url or os.environ.get("BROWSER_USE_CDP_URL") or None

    return asyncio.run(
        run_queue(
            tasks,
            timeout_s=args.timeout_s,
            max_cost_usd=args.max_cost_usd,
            model=model,
            judge_model=judge_model,
            start_concurrency=args.concurrency,
            limit=args.limit,
            headless=not args.headed,
            cdp_url=cdp_url,
            enable_mid_run_nudge=not args.no_mid_run_nudge,
        )
    )


if __name__ == "__main__":
    raise SystemExit(main())
