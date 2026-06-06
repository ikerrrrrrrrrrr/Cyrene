"""Tool implementation for StartShell."""

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
    _resolve_workspace_path,
    _start_shell_session,
    json,
)

TOOL_NAME = 'StartShell'
TOOL_DEF = next(td for td in _legacy.TOOL_DEFS if td["function"]["name"] == TOOL_NAME)


async def _tool_start_shell(args: dict[str, Any], _bot: Any, _chat_id: int, _db_path: str, _notify_state: dict[str, bool] | None) -> str:
    from cyrene.agent.state import _current_round_id

    cwd = str(_resolve_workspace_path(str(args.get("cwd", ".") or ".")))
    from cyrene.agent.state import _temporary_full_access
    command = str(args.get("command", "") or "")
    _full_access = _temporary_full_access.get()
    if command:
        if not _full_access and _is_dangerous_subshell(command):
            return await _request_scope_elevation(
                tool_name="StartShell",
                path_hint="",
                operation="包含命令替换的 Shell 操作",
                reason=f"命令包含 $() 或反引号，其展开路径无法静态验证。\n命令：{command[:240]}",
                permission_kind="subshell_elevation",
                options=["允许执行", "拒绝"],
            )
        try:
            _guard_shell_command_workspace_write(command)
        except ValueError:
            return await _request_write_elevation(tool_name="StartShell", path_hint=cwd, reason=command[:240])
        if not _full_access and _command_is_file_deletion(command):
            delete_result = await _request_delete_confirmation(tool_name="StartShell", command=command)
            status = json.loads(delete_result)
            if str(status.get("status", "")).strip() == "awaiting_user":
                return delete_result
    snap = await _start_shell_session(
        command=command,
        cwd=cwd,
        title=str(args.get("title", "") or ""),
        round_id=_current_round_id.get(),
    )
    return _json_result({
        "shell_id": snap.get("id", ""),
        "status": snap.get("status", ""),
        "cwd": snap.get("cwd", "."),
        "title": snap.get("title", "independent shell"),
    })


handler = _tool_start_shell

__all__ = ["TOOL_NAME", "TOOL_DEF", "handler", "_tool_start_shell"]
