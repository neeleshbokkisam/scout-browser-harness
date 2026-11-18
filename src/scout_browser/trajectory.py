"""Format Scout transcripts into Odysseys2 judge-facing trajectories."""

from __future__ import annotations

import json
from typing import Any


def clip_middle(
    text: str,
    *,
    max_chars: int = 4000,
    head: int | None = None,
    tail: int | None = None,
) -> str:
    """Clip long text in the middle, preserving start and end.

    When ``head``/``tail`` are omitted, they share nearly all of ``max_chars``
    (minus a short omission marker). Older callers that passed fixed 1600/1600
    with a larger max_chars accidentally kept only ~3.2k of evidence.
    """
    if len(text) <= max_chars:
        return text
    marker_budget = 48
    usable = max(0, max_chars - marker_budget)
    if head is None and tail is None:
        head = usable // 2
        tail = usable - head
    elif head is None:
        assert tail is not None
        head = max(0, usable - tail)
    elif tail is None:
        tail = max(0, usable - head)
    # If caller asked for more head+tail than max_chars, shrink proportionally.
    if head + tail > usable and usable > 0:
        scale = usable / float(head + tail)
        head = int(head * scale)
        tail = usable - head
    omitted = len(text) - head - tail
    return f"{text[:head]}\n…[omitted {omitted} chars]…\n{text[-tail:]}"


def format_agent_trajectory(
    *,
    messages: list[dict[str, Any]] | None,
    tool_history: list[dict[str, Any]] | None,
    max_tool_chars: int = 4000,
) -> str:
    """Numbered [step N] trajectory from Scout messages / tool_history.

    Screenshot files and output deliverables are attached separately in the judge
    packet (see judge_odysseys2); tool_result text may reference harness screenshot paths.
    """
    steps: list[str] = []
    step = 0

    if messages:
        for message in messages:
            role = message.get("role")
            if role == "system":
                continue
            if role == "user":
                step += 1
                content = clip_middle(str(message.get("content") or ""), max_chars=max_tool_chars)
                steps.append(f"[step {step}] user\n{content}")
                continue
            if role == "assistant":
                step += 1
                parts: list[str] = []
                content = message.get("content")
                if content:
                    parts.append(clip_middle(str(content), max_chars=max_tool_chars))
                for call in message.get("tool_calls") or []:
                    fn = call.get("function") or {}
                    name = fn.get("name") or call.get("name") or "tool"
                    args = fn.get("arguments") or call.get("arguments") or "{}"
                    parts.append(f"tool_call {name}({args})")
                steps.append(f"[step {step}] assistant\n" + ("\n".join(parts) if parts else "(empty)"))
                continue
            if role == "tool":
                step += 1
                content = clip_middle(str(message.get("content") or ""), max_chars=max_tool_chars)
                steps.append(f"[step {step}] tool_result\n{content}")
                continue

    if not steps and tool_history:
        for entry in tool_history:
            step += 1
            payload = clip_middle(json.dumps(entry, ensure_ascii=False), max_chars=max_tool_chars)
            tool = entry.get("tool") or "tool"
            steps.append(f"[step {step}] {tool}\n{payload}")

    if not steps:
        return "(empty trajectory)"
    return "\n\n".join(steps)
