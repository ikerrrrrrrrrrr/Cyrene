"""Tool implementation for WebFetch."""

from __future__ import annotations

from typing import Any

from cyrene import tool_legacy as _legacy
from cyrene.tool_legacy import (
    _truncate,
    httpx,
)

TOOL_NAME = 'WebFetch'
TOOL_DEF = next(td for td in _legacy.TOOL_DEFS if td["function"]["name"] == TOOL_NAME)


async def _tool_webfetch(args: dict[str, Any], _bot: Any, _chat_id: int, _db_path: str, _notify_state: dict[str, bool] | None) -> str:
    url = str(args["url"])
    async with httpx.AsyncClient(follow_redirects=True, timeout=30.0) as client:
        response = await client.get(url)
        response.raise_for_status()
    return _truncate(response.text)


handler = _tool_webfetch

__all__ = ["TOOL_NAME", "TOOL_DEF", "handler", "_tool_webfetch"]
