"""Tool implementation for list_tasks."""

from __future__ import annotations

from typing import Any

from cyrene import tool_legacy as _legacy
from cyrene.tool_legacy import (
    db,
)

TOOL_NAME = 'list_tasks'
TOOL_DEF = next(td for td in _legacy.TOOL_DEFS if td["function"]["name"] == TOOL_NAME)


async def _tool_list_tasks(_args: dict[str, Any], _bot: Any, _chat_id: int, db_path: str, _notify_state: dict[str, bool] | None) -> str:
    tasks = await db.get_all_tasks(db_path)
    if not tasks:
        return "No scheduled tasks."
    lines = []
    for t in tasks:
        perm = str(t.get("permission_mode") or "workspace_only")
        tag = " 🔓" if perm == "full_access" else ""
        lines.append(f"- [{t['id']}]{tag} {t['status']} | {t['schedule_type']}({t['schedule_value']}) | {t['prompt'][:60]}")
    return "\n".join(lines)


handler = _tool_list_tasks

__all__ = ["TOOL_NAME", "TOOL_DEF", "handler", "_tool_list_tasks"]
