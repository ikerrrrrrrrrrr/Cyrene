"""Tool implementation for send_agent_message."""

from __future__ import annotations

from typing import Any

from cyrene import tool_legacy as _legacy
from cyrene.tool_legacy import (
    _send_inbox,
    can_receive,
    datetime,
    timezone,
)

TOOL_NAME = 'send_agent_message'
TOOL_DEF = next(td for td in _legacy.TOOL_DEFS if td["function"]["name"] == TOOL_NAME)


async def _tool_send_agent_message(args: dict[str, Any], _bot: Any, _chat_id: int, _db_path: str, _notify_state: dict[str, bool] | None) -> str:
    """Send a message to another sub-agent via inbox."""
    target = str(args.get("to", ""))
    content = str(args.get("content", ""))
    if not target or not content:
        return "Error: both 'to' and 'content' are required."
    from cyrene.agent.state import _current_agent_id, _current_round_id
    current_round_id = _current_round_id.get()
    if not await can_receive(target, round_id=current_round_id):
        if target.lower() in {"main", "main_agent", "cyrene", "danny", "host", "coordinator", "parent"}:
            return "The main-agent inbox is reserved for user guidance. Put your final conclusion in your next quit response; the parent agent will collect it automatically."
        if current_round_id:
            return f"Cannot deliver: agent '{target}' is not available in the current round ({current_round_id})."
        return f"Cannot deliver: agent '{target}' is not available (finished or timed out)."
    from_agent = _current_agent_id.get()
    await _send_inbox(from_agent, target, "chat", content, round_id=current_round_id)
    # Publish SSE event for real-time flow diagram updates
    from cyrene import debug as _debug_comm
    await _debug_comm.publish_event({
        "type": "agent_comm",
        "from": from_agent,
        "to": target,
        "content": content,  # full content for group chat
        "summary": content[:100].replace("\n", " ").strip() + ("..." if len(content) > 100 else ""),
        "msg_type": "chat",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "round_id": current_round_id,
    })
    return f"Message sent to {target}."


handler = _tool_send_agent_message

__all__ = ["TOOL_NAME", "TOOL_DEF", "handler", "_tool_send_agent_message"]
