"""Tool implementation for browser_screenshot."""

from __future__ import annotations

import os
from typing import Any

from cyrene import tool_legacy as _legacy

TOOL_NAME = 'browser_screenshot'
TOOL_DEF = next(td for td in _legacy.TOOL_DEFS if td["function"]["name"] == TOOL_NAME)


async def _tool_browser_screenshot(args: dict[str, Any], _bot: Any, _chat_id: int, _db_path: str, _notify_state: dict[str, bool] | None) -> str:
    from cyrene.browser import screenshot
    url = str(args.get("url") or "").strip()
    if not url:
        return "No URL provided."
    result = await screenshot(url)
    if result.get("ok"):
        try:
            os.unlink(result["path"])
        except OSError:
            pass
        return f"Screenshot taken.\nTitle: {result.get('title', '—')}"
    return f"Screenshot failed: {result.get('error', 'unknown error')}"


handler = _tool_browser_screenshot

__all__ = ["TOOL_NAME", "TOOL_DEF", "handler", "_tool_browser_screenshot"]
