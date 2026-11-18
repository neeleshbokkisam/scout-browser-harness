"""Complementary search→browser policy for Systems B/C (Scout loop mostly unchanged).

P3: teach complementary use; stronger prompt when interaction-heavy; soft
mid-run nudge via SearchTool wrapper (never a hard submit gate).
System C adds deliverable + wait/scroll one-liners only.
"""

from __future__ import annotations

import re
from typing import Any

from harness.tools import SearchTool, ToolOutcome

from scout_browser.odysseys2 import Odysseys2Task

POLICY_NAME = "complementary_s_to_b"

MID_RUN_NUDGE_TEXT = (
    "\n\n[policy nudge] This task looks interaction-heavy and you have not used "
    "browser tools yet. After finding target URLs via search, use browser_navigate / "
    "browser_extract (and click/type/wait/scroll when needed) to capture live page "
    "state before submit_final_result. Do not browse randomly — open concrete pages "
    "that settle the rubric."
)

COMPLEMENTARY_USER_BLOCK = """Tool policy (complementary search + browser):
- Use `search` for discovery: find URLs, docs, news, registries, and cheap orientation.
- Use `browser_*` for live page state: open concrete URLs, click/type when the task needs UI (ZIP, filters, pagination, forms), and extract what snippets miss. Harness screenshots are captured after navigate/click/type.
- Prefer search → then browser to verify/extract on the real page. Do not browse blindly when search already settled a pure lookup. Do not search-only when the task needs on-page interaction or live listing/ZIP/cart state.
- Respect action restrictions. Finish with submit_final_result."""

COMPLEMENTARY_USER_BLOCK_C = """Tool policy (complementary search + browser + deliverables):
- Use `search` for discovery: find URLs, docs, news, registries, and cheap orientation.
- Use `browser_*` for live page state: open concrete URLs; click/type for ZIP/filters/forms; use `browser_wait` / `browser_scroll` for dialogs, lazy lists, and below-fold content; extract what snippets miss. Screenshots follow mutating browser actions.
- If the task asks for files/tables/CSV/xlsx/workbook/structured deliverables, call `write_output_file` (under output_files/) before submit — do not leave required files only in prose.
- File grounding (hard): only write_output_file rows/facts taken from tool results you just saw (browser_extract/snapshot/search snippet). In each file, include a source URL or step reference when possible. Do NOT invent provenance, hit counts, “recorded before search” chronology, or placeholder URLs. Prefer fewer grounded rows over a complete-looking ungrounded table.
- Prefer search → then browser to verify/extract. Do not browse blindly when search settled a pure lookup; do not search-only when UI/page state is required. No hard must-browse gate.
- Respect action restrictions. Finish with submit_final_result."""

INTERACTION_HEAVY_USER_LINE = (
    "This task likely needs browser tools for live page / ZIP / listing / form / "
    "product-page state before submit — search alone usually cannot meet the rubric."
)

FILE_DELIVERABLE_USER_LINE = (
    "This task likely needs file deliverables — write CSV/tables/notes via "
    "`write_output_file` before submit_final_result. Only include rows/facts from "
    "tool results you just saw; cite source URL/step when possible; never invent "
    "provenance, hit counts, chronology, or placeholder URLs; fewer grounded rows "
    "beat a complete-looking ungrounded table."
)

SYSTEM_B_PROMPT = """You are a web research agent. You answer questions by searching the web and, when needed, browsing live pages, then synthesizing a direct answer.

Today's date: {current_date}

You have two complementary tool families:
1) `search` — Exa web search for discovery (URLs, docs, news, broad orientation). Craft precise queries; refine; cross-reference.
2) `browser_navigate`, `browser_snapshot`, `browser_click`, `browser_type`, `browser_extract` — a real browser for live page state. Open concrete URLs from search or the task; interact when the instruction needs ZIP/filters/pagination/forms; extract visible text that snippets miss.

Complementary use (not always-browse, not search-only):
- Search first to find targets when you lack URLs.
- Browse to verify, extract, or act on the live page when the task needs page state, UI, or screenshot-corroborated evidence.
- Skip browser for pure lookups that search results fully settle; skip aimless browsing.

When answering:
- Ground claims in tool evidence (search snippets and/or page extracts). Include URLs, prices, specs, dates as demanded.
- Be specific and actionable. If evidence is insufficient or conflicting, say so.
- Don't pad. Answer the question, then call submit_final_result.
- Use today's date to resolve relative time references."""

