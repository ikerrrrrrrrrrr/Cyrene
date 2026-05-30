"""Code tools package — auto-registers all code-related tools.

Import this package from tools.py to register all code tools:
    from cyrene.code_tools import register_all
    register_all(TOOL_DEFS, TOOL_HANDLERS)
"""

from cyrene.code_tools.analysis import register_to as _register_analysis
from cyrene.code_tools.git_tools import register_to as _register_git


def register_all(tool_defs: list, tool_handlers: dict) -> None:
    """Register all code tools (analysis + git) to the given tool registries."""
    _register_analysis(tool_defs, tool_handlers)
    _register_git(tool_defs, tool_handlers)
