"""Browser automation — fetch pages, extract content, and optionally control a real browser.

Uses ``httpx`` for basic HTTP fetching (always available). When Playwright is
installed, provides full browser automation: navigation, screenshots, clicks,
and typing.

Tools exposed to the agent:
  - ``browser_navigate`` — fetch a page, return readable text + metadata
  - ``browser_screenshot`` — take a page screenshot (Playwright required)
  - ``browser_click`` — click an element by CSS selector (Playwright required)
  - ``browser_type`` — type text into an input (Playwright required)

Playwright setup::

    pip install playwright
    playwright install chromium
"""

from __future__ import annotations

import asyncio
import logging
import re
import tempfile
from html.parser import HTMLParser
from typing import Any

import httpx

logger = logging.getLogger(__name__)

_PLAYWRIGHT_AVAILABLE: bool | None = None
_browser_instance: Any = None
_current_page: Any = None


# ---------------------------------------------------------------------------
# HTML → text extraction (stdlib, no external deps)
# ---------------------------------------------------------------------------

class _HTMLToText(HTMLParser):
    """Convert HTML to readable plain text."""

    def __init__(self) -> None:
        super().__init__()
        self._result: list[str] = []
        self._skip = False
        self._block_tags = {"p", "br", "tr", "li", "h1", "h2", "h3", "h4", "h5", "h6", "div", "section", "blockquote", "pre"}

    def handle_starttag(self, tag: str, _attrs: list[tuple[str, str | None]]) -> None:
        if tag in ("script", "style", "noscript"):
            self._skip = True
        if tag in self._block_tags and self._result and not self._result[-1].endswith("\n"):
            self._result.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in ("script", "style", "noscript"):
            self._skip = False
        if tag in self._block_tags and self._result and not self._result[-1].endswith("\n"):
            self._result.append("\n")

    def handle_data(self, data: str) -> None:
        if self._skip:
            return
        text = re.sub(r"\s+", " ", data).strip()
        if text:
            self._result.append(text)

    def text(self) -> str:
        raw = "".join(self._result)
        return re.sub(r"\n{3,}", "\n\n", raw).strip()


def _html_to_text(html: str, max_chars: int = 8000) -> str:
    parser = _HTMLToText()
    parser.feed(html)
    text = parser.text()
    if len(text) > max_chars:
        text = text[:max_chars] + "\n…[truncated]"
    return text


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


async def navigate(
    url: str,
    *,
    extract_text: bool = True,
    max_chars: int = 8000,
    headers: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Fetch *url* and return structured page data.

    Returns::
        {"url": str, "status": int, "title": str, "text": str, "error": str | None}
    """
    result: dict[str, Any] = {"url": url, "status": 0, "title": "", "text": "", "error": None}
    try:
        req_headers = {
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        }
        if headers:
            req_headers.update(headers)
        async with httpx.AsyncClient(follow_redirects=True, timeout=30.0) as client:
            response = await client.get(url, headers=req_headers)
            result["status"] = response.status_code
            result["url"] = str(response.url)
            response.raise_for_status()
            html = response.text

            # Extract title
            title_match = re.search(r"<title[^>]*>(.*?)</title>", html, re.IGNORECASE | re.DOTALL)
            if title_match:
                result["title"] = re.sub(r"\s+", " ", title_match.group(1)).strip()

            if extract_text:
                result["text"] = _html_to_text(html, max_chars=max_chars)
    except httpx.TimeoutException:
        result["error"] = f"Request timed out: {url}"
    except httpx.HTTPError as exc:
        result["error"] = f"HTTP error: {exc}"
    except Exception as exc:
        result["error"] = f"Failed to fetch {url}: {exc}"
        logger.exception("browser_navigate failed for %s", url)
    return result


async def screenshot(url: str, *, full_page: bool = True) -> dict[str, Any]:
    """Take a screenshot of *url* using Playwright.

    Returns ``{"ok": True, "path": "/tmp/…png"}`` or ``{"ok": False, "error": "..."}``.
    """
    pw = _ensure_playwright()
    if pw is None:
        return {"ok": False, "error": "Playwright is not installed. Run: pip install playwright && playwright install chromium"}
    try:
        return await _playwright_screenshot(url, full_page=full_page)
    except Exception as exc:
        logger.exception("screenshot failed for %s", url)
        return {"ok": False, "error": str(exc)}


async def click(selector: str) -> dict[str, Any]:
    """Click an element on the current page by CSS selector."""
    pw = _ensure_playwright()
    if pw is None:
        return {"ok": False, "error": "Playwright is not installed."}
    global _current_page
    if _current_page is None:
        return {"ok": False, "error": "No page open. Call browser_navigate first."}
    try:
        from playwright.async_api import expect

        el = _current_page.locator(selector)
        await expect(el).to_be_visible(timeout=5000)
        await el.click()
        await _current_page.wait_for_load_state()
        return {"ok": True, "url": _current_page.url, "title": await _current_page.title()}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


async def type_text(selector: str, text: str, *, submit: bool = False) -> dict[str, Any]:
    """Type *text* into an element and optionally submit."""
    pw = _ensure_playwright()
    if pw is None:
        return {"ok": False, "error": "Playwright is not installed."}
    global _current_page
    if _current_page is None:
        return {"ok": False, "error": "No page open. Call browser_navigate first."}
    try:
        el = _current_page.locator(selector)
        await el.fill(text)
        if submit:
            await el.press("Enter")
            await _current_page.wait_for_load_state()
        return {"ok": True, "url": _current_page.url, "title": await _current_page.title()}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


# ---------------------------------------------------------------------------
# Playwright helpers
# ---------------------------------------------------------------------------


def _ensure_playwright() -> Any:
    """Lazy-check if Playwright is importable."""
    global _PLAYWRIGHT_AVAILABLE
    if _PLAYWRIGHT_AVAILABLE is False:
        return None
    if _PLAYWRIGHT_AVAILABLE is None:
        try:
            import playwright  # noqa: F401
            _PLAYWRIGHT_AVAILABLE = True
        except ImportError:
            _PLAYWRIGHT_AVAILABLE = False
            return None
    return True


async def _playwright_screenshot(url: str, *, full_page: bool = True) -> dict[str, Any]:
    try:
        from playwright.async_api import async_playwright
    except ImportError:
        return {"ok": False, "error": "playwright not installed"}

    pw = await async_playwright().start()
    try:
        browser = await pw.chromium.launch(headless=True)
        try:
            page = await browser.new_page()
            await page.goto(url, wait_until="networkidle")
            tmp = tempfile.NamedTemporaryFile(suffix=".png", delete=False)
            if full_page:
                await page.screenshot(path=tmp.name, full_page=True)
            else:
                await page.screenshot(path=tmp.name)
            title = await page.title()
        finally:
            await browser.close()
    finally:
        await pw.stop()
    return {"ok": True, "path": tmp.name, "title": title}
