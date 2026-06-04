"""Tool implementation for browser_navigate."""

from __future__ import annotations

from typing import Any

from cyrene import tool_legacy as _legacy

TOOL_NAME = 'browser_navigate'
TOOL_DEF = next(td for td in _legacy.TOOL_DEFS if td["function"]["name"] == TOOL_NAME)


async def _tool_browser_navigate(args: dict[str, Any], _bot: Any, _chat_id: int, _db_path: str, _notify_state: dict[str, bool] | None) -> str:
    from cyrene.browser import navigate
    url = str(args.get("url") or "").strip()
    if not url:
        return "No URL provided."
    result = await navigate(url, extract_text=True)
    parts = [f"Title: {result.get('title', '—')}", f"URL: {result.get('url', url)}"]
    if result.get("text"):
        parts.append(result["text"])
    if result.get("error"):
        parts.append(f"Error: {result['error']}")
    return "\n\n".join(parts)


handler = _tool_browser_navigate

__all__ = ["TOOL_NAME", "TOOL_DEF", "handler", "_tool_browser_navigate"]
