"""Tool implementation for ListSkills."""

from __future__ import annotations

from typing import Any

from cyrene import tool_legacy as _legacy
from cyrene.tool_legacy import (
    _build_skills,
    json,
)

TOOL_NAME = 'ListSkills'
TOOL_DEF = next(td for td in _legacy.TOOL_DEFS if td["function"]["name"] == TOOL_NAME)


async def _tool_list_skills(_args: dict[str, Any], _bot: Any, _chat_id: int, _db_path: str, _notify_state: dict[str, bool] | None) -> str:
    skills = [
        {
            "id": s.get("id"),
            "name": s.get("name"),
            "desc": s.get("desc", "")[:120],
            "enabled": s.get("enabled", True),
            "files": len(s.get("files", [])),
        }
        for s in _build_skills()
    ]
    return json.dumps({"ok": True, "skills": skills}, ensure_ascii=False)


handler = _tool_list_skills

__all__ = ["TOOL_NAME", "TOOL_DEF", "handler", "_tool_list_skills"]
