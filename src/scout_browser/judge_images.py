"""Screenshot selection + Responses API image parts for multimodal Odysseys2 judging."""

from __future__ import annotations

import base64
import os
import struct
from pathlib import Path
from typing import Any

DEFAULT_MAX_IMAGES = 8
DEFAULT_MAX_BYTES = 1_500_000
DEFAULT_MAX_DIM = 1600


def env_flag_true(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def resolve_multimodal(explicit: bool | None = None) -> bool:
    if explicit is not None:
        return bool(explicit)
    return env_flag_true("JUDGE_MULTIMODAL", default=False)


def resolve_max_images(explicit: int | None = None) -> int:
    if explicit is not None:
        return max(0, int(explicit))
    raw = os.environ.get("JUDGE_MAX_IMAGES")
    if raw is None or not str(raw).strip():
        return DEFAULT_MAX_IMAGES
    try:
        return max(0, int(raw))
    except ValueError:
        return DEFAULT_MAX_IMAGES


def _png_dims(path: Path) -> tuple[int, int] | None:
    """Read width/height from PNG IHDR without decoding pixels."""
    try:
        with path.open("rb") as fh:
            header = fh.read(24)
        if len(header) < 24 or header[:8] != b"\x89PNG\r\n\x1a\n":
            return None
        width, height = struct.unpack(">II", header[16:24])
        return int(width), int(height)
    except OSError:
        return None


def resolve_screenshot_path(
    shot: dict[str, Any],
    *,
    run_dir: Path | None = None,
) -> Path | None:
    raw = shot.get("screenshot") or shot.get("path")
    if not raw:
        return None
    path = Path(str(raw))
    candidates: list[Path] = []
    if path.is_absolute():
        candidates.append(path)
    if run_dir is not None:
        candidates.append(run_dir / path.name)
        candidates.append(run_dir / "screenshots" / path.name)
        if not path.is_absolute():
            candidates.append(run_dir / path)
    for candidate in candidates:
        try:
            if candidate.is_file():
                return candidate.resolve()
        except OSError:
            continue
    return None


def _diverse_indices(n: int, k: int) -> list[int]:
    """Pick up to k indices spanning [0, n): first, last, then evenly spaced."""
    if n <= 0 or k <= 0:
        return []
    if k >= n:
        return list(range(n))
    if k == 1:
        return [0]
    chosen: list[int] = [0, n - 1]
    if k == 2:
        return chosen
    interior = k - 2
    for i in range(1, interior + 1):
        idx = round(i * (n - 1) / (interior + 1))
        if idx not in chosen:
            chosen.append(idx)
    chosen = sorted(set(chosen))
    remaining = [i for i in range(n) if i not in chosen]
    while len(chosen) < k and remaining:
        best = max(remaining, key=lambda i: min(abs(i - c) for c in chosen))
        chosen.append(best)
        remaining.remove(best)
    return sorted(chosen)[:k]


def select_screenshot_images(
    screenshots: list[dict[str, Any]] | None,
    *,
    run_dir: Path | None = None,
    max_images: int = DEFAULT_MAX_IMAGES,
    max_bytes: int = DEFAULT_MAX_BYTES,
    max_dim: int = DEFAULT_MAX_DIM,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Select diverse, valid PNGs for the judge.

    Returns (selected, skipped) where each entry includes path/label/tool/reason.
    """
    selected: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    if not screenshots or max_images <= 0:
        return selected, skipped

    ordered = sorted(
        enumerate(screenshots),
        key=lambda pair: (
            pair[1].get("index") is None,
            pair[1].get("index") if isinstance(pair[1].get("index"), int) else 10**9,
            pair[0],
        ),
    )

    eligible: list[dict[str, Any]] = []
    for _, shot in ordered:
        label = shot.get("label") or "?"
        path = resolve_screenshot_path(shot, run_dir=run_dir)
        if path is None:
            skipped.append(
                {"label": label, "reason": "missing_file", "path": shot.get("screenshot")}
            )
            continue
        try:
            size = path.stat().st_size
        except OSError as exc:
            skipped.append({"label": label, "reason": f"stat_error:{exc}", "path": str(path)})
            continue
        if size <= 0:
            skipped.append({"label": label, "reason": "empty_file", "path": str(path)})
            continue
        if size > max_bytes:
            skipped.append(
                {
                    "label": label,
                    "reason": f"over_max_bytes:{size}>{max_bytes}",
                    "path": str(path),
                }
            )
            continue
        dims = _png_dims(path)
        if dims is not None:
            w, h = dims
            if max(w, h) > max_dim:
                skipped.append(
                    {
                        "label": label,
                        "reason": f"over_max_dim:{w}x{h}>{max_dim}",
                        "path": str(path),
                    }
                )
                continue
        eligible.append(
            {
                "path": str(path),
                "label": label,
                "tool": shot.get("tool"),
                "index": shot.get("index"),
                "url": shot.get("url"),
                "bytes": size,
                "dims": dims,
            }
        )

    if not eligible:
        return selected, skipped

    indices = _diverse_indices(len(eligible), max_images)
    picked = [eligible[i] for i in indices]
    used_tools = {p.get("tool") for p in picked if p.get("tool")}
    unused = [
        e
        for e in eligible
        if e not in picked and e.get("tool") and e.get("tool") not in used_tools
    ]
    for i in range(1, len(picked) - 1):
        if not unused:
            break
        tool = picked[i].get("tool")
        if tool and sum(1 for p in picked if p.get("tool") == tool) > 1:
            replacement = unused.pop(0)
            picked[i] = replacement
            used_tools.add(replacement.get("tool"))

    picked.sort(
        key=lambda p: (
            p.get("index") is None,
            p.get("index") if isinstance(p.get("index"), int) else 10**9,
            p.get("path") or "",
        )
    )
    seen_paths: set[str] = set()
    for item in picked:
        path = item["path"]
        if path in seen_paths:
            continue
        seen_paths.add(path)
        selected.append(item)
        if len(selected) >= max_images:
            break

    selected_paths = {s["path"] for s in selected}
    for item in eligible:
        if item["path"] not in selected_paths:
            skipped.append(
                {
                    "label": item.get("label"),
                    "reason": "not_selected_cap",
                    "path": item["path"],
                }
            )
    return selected, skipped


def encode_image_parts(selected: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Build OpenAI Responses API input_image parts from selected PNGs."""
    parts: list[dict[str, Any]] = []
    for item in selected:
        path = Path(item["path"])
        data = path.read_bytes()
        b64 = base64.standard_b64encode(data).decode("ascii")
        parts.append(
            {
                "type": "input_image",
                "image_url": f"data:image/png;base64,{b64}",
            }
        )
    return parts
