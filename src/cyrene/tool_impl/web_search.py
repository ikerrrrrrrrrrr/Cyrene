"""Tool implementation for WebSearch."""

from __future__ import annotations

from typing import Any

from cyrene import tool_legacy as _legacy
from cyrene.tool_legacy import (
    deep_search,
)

TOOL_NAME = 'WebSearch'
TOOL_DEF = next(td for td in _legacy.TOOL_DEFS if td["function"]["name"] == TOOL_NAME)


async def _tool_websearch(args: dict[str, Any], _bot: Any, _chat_id: int, _db_path: str, _notify_state: dict[str, bool] | None) -> str:
    query = str(args.get("query", ""))
    if not query:
        return "No query provided."
    return await deep_search(query)


handler = _tool_websearch

__all__ = ["TOOL_NAME", "TOOL_DEF", "handler", "_tool_websearch"]
