"""Tool implementation for UninstallSkill."""

from __future__ import annotations

from typing import Any

from cyrene import tool_legacy as _legacy
from cyrene.tool_legacy import (
    _build_skills,
    _uninstall_skill,
    json,
)

TOOL_NAME = 'UninstallSkill'
TOOL_DEF = next(td for td in _legacy.TOOL_DEFS if td["function"]["name"] == TOOL_NAME)


async def _tool_uninstall_skill(args: dict[str, Any], _bot: Any, _chat_id: int, _db_path: str, _notify_state: dict[str, bool] | None) -> str:
    skill_id = str(args.get("skill_id", "")).strip()
    if not skill_id:
        return json.dumps({"ok": False, "error": "skill_id is required"}, ensure_ascii=False)
    skills = _build_skills()
    match = None
    for s in skills:
        if s.get("id") == skill_id or s.get("name", "").lower() == skill_id.lower():
            match = s
            break
    if not match:
        return json.dumps({"ok": False, "error": f"skill not found: {skill_id}"}, ensure_ascii=False)
    removed = _uninstall_skill(match["id"])
    return json.dumps({"ok": removed, "skill_id": match["id"], "name": match.get("name")}, ensure_ascii=False)


handler = _tool_uninstall_skill

__all__ = ["TOOL_NAME", "TOOL_DEF", "handler", "_tool_uninstall_skill"]
