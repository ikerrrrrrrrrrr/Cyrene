"""Regression tests for verified GitHub issue fixes.

* #50 — scheduler interval unit unification + ``once``/validation behavior
* #44 — attachment analysis cache moved out of source dirs + versioned key
* #45 — notification ``auto`` mode stops after the first successful channel
* #52 — browser tools are reserved for the main agent (no subagent access)
"""

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

# attachments.py imports PIL/pypdf at module load; stub them so the suite runs
# without those heavy deps installed (mirrors tests/test_runtime_fixes.py).
sys.modules.setdefault("PIL", MagicMock())
sys.modules["PIL"].Image = MagicMock()
sys.modules.setdefault("pypdf", MagicMock())


# ---------------------------------------------------------------------------
# #50 — scheduler interval units, once, and validation
# ---------------------------------------------------------------------------

FIXED_NOW = datetime(2026, 6, 4, 12, 0, 0, tzinfo=timezone.utc)


def test_interval_is_seconds_not_milliseconds():
    """An interval of "3600" means one hour — the value the Web UI promises."""
    from cyrene.schedule_spec import compute_next_run

    nxt = compute_next_run("interval", "3600", now=FIXED_NOW)
    assert datetime.fromisoformat(nxt) == FIXED_NOW + timedelta(seconds=3600)


def test_rest_agent_and_runner_agree_on_next_run():
    """All three call sites route through compute_next_run, so identical inputs
    produce identical next_run values (the core of issue #50)."""
    from cyrene import scheduler, tools
    from cyrene.schedule_spec import compute_next_run

    # The agent tool and the scheduler runner both import the shared helper.
    assert tools.compute_next_run is compute_next_run
    assert scheduler.compute_next_run is compute_next_run

    a = compute_next_run("interval", "90", now=FIXED_NOW)
    b = compute_next_run("interval", "90", now=FIXED_NOW)
    assert a == b == (FIXED_NOW + timedelta(seconds=90)).isoformat()


def test_once_respects_provided_time():
    """``once`` must schedule for the requested time, not 'now' (REST bug)."""
    from cyrene.schedule_spec import compute_next_run

    nxt = compute_next_run("once", "2026-12-25T09:30:00+00:00", now=FIXED_NOW)
    assert datetime.fromisoformat(nxt) == datetime(2026, 12, 25, 9, 30, tzinfo=timezone.utc)


def test_once_empty_means_now():
    from cyrene.schedule_spec import compute_next_run

    assert compute_next_run("once", "", now=FIXED_NOW) == FIXED_NOW.isoformat()


def test_once_naive_datetime_interpreted_local_then_utc():
    from cyrene.schedule_spec import compute_next_run

    nxt = compute_next_run("once", "2026-06-04T12:00:00", now=FIXED_NOW)
    # Whatever the machine tz, the result is a valid UTC ISO timestamp.
    parsed = datetime.fromisoformat(nxt)
    assert parsed.tzinfo is not None
    assert parsed.utcoffset() == timedelta(0)


def test_cron_next_run():
    from cyrene.schedule_spec import compute_next_run

    nxt = compute_next_run("cron", "0 9 * * *", now=FIXED_NOW)
    assert datetime.fromisoformat(nxt) == datetime(2026, 6, 5, 9, 0, tzinfo=timezone.utc)


@pytest.mark.parametrize(
    "stype,svalue",
    [
        ("interval", "not-a-number"),
        ("interval", "0"),
        ("interval", "-5"),
        ("cron", "not a cron"),
        ("bogus", "whatever"),
    ],
)
def test_invalid_schedules_raise_valueerror(stype, svalue):
    """Invalid values raise ValueError so the REST API can answer 400 instead of
    silently scheduling for 'now'."""
    from cyrene.schedule_spec import compute_next_run

    with pytest.raises(ValueError):
        compute_next_run(stype, svalue, now=FIXED_NOW)


