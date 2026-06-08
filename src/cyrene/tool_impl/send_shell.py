"""Tool implementation for SendShell."""

from __future__ import annotations

from typing import Any

from cyrene import tool_legacy as _legacy
from cyrene.tool_legacy import (
    _command_is_file_deletion,
    _guard_shell_command_workspace_write,
    _is_dangerous_subshell,
    _json_result,
    _request_delete_confirmation,
    _request_scope_elevation,
    _request_write_elevation,
    _send_shell_session,
    json,
)

TOOL_NAME = 'SendShell'
TOOL_DEF = next(td for td in _legacy.TOOL_DEFS if td["function"]["name"] == TOOL_NAME)


async def _tool_send_shell(args: dict[str, Any], _bot: Any, _chat_id: int, _db_path: str, _notify_state: dict[str, bool] | None) -> str:
    from cyrene.agent.state import _temporary_full_access
    command = str(args.get("command", ""))
    _full_access = _temporary_full_access.get()
    if not _full_access and _is_dangerous_subshell(command):
        elev = await _request_scope_elevation(
            tool_name="SendShell",
            path_hint="",
            operation="包含命令替换的 Shell 操作",
            reason=f"命令包含 $() 或反引号，其展开路径无法静态验证。\n命令：{command[:240]}",
            permission_kind="subshell_elevation",
            options=["允许执行", "拒绝"],
            scope_hint="",
        )
        if elev is not None:
            return elev
    try:
        _guard_shell_command_workspace_write(command)
    except ValueError:
        elev = await _request_write_elevation(tool_name="SendShell", path_hint="", reason=command[:240])
        if elev is not None:
            return elev
    if _command_is_file_deletion(command) and not _temporary_full_access.get():
        delete_result = await _request_delete_confirmation(tool_name="SendShell", command=command)
        if delete_result is not None:
            return delete_result
    snap = await _send_shell_session(
        str(args.get("shell_id", "")),
        command,
        wait_ms=int(args.get("wait_ms", 700) or 700),
    )
    return _json_result({
        "shell_id": snap.get("id", ""),
        "status": snap.get("status", ""),
        "elapsed": snap.get("elapsed", "—"),
        "lines": snap.get("lines", [])[-20:],
    })


handler = _tool_send_shell

__all__ = ["TOOL_NAME", "TOOL_DEF", "handler", "_tool_send_shell"]
