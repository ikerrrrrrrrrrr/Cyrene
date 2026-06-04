"""Tool implementation for browser_click."""

from __future__ import annotations

from typing import Any

from cyrene import tool_legacy as _legacy

TOOL_NAME = 'browser_click'
TOOL_DEF = next(td for td in _legacy.TOOL_DEFS if td["function"]["name"] == TOOL_NAME)


async def _tool_browser_click(args: dict[str, Any], _bot: Any, _chat_id: int, _db_path: str, _notify_state: dict[str, bool] | None) -> str:
    from cyrene.browser import click
    selector = str(args.get("selector") or "").strip()
    if not selector:
        return "No CSS selector provided."
    result = await click(selector)
    if result.get("ok"):
        return f"Clicked {selector}.\nURL: {result.get('url', '—')}\nTitle: {result.get('title', '—')}"
    return f"Click failed: {result.get('error', 'unknown error')}"


handler = _tool_browser_click

__all__ = ["TOOL_NAME", "TOOL_DEF", "handler", "_tool_browser_click"]
