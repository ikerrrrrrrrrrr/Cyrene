"""Notification system — desktop notifications, webhook alerts, and in-app SSE events.

Supports three delivery channels:
  1. **macOS native** — uses ``osascript`` for Notification Center alerts.
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
import subprocess
import sys
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
        channel: ``"auto"`` (try desktop → webhook → sse), ``"desktop"``,
                 ``"webhook"``, or ``"sse"``.
        webhook_url: Override the configured webhook URL.
        webhook_type: Override the configured webhook type.

    Returns:
        ``{"ok": True}`` or ``{"ok": False, "error": "..."}`` with per-channel results.
    """
    if not _NOTIFICATION_ENABLED:
        return {"ok": False, "error": "notifications are disabled"}

    results: dict[str, Any] = {}

    if channel in ("auto", "desktop"):
        desktop_result = _notify_desktop(title, body)
        results["desktop"] = desktop_result
        if desktop_result.get("ok") and channel == "desktop":
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

    if channel in ("auto", "sse"):
        try:
            from cyrene import debug as cy_debug

            await cy_debug.publish_event({
                "type": "notification",
                "title": title,
                "body": body,
                "ts": __import__("datetime").datetime.now().isoformat(),
            })
            results["sse"] = {"ok": True}
        except Exception as exc:
            results["sse"] = {"ok": False, "error": str(exc)}
        if channel == "sse" or (channel == "auto" and not any(r.get("ok") for r in results.values())):
            return {"ok": results.get("sse", {}).get("ok", False), "channels": results}

    any_ok = any(r.get("ok") for r in results.values())
    return {"ok": any_ok, "channels": results}


# ---------------------------------------------------------------------------
# Desktop (macOS)
# ---------------------------------------------------------------------------


def _notify_desktop(title: str, body: str) -> dict[str, Any]:
    if sys.platform != "darwin":
        return {"ok": False, "error": "desktop notifications only supported on macOS"}
    try:
        script = f'display notification "{_escape(body)}" with title "{_escape(title)}"'
        subprocess.run(["osascript", "-e", script], capture_output=True, timeout=10)
        return {"ok": True}
    except Exception as exc:
        logger.warning("Desktop notification failed: %s", exc)
        return {"ok": False, "error": str(exc)}


def _escape(text: str) -> str:
    """Escape text for AppleScript."""
    return text.replace('"', '\\"').replace("\n", " \\n")


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
