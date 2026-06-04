"""Tool implementation for CloseShell."""

from __future__ import annotations

from typing import Any

from cyrene import tool_legacy as _legacy
from cyrene.tool_legacy import (
    _close_shell_session,
    _json_result,
)

TOOL_NAME = 'CloseShell'
TOOL_DEF = next(td for td in _legacy.TOOL_DEFS if td["function"]["name"] == TOOL_NAME)


async def _tool_close_shell(args: dict[str, Any], _bot: Any, _chat_id: int, _db_path: str, _notify_state: dict[str, bool] | None) -> str:
    snap = await _close_shell_session(str(args.get("shell_id", "")))
    return _json_result({
        "shell_id": snap.get("id", ""),
        "status": snap.get("status", ""),
        "elapsed": snap.get("elapsed", "—"),
    })


handler = _tool_close_shell

__all__ = ["TOOL_NAME", "TOOL_DEF", "handler", "_tool_close_shell"]
