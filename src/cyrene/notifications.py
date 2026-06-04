"""Notification system — desktop notifications, webhook alerts, and in-app SSE events.

Supports three delivery channels:
  1. **Desktop native** — macOS (``terminal-notifier`` with app icon), Windows (VBScript popup), Linux (notify-send).
  2. **Webhook** — POST to Discord, Slack, or generic webhook URLs.
  3. **In-app SSE** — pushes through the existing ``debug.publish_event`` bus.

Configure via ``.env``:
  - ``NOTIFICATION_WEBHOOK_URL`` — optional webhook endpoint (Discord/Slack/Generic).
  - ``NOTIFICATION_WEBHOOK_TYPE`` — ``discord``, ``slack``, or ``generic``.

Agent tool ``send_notification`` lets the agent send notifications directly.
"""

from __future__ import annotations

import asyncio
import logging
import os
import platform
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from typing import Any

import httpx

logger = logging.getLogger(__name__)

_WEBHOOK_URL = os.getenv("NOTIFICATION_WEBHOOK_URL", "").strip()
_WEBHOOK_TYPE = os.getenv("NOTIFICATION_WEBHOOK_TYPE", "generic").strip().lower()
_NOTIFICATION_ENABLED = os.getenv("NOTIFICATION_ENABLED", "1") not in ("0", "false", "no")


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


_ALL_CHANNELS = ("desktop", "webhook", "telegram", "wechat", "sse")


async def notify(
    title: str,
    body: str,
    *,
    channel: str = "auto",
    webhook_url: str | None = None,
    webhook_type: str | None = None,
) -> dict[str, Any]:
    """Send a notification through one or more channels.

    Args:
        title: Notification title (short).
        body: Notification body text.
        channel: delivery mode —
            ``"auto"`` tries channels in order (desktop → webhook → telegram →
            wechat → sse) and **stops after the first success**, so a delivered
            desktop notification never fans out to external messengers (#45);
            ``"broadcast"`` delivers through *every* configured channel; or a
            single channel name (``"desktop"``, ``"webhook"``, ``"telegram"``,
            ``"wechat"``, ``"sse"``).
        webhook_url: Override the configured webhook URL.
        webhook_type: Override the configured webhook type.

    Returns:
        ``{"ok": bool, "channels": {name: {"ok": bool, ...}}}``.
    """
    if not _NOTIFICATION_ENABLED:
        return {"ok": False, "error": "notifications are disabled"}

    if channel == "auto":
        order, stop_after_first = list(_ALL_CHANNELS), True
    elif channel == "broadcast":
        order, stop_after_first = list(_ALL_CHANNELS), False
    elif channel in _ALL_CHANNELS:
        order, stop_after_first = [channel], False
    else:
        return {"ok": False, "error": f"unknown channel: {channel}"}

    results: dict[str, Any] = {}
    for ch in order:
        res = await _dispatch_channel(ch, title, body, webhook_url, webhook_type)
        if res is None:
            # Channel not applicable (e.g. no webhook URL). Surface it as an
            # error only when that channel was explicitly requested.
            if len(order) == 1:
                results[ch] = {"ok": False, "error": f"{ch} not configured"}
            continue
        results[ch] = res
        if stop_after_first and res.get("ok"):
            break

    any_ok = any(r.get("ok") for r in results.values())
    return {"ok": any_ok, "channels": results}


async def _dispatch_channel(
    ch: str,
    title: str,
    body: str,
    webhook_url: str | None,
    webhook_type: str | None,
) -> dict[str, Any] | None:
    """Deliver through a single channel. Returns the per-channel result, or
    ``None`` when the channel is not applicable (e.g. no webhook configured)."""
    if ch == "desktop":
        return await _notify_desktop(title, body)
    if ch == "webhook":
        wh_url = webhook_url or _WEBHOOK_URL
        if not wh_url:
            return None
        return await _notify_webhook(wh_url, webhook_type or _WEBHOOK_TYPE, title, body)
    if ch == "telegram":
        return await _notify_telegram(title, body)
    if ch == "wechat":
        return await _notify_wechat(title, body)
    if ch == "sse":
        return await _publish_sse(title, body)
    return {"ok": False, "error": f"unknown channel: {ch}"}


