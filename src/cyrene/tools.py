"""Compatibility facade for Cyrene tools.

Tool implementation modules now live behind ``cyrene.registry_tools`` and
``cyrene.tool_impl``. This module keeps the historical ``cyrene.tools`` API
stable for callers, tests, and dynamic tool registration.
"""

from __future__ import annotations

import sys
import types
from importlib import import_module

from cyrene import tool_legacy as _legacy

for _name, _value in vars(_legacy).items():
    if not _name.startswith("__"):
        globals()[_name] = _value

from cyrene.registry_tools import (  # noqa: F401
    AGENT_TOOL_GROUPS,
    TOOL_DEFS,
    TOOL_HANDLERS,
    get_active_tool_defs,
    get_active_tool_defs_for_actor,
    get_tool_names,
    is_tool_allowed_for_actor,
    register_tool,
    register_tools,
)
from cyrene.tool_executor import _execute_tool  # noqa: F401

_TOOL_IMPL_MODULES = {
    _handler.__module__: import_module(_handler.__module__)
    for _handler in TOOL_HANDLERS.values()
    if str(getattr(_handler, "__module__", "")).startswith("cyrene.tool_impl.")
}

for _handler in TOOL_HANDLERS.values():
    _handler_name = getattr(_handler, "__name__", "")
    if _handler_name.startswith("_tool_"):
        globals()[_handler_name] = _handler


class _ToolsFacade(types.ModuleType):
    """Forward monkeypatches of legacy globals to the legacy implementation.

    Several tests and integrations patch attributes like ``cyrene.tools.db`` or
    ``cyrene.tools.datetime``. The handler functions are defined in
    ``cyrene.tool_legacy``, so those assignments must update the legacy module
    too for old behavior to remain intact.
    """

    def __setattr__(self, name, value):  # type: ignore[override]
        super().__setattr__(name, value)
        if hasattr(_legacy, name):
            setattr(_legacy, name, value)
        for module in _TOOL_IMPL_MODULES.values():
            if hasattr(module, name):
                setattr(module, name, value)


sys.modules[__name__].__class__ = _ToolsFacade
