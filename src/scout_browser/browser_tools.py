"""Browser tool belt for Scout — schemas + Playwright backends.

Scout's research loop stays untouched; these tools are additional callables
with the same ToolOutcome contract as SearchTool.
"""

from __future__ import annotations

import copy
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

try:
    from harness.tools import ToolOutcome
except ImportError:  # pragma: no cover — allow import without vendor on path

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


# Actions that change page state → harness captures a screenshot afterward.
_MUTATING_TOOLS = frozenset(
    {
        "browser_navigate",
        "browser_click",
        "browser_type",
        "browser_wait",
        "browser_scroll",
    }
)


BROWSER_NAVIGATE = {
    "name": "browser_navigate",
    "description": (
        "Open a concrete http(s) URL in the browser and return a short page snapshot. "
        "Use after search finds targets, or when the task needs live page state — "
        "not as a substitute for discovery search."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "url": {"type": "string", "description": "Absolute http(s) URL to open."},
        },
        "required": ["url"],
        "additionalProperties": False,
    },
    "strict": True,
}

BROWSER_SNAPSHOT = {
    "name": "browser_snapshot",
    "description": (
        "Return the current page URL, title, and visible text snapshot. "
        "Use to re-read the open page without navigating away."
    ),
    "parameters": {
        "type": "object",
        "properties": {},
        "required": [],
        "additionalProperties": False,
    },
    "strict": True,
}

BROWSER_CLICK = {
    "name": "browser_click",
    "description": (
        "Click an element by CSS selector or visible text (text=...). "
        "Use for pagination, filters, Search/Go buttons, and other on-page UI — "
        "not for discovering unknown URLs (use search first)."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "selector": {
                "type": "string",
                "description": "CSS selector, or text=... for visible text.",
            },
        },
        "required": ["selector"],
        "additionalProperties": False,
    },
    "strict": True,
}

BROWSER_TYPE = {
    "name": "browser_type",
    "description": (
        "Type into an input matched by selector (clears existing value first). "
        "Use for ZIP codes, search boxes, filters, and forms on an already-open page."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "selector": {"type": "string"},
            "text": {"type": "string"},
            "submit": {
                "type": "boolean",
                "description": "If true, press Enter after typing. Pass false to leave focus in the field.",
            },
        },
        # OpenAI strict tools require every property key to appear in required.
        "required": ["selector", "text", "submit"],
        "additionalProperties": False,
    },
    "strict": True,
}

BROWSER_EXTRACT = {
    "name": "browser_extract",
    "description": (
        "Extract visible text from the open page or a CSS selector region. "
        "Use to capture live listing/product/page content that search snippets miss."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "selector": {
                "type": "string",
                "description": "CSS selector region to extract; use 'body' for the full page.",
            },
            "max_chars": {
                "type": "integer",
                "description": "Truncate extracted text to this many characters (e.g. 12000).",
            },
        },
        "required": ["selector", "max_chars"],
        "additionalProperties": False,
    },
    "strict": True,
}

BROWSER_WAIT = {
    "name": "browser_wait",
    "description": (
        "Wait for a selector to appear (ZIP dialogs, lazy panels, post-click UI) "
        "or wait a fixed timeout when selector is empty. Prefer waiting over "
        "blind re-navigate thrash."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "selector": {
                "type": "string",
                "description": (
                    "CSS selector or text=... to wait for. Pass empty string \"\" "
                    "to sleep only for timeout_ms."
                ),
            },
            "timeout_ms": {
                "type": "integer",
                "description": "Max wait in milliseconds (e.g. 3000–15000).",
            },
        },
        "required": ["selector", "timeout_ms"],
        "additionalProperties": False,
    },
    "strict": True,
}

BROWSER_SCROLL = {
    "name": "browser_scroll",
    "description": (
        "Scroll the open page: wheel up/down by amount pixels, or scroll a selector "
        "into view. Use for below-fold listings, pagination footers, and lazy-loaded "
        "content after ZIP/filter changes."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "direction": {
                "type": "string",
                "description": "Wheel direction: 'down' or 'up'. Ignored when selector is set.",
            },
            "selector": {
                "type": "string",
                "description": (
                    "If non-empty, scroll this CSS/text= selector into view instead of "
                    "wheeling. Pass \"\" to use direction+amount."
                ),
            },
            "amount": {
                "type": "integer",
                "description": "Wheel pixel delta when selector is empty (e.g. 800).",
            },
        },
        "required": ["direction", "selector", "amount"],
        "additionalProperties": False,
    },
    "strict": True,
}