SYSTEM_C_PROMPT = """You are a web research agent. You answer questions by searching the web and, when needed, browsing live pages and writing deliverable files, then synthesizing a direct answer.

Today's date: {current_date}

You have complementary tool families:
1) `search` — Exa web search for discovery (URLs, docs, news, broad orientation). Craft precise queries; refine; cross-reference.
2) `browser_navigate`, `browser_snapshot`, `browser_click`, `browser_type`, `browser_extract`, `browser_wait`, `browser_scroll` — a real browser for live page state. Open concrete URLs; interact for ZIP/filters/pagination/forms; wait/scroll for dialogs and lazy/below-fold content; extract what snippets miss.
3) `write_output_file` — save text/CSV deliverables under output_files/ when the rubric wants files/tables/workbooks (not prose-only).

Complementary use (not always-browse, not search-only):
- Search first to find targets when you lack URLs.
- Browse to verify, extract, or act on the live page when the task needs page state, UI, or screenshot-corroborated evidence.
- Write files when the task asks for CSV/tables/structured outputs.
- Skip browser for pure lookups that search results fully settle; skip aimless browsing.

File grounding (hard rules for write_output_file):
- Only write rows/facts taken from tool results you just saw (browser_extract / browser_snapshot / search snippet).
- In each file, include a source URL or step reference when possible.
- Do NOT invent provenance, hit counts, “recorded before search” chronology, or placeholder URLs.
- Prefer fewer grounded rows over a complete-looking ungrounded table.

When answering:
- Ground claims in tool evidence (search snippets, page extracts, and/or output files). Include URLs, prices, specs, dates as demanded.
- Be specific and actionable. If evidence is insufficient or conflicting, say so.
- Don't pad. Answer the question, then call submit_final_result.
- Use today's date to resolve relative time references."""

# System D: C tool belt minus Exa search (browser-only ablation).
POLICY_NAME_D = "browser_only"

BROWSER_ONLY_USER_BLOCK = """Tool policy (browser only + deliverables; no Exa search tool):
- There is no `search` tool. Discover via the live browser: open the Start URL, follow links, use on-page site search / Google if needed, and navigate to concrete pages.
- Use `browser_navigate`, `browser_snapshot`, `browser_click`, `browser_type`, `browser_extract`, `browser_wait`, `browser_scroll` for live page state (ZIP/filters/forms, dialogs, lazy/below-fold content). Screenshots follow mutating browser actions.
- If the task asks for files/tables/CSV/xlsx/workbook/structured deliverables, call `write_output_file` (under output_files/) before submit — do not leave required files only in prose.
- Respect action restrictions. Finish with submit_final_result."""

SYSTEM_D_PROMPT = """You are a web research agent. You answer questions by browsing live pages and writing deliverable files, then synthesizing a direct answer. You do not have an Exa search tool.

Today's date: {current_date}

You have these tool families:
1) `browser_navigate`, `browser_snapshot`, `browser_click`, `browser_type`, `browser_extract`, `browser_wait`, `browser_scroll` — a real browser for discovery and live page state. Open the Start URL or other concrete URLs; use on-page search when needed; interact for ZIP/filters/pagination/forms; wait/scroll for dialogs and lazy/below-fold content; extract visible text.
2) `write_output_file` — save text/CSV deliverables under output_files/ when the rubric wants files/tables/workbooks (not prose-only).

Use the browser to find targets and capture page state. Write files when the task asks for CSV/tables/structured outputs. Skip aimless browsing.

When answering:
- Ground claims in tool evidence (page extracts and/or output files). Include URLs, prices, specs, dates as demanded.
- Be specific and actionable. If evidence is insufficient or conflicting, say so.
- Don't pad. Answer the question, then call submit_final_result.
- Use today's date to resolve relative time references."""

INTERACTION_HEAVY_USER_LINE_D = (
    "This task likely needs browser tools for live page / ZIP / listing / form / "
    "product-page state before submit — navigate and interact on real pages."
)

