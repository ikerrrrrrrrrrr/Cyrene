"""Tool implementation for schedule_task."""

from __future__ import annotations

from typing import Any

from cyrene import tool_legacy as _legacy
from cyrene.tool_legacy import (
    _request_scope_elevation,
    compute_next_run,
    datetime,
    db,
    json,
    timezone,
)

TOOL_NAME = 'schedule_task'
TOOL_DEF = next(td for td in _legacy.TOOL_DEFS if td["function"]["name"] == TOOL_NAME)


async def _tool_schedule_task(args: dict[str, Any], _bot: Any, chat_id: int, db_path: str, _notify_state: dict[str, bool] | None) -> str:
    stype = str(args["schedule_type"])
    svalue = str(args["schedule_value"])
    now = datetime.now(timezone.utc)
    permission_mode = str(args.get("permission_mode", "workspace_only") or "workspace_only").strip().lower()
    if permission_mode not in ("workspace_only", "full_access"):
        permission_mode = "workspace_only"

    next_run = compute_next_run(stype, svalue, now=now)
    if stype == "once":
        # Persist the normalized UTC time as the stored value too, so a re-read
        # of the task shows exactly when it will fire.
        svalue = next_run

    # 如果任务需要 full_access 权限，先向用户申请（已授权时跳过）
    if permission_mode == "full_access":
        from cyrene.agent.state import _temporary_full_access
        if not _temporary_full_access.get():
            prompt_preview = str(args.get("prompt", ""))[:120]
            elevation_result = await _request_scope_elevation(
                tool_name="schedule_task",
                path_hint="",
                operation="定时任务的外部文件访问权限",
                reason=f"此定时任务可能在执行时需要读写 workspace 之外的文件。\n任务内容：{prompt_preview}",
                permission_kind="task_permission_request",
                options=["仅此任务允许 full_access", "拒绝，保持 workspace_only"],
            )
            status = json.loads(elevation_result)
            if str(status.get("status", "")).strip() == "awaiting_user":
                return elevation_result

    task_id = await db.create_task(db_path, chat_id, str(args["prompt"]), stype, svalue, next_run, permission_mode=permission_mode)
    return f"Task {task_id} scheduled. Next run: {next_run} 权限模式：{permission_mode}"


handler = _tool_schedule_task

__all__ = ["TOOL_NAME", "TOOL_DEF", "handler", "_tool_schedule_task"]
