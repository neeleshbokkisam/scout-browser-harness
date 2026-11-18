"""Sandboxed deliverable writer for System C — run_dir/output_files/ only."""

from __future__ import annotations

import copy
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

try:
    from harness.tools import ToolOutcome
except ImportError:  # pragma: no cover

    @dataclass
    class ToolOutcome:
        content: str
        error: str | None = None
        final_answer: str | None = None
        records: list[dict[str, Any]] = field(default_factory=list)
        evidence: list[dict[str, Any]] = field(default_factory=list)
        num_searches: int = 0
        queries_used: int = 0
        cost_usd: float = 0.0
        cost_known: bool = True
        latency_ms: float = 0.0

        @property
        def done(self) -> bool:
            return self.final_answer is not None


WRITE_OUTPUT_FILE_NAME = "write_output_file"
MAX_CONTENT_BYTES = 2_000_000

WRITE_OUTPUT_FILE = {
    "name": WRITE_OUTPUT_FILE_NAME,
    "description": (
        "Write a text/CSV deliverable under this run's output_files/ directory only. "
        "Use when the task asks for files, tables, CSV, xlsx/workbook content, ranked "
        "lists, or other structured outputs — do not leave deliverables only in prose. "
        "Path is relative (e.g. 'workbook.csv' or 'notes/gaps.txt'); absolute paths and "
        "'..' are rejected."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": "Relative path under output_files/ (e.g. results.csv).",
            },
            "content": {
                "type": "string",
                "description": "Full file contents as UTF-8 text (CSV/TSV/markdown/plain).",
            },
        },
        "required": ["path", "content"],
        "additionalProperties": False,
    },
    "strict": True,
}


class WriteOutputFileTool:
    """Scout tool that writes only inside run_dir/output_files/."""

    def __init__(self, output_root: Path | str) -> None:
        self.output_root = Path(output_root).resolve()
        self.output_root.mkdir(parents=True, exist_ok=True)
        self.schema = copy.deepcopy(WRITE_OUTPUT_FILE)
        self.name = WRITE_OUTPUT_FILE_NAME

    def requested_queries(self, arguments: dict[str, Any]) -> tuple[str, list[str]]:
        _ = arguments
        return "", []

    def _resolve_safe(self, rel: str) -> Path:
        raw = (rel or "").strip()
        if not raw:
            raise ValueError("path must be a non-empty relative path under output_files/")
        if Path(raw).is_absolute():
            raise ValueError("absolute paths are not allowed; use a relative path")
        if ".." in Path(raw).parts:
            raise ValueError("path must not contain '..'")
        candidate = (self.output_root / raw).resolve()
        try:
            candidate.relative_to(self.output_root)
        except ValueError as exc:
            raise ValueError("path escapes output_files/ sandbox") from exc
        return candidate

    async def __call__(
        self, arguments: dict[str, Any], *, max_queries: int | None = None
    ) -> ToolOutcome:
        _ = max_queries
        try:
            path_arg = str((arguments or {}).get("path") or "")
            content = str(
                (arguments or {}).get("content")
                if (arguments or {}).get("content") is not None
                else ""
            )
            data = content.encode("utf-8")
            if len(data) > MAX_CONTENT_BYTES:
                raise ValueError(
                    f"content too large ({len(data)} bytes); max {MAX_CONTENT_BYTES}"
                )
            dest = self._resolve_safe(path_arg)
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(data)
            rel = dest.relative_to(self.output_root)
            rel_run = f"output_files/{rel.as_posix()}"
            return ToolOutcome(
                content=(
                    f"Wrote {len(data)} bytes to {rel_run}. "
                    "Judge can preview this file under output_files/."
                ),
                records=[{"tool": self.name, "path": rel_run, "bytes": len(data)}],
            )
        except Exception as exc:  # noqa: BLE001
            return ToolOutcome(content=f"Error: {exc}", error=str(exc))


def default_output_tools(output_root: Path | str) -> list[WriteOutputFileTool]:
    return [WriteOutputFileTool(output_root)]