_FILE_DELIVERABLE_PATTERNS: list[re.Pattern[str]] = [
    re.compile(p, re.I)
    for p in (
        r"\bcsv\b",
        r"\bxlsx\b",
        r"\bworkbook\b",
        r"spreadsheet",
        r"output files?",
        r"four files",
        r"deliverable",
        r"save .{0,40}file",
        r"write .{0,40}(?:csv|file|table)",
        r"structured outputs?",
    )
]
_INTERACTION_PATTERNS: list[re.Pattern[str]] = [
    re.compile(p, re.I)
    for p in (
        r"\bzip\b",
        r"zip code",
        r"delivery",
        r"\bcart\b",
        r"checkout",
        r"guest path",
        r"product page",
        r"results pages?",
        r"pagination",
        r"screenshot",
        # Prefer UI verbs; avoid matching prose like "filled field has a source".
        r"\bclick(?:ing|s)?\b",
        r"fill(?:ing)? (?:in |out )?(?:the )?(?:zip|form|input|box|field)",
        r"(?:web|html|checkout) forms?\b",
        r"search forms?\b",
        r"set .{0,40}location",
        r"store panel",
        r"availability module",
        r"add to cart",
    )
]


def task_policy_text(task: Odysseys2Task) -> str:
    """Instruction + rubric text used by the interaction heuristic."""
    parts = [task.category or "", task.title or "", task.instruction or ""]
    for item in task.rubric or []:
        for key in ("name", "requirement", "description"):
            val = item.get(key)
            if val:
                parts.append(str(val))
    return "\n".join(parts)


def is_interaction_heavy(task: Odysseys2Task) -> bool:
    """True when the task likely needs live UI / page-state browser work."""
    if (task.category or "").strip().lower() == "shopping":
        return True
    text = task_policy_text(task)
    return any(p.search(text) for p in _INTERACTION_PATTERNS)


def is_file_deliverable_heavy(task: Odysseys2Task) -> bool:
    """True when the task likely needs write_output_file deliverables."""
    text = task_policy_text(task)
    return any(p.search(text) for p in _FILE_DELIVERABLE_PATTERNS)


def build_system_b_prompt() -> str:
    return SYSTEM_B_PROMPT


def build_system_c_prompt() -> str:
    return SYSTEM_C_PROMPT


def build_system_d_prompt() -> str:
    return SYSTEM_D_PROMPT


def build_user_prompt(task: Odysseys2Task, *, interaction_heavy: bool | None = None) -> str:
    """Base scout_query + complementary policy (+ stronger line if heavy)."""
    heavy = is_interaction_heavy(task) if interaction_heavy is None else interaction_heavy
    parts = [task.scout_query.strip(), "", COMPLEMENTARY_USER_BLOCK]
    if heavy:
        parts.extend(["", INTERACTION_HEAVY_USER_LINE])
    return "\n".join(parts)


def build_user_prompt_c(task: Odysseys2Task, *, interaction_heavy: bool | None = None) -> str:
    """System C user prompt: complementary + wait/scroll/files one-liners."""
    heavy = is_interaction_heavy(task) if interaction_heavy is None else interaction_heavy
    parts = [task.scout_query.strip(), "", COMPLEMENTARY_USER_BLOCK_C]
    if heavy:
        parts.extend(["", INTERACTION_HEAVY_USER_LINE])
    if is_file_deliverable_heavy(task):
        parts.extend(["", FILE_DELIVERABLE_USER_LINE])
    return "\n".join(parts)


def build_user_prompt_d(task: Odysseys2Task, *, interaction_heavy: bool | None = None) -> str:
    """System D user prompt: browser-only + wait/scroll/files (no Exa search)."""
    heavy = is_interaction_heavy(task) if interaction_heavy is None else interaction_heavy
    parts = [task.scout_query.strip(), "", BROWSER_ONLY_USER_BLOCK]
    if heavy:
        parts.extend(["", INTERACTION_HEAVY_USER_LINE_D])
    if is_file_deliverable_heavy(task):
        parts.extend(["", FILE_DELIVERABLE_USER_LINE])
    return "\n".join(parts)


def initial_policy_meta(
    task: Odysseys2Task,
    *,
    enable_mid_run_nudge: bool = True,
) -> dict[str, Any]:
    heavy = is_interaction_heavy(task)
    return {
        "name": POLICY_NAME,
        "interaction_heavy": heavy,
        "strong_prompt": heavy,
        "file_deliverable_heavy": is_file_deliverable_heavy(task),
        "enable_mid_run_nudge": enable_mid_run_nudge,
        "mid_run_nudge_fired": False,
        "mid_run_nudge_after_searches": 2,
        "auto_open_start_url": False,
        "hard_browser_gate": False,
    }