class BrowserSession:
    """Minimal Playwright session shared by browser tools.

    Optional CDP: set ``cdp_url`` or env ``BROWSER_USE_CDP_URL`` to attach to a
    remote Chromium (e.g. Browser Use Cloud) instead of launching locally.
    """

    def __init__(
        self,
        *,
        headless: bool = True,
        screenshot_dir: Path | str | None = None,
        cdp_url: str | None = None,
    ) -> None:
        self.headless = headless
        self.screenshot_dir = Path(screenshot_dir) if screenshot_dir else None
        self.cdp_url = (cdp_url or os.environ.get("BROWSER_USE_CDP_URL") or "").strip() or None
        self._pw = None
        self._browser = None
        self._page = None
        self._shot_index = 0
        self.screenshots: list[dict[str, Any]] = []

    async def start(self) -> None:
        from playwright.async_api import async_playwright

        self._pw = await async_playwright().start()
        if self.cdp_url:
            self._browser = await self._pw.chromium.connect_over_cdp(self.cdp_url)
            contexts = self._browser.contexts
            if contexts and contexts[0].pages:
                self._page = contexts[0].pages[0]
            else:
                context = contexts[0] if contexts else await self._browser.new_context()
                self._page = await context.new_page()
        else:
            self._browser = await self._pw.chromium.launch(headless=self.headless)
            self._page = await self._browser.new_page()
        if self.screenshot_dir is not None:
            self.screenshot_dir.mkdir(parents=True, exist_ok=True)

    async def close(self) -> None:
        if self._browser:
            if self.cdp_url:
                await self._browser.close()
            else:
                await self._browser.close()
        if self._pw:
            await self._pw.stop()
        self._page = None
        self._browser = None
        self._pw = None

    @property
    def page(self):
        if self._page is None:
            raise RuntimeError("BrowserSession not started")
        return self._page

    async def snapshot_text(self, max_chars: int = 8000) -> str:
        page = self.page
        title = await page.title()
        url = page.url
        try:
            text = await page.inner_text("body")
        except Exception:
            text = ""
        text = text.strip()
        if len(text) > max_chars:
            text = text[:max_chars] + "\n…[truncated]"
        return f"URL: {url}\nTitle: {title}\n\n{text}"

    async def capture_screenshot(self, *, tool: str) -> dict[str, Any] | None:
        """Save a PNG after a browser action; return metadata for tool_history."""
        if self.screenshot_dir is None:
            return None
        self._shot_index += 1
        page = self.page
        url = page.url
        filename = f"step-{self._shot_index:03d}-{tool}.png"
        path = self.screenshot_dir / filename
        try:
            await page.screenshot(path=str(path), full_page=False)
        except Exception as exc:  # noqa: BLE001 — still return a record of the attempt
            meta = {
                "tool": tool,
                "screenshot": str(path),
                "label": f"step {self._shot_index} ({tool})",
                "url": url,
                "error": str(exc),
            }
            self.screenshots.append(meta)
            return meta
        meta = {
            "tool": tool,
            "screenshot": str(path),
            "label": f"step {self._shot_index} ({tool})",
            "url": url,
            "index": self._shot_index,
        }
        self.screenshots.append(meta)
        return meta


