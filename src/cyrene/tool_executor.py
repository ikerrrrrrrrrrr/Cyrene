"""Tool execution dispatch for Cyrene."""

from __future__ import annotations

import time
from typing import Any

from cyrene.registry_tools import TOOL_HANDLERS


async def _execute_tool(name: str, arguments: dict[str, Any], bot: Any, chat_id: int, db_path: str, notify_state: dict[str, bool] | None) -> str:
    if name == "spawn_subagent":
        from cyrene.settings_store import get_spawn_policy
        if get_spawn_policy() == "off":
            return "Subagent spawning is disabled by the current spawn policy (`off`). Stay in single-agent mode unless the user explicitly changes this setting."
    handler = TOOL_HANDLERS.get(name)
    if handler is None:
        from cyrene import debug as _debug
        from cyrene.agent.state import _caller_type, _current_round_id
        from cyrene.mcp_manager import get_manager as _get_mcp_mgr

        _t0 = time.monotonic()
        try:
            manager = _get_mcp_mgr()
            result = await manager.execute_tool(name, arguments)
            if _debug.VERBOSE:
                _debug.log_tool_call(_caller_type.get(), name, arguments, result, (time.monotonic() - _t0) * 1000)
            await _debug.publish_event({
                "type": "tool_call", "caller": _caller_type.get(), "tool": name, "args": arguments,
                "result": str(result),
                "round_id": _current_round_id.get(),
            })
            from cyrene.pattern import record_action
            await record_action(name, arguments, _caller_type.get(), _current_round_id.get(),
                          (time.monotonic() - _t0) * 1000,
                          result=result, success=True, error="")
            return result
        except ValueError:
            raise ValueError(f"Unknown tool: {name}")
        except Exception as e:
            from cyrene.pattern import record_action
            await record_action(
                name,
                arguments,
                _caller_type.get(),
                _current_round_id.get(),
                (time.monotonic() - _t0) * 1000,
                result=f"Tool {name} failed: {e}",
                success=False,
                error=str(e),
            )
            return f"Tool {name} failed: {e}"

    _t0 = time.monotonic()
    try:
        result = await handler(arguments, bot, chat_id, db_path, notify_state)
    except Exception as e:
        from cyrene import debug
        from cyrene.agent.state import _caller_type, _current_round_id
        await debug.publish_event({
            "type": "tool_call", "caller": _caller_type.get(), "tool": name, "args": arguments,
            "result": f"Tool failed: {e}",
            "round_id": _current_round_id.get(),
        })
        from cyrene.pattern import record_action
        await record_action(
            name,
            arguments,
            _caller_type.get(),
            _current_round_id.get(),
            (time.monotonic() - _t0) * 1000,
            result=f"Tool failed: {e}",
            success=False,
            error=str(e),
        )
        raise
    from cyrene import debug
    if debug.VERBOSE:
        from cyrene.agent.state import _caller_type
        debug.log_tool_call(_caller_type.get(), name, arguments, result, (time.monotonic() - _t0) * 1000)
    from cyrene.agent.state import _caller_type, _current_round_id
    await debug.publish_event({
        "type": "tool_call", "caller": _caller_type.get(), "tool": name, "args": arguments,
        "result": str(result),
        "round_id": _current_round_id.get(),
    })
    from cyrene.pattern import record_action
    tool_success = not str(result).lower().startswith("tool failed:")
    await record_action(
        name,
        arguments,
        _caller_type.get(),
        _current_round_id.get(),
        (time.monotonic() - _t0) * 1000,
        result=result,
        success=tool_success,
        error="" if tool_success else str(result),
    )
    return result
