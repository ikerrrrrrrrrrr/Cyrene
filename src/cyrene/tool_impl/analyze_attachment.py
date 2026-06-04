"""Tool implementation for AnalyzeAttachment."""

from __future__ import annotations

from typing import Any

from cyrene import tool_legacy as _legacy
from cyrene.tool_legacy import (
    _json_result,
    _request_read_elevation,
    _resolve_tool_path,
    analyze_attachment,
)

TOOL_NAME = 'AnalyzeAttachment'
TOOL_DEF = next(td for td in _legacy.TOOL_DEFS if td["function"]["name"] == TOOL_NAME)


async def _tool_analyze_attachment(args: dict[str, Any], _bot: Any, _chat_id: int, _db_path: str, _notify_state: dict[str, bool] | None) -> str:
    try:
        path = _resolve_tool_path(str(args["path"]))
    except ValueError:
        return await _request_read_elevation(
            tool_name="AnalyzeAttachment",
            path_hint=str(args.get("path", "")),
            reason="Agent 想要分析此文件内容。",
        )
    prompt = str(args.get("prompt", "") or "")
    force_refresh = bool(args.get("force_refresh", False))
    result = await analyze_attachment(str(path), prompt=prompt, force_refresh=force_refresh)
    return _json_result(result)


handler = _tool_analyze_attachment

__all__ = ["TOOL_NAME", "TOOL_DEF", "handler", "_tool_analyze_attachment"]
