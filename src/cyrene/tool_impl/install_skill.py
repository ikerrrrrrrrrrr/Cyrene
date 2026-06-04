"""Tool implementation for InstallSkill."""

from __future__ import annotations

from typing import Any

from cyrene import tool_legacy as _legacy
from cyrene.tool_legacy import (
    _install_skill,
    _resolve_tool_path,
    json,
)

TOOL_NAME = 'InstallSkill'
TOOL_DEF = next(td for td in _legacy.TOOL_DEFS if td["function"]["name"] == TOOL_NAME)


async def _tool_install_skill(args: dict[str, Any], _bot: Any, _chat_id: int, _db_path: str, _notify_state: dict[str, bool] | None) -> str:
    path_str = str(args.get("path", "")).strip()
    if not path_str:
        return json.dumps({"ok": False, "error": "path is required"}, ensure_ascii=False)
    try:
        source = _resolve_tool_path(path_str)
    except ValueError:
        return json.dumps({"ok": False, "error": "skill source must be within workspace"}, ensure_ascii=False)
    source = source.resolve()
    if not source.exists():
        return json.dumps({"ok": False, "error": f"path does not exist: {source}"}, ensure_ascii=False)
    result = _install_skill(source)
    if result.get("ok"):
        skill = result.get("skill", {})
        summary = {
            "ok": True,
            "skill": {
                "id": skill.get("id"),
                "name": skill.get("name"),
                "desc": skill.get("desc"),
                "enabled": skill.get("enabled", True),
                "files": len(skill.get("files", [])),
            },
        }
        if result.get("already_installed"):
            summary["already_installed"] = True
        return json.dumps(summary, ensure_ascii=False)
    return json.dumps({"ok": False, "error": result.get("error", "unknown error")}, ensure_ascii=False)


handler = _tool_install_skill

__all__ = ["TOOL_NAME", "TOOL_DEF", "handler", "_tool_install_skill"]
