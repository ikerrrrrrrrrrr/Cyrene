"""Runtime settings store — persists user preferences that can be changed via Web UI.

These are NOT env vars (which require restart) — they are live-editable settings
stored in a JSON file under DATA_DIR.
"""

import json
import logging

from cyrene.config import DATA_DIR

logger = logging.getLogger(__name__)

_SETTINGS_PATH = DATA_DIR / "web_settings.json"

_DEFAULT_MODELS = [
    {"id": "deepseek-chat", "name": "deepseek-chat", "desc": "DeepSeek default",
     "ctx": "64k", "price": "low"},
    {"id": "haiku45", "name": "claude-haiku-4-5", "desc": "Fast, capable",
     "ctx": "200k", "price": "$0.25 / $1.25"},
    {"id": "sonnet45", "name": "claude-sonnet-4-5", "desc": "Heavy reasoning",
     "ctx": "200k", "price": "$3.00 / $15.00"},
]

_DEFAULT_ENABLED_TOOLS = {
    "Read": True,
    "Write": True,
    "Edit": True,
    "Glob": True,
    "Grep": True,
    "Bash": True,
    "StartShell": True,
    "SendShell": True,
    "ListShells": True,
    "CloseShell": True,
    "WebFetch": True,
    "WebSearch": True,
    "spawn_subagent": True,
    "send_agent_message": True,
    "schedule_task": True,
    "list_tasks": True,
    "pause_task": True,
    "resume_task": True,
    "cancel_task": True,
    "send_telegram": False,
    "query_round": True,
}

# quit is always enabled — never stored, never filtered
_PROTECTED_TOOLS = {"quit"}

_DEFAULTS: dict = {
    "search_mode": "builtin",
    "search_external_url": "",
    "models": _DEFAULT_MODELS,
    "enabled_tools": _DEFAULT_ENABLED_TOOLS,
}


def _load() -> dict:
    if not _SETTINGS_PATH.exists():
        return dict(_DEFAULTS)
    try:
        data = json.loads(_SETTINGS_PATH.read_text(encoding="utf-8"))
    except Exception:
        logger.warning("Corrupted web_settings.json, using defaults")
        return dict(_DEFAULTS)
    merged = dict(_DEFAULTS)
    merged.update(data)
    return merged


def _save(data: dict) -> None:
    _SETTINGS_PATH.write_text(
        json.dumps(data, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def get(key: str, default=None):
    """Read a single setting value."""
    return _load().get(key, default)


def set_(key: str, value) -> None:
    """Write a single setting value."""
    data = _load()
    data[key] = value
    _save(data)


def get_all() -> dict:
    """Return all settings as a flat dict."""
    return _load()


# ---------------------------------------------------------------------------
# Models helpers
# ---------------------------------------------------------------------------


def get_models() -> list[dict]:
    """Return the user-managed model list."""
    return _load().get("models", _DEFAULT_MODELS)


def save_models(models: list[dict]) -> None:
    """Replace the entire models list."""
    set_("models", models)


# ---------------------------------------------------------------------------
# Tools helpers
# ---------------------------------------------------------------------------


def is_tool_enabled(name: str) -> bool:
    """Check if a tool is enabled. Protected tools (quit) are always enabled."""
    if name in _PROTECTED_TOOLS:
        return True
    return _load().get("enabled_tools", _DEFAULT_ENABLED_TOOLS).get(name, True)


def get_enabled_tools() -> dict[str, bool]:
    """Return the full enabled/disabled map for all tools."""
    return dict(_load().get("enabled_tools", _DEFAULT_ENABLED_TOOLS))


def save_enabled_tools(tools: dict[str, bool]) -> None:
    """Replace the enabled tools map."""
    # Never persist protected tools
    clean = {k: v for k, v in tools.items() if k not in _PROTECTED_TOOLS}
    set_("enabled_tools", clean)
