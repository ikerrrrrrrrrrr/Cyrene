"""Tool implementation for enter_plan_mode.

Lets the main agent self-trigger 计划模式: decompose the current request into
steps → tasks, show it in the right sidebar 计划 tab, and pause for the user's
approve / reject / revise decision. The actual flow lives in
``cyrene.agent.planning.run_plan_flow``.
"""

from __future__ import annotations

from typing import Any

from cyrene import tool_legacy as _legacy

TOOL_NAME = 'enter_plan_mode'
TOOL_DEF = next(td for td in _legacy.TOOL_DEFS if td["function"]["name"] == TOOL_NAME)


async def _tool_enter_plan_mode(args: dict[str, Any], _bot: Any, _chat_id: int, _db_path: str, _notify_state: dict[str, bool] | None) -> str:
    import cyrene.agent.state as _state
    from cyrene.agent.state import _current_agent_id, _current_client_request_id, _current_round_id
    from cyrene.agent.session import _load_session_messages
    from cyrene.agent.planning import run_plan_flow

    if _current_agent_id.get() != "main":
        return "Only the main agent can enter plan mode."
    round_id = str(_current_round_id.get() or "").strip()
    if not round_id:
        return "Cannot enter plan mode outside an active chat round."

    user_message = str(
        _state._active_main_round_public_prompt
        or _state._active_main_round_prompt
        or ""
    ).strip()
    focus = str(args.get("focus", "") or "").strip()
    history = _load_session_messages()

    # 用户消息在本轮开始时已持久化，这里不重复持久化。
    return await run_plan_flow(
        user_message=user_message,
        history=history,
        round_id=round_id,
        client_request_id=str(_current_client_request_id.get() or "").strip(),
        persist_user_message=False,
        modification=focus,
    )


handler = _tool_enter_plan_mode

__all__ = ["TOOL_NAME", "TOOL_DEF", "handler", "_tool_enter_plan_mode"]
