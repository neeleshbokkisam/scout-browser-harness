from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
VENDOR = ROOT / "vendor"
EXA_BENCHMARKS = VENDOR / "exa-benchmarks"
ODYSSEYS2 = VENDOR / "odysseys2-benchmark"
ODYSSEYS2_TASKS = ODYSSEYS2 / "odysseys2-tasks.json"
RESULTS = ROOT / "results"
CONFIGS = ROOT / "configs"
