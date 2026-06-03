"""Browser automation — a persistent Playwright session with live-view frame streaming.

Uses ``httpx`` for basic HTTP fetching (always available, used as a fallback when
Playwright is missing). When Playwright is installed, a single **persistent browser
context** (with an on-disk profile, so logins survive across runs) is launched lazily
and reused across navigate / click / type. After every action a JPEG screenshot is
published as a ``browser_frame`` SSE event so the WebUI can show, in real time, what
the agent sees and does.

Tools exposed to the agent (see ``tools.py``):
  - ``browser_navigate`` — open a page in the shared session, return readable text
  - ``browser_screenshot`` — screenshot the current page (Playwright required)
  - ``browser_click`` — click an element by CSS selector (Playwright required)
  - ``browser_type`` — type text into an input (Playwright required)

Live-view / takeover design lives in ``~/.claude/plans/browser-live-view-takeover.md``.
This module currently implements M1 (persistent session + frame events); screencast
WebSocket (M2) and native-window login takeover (M3) slot in on top of ``_BrowserSession``.

Playwright setup::

    pip install playwright
    playwright install chromium
"""

from __future__ import annotations

import asyncio
import base64
import logging
import re
import tempfile
import time
from html.parser import HTMLParser
from typing import Any

import httpx

logger = logging.getLogger(__name__)

_PLAYWRIGHT_AVAILABLE: bool | None = None

# Screenshot/frame tuning — keep base64 frames small enough to ride the SSE bus.
_FRAME_QUALITY = 60
_VIEWPORT = {"width": 1280, "height": 800}


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
# Persistent browser session
# ---------------------------------------------------------------------------


