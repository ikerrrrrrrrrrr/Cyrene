"""M1 tests for the persistent browser session (src/cyrene/browser.py).

Covers the live-view foundation without launching a real browser:
  - navigate() drives the shared page and returns extracted text
  - every action emits a structured ``browser_frame`` SSE event
  - click/type refuse to run before navigate (regression: the old
    ``_current_page`` global was never assigned, so these were dead code)
  - navigate falls back to httpx when Playwright is unavailable
"""

import asyncio
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

# Patch missing optional deps before any cyrene import (mirrors test_runtime_fixes).
sys.modules.setdefault("PIL", MagicMock())
sys.modules["PIL"].Image = MagicMock()
sys.modules.setdefault("pypdf", MagicMock())


class _FakePage:
    """Minimal stand-in for a Playwright Page."""

    def __init__(self, url: str = "https://example.com/") -> None:
        self.url = url

    async def goto(self, url, **_kw):
        self.url = url
        return MagicMock(status=200)

    async def title(self):
        return "Example"

    async def content(self):
        return (
            "<html><head><title>Example</title></head>"
            "<body><h1>Hello</h1><p>World</p></body></html>"
        )

    async def screenshot(self, **_kw):
        return b"\xff\xd8\xff\xe0fake-jpeg-bytes"

    async def wait_for_load_state(self, *_a, **_k):
        return None


def _capture_publish(monkeypatch):
    """Patch debug.publish_event and return the list it appends events to."""
    from cyrene import debug

    captured: list[dict] = []

    async def fake_publish(event):
        captured.append(event)

    monkeypatch.setattr(debug, "publish_event", fake_publish)
    return captured


async def test_session_navigate_returns_text_and_emits_frame(monkeypatch):
    from cyrene import browser

    captured = _capture_publish(monkeypatch)
    session = browser._BrowserSession()
    session._page = _FakePage()

    async def _noop(**_kw):
        return None

    monkeypatch.setattr(session, "_ensure_started", _noop)

    result = await session.navigate("https://example.com/page")

    assert result["title"] == "Example"
    assert "Hello" in result["text"] and "World" in result["text"]
    assert session._page.url == "https://example.com/page"

    frames = [e for e in captured if e.get("type") == "browser_frame"]
    assert len(frames) == 1
    assert frames[0]["action"] == "navigate"
    assert frames[0]["image"].startswith("data:image/jpeg;base64,")


async def test_emit_frame_normalizes_box_and_target(monkeypatch):
    from cyrene import browser

    captured = _capture_publish(monkeypatch)
    session = browser._BrowserSession()
    session._page = _FakePage("https://site/login")

    await session._emit_frame(
        "click", target="#submit", box={"x": 10, "y": 20, "width": 30, "height": 40}
    )

    ev = captured[-1]
    assert ev["type"] == "browser_frame"
    assert ev["action"] == "click"
    assert ev["target"] == "#submit"
    assert ev["box"] == {"x": 10, "y": 20, "w": 30, "h": 40}
    assert ev["url"] == "https://site/login"


async def test_emit_frame_is_best_effort(monkeypatch):
    """A screenshot failure must not raise out of _emit_frame."""
    from cyrene import browser

    _capture_publish(monkeypatch)
    session = browser._BrowserSession()

    class _BrokenPage(_FakePage):
        async def screenshot(self, **_kw):
            raise RuntimeError("boom")

    session._page = _BrokenPage()
    # Should swallow the error rather than propagate.
    await session._emit_frame("navigate")


async def test_click_requires_navigate_first(monkeypatch):
    from cyrene import browser

    monkeypatch.setattr(browser, "_PLAYWRIGHT_AVAILABLE", True)
    monkeypatch.setattr(browser, "_session", None)

    result = await browser.click("#x")

    assert result["ok"] is False
    assert "navigate" in result["error"].lower()


async def test_type_requires_navigate_first(monkeypatch):
    from cyrene import browser

    monkeypatch.setattr(browser, "_PLAYWRIGHT_AVAILABLE", True)
    monkeypatch.setattr(browser, "_session", None)

    result = await browser.type_text("#x", "hello")

    assert result["ok"] is False
    assert "navigate" in result["error"].lower()


async def test_navigate_falls_back_to_httpx_without_playwright(monkeypatch):
    from cyrene import browser

    monkeypatch.setattr(browser, "_PLAYWRIGHT_AVAILABLE", False)

    called: dict = {}

    async def fake_httpx(url, **_kw):
        called["url"] = url
        return {"url": url, "status": 200, "title": "x", "text": "y", "error": None}

    monkeypatch.setattr(browser, "_httpx_navigate", fake_httpx)

    result = await browser.navigate("https://ex.com")

    assert called["url"] == "https://ex.com"
    assert result["status"] == 200


async def test_click_delegates_to_session(monkeypatch):
    """When a page is open, browser.click drives the session and emits a frame."""
    pytest.importorskip("playwright")
    from cyrene import browser

    captured = _capture_publish(monkeypatch)
    monkeypatch.setattr(browser, "_PLAYWRIGHT_AVAILABLE", True)

    session = browser._BrowserSession()
    session._page = _FakePage("https://site/")

    # Stub the heavy Playwright bits: locator + expect.
    fake_locator = MagicMock()

    async def _box():
        return {"x": 1, "y": 2, "width": 3, "height": 4}

    async def _click():
        return None

    fake_locator.bounding_box = _box
    fake_locator.click = _click
    session._page.locator = lambda _sel: fake_locator

    import playwright.async_api as _pw  # noqa: F401  (present in this env)

    async def _expect_visible(*_a, **_k):
        return None

    fake_expect = MagicMock()
    fake_expect.return_value.to_be_visible = _expect_visible
    monkeypatch.setattr("playwright.async_api.expect", fake_expect)

    monkeypatch.setattr(browser, "_session", session)

    result = await browser.click("#go")

    assert result["ok"] is True
    frames = [e for e in captured if e.get("type") == "browser_frame"]
    assert frames and frames[-1]["action"] == "click"
    assert frames[-1]["box"] == {"x": 1, "y": 2, "w": 3, "h": 4}


