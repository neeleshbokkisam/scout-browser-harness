#!/usr/bin/env python3
"""Sanity-check Odysseys2 task load (no API calls)."""

from __future__ import annotations

import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from scout_browser.odysseys2 import filter_tasks, load_tasks  # noqa: E402
from scout_browser.paths import EXA_BENCHMARKS, ODYSSEYS2_TASKS  # noqa: E402


def main() -> None:
    assert ODYSSEYS2_TASKS.exists(), ODYSSEYS2_TASKS
    assert (EXA_BENCHMARKS / "harness" / "scout.py").exists()
    tasks = load_tasks()
    cats = Counter(t.category for t in tasks)
    sample = filter_tasks(tasks, limit=3)
    print(f"odysseys2 tasks: {len(tasks)}")
    print(f"categories: {dict(cats)}")
    print("sample:")
    for t in sample:
        print(f"  {t.id} [{t.category}] {t.title} ({len(t.rubric)} rubric items)")
        print(f"    start: {t.start_url}")
    print("ok — workspace is personal scout-browser-harness (not Merged Systems)")


if __name__ == "__main__":
    main()