class _BrowserSession:
    """A single, lazily-launched persistent Playwright context shared by all browser
    tools. One context, one page (for now); access is serialized by ``_action_lock``.

    ``_mode_lock`` guards mode switches (M3 takeover restart) because a persistent
    ``user_data_dir`` may only back one Chromium instance at a time.
    """

    def __init__(self) -> None:
        self._pw: Any = None
        self._context: Any = None
        self._page: Any = None
        self._mode: str = "headless"
        self._action_lock = asyncio.Lock()
        self._mode_lock = asyncio.Lock()
        # Screencast (M2): live JPEG frames fanned out to WebSocket subscribers.
        self._cdp: Any = None
        self._screencasting = False
        self._screencast_lock = asyncio.Lock()
        self._frame_subs: set[asyncio.Queue] = set()

    @property
    def profile_dir(self) -> str:
        from cyrene.config import DATA_DIR

        d = DATA_DIR / "browser_profile"
        d.mkdir(parents=True, exist_ok=True)
        return str(d)

    async def _ensure_started(self, *, headless: bool = True) -> None:
        if self._context is not None:
            return
        from playwright.async_api import async_playwright

        self._pw = await async_playwright().start()
        self._context = await self._pw.chromium.launch_persistent_context(
            self.profile_dir,
            headless=headless,
            viewport=_VIEWPORT,
        )
        pages = self._context.pages
        self._page = pages[0] if pages else await self._context.new_page()
        self._mode = "headless" if headless else "headed"

    async def page(self) -> Any:
        await self._ensure_started()
        return self._page

    async def current_url(self) -> str:
        if self._page is None:
            return ""
        try:
            return self._page.url
        except Exception:
            return ""

    async def navigate(self, url: str, *, max_chars: int = 8000) -> dict[str, Any]:
        async with self._action_lock:
            page = await self.page()
            response = await page.goto(url, wait_until="domcontentloaded", timeout=30000)
            status = response.status if response else 0
            title = await page.title()
            html = await page.content()
            text = _html_to_text(html, max_chars=max_chars)
            await self._emit_frame("navigate", url=page.url, title=title)
            return {"url": page.url, "status": status, "title": title, "text": text, "error": None}

    async def click(self, selector: str) -> dict[str, Any]:
        async with self._action_lock:
            from playwright.async_api import expect

            page = await self.page()
            el = page.locator(selector)
            await expect(el).to_be_visible(timeout=5000)
            box = await el.bounding_box()
            await el.click()
            await page.wait_for_load_state()
            title = await page.title()
            await self._emit_frame("click", target=selector, box=box, url=page.url, title=title)
            return {"ok": True, "url": page.url, "title": title}

    async def type_text(self, selector: str, text: str, *, submit: bool = False) -> dict[str, Any]:
        async with self._action_lock:
            page = await self.page()
            el = page.locator(selector)
            box = await el.bounding_box()
            await el.fill(text)
            if submit:
                await el.press("Enter")
                await page.wait_for_load_state()
            title = await page.title()
            await self._emit_frame("type", target=selector, box=box, url=page.url, title=title)
            return {"ok": True, "url": page.url, "title": title}

    async def screenshot_path(self, *, full_page: bool = True) -> str:
        page = await self.page()
        tmp = tempfile.NamedTemporaryFile(suffix=".png", delete=False)
        await page.screenshot(path=tmp.name, full_page=full_page)
        return tmp.name

    async def _emit_frame(self, action: str, *, target: str | None = None, box: Any = None, url: str = "", title: str = "") -> None:
        """Publish a ``browser_frame`` SSE event with a JPEG snapshot of the page.

        Best-effort: a failed frame must never break the underlying browser action.
        """
        try:
            from cyrene import debug
            from cyrene.agent.state import _current_round_id

            page = self._page
            if page is None:
                return
            raw = await page.screenshot(type="jpeg", quality=_FRAME_QUALITY)
            image = "data:image/jpeg;base64," + base64.b64encode(raw).decode("ascii")
            norm_box = None
            if isinstance(box, dict) and box:
                norm_box = {
                    "x": box.get("x", 0),
                    "y": box.get("y", 0),
                    "w": box.get("width", 0),
                    "h": box.get("height", 0),
                }
            await debug.publish_event({
                "type": "browser_frame",
                "round_id": _current_round_id.get(),
                "url": url or self._page.url,
                "title": title,
                "action": action,
                "target": target,
                "box": norm_box,
                "image": image,
                "ts": time.time(),
            })
        except Exception:
            logger.debug("browser_frame emit failed", exc_info=True)

    # -- Screencast (M2): continuous live frames over WebSocket --------------

    async def start_screencast(self, queue: "asyncio.Queue") -> None:
        """Register *queue* as a frame subscriber; start CDP screencast on demand."""
        async with self._screencast_lock:
            self._frame_subs.add(queue)
            if self._screencasting:
                return
            await self._ensure_started()
            self._cdp = await self._context.new_cdp_session(self._page)
            self._cdp.on("Page.screencastFrame", self._on_screencast_frame)
            await self._cdp.send("Page.startScreencast", {
                "format": "jpeg",
                "quality": _FRAME_QUALITY,
                "maxWidth": _VIEWPORT["width"],
                "maxHeight": _VIEWPORT["height"],
                "everyNthFrame": 1,
            })
            self._screencasting = True

    async def stop_screencast(self, queue: "asyncio.Queue") -> None:
        """Unregister *queue*; tear the CDP screencast down when the last one leaves."""
        async with self._screencast_lock:
            self._frame_subs.discard(queue)
            if self._frame_subs or not self._screencasting:
                return
            await self._teardown_screencast()

    async def _teardown_screencast(self) -> None:
        if self._cdp is not None:
            try:
                await self._cdp.send("Page.stopScreencast")
            except Exception:
                pass
            try:
                await self._cdp.detach()
            except Exception:
                pass
        self._cdp = None
        self._screencasting = False

    def _on_screencast_frame(self, params: dict[str, Any]) -> None:
        """CDP callback (sync): ack the frame and fan it out to subscriber queues.

        Slow consumers simply drop frames (bounded queues) rather than apply
        backpressure to the browser.
        """
        session_id = params.get("sessionId")
        if self._cdp is not None and session_id is not None:
            asyncio.create_task(self._safe_ack(session_id))
        frame = {
            "data": params.get("data", ""),
            "url": self._page.url if self._page is not None else "",
        }
        for queue in list(self._frame_subs):
            try:
                queue.put_nowait(frame)
            except asyncio.QueueFull:
                pass

    async def _safe_ack(self, session_id: str) -> None:
        try:
            if self._cdp is not None:
                await self._cdp.send("Page.screencastFrameAck", {"sessionId": session_id})
        except Exception:
            pass

    async def close(self) -> None:
        try:
            await self._teardown_screencast()
        except Exception:
            pass
        self._frame_subs.clear()
        try:
            if self._context is not None:
                await self._context.close()
        except Exception:
            pass
        try:
            if self._pw is not None:
                await self._pw.stop()
        except Exception:
            pass
        self._context = None
        self._page = None
        self._pw = None


