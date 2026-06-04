"""Tool implementation for Write."""

from __future__ import annotations

from typing import Any

from cyrene import tool_legacy as _legacy
from cyrene.tool_legacy import (
    _request_write_elevation,
    _resolve_workspace_write_target,
)

TOOL_NAME = 'Write'
TOOL_DEF = next(td for td in _legacy.TOOL_DEFS if td["function"]["name"] == TOOL_NAME)


async def _tool_write(args: dict[str, Any], _bot: Any, _chat_id: int, _db_path: str, _notify_state: dict[str, bool] | None) -> str:
    from cyrene.settings_store import is_workspace_active
    if not is_workspace_active():
        return "Workspace access is disabled. Ask the user to add workspace via '+ add context' in the chat input, or set a workspace directory in Settings."
    try:
        path = _resolve_workspace_write_target(str(args["path"]))
    except ValueError:
        return await _request_write_elevation(tool_name="Write", path_hint=str(args.get("path", "")))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(str(args.get("content", "")), encoding="utf-8")
    return f"Wrote {path}"


handler = _tool_write

__all__ = ["TOOL_NAME", "TOOL_DEF", "handler", "_tool_write"]
