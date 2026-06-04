"""Tool implementation for StartClaudeCode."""

from __future__ import annotations

from typing import Any

from cyrene import tool_legacy as _legacy
from cyrene.tool_legacy import (
    _CC_PROJECT_DIR,
    json,
)

TOOL_NAME = 'StartClaudeCode'
TOOL_DEF = next(td for td in _legacy.TOOL_DEFS if td["function"]["name"] == TOOL_NAME)


async def _tool_cc_launch(args: dict[str, Any], _bot: Any, _chat_id: int, _db_path: str, _notify_state: dict[str, bool] | None) -> str:
    from cyrene.cc_bridge import launch_cc_tmux
    session_name = str(args.get("session_name", "") or "").strip()
    return json.dumps(launch_cc_tmux(cwd=_CC_PROJECT_DIR, session_name=session_name), ensure_ascii=False)


handler = _tool_cc_launch

__all__ = ["TOOL_NAME", "TOOL_DEF", "handler", "_tool_cc_launch"]
