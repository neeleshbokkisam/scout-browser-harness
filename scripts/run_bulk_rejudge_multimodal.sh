#!/usr/bin/env bash
# Durable bulk multimodal rejudge for A and B in parallel (side-by-side outs only).
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT/vendor/exa-benchmarks"
set -a
# shellcheck disable=SC1091
source "$ROOT/.env"
set +a
export PYTHONPATH="$ROOT/src:."

mkdir -p "$ROOT/results/rejudge-multimodal"
: > "$ROOT/results/rejudge-multimodal/bulk-a.log"
: > "$ROOT/results/rejudge-multimodal/bulk-b.log"

uv run python "$ROOT/scripts/rejudge_multimodal.py" \
  --root "$ROOT/results/system-a-baseline" \
  --multimodal \
  --reasoning-effort high \
  --out-root "$ROOT/results/rejudge-multimodal/system-a-baseline" \
  >"$ROOT/results/rejudge-multimodal/bulk-a.log" 2>&1 &
A_PID=$!

uv run python "$ROOT/scripts/rejudge_multimodal.py" \
  --root "$ROOT/results/system-b-baseline" \
  --multimodal \
  --reasoning-effort high \
  --out-root "$ROOT/results/rejudge-multimodal/system-b-baseline" \
  >"$ROOT/results/rejudge-multimodal/bulk-b.log" 2>&1 &
B_PID=$!

echo "started A_PID=$A_PID B_PID=$B_PID" | tee "$ROOT/results/rejudge-multimodal/bulk-pids.txt"
wait "$A_PID"
A_EC=$?
wait "$B_PID"
B_EC=$?
echo "finished A_ec=$A_EC B_ec=$B_EC" | tee "$ROOT/results/rejudge-multimodal/bulk-done.log"
exit $(( A_EC != 0 || B_EC != 0 ? 1 : 0 ))
