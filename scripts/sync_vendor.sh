#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

mkdir -p vendor
clone_or_update() {
  local url="$1" dir="$2" ref="${3:-}"
  if [[ -d "$dir/.git" ]]; then
    git -C "$dir" fetch --depth 1 origin
    if [[ -n "$ref" ]]; then
      git -C "$dir" checkout "$ref"
    else
      git -C "$dir" pull --ff-only
    fi
  else
    rm -rf "$dir"
    git clone --depth 1 "$url" "$dir"
    if [[ -n "$ref" ]]; then
      git -C "$dir" fetch --depth 1 origin "$ref"
      git -C "$dir" checkout "$ref"
    fi
  fi
  echo "$dir @ $(git -C "$dir" rev-parse HEAD)"
}

clone_or_update https://github.com/exa-labs/benchmarks.git vendor/exa-benchmarks
clone_or_update https://github.com/ljang0/odysseys2-benchmark.git vendor/odysseys2-benchmark

echo "Update UPSTREAM.md with the printed SHAs."
