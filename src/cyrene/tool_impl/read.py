"""Tool implementation for Read."""

from __future__ import annotations

from typing import Any

from cyrene import tool_legacy as _legacy
from cyrene.tool_legacy import (
    _request_read_elevation,
    _resolve_tool_path,
    _truncate,
)

TOOL_NAME = 'Read'
TOOL_DEF = next(td for td in _legacy.TOOL_DEFS if td["function"]["name"] == TOOL_NAME)


async def _tool_read(args: dict[str, Any], _bot: Any, _chat_id: int, _db_path: str, _notify_state: dict[str, bool] | None) -> str:
    from cyrene.settings_store import is_workspace_active
    if not is_workspace_active():
        return "Workspace access is disabled. Ask the user to add workspace via '+ add context' in the chat input, or set a workspace directory in Settings."
    try:
        path = _resolve_tool_path(str(args["path"]))
    except ValueError:
        elev = await _request_read_elevation(
            tool_name="Read",
            path_hint=str(args.get("path", "")),
            reason=f"Agent 想要读取此文件。",
        )
        if elev is not None:
            return elev
        # 已放行（完全访问 / 审核 agent 批准）：full-access 已置位，重新解析即成功
        path = _resolve_tool_path(str(args["path"]))
    return _truncate(path.read_text(encoding="utf-8"))


handler = _tool_read

__all__ = ["TOOL_NAME", "TOOL_DEF", "handler", "_tool_read"]