def initial_policy_meta_d(task: Odysseys2Task) -> dict[str, Any]:
    """Browser-only ablation: no search nudge / no search tool."""
    heavy = is_interaction_heavy(task)
    return {
        "name": POLICY_NAME_D,
        "interaction_heavy": heavy,
        "strong_prompt": heavy,
        "file_deliverable_heavy": is_file_deliverable_heavy(task),
        "enable_mid_run_nudge": False,
        "mid_run_nudge_fired": False,
        "mid_run_nudge_after_searches": None,
        "auto_open_start_url": False,
        "hard_browser_gate": False,
        "search": False,
    }

def count_tool_calls(messages: list[dict[str, Any]] | None) -> dict[str, int]:
    """Count search / browser / write_output_file tool_calls from Scout messages."""
    n_search = 0
    n_browser = 0
    n_write = 0
    for message in messages or []:
        if message.get("role") != "assistant":
            continue
        for call in message.get("tool_calls") or []:
            fn = call.get("function") or {}
            name = str(fn.get("name") or call.get("name") or "")
            if name == "search":
                n_search += 1
            elif name.startswith("browser_"):
                n_browser += 1
            elif name == "write_output_file":
                n_write += 1
    return {"search": n_search, "browser": n_browser, "write_output_file": n_write}


def policy_triggered(meta: dict[str, Any] | None) -> bool:
    """True if strong prompt and/or mid-run nudge fired."""
    if not meta:
        return False
    return bool(meta.get("strong_prompt") or meta.get("mid_run_nudge_fired"))


class NudgeSearchTool:
    """Wrap SearchTool; optionally append a one-time soft browser nudge.

    Duck-types Scout's tool contract (name, schema, requested_queries, __call__).
    """

    def __init__(
        self,
        inner: SearchTool,
        *,
        interaction_heavy: bool,
        enable_mid_run_nudge: bool = True,
        after_searches: int = 2,
        policy_meta: dict[str, Any] | None = None,
        browser_call_counter: dict[str, int] | None = None,
    ) -> None:
        self.inner = inner
        self.name = inner.name
        self.schema = inner.schema
        self.interaction_heavy = interaction_heavy
        self.enable_mid_run_nudge = enable_mid_run_nudge
        self.after_searches = after_searches
        self.policy_meta = policy_meta if policy_meta is not None else {}
        self._search_calls = 0
        self._nudge_fired = False
        self.browser_call_counter = (
            browser_call_counter if browser_call_counter is not None else {"browser": 0}
        )

    def requested_queries(self, arguments: dict[str, Any]) -> tuple[str, list[str]]:
        return self.inner.requested_queries(arguments)

    async def __call__(
        self, arguments: dict[str, Any], *, max_queries: int | None = None
    ) -> ToolOutcome:
        outcome = await self.inner(arguments, max_queries=max_queries)
        self._search_calls += 1
        if (
            self.enable_mid_run_nudge
            and self.interaction_heavy
            and not self._nudge_fired
            and self._search_calls >= self.after_searches
            and int(self.browser_call_counter.get("browser") or 0) == 0
            and not outcome.error
        ):
            self._nudge_fired = True
            self.policy_meta["mid_run_nudge_fired"] = True
            outcome = ToolOutcome(
                content=outcome.content + MID_RUN_NUDGE_TEXT,
                error=outcome.error,
                final_answer=outcome.final_answer,
                records=list(outcome.records),
                evidence=list(outcome.evidence),
                num_searches=outcome.num_searches,
                queries_used=outcome.queries_used,
                cost_usd=outcome.cost_usd,
                cost_known=outcome.cost_known,
                latency_ms=outcome.latency_ms,
            )
        return outcome


class CountingBrowserTool:
    """Wrap BrowserTool to increment a shared browser-call counter for nudges."""

    def __init__(self, inner: Any, browser_call_counter: dict[str, int]) -> None:
        self.inner = inner
        self.name = inner.name
        self.schema = inner.schema
        self.browser_call_counter = browser_call_counter

    def requested_queries(self, arguments: dict[str, Any]) -> tuple[str, list[str]]:
        return "", []

    async def __call__(
        self, arguments: dict[str, Any], *, max_queries: int | None = None
    ) -> ToolOutcome:
        self.browser_call_counter["browser"] = (
            int(self.browser_call_counter.get("browser") or 0) + 1
        )
        return await self.inner(arguments, max_queries=max_queries)