async def _publish_sse(title: str, body: str) -> dict[str, Any]:
    """Publish a ``notification`` event on the in-app SSE bus."""
    try:
        from cyrene import debug as cy_debug

        await cy_debug.publish_event({
            "type": "notification",
            "title": title,
            "body": body,
            "ts": datetime.now(timezone.utc).isoformat(),
        })
        return {"ok": True}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


# ---------------------------------------------------------------------------
# Desktop — cross-platform (macOS, Windows, Linux)
# ---------------------------------------------------------------------------


async def _notify_desktop(title: str, body: str) -> dict[str, Any]:
    system = platform.system()
    try:
        if system == "Darwin":
            return _notify_macos(title, body)
        elif system == "Windows":
            return _notify_windows(title, body)
        elif system == "Linux":
            return _notify_linux(title, body)
        else:
            return {"ok": False, "error": f"unsupported platform: {system}"}
    except Exception as exc:
        logger.warning("Desktop notification failed: %s", exc)
        return {"ok": False, "error": str(exc)}


def _notify_macos(title: str, body: str) -> dict[str, Any]:
    """macOS native notification via ``terminal-notifier``.

    ``terminal-notifier`` is a small CLI tool (``brew install terminal-notifier``)
    that sends real Notification Center alerts from any process — no bundle ID,
    no running NSApplication, no AppleScript required.  It fires even when no
    Web UI or Electron window is open, which is the whole point for scheduled-task
    reminders (#12).

    Three-tier layout so each piece of information has its own line::

        [Cyrene icon]  Cyrene                ← ASSISTANT_NAME (always)
                       Scheduled task done   ← title arg  (event/task label)
                       Backed up 42 files    ← body arg   (execution detail)

    When ``terminal-notifier`` is not installed the channel reports failure so
    ``auto`` mode can fall through to the next available channel (SSE, WeChat,
    Telegram) rather than silently dropping the notification.
    """
    import shutil
    from cyrene.config import ASSISTANT_NAME, BASE_DIR

    binary = shutil.which("terminal-notifier")
    if not binary:
        return {
            "ok": False,
            "error": (
                "terminal-notifier not found; "
                "install it with: brew install terminal-notifier"
            ),
        }

    # Use the installed Cyrene.app as the notification sender so the left icon
    # shows Cyrene's own app icon on all macOS versions (the -appIcon flag was
    # restricted by Apple on macOS 12+, but -sender is reliable).  If the
    # Electron app is not installed, terminal-notifier falls back to its own
    # icon gracefully — no error, no crash.
    #
    # Three-tier layout:
    #   -title    → ASSISTANT_NAME ("Cyrene")  — always the app/agent name
    #   -subtitle → title arg                  — task type / event label
    #   -message  → body arg                   — execution detail / content
    cmd = [
        binary,
        "-sender",   "com.cyrene.app",
        "-title",    ASSISTANT_NAME,
        "-subtitle", title,
        "-message",  body,
        "-sound",    "default",
    ]

    proc = subprocess.run(cmd, capture_output=True, timeout=10)
    if proc.returncode != 0:
        err = proc.stderr.decode("utf-8", "replace").strip() or f"terminal-notifier exited {proc.returncode}"
        return {"ok": False, "error": err}
    return {"ok": True}


def _notify_windows(title: str, body: str) -> dict[str, Any]:
    """Windows toast popup via VBScript (auto-dismisses after 5s)."""
    safe_title = title.replace('"', '""')
    safe_body = body.replace('"', '""')
    vbs = f'CreateObject("Wscript.Shell").Popup "{safe_body}", 5, "{safe_title}", 64'
    tmp = None
    try:
        with tempfile.NamedTemporaryFile(suffix=".vbs", mode="w", delete=False) as f:
            f.write(vbs)
            tmp = f.name
        subprocess.run(["cscript", "//NoLogo", tmp], capture_output=True, timeout=10)
        return {"ok": True}
    finally:
        if tmp:
            try:
                os.unlink(tmp)
            except Exception:
                pass