# ---------------------------------------------------------------------------
# #44 — attachment cache out of source dirs + versioned key
# ---------------------------------------------------------------------------


def test_cache_file_lives_under_data_dir_not_source(tmp_path, monkeypatch):
    from cyrene import attachments

    monkeypatch.setattr(attachments, "ANALYSIS_CACHE_DIR", tmp_path / "cache")
    cache_file = attachments._cache_file("deadbeef")
    assert (tmp_path / "cache") in cache_file.parents


def test_cache_key_changes_with_content(tmp_path):
    from cyrene import attachments

    f = tmp_path / "a.txt"
    f.write_text("hello", encoding="utf-8")
    k1 = attachments._analysis_cache_key(f, "")
    f.write_text("hello world", encoding="utf-8")
    k2 = attachments._analysis_cache_key(f, "")
    assert k1 != k2


def test_cache_key_changes_with_prompt(tmp_path):
    from cyrene import attachments

    f = tmp_path / "a.txt"
    f.write_text("hello", encoding="utf-8")
    assert attachments._analysis_cache_key(f, "describe") != attachments._analysis_cache_key(f, "summarize")


def test_cache_key_changes_with_model_and_parser_version(tmp_path, monkeypatch):
    from cyrene import attachments

    f = tmp_path / "a.txt"
    f.write_text("hello", encoding="utf-8")
    base = attachments._analysis_cache_key(f, "")

    monkeypatch.setattr(attachments, "_vision_model_fingerprint", lambda: "model-X")
    changed_model = attachments._analysis_cache_key(f, "")
    assert changed_model != base

    monkeypatch.setattr(attachments, "_ANALYSIS_PARSER_VERSION", "999")
    changed_parser = attachments._analysis_cache_key(f, "")
    assert changed_parser != changed_model


async def test_analyze_attachment_does_not_write_next_to_source(tmp_path, monkeypatch):
    from cyrene import attachments

    cache_dir = tmp_path / "cache"
    monkeypatch.setattr(attachments, "ANALYSIS_CACHE_DIR", cache_dir)
    monkeypatch.setattr(attachments, "_vision_model_fingerprint", lambda: "fp")

    src = tmp_path / "notes.txt"
    src.write_text("some workspace content", encoding="utf-8")

    result = await attachments.analyze_attachment(str(src))
    assert result["kind"] == "file"

    # No sidecar pollution next to the user's file...
    assert not (tmp_path / "notes.txt.analysis.json").exists()
    assert list(tmp_path.glob("*.analysis.json")) == []
    # ...the cache landed under the app data dir instead.
    assert cache_dir.exists()
    assert list(cache_dir.glob("*.json"))


async def test_analyze_attachment_reuses_and_invalidates_cache(tmp_path, monkeypatch):
    from cyrene import attachments

    monkeypatch.setattr(attachments, "ANALYSIS_CACHE_DIR", tmp_path / "cache")
    monkeypatch.setattr(attachments, "_vision_model_fingerprint", lambda: "fp")

    calls = {"n": 0}
    real_preview = attachments._build_attachment_preview

    def _counting_preview(payload):
        calls["n"] += 1
        return real_preview(payload)

    monkeypatch.setattr(attachments, "_build_attachment_preview", _counting_preview)

    src = tmp_path / "data.txt"
    src.write_text("v1", encoding="utf-8")

    await attachments.analyze_attachment(str(src))
    await attachments.analyze_attachment(str(src))
    assert calls["n"] == 1  # second call served from cache

    src.write_text("v2 different content", encoding="utf-8")
    await attachments.analyze_attachment(str(src))
    assert calls["n"] == 2  # content change busts the cache


# ---------------------------------------------------------------------------
# #45 — notification auto mode stops after the first success
# ---------------------------------------------------------------------------


