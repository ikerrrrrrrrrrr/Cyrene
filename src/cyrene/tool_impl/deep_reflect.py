"""Tool implementation for DeepReflect."""

from __future__ import annotations

from typing import Any

from cyrene import tool_legacy as _legacy

TOOL_NAME = 'DeepReflect'
TOOL_DEF = next(td for td in _legacy.TOOL_DEFS if td["function"]["name"] == TOOL_NAME)


async def _tool_deep_reflect(args: dict[str, Any], _bot: Any, _chat_id: int, _db_path: str, _notify_state: dict[str, bool] | None) -> str:
    return (
        "DeepReflect is handled inside the main chat loop so it can access the live visible transcript. "
        "If you see this fallback, continue without changing persisted history."
    )


handler = _tool_deep_reflect

__all__ = ["TOOL_NAME", "TOOL_DEF", "handler", "_tool_deep_reflect"]