def _notify_linux(title: str, body: str) -> dict[str, Any]:
    """Linux desktop notification via notify-send (libnotify)."""
    subprocess.run(
        ["notify-send", title, body],
        capture_output=True, timeout=10,
    )
    return {"ok": True}


# ---------------------------------------------------------------------------
# Telegram
# ---------------------------------------------------------------------------


async def _notify_telegram(title: str, body: str) -> dict[str, Any]:
    """Send a Telegram message to the configured owner."""
    from cyrene.config import OWNER_ID, TELEGRAM_BOT_TOKEN
    from cyrene.settings_store import get as get_setting

    if not TELEGRAM_BOT_TOKEN or not OWNER_ID:
        return {"ok": False, "error": "TELEGRAM_BOT_TOKEN or OWNER_ID not configured"}
    if not get_setting("notify_telegram", True):
        return {"ok": False, "error": "Telegram notifications disabled in settings"}

    try:
        text = f"*{title}*\n{body}"
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(
                f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
                json={"chat_id": OWNER_ID, "text": text, "parse_mode": "Markdown"},
            )
            resp.raise_for_status()
        return {"ok": True}
    except Exception as exc:
        logger.warning("Telegram notification failed: %s", exc)
        return {"ok": False, "error": str(exc)}


# ---------------------------------------------------------------------------
# WeChat
# ---------------------------------------------------------------------------


async def _notify_wechat(title: str, body: str) -> dict[str, Any]:
    """Send a WeChat message to the configured owner."""
    from cyrene.channels.wechat import get_current_client
    from cyrene.config import WECHAT_OWNER_ID
    from cyrene.settings_store import get as get_setting

    if not get_setting("notify_wechat", True):
        return {"ok": False, "error": "WeChat notifications disabled in settings"}
    client = get_current_client()
    if not client:
        return {"ok": False, "error": "WeChat client not connected"}

    owner_id = WECHAT_OWNER_ID or client._config.owner_wxid
    if not owner_id:
        return {"ok": False, "error": "WeChat owner wxid not configured"}

    text = f"📋 {title}\n{body}"
    try:
        await client.send_message(owner_id, text)
        return {"ok": True}
    except Exception as exc:
        logger.warning("WeChat notification failed, retrying: %s", exc)

    # Retry once with a fresh client (token may have expired or been replaced)
    client2 = get_current_client()
    if client2 is not client and client2:
        owner_id2 = WECHAT_OWNER_ID or client2._config.owner_wxid
        if owner_id2:
            try:
                await client2.send_message(owner_id2, text)
                return {"ok": True}
            except Exception as exc2:
                logger.warning("WeChat notification retry also failed: %s", exc2)
                return {"ok": False, "error": str(exc2)}

    return {"ok": False, "error": "WeChat notification failed"}


# ---------------------------------------------------------------------------
# Webhook (Discord / Slack / Generic)
# ---------------------------------------------------------------------------


async def _notify_webhook(url: str, wh_type: str, title: str, body: str) -> dict[str, Any]:
    try:
        payload = _webhook_payload(wh_type, title, body)
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.post(url, json=payload)
            resp.raise_for_status()
        return {"ok": True}
    except Exception as exc:
        logger.warning("Webhook notification failed: %s", exc)
        return {"ok": False, "error": str(exc)}


def _webhook_payload(wh_type: str, title: str, body: str) -> dict[str, Any]:
    if wh_type == "discord":
        return {
            "content": f"**{title}**\n{body}",
            "username": "Cyrene",
        }
    if wh_type == "slack":
        return {
            "text": f"*{title}*\n{body}",
            "username": "Cyrene",
        }
    # Generic
    return {"title": title, "body": body, "source": "cyrene"}