def _patch_channels(monkeypatch, *, desktop=True, webhook=True, telegram=True, wechat=True, sse=True):
    from cyrene import notifications as n

    mocks = {
        "_notify_desktop": AsyncMock(return_value={"ok": desktop}),
        "_notify_webhook": AsyncMock(return_value={"ok": webhook}),
        "_notify_telegram": AsyncMock(return_value={"ok": telegram}),
        "_notify_wechat": AsyncMock(return_value={"ok": wechat}),
        "_publish_sse": AsyncMock(return_value={"ok": sse}),
    }
    for name, mock in mocks.items():
        monkeypatch.setattr(n, name, mock)
    return mocks


async def test_auto_stops_after_first_success(monkeypatch):
    """A successful desktop notification must NOT fan out to Telegram/WeChat (#45)."""
    from cyrene import notifications as n

    mocks = _patch_channels(monkeypatch, desktop=True)
    result = await n.notify("t", "b", channel="auto")

    assert result["ok"] is True
    mocks["_notify_desktop"].assert_awaited_once()
    mocks["_notify_telegram"].assert_not_awaited()
    mocks["_notify_wechat"].assert_not_awaited()
    mocks["_notify_webhook"].assert_not_awaited()


async def test_auto_falls_through_when_earlier_channels_fail(monkeypatch):
    from cyrene import notifications as n

    # desktop fails, no webhook configured, telegram succeeds -> stop there.
    mocks = _patch_channels(monkeypatch, desktop=False, telegram=True)
    monkeypatch.setattr(n, "_WEBHOOK_URL", "")
    result = await n.notify("t", "b", channel="auto")

    assert result["ok"] is True
    mocks["_notify_desktop"].assert_awaited_once()
    mocks["_notify_telegram"].assert_awaited_once()
    mocks["_notify_wechat"].assert_not_awaited()  # stopped after telegram


async def test_broadcast_hits_every_channel(monkeypatch):
    from cyrene import notifications as n

    mocks = _patch_channels(monkeypatch)
    result = await n.notify("t", "b", channel="broadcast", webhook_url="https://example.test/hook")

    assert result["ok"] is True
    mocks["_notify_desktop"].assert_awaited_once()
    mocks["_notify_webhook"].assert_awaited_once()
    mocks["_notify_telegram"].assert_awaited_once()
    mocks["_notify_wechat"].assert_awaited_once()


async def test_explicit_single_channel(monkeypatch):
    from cyrene import notifications as n

    mocks = _patch_channels(monkeypatch)
    result = await n.notify("t", "b", channel="telegram")

    assert result["ok"] is True
    mocks["_notify_telegram"].assert_awaited_once()
    mocks["_notify_desktop"].assert_not_awaited()
    mocks["_notify_wechat"].assert_not_awaited()


async def test_unknown_channel_is_rejected(monkeypatch):
    from cyrene import notifications as n

    _patch_channels(monkeypatch)
    result = await n.notify("t", "b", channel="carrier-pigeon")
    assert result["ok"] is False
    assert "unknown channel" in result.get("error", "")


# ---------------------------------------------------------------------------
# #52 — browser tools reserved for the main agent
# ---------------------------------------------------------------------------

BROWSER_TOOLS = [
    "browser_navigate",
    "browser_screenshot",
    "browser_click",
    "browser_type",
    "browser_request_takeover",
]


@pytest.mark.parametrize("tool", BROWSER_TOOLS)
def test_subagent_cannot_use_browser_tools(tool):
    """The single shared browser session must not be driven by subagents (#52)."""
    from cyrene import tools

    assert tools.is_tool_allowed_for_actor(tool, "subagent") is False
    assert tools.is_tool_allowed_for_actor(tool, "main") is True


def test_subagent_tool_defs_exclude_browser():
    from cyrene import tools

    names = {td["function"]["name"] for td in tools.get_active_tool_defs_for_actor("subagent")}
    assert names.isdisjoint(BROWSER_TOOLS)
