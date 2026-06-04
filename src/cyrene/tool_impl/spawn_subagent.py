"""Tool implementation for spawn_subagent."""

from __future__ import annotations

from typing import Any

from cyrene import tool_legacy as _legacy
from cyrene.tool_legacy import (
    _reg_subagent,
    _run_subagent,
    _spawn_subagent_task,
)

TOOL_NAME = 'spawn_subagent'
TOOL_DEF = next(td for td in _legacy.TOOL_DEFS if td["function"]["name"] == TOOL_NAME)


async def _tool_spawn_subagent(args: dict[str, Any], bot: Any, chat_id: int, db_path: str, _notify_state: dict[str, bool] | None) -> str:
    """Spawn a sub-agent to handle a specific task."""
    agent_id = str(args.get("agent_id", ""))
    task = str(args.get("task", ""))
    use_secondary = bool(args.get("use_secondary", False))
    role = str(args.get("role", ""))
    if role and role not in ("moderator", "participant"):
        role = ""
    if not agent_id or not task:
        return "Error: agent_id and task are required."
    from cyrene.agent.state import _current_agent_id, _current_round_id
    if _current_agent_id.get() != "main":
        return "Only the main agent can spawn subagents."
    await _reg_subagent(agent_id, task, round_id=_current_round_id.get(), role=role)
    _spawn_subagent_task(_run_subagent(agent_id, task, bot, chat_id, db_path, use_secondary=use_secondary, role=role), agent_id)
    suffix = " (secondary model)" if use_secondary else ""
    role_suffix = f" [role={role}]" if role else ""
    return f"Sub-agent '{agent_id}' spawned{suffix}{role_suffix}. Task: {task[:80]}"


handler = _tool_spawn_subagent

__all__ = ["TOOL_NAME", "TOOL_DEF", "handler", "_tool_spawn_subagent"]
