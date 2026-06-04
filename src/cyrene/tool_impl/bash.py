"""Tool implementation for Bash."""

from __future__ import annotations

from typing import Any

from cyrene import tool_legacy as _legacy
from cyrene.tool_legacy import (
    WORKSPACE_DIR,
    _command_is_file_deletion,
    _guard_shell_command_workspace_write,
    _is_dangerous_subshell,
    _json_result,
    _request_delete_confirmation,
    _request_scope_elevation,
    _request_write_elevation,
    _truncate,
    asyncio,
    json,
    os,
)

TOOL_NAME = 'Bash'
TOOL_DEF = next(td for td in _legacy.TOOL_DEFS if td["function"]["name"] == TOOL_NAME)


async def _tool_bash(args: dict[str, Any], _bot: Any, _chat_id: int, _db_path: str, _notify_state: dict[str, bool] | None) -> str:
    command = str(args["command"])
    # 命令替换无法提前验证路径，先拦截并询问用户
    if _is_dangerous_subshell(command):
        return await _request_scope_elevation(
            tool_name="Bash",
            path_hint="",
            operation="包含命令替换的 Shell 操作",
            reason=f"命令包含 $() 或反引号，其展开路径无法静态验证。\n命令：{command[:240]}",
            permission_kind="subshell_elevation",
            options=["允许执行", "拒绝"],
        )
    try:
        _guard_shell_command_workspace_write(command)
    except ValueError:
        return await _request_write_elevation(tool_name="Bash", path_hint="", reason=command[:240])
    # 即使是 workspace 内的文件删除操作，也需要用户确认
    if _command_is_file_deletion(command):
        delete_result = await _request_delete_confirmation(tool_name="Bash", command=command)
        status = json.loads(delete_result)
        if str(status.get("status", "")).strip() == "awaiting_user":
            return delete_result
    timeout_ms = int(args.get("timeout_ms", 120000))
    timeout_sec = timeout_ms / 1000
    shell = os.environ.get("SHELL") or "/bin/sh"
    proc = await asyncio.create_subprocess_exec(
        shell,
        "-lc",
        command,
        cwd=str(WORKSPACE_DIR),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )

    from cyrene.agent.state import _interrupt_event

    stdout_chunks: list[bytes] = []
    stderr_chunks: list[bytes] = []

    async def _read(stream: asyncio.StreamReader | None, chunks: list[bytes]) -> None:
        if stream is None:
            return
        while True:
            chunk = await stream.read(8192)
            if not chunk:
                break
            chunks.append(chunk)

    reads = asyncio.gather(_read(proc.stdout, stdout_chunks), _read(proc.stderr, stderr_chunks))
    import time as _time
    deadline = _time.monotonic() + timeout_sec

    try:
        while True:
            if reads.done():
                break
            remaining = deadline - _time.monotonic()
            if remaining <= 0:
                proc.kill()
                reads.cancel()
                try:
                    await reads
                except (asyncio.CancelledError, Exception):
                    pass
                raise ValueError(f"Command timed out after {timeout_ms} ms")
            if _interrupt_event.is_set():
                proc.kill()
                reads.cancel()
                try:
                    await reads
                except (asyncio.CancelledError, Exception):
                    pass
                payload = {
                    "exit_code": -1,
                    "stdout": _truncate(b"".join(stdout_chunks).decode("utf-8", errors="replace")),
                    "stderr": "Command interrupted by new user message.",
                }
                return _json_result(payload)
            try:
                await asyncio.wait_for(asyncio.shield(reads), timeout=min(1, remaining))
            except asyncio.TimeoutError:
                pass

        await proc.wait()
    except ValueError:
        raise
    except Exception:
        proc.kill()
        raise

    payload = {
        "exit_code": proc.returncode,
        "stdout": _truncate(b"".join(stdout_chunks).decode("utf-8", errors="replace")),
        "stderr": _truncate(b"".join(stderr_chunks).decode("utf-8", errors="replace")),
    }
    return _json_result(payload)


handler = _tool_bash

__all__ = ["TOOL_NAME", "TOOL_DEF", "handler", "_tool_bash"]
