"""Tool execution dispatch for Cyrene."""

from __future__ import annotations

import time
from typing import Any

from cyrene.registry_tools import TOOL_HANDLERS
from cyrene.secret_redaction import redact_text, redact_value

_BROWSER_TOOL_NAMES = {
    "browser_navigate",
    "browser_screenshot",
    "browser_click",
    "browser_type",
    "browser_request_takeover",
}


async def _execute_tool(name: str, arguments: dict[str, Any], bot: Any, chat_id: int, db_path: str, notify_state: dict[str, bool] | None) -> str:
    if name == "spawn_subagent":
        from cyrene.settings_store import get_spawn_policy
        if get_spawn_policy() == "off":
            return "Subagent spawning is disabled by the current spawn policy (`off`). Stay in single-agent mode unless the user explicitly changes this setting."
    if name in _BROWSER_TOOL_NAMES:
        from cyrene.settings_store import is_tool_enabled
        if not is_tool_enabled(name):
            return "Browser automation tools are disabled in settings. Re-enable browser tools before using this action."
    handler = TOOL_HANDLERS.get(name)
    if handler is None:
        from cyrene import debug as _debug
        from cyrene.agent.state import _caller_type, _current_round_id, _current_session_id
        from cyrene.mcp_manager import get_manager as _get_mcp_mgr

        _t0 = time.monotonic()
        try:
            manager = _get_mcp_mgr()
            result = await manager.execute_tool(name, arguments)
            if _debug.VERBOSE:
                _debug.log_tool_call(_caller_type.get(), name, redact_value(arguments), redact_text(result), (time.monotonic() - _t0) * 1000)
            await _debug.publish_event({
                "type": "tool_call", "caller": _caller_type.get(), "tool": name, "args": redact_value(arguments),
                "result": redact_text(str(result)),
                "round_id": _current_round_id.get(),
            }, session_id=_current_session_id.get())
            from cyrene.pattern import record_action
            await record_action(name, redact_value(arguments), _caller_type.get(), _current_round_id.get(),
                          (time.monotonic() - _t0) * 1000,
                          result=redact_text(result), success=True, error="")
            return result
        except ValueError:
            raise ValueError(f"Unknown tool: {name}")
        except Exception as e:
            from cyrene.pattern import record_action
            await record_action(
                name,
                redact_value(arguments),
                _caller_type.get(),
                _current_round_id.get(),
                (time.monotonic() - _t0) * 1000,
                result=redact_text(f"Tool {name} failed: {e}"),
                success=False,
                error=redact_text(str(e)),
            )
            return f"Tool {name} failed: {e}"

    _t0 = time.monotonic()
    try:
        result = await handler(arguments, bot, chat_id, db_path, notify_state)
    except Exception as e:
        from cyrene import debug
        from cyrene.agent.state import _caller_type, _current_round_id, _current_session_id
        await debug.publish_event({
            "type": "tool_call", "caller": _caller_type.get(), "tool": name, "args": redact_value(arguments),
            "result": redact_text(f"Tool failed: {e}"),
            "round_id": _current_round_id.get(),
        }, session_id=_current_session_id.get())
        from cyrene.pattern import record_action
        await record_action(
            name,
            redact_value(arguments),
            _caller_type.get(),
            _current_round_id.get(),
            (time.monotonic() - _t0) * 1000,
            result=redact_text(f"Tool failed: {e}"),
            success=False,
            error=redact_text(str(e)),
        )
        raise
    from cyrene import debug
    if debug.VERBOSE:
        from cyrene.agent.state import _caller_type
        debug.log_tool_call(_caller_type.get(), name, redact_value(arguments), redact_text(result), (time.monotonic() - _t0) * 1000)
    from cyrene.agent.state import _caller_type, _current_round_id, _current_session_id
    await debug.publish_event({
        "type": "tool_call", "caller": _caller_type.get(), "tool": name, "args": redact_value(arguments),
        "result": redact_text(str(result)),
        "round_id": _current_round_id.get(),
    }, session_id=_current_session_id.get())
    from cyrene.pattern import record_action
    tool_success = not str(result).lower().startswith("tool failed:")
    await record_action(
        name,
        redact_value(arguments),
        _caller_type.get(),
        _current_round_id.get(),
        (time.monotonic() - _t0) * 1000,
        result=redact_text(result),
        success=tool_success,
        error="" if tool_success else redact_text(str(result)),
    )
    return result