# --- M2: screencast fan-out -------------------------------------------------


class _FakeCDP:
    def __init__(self) -> None:
        self.sent: list = []
        self.handlers: dict = {}
        self.detached = False

    def on(self, event, handler):
        self.handlers[event] = handler

    async def send(self, method, params=None):
        self.sent.append((method, params))

    async def detach(self):
        self.detached = True


class _FakeContext:
    def __init__(self, cdp):
        self._cdp = cdp

    async def new_cdp_session(self, _page):
        return self._cdp


async def test_screencast_start_stop_bookkeeping(monkeypatch):
    from cyrene import browser

    session = browser._BrowserSession()
    session._page = _FakePage()
    cdp = _FakeCDP()
    session._context = _FakeContext(cdp)

    async def _noop(**_kw):
        return None

    monkeypatch.setattr(session, "_ensure_started", _noop)

    q1, q2 = asyncio.Queue(), asyncio.Queue()

    await session.start_screencast(q1)
    assert session._screencasting is True
    assert "Page.startScreencast" in [m for m, _ in cdp.sent]
    assert "Page.screencastFrame" in cdp.handlers

    # A second subscriber must not restart the screencast.
    cdp.sent.clear()
    await session.start_screencast(q2)
    assert "Page.startScreencast" not in [m for m, _ in cdp.sent]
    assert session._frame_subs == {q1, q2}

    # Dropping one keeps casting; dropping the last tears it down.
    await session.stop_screencast(q1)
    assert session._screencasting is True
    await session.stop_screencast(q2)
    assert session._screencasting is False
    assert cdp.detached is True
    assert "Page.stopScreencast" in [m for m, _ in cdp.sent]


async def test_screencast_frame_fans_out_and_acks():
    from cyrene import browser

    session = browser._BrowserSession()
    session._page = _FakePage("https://x/")
    cdp = _FakeCDP()
    session._cdp = cdp
    q1, q2 = asyncio.Queue(), asyncio.Queue()
    session._frame_subs = {q1, q2}

    session._on_screencast_frame({"data": "BASE64", "sessionId": "s1"})
    await asyncio.sleep(0)  # let the ack task run

    f1, f2 = q1.get_nowait(), q2.get_nowait()
    assert f1["data"] == "BASE64" and f1["url"] == "https://x/"
    assert f2["data"] == "BASE64"
    assert ("Page.screencastFrameAck", {"sessionId": "s1"}) in cdp.sent


async def test_screencast_drops_frames_when_queue_full():
    from cyrene import browser

    session = browser._BrowserSession()
    session._page = _FakePage()
    session._cdp = None  # no ack path
    q = asyncio.Queue(maxsize=1)
    q.put_nowait({"data": "old", "url": ""})
    session._frame_subs = {q}

    # Must not raise even though the queue is full; the new frame is dropped.
    session._on_screencast_frame({"data": "new", "sessionId": None})
    assert q.qsize() == 1
    assert q.get_nowait()["data"] == "old"


# --- M3: native-window login takeover --------------------------------------


async def test_browser_request_takeover_pauses_with_takeover_meta(monkeypatch):
    import json

    from cyrene import tools as _tools
    from cyrene import debug as _debug
    from cyrene import browser as _browser
    from cyrene.agent import state as _state
    from cyrene.agent import session as _session

    agent_token = _state._current_agent_id.set("main")
    round_token = _state._current_round_id.set("round_1")
    try:
        events = []

        async def fake_publish(event):
            events.append(event)

        monkeypatch.setattr(_debug, "publish_event", fake_publish)

        switched = []

        class _FakeSession:
            async def current_url(self):
                return "https://example.com/login"

            async def switch_to_headed(self, url=""):
                switched.append(url)

        async def fake_get_session():
            return _FakeSession()

        monkeypatch.setattr(_browser, "get_session", fake_get_session)

        captured = {}

        async def fake_upsert(payload):
            captured.update(payload)
            return {"id": "q_123"}

        monkeypatch.setattr(_session, "_upsert_pending_question", fake_upsert)
        monkeypatch.setattr(_session, "get_session_labels", lambda rid=None: {})

        result = await _tools._tool_browser_request_takeover(
            {"reason": "Please log in to Gmail"}, None, 0, "db", None
        )

        payload = json.loads(result)
        assert payload["status"] == "awaiting_user"
        assert payload["question_id"] == "q_123"
        # The native window was raised before pausing.
        assert switched == ["https://example.com/login"]
        # The pending question is tagged so the resume hook can restore headless.
        assert captured["meta"] == {"kind": "browser_takeover", "url": "https://example.com/login"}
        assert any(e.get("type") == "browser_takeover_request" for e in events)
    finally:
        _state._current_agent_id.reset(agent_token)
        _state._current_round_id.reset(round_token)


async def test_browser_request_takeover_rejects_non_main_agent(monkeypatch):
    from cyrene import tools as _tools
    from cyrene.agent import state as _state

    agent_token = _state._current_agent_id.set("alice")
    round_token = _state._current_round_id.set("round_1")
    try:
        result = await _tools._tool_browser_request_takeover({"reason": "x"}, None, 0, "db", None)
        assert "main agent" in result.lower()
    finally:
        _state._current_agent_id.reset(agent_token)
        _state._current_round_id.reset(round_token)
