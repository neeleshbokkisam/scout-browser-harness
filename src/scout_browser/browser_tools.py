"""Browser tool belt for Scout — schemas + Playwright backends.

Scout's research loop stays untouched; these tools are additional callables
with the same ToolOutcome contract as SearchTool.
"""

from __future__ import annotations

import copy
from dataclasses import dataclass, field
from typing import Any

# Lazy playwright import so search-only runs don't need browsers installed.


BROWSER_NAVIGATE = {
    "name": "browser_navigate",
    "description": "Open a URL in the browser tab and return a short page snapshot.",
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
    "description": "Return the current page URL, title, and a text accessibility snapshot.",
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
    "description": "Click an element described by a CSS selector or visible text.",
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
    "description": "Type text into an input matched by selector (clears existing value first).",
    "parameters": {
        "type": "object",
        "properties": {
            "selector": {"type": "string"},
            "text": {"type": "string"},
            "submit": {
                "type": "boolean",
                "description": "If true, press Enter after typing.",
            },
        },
        "required": ["selector", "text"],
        "additionalProperties": False,
    },
    "strict": True,
}

BROWSER_EXTRACT = {
    "name": "browser_extract",
    "description": "Extract visible text from the page or a CSS selector region.",
    "parameters": {
        "type": "object",
        "properties": {
            "selector": {
                "type": "string",
                "description": "Optional CSS selector; default is document body.",
            },
            "max_chars": {
                "type": "integer",
                "description": "Truncate extracted text to this many characters.",
            },
        },
        "required": [],
        "additionalProperties": False,
    },
    "strict": True,
}


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


class BrowserSession:
    """Minimal Playwright session shared by browser tools."""

    def __init__(self, *, headless: bool = True) -> None:
        self.headless = headless
        self._pw = None
        self._browser = None
        self._page = None

    async def start(self) -> None:
        from playwright.async_api import async_playwright

        self._pw = await async_playwright().start()
        self._browser = await self._pw.chromium.launch(headless=self.headless)
        self._page = await self._browser.new_page()

    async def close(self) -> None:
        if self._browser:
            await self._browser.close()
        if self._pw:
            await self._pw.stop()

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


class BrowserTool:
    """One Scout-facing browser function."""

    def __init__(self, session: BrowserSession, schema: dict[str, Any]) -> None:
        self.session = session
        self.schema = copy.deepcopy(schema)
        self.name = self.schema["name"]

    async def __call__(
        self, arguments: dict[str, Any], *, max_queries: int | None = None
    ) -> ToolOutcome:
        _ = max_queries
        try:
            content = await self._run(arguments or {})
            return ToolOutcome(content=content)
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
            selector = arguments.get("selector") or "body"
            max_chars = int(arguments.get("max_chars") or 12000)
            text = (await page.inner_text(selector)).strip()
            if len(text) > max_chars:
                text = text[:max_chars] + "\n…[truncated]"
            return f"Extracted from {selector}:\n{text}"
        raise ValueError(f"unknown browser tool {self.name!r}")


def default_browser_tools(session: BrowserSession) -> list[BrowserTool]:
    return [
        BrowserTool(session, BROWSER_NAVIGATE),
        BrowserTool(session, BROWSER_SNAPSHOT),
        BrowserTool(session, BROWSER_CLICK),
        BrowserTool(session, BROWSER_TYPE),
        BrowserTool(session, BROWSER_EXTRACT),
    ]
