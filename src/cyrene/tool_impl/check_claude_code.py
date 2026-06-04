"""Tool implementation for CheckClaudeCode."""

from __future__ import annotations

from typing import Any

from cyrene import tool_legacy as _legacy
from cyrene.tool_legacy import (
    _CC_PROJECT_DIR,
    json,
)

TOOL_NAME = 'CheckClaudeCode'
TOOL_DEF = next(td for td in _legacy.TOOL_DEFS if td["function"]["name"] == TOOL_NAME)


async def _tool_cc_status(_args: dict[str, Any], _bot: Any, _chat_id: int, _db_path: str, _notify_state: dict[str, bool] | None) -> str:
    from cyrene.cc_bridge import get_cc_status
    return json.dumps(get_cc_status(_CC_PROJECT_DIR), ensure_ascii=False)


handler = _tool_cc_status

__all__ = ["TOOL_NAME", "TOOL_DEF", "handler", "_tool_cc_status"]