_session: _BrowserSession | None = None


def _get_session() -> _BrowserSession:
    global _session
    if _session is None:
        _session = _BrowserSession()
    return _session


async def get_session() -> _BrowserSession:
    """Return the started, ready-to-use shared browser session."""
    session = _get_session()
    await session._ensure_started()
    return session


async def close_session() -> None:
    """Shut the shared browser session down (call on app shutdown)."""
    global _session
    if _session is not None:
        await _session.close()
        _session = None


# ---------------------------------------------------------------------------
# Public API (stable signatures consumed by tools.py)
# ---------------------------------------------------------------------------


async def navigate(
    url: str,
    *,
    extract_text: bool = True,
    max_chars: int = 8000,
    headers: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Open *url* in the shared browser session and return structured page data.

    Falls back to a plain ``httpx`` fetch when Playwright is unavailable (or the
    persistent session fails to launch), preserving the original behavior.

    Returns::
        {"url": str, "status": int, "title": str, "text": str, "error": str | None}
    """
    if _ensure_playwright() is not None:
        try:
            session = await get_session()
            return await session.navigate(url, max_chars=max_chars)
        except Exception as exc:
            logger.warning("Playwright navigate failed (%s); falling back to httpx", exc)
    return await _httpx_navigate(url, extract_text=extract_text, max_chars=max_chars, headers=headers)


async def _httpx_navigate(
    url: str,
    *,
    extract_text: bool = True,
    max_chars: int = 8000,
    headers: dict[str, str] | None = None,
) -> dict[str, Any]:
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
    """Open *url* in the shared session and screenshot it to a temp PNG.

    Returns ``{"ok": True, "path": "/tmp/…png"}`` or ``{"ok": False, "error": "..."}``.
    """
    if _ensure_playwright() is None:
        return {"ok": False, "error": "Playwright is not installed. Run: pip install playwright && playwright install chromium"}
    try:
        session = await get_session()
        await session.navigate(url)
        path = await session.screenshot_path(full_page=full_page)
        title = await (await session.page()).title()
        return {"ok": True, "path": path, "title": title}
    except Exception as exc:
        logger.exception("screenshot failed for %s", url)
        return {"ok": False, "error": str(exc)}


async def click(selector: str) -> dict[str, Any]:
    """Click an element on the current page by CSS selector."""
    if _ensure_playwright() is None:
        return {"ok": False, "error": "Playwright is not installed."}
    session = _get_session()
    if session._page is None:
        return {"ok": False, "error": "No page open. Call browser_navigate first."}
    try:
        return await session.click(selector)
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


async def type_text(selector: str, text: str, *, submit: bool = False) -> dict[str, Any]:
    """Type *text* into an element and optionally submit."""
    if _ensure_playwright() is None:
        return {"ok": False, "error": "Playwright is not installed."}
    session = _get_session()
    if session._page is None:
        return {"ok": False, "error": "No page open. Call browser_navigate first."}
    try:
        return await session.type_text(selector, text, submit=submit)
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


# ---------------------------------------------------------------------------
# Playwright availability
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
