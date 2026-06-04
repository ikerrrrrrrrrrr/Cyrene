"""Tool implementation for pause_task."""

from __future__ import annotations

from typing import Any

from cyrene import tool_legacy as _legacy
from cyrene.tool_legacy import (
    db,
)

TOOL_NAME = 'pause_task'
TOOL_DEF = next(td for td in _legacy.TOOL_DEFS if td["function"]["name"] == TOOL_NAME)


async def _tool_pause_task(args: dict[str, Any], _bot: Any, _chat_id: int, db_path: str, _notify_state: dict[str, bool] | None) -> str:
    task_id = str(args["task_id"])
    ok = await db.update_task_status(db_path, task_id, "paused")
    return f"Task {task_id} paused." if ok else f"Task {task_id} not found."


handler = _tool_pause_task

__all__ = ["TOOL_NAME", "TOOL_DEF", "handler", "_tool_pause_task"]