class BrowserTool:
    """One Scout-facing browser function."""

    def __init__(self, session: BrowserSession, schema: dict[str, Any]) -> None:
        self.session = session
        self.schema = copy.deepcopy(schema)
        self.name = self.schema["name"]

    def requested_queries(self, arguments: dict[str, Any]) -> tuple[str, list[str]]:
        """Browser tools do not consume search budget."""
        _ = arguments
        return "", []

    async def __call__(
        self, arguments: dict[str, Any], *, max_queries: int | None = None
    ) -> ToolOutcome:
        _ = max_queries
        try:
            content = await self._run(arguments or {})
            records: list[dict[str, Any]] = []
            if self.name in _MUTATING_TOOLS:
                shot = await self.session.capture_screenshot(tool=self.name)
                if shot:
                    records.append(
                        {
                            "tool": self.name,
                            "screenshot": shot.get("screenshot"),
                            "label": shot.get("label"),
                            "url": shot.get("url"),
                            "index": shot.get("index"),
                        }
                    )
                    content = f"{content}\n\n[harness screenshot: {shot.get('label')} → {shot.get('screenshot')}]"
            return ToolOutcome(content=content, records=records)
        except Exception as exc:  # noqa: BLE001 — surface tool errors to the model
            return ToolOutcome(content=f"Error: {exc}", error=str(exc))

    async def _run(self, arguments: dict[str, Any]) -> str:
        page = self.session.page
        if self.name == "browser_navigate":
            url = str(arguments["url"]).strip()
            await page.goto(url, wait_until="domcontentloaded")
            return await self.session.snapshot_text()
        if self.name == "browser_snapshot":
            return await self.session.snapshot_text()
        if self.name == "browser_click":
            selector = str(arguments["selector"])
            if selector.startswith("text="):
                await page.get_by_text(selector[5:], exact=False).first.click()
            else:
                await page.click(selector)
            return await self.session.snapshot_text()
        if self.name == "browser_type":
            selector = str(arguments["selector"])
            text = str(arguments["text"])
            await page.fill(selector, text)
            if arguments.get("submit"):
                await page.press(selector, "Enter")
            return await self.session.snapshot_text()
        if self.name == "browser_extract":
            selector = str(arguments.get("selector") or "body").strip() or "body"
            max_chars = int(arguments.get("max_chars") if arguments.get("max_chars") is not None else 12000)
            text = (await page.inner_text(selector)).strip()
            if len(text) > max_chars:
                text = text[:max_chars] + "\n…[truncated]"
            return f"Extracted from {selector}:\n{text}"
        if self.name == "browser_wait":
            import asyncio

            selector = str(arguments.get("selector") or "").strip()
            timeout_ms = int(arguments.get("timeout_ms") if arguments.get("timeout_ms") is not None else 3000)
            timeout_ms = max(0, min(timeout_ms, 60_000))
            if selector:
                if selector.startswith("text="):
                    locator = page.get_by_text(selector[5:], exact=False).first
                    await locator.wait_for(state="visible", timeout=timeout_ms)
                else:
                    await page.wait_for_selector(selector, timeout=timeout_ms)
                return f"Waited for selector {selector!r} (timeout_ms={timeout_ms}).\n\n" + (
                    await self.session.snapshot_text()
                )
            await asyncio.sleep(timeout_ms / 1000.0)
            return f"Waited {timeout_ms}ms (no selector).\n\n" + await self.session.snapshot_text()
        if self.name == "browser_scroll":
            selector = str(arguments.get("selector") or "").strip()
            direction = str(arguments.get("direction") or "down").strip().lower()
            amount = int(arguments.get("amount") if arguments.get("amount") is not None else 800)
            amount = max(1, min(abs(amount), 20_000))
            if selector:
                if selector.startswith("text="):
                    locator = page.get_by_text(selector[5:], exact=False).first
                else:
                    locator = page.locator(selector).first
                await locator.scroll_into_view_if_needed()
                return f"Scrolled selector {selector!r} into view.\n\n" + await self.session.snapshot_text()
            delta = amount if direction != "up" else -amount
            await page.mouse.wheel(0, delta)
            return (
                f"Scrolled {direction} by {amount}px.\n\n" + await self.session.snapshot_text()
            )
        raise ValueError(f"unknown browser tool {self.name!r}")


def default_browser_tools(session: BrowserSession) -> list[BrowserTool]:
    """System B belt (no wait/scroll)."""
    return [
        BrowserTool(session, BROWSER_NAVIGATE),
        BrowserTool(session, BROWSER_SNAPSHOT),
        BrowserTool(session, BROWSER_CLICK),
        BrowserTool(session, BROWSER_TYPE),
        BrowserTool(session, BROWSER_EXTRACT),
    ]


def system_c_browser_tools(session: BrowserSession) -> list[BrowserTool]:
    """System C belt: B tools + wait/scroll for ZIP/lazy UI depth."""
    return [
        *default_browser_tools(session),
        BrowserTool(session, BROWSER_WAIT),
        BrowserTool(session, BROWSER_SCROLL),
    ]
