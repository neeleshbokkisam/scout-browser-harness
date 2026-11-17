"""Load Odysseys2 tasks for Scout runs."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from scout_browser.paths import ODYSSEYS2_TASKS


@dataclass(frozen=True)
class Odysseys2Task:
    id: str
    title: str
    category: str
    start_url: str
    instruction: str
    rubric: list[dict[str, Any]]

    @property
    def scout_query(self) -> str:
        """Prompt text Scout sees — instruction plus start URL constraint."""
        return (
            f"Start URL: {self.start_url}\n\n"
            f"Task: {self.instruction}\n\n"
            "Follow the instruction exactly. Respect any action restrictions. "
            "When finished, call submit_final_result with a complete answer that "
            "includes the concrete fields the task asked you to record."
        )


def load_tasks(path: Path | None = None) -> list[Odysseys2Task]:
    data = json.loads((path or ODYSSEYS2_TASKS).read_text())
    tasks = data["tasks"] if isinstance(data, dict) else data
    return [
        Odysseys2Task(
            id=t["id"],
            title=t["title"],
            category=t["category"],
            start_url=t["start_url"],
            instruction=t["instruction"],
            rubric=t.get("rubric") or [],
        )
        for t in tasks
    ]


def filter_tasks(
    tasks: list[Odysseys2Task],
    *,
    ids: list[str] | None = None,
    categories: list[str] | None = None,
    limit: int | None = None,
) -> list[Odysseys2Task]:
    out = tasks
    if ids:
        wanted = set(ids)
        out = [t for t in out if t.id in wanted]
    if categories:
        wanted_c = {c.lower() for c in categories}
        out = [t for t in out if t.category.lower() in wanted_c]
    if limit is not None:
        out = out[:limit]
    return out
