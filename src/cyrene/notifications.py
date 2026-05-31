"""Notification system — desktop notifications, webhook alerts, and in-app SSE events.

Supports three delivery channels:
  1. **Desktop native** — macOS (SSE + frontend Notification API), Windows (VBScript popup), Linux (notify-send).
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


async def notify(
    title: str,
    body: str,
    *,
    channel: str = "auto",
    webhook_url: str | None = None,
    webhook_type: str | None = None,
) -> dict[str, Any]:
    """Send a notification through available channels.

    Args:
        title: Notification title (short).
        body: Notification body text.
        channel: ``"auto"`` (try desktop → webhook → telegram → wechat → sse),
                 ``"desktop"``, ``"webhook"``, ``"telegram"``, ``"wechat"``,
                 or ``"sse"``.
        webhook_url: Override the configured webhook URL.
        webhook_type: Override the configured webhook type.

    Returns:
        ``{"ok": True}`` or ``{"ok": False, "error": "..."}`` with per-channel results.
    """
    if not _NOTIFICATION_ENABLED:
        return {"ok": False, "error": "notifications are disabled"}

    results: dict[str, Any] = {}

    if channel in ("auto", "desktop"):
        desktop_result = await _notify_desktop(title, body)
        results["desktop"] = desktop_result
        if desktop_result.get("ok") and channel == "desktop":
            # On macOS, desktop is delivered via SSE — the return is deferred
            # to the SSE block below so the event actually gets published.
            if platform.system() == "Darwin":
                pass
            else:
                return {"ok": True, "channels": results}

    if channel in ("auto", "webhook"):
        wh_url = webhook_url or _WEBHOOK_URL
        wh_type = webhook_type or _WEBHOOK_TYPE
        if wh_url:
            webhook_result = await _notify_webhook(wh_url, wh_type, title, body)
            results["webhook"] = webhook_result
            if webhook_result.get("ok") and channel == "webhook":
                return {"ok": True, "channels": results}
        elif channel == "webhook":
            results["webhook"] = {"ok": False, "error": "no webhook URL configured"}

    if channel in ("auto", "telegram"):
        telegram_result = await _notify_telegram(title, body)
        results["telegram"] = telegram_result
        if telegram_result.get("ok") and channel == "telegram":
            return {"ok": True, "channels": results}

    if channel in ("auto", "wechat"):
        wechat_result = await _notify_wechat(title, body)
        results["wechat"] = wechat_result
        if wechat_result.get("ok") and channel == "wechat":
            return {"ok": True, "channels": results}

    _desktop_is_sse = platform.system() == "Darwin" and results.get("desktop", {}).get("ok")
    if channel in ("auto", "sse") or _desktop_is_sse:
        try:
            from cyrene import debug as cy_debug

            await cy_debug.publish_event({
                "type": "notification",
                "title": title,
                "body": body,
                "ts": datetime.now(timezone.utc).isoformat(),
            })
            results["sse"] = {"ok": True}
        except Exception as exc:
            results["sse"] = {"ok": False, "error": str(exc)}
        if channel == "sse" or (channel == "desktop" and _desktop_is_sse) or (channel == "auto" and not any(r.get("ok") for r in results.values())):
            return {"ok": results.get("sse", {}).get("ok", False), "channels": results}

    any_ok = any(r.get("ok") for r in results.values())
    return {"ok": any_ok, "channels": results}


# ---------------------------------------------------------------------------
# Desktop — cross-platform (macOS, Windows, Linux)
# ---------------------------------------------------------------------------


async def _notify_desktop(title: str, body: str) -> dict[str, Any]:
    system = platform.system()
    try:
        if system == "Darwin":
            # macOS desktop notifications are delivered via SSE + frontend
            # Notification API. Return ok so notify() knows to skip other
            # channels — the actual SSE publish happens in the SSE block.
            return {"ok": True, "via": "sse"}
        elif system == "Windows":
            return _notify_windows(title, body)
        elif system == "Linux":
            return _notify_linux(title, body)
        else:
            return {"ok": False, "error": f"unsupported platform: {system}"}
    except Exception as exc:
        logger.warning("Desktop notification failed: %s", exc)
        return {"ok": False, "error": str(exc)}


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
