"""Runtime settings store — persists user preferences that can be changed via Web UI.

These are NOT env vars (which require restart) — they are live-editable settings
stored in a JSON file under DATA_DIR.
"""

import copy
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
    "send_message": True,
    "send_file": True,
    "ask_user": True,
    "PromptClaudeCode": True,
    "send_telegram": False,
    "query_round": True,
    "CheckClaudeCode": True,
    "StartClaudeCode": True,
}

# quit is always enabled — never stored, never filtered
_PROTECTED_TOOLS = {"quit"}

_DEFAULTS: dict = {
    "search_mode": "builtin",
    "search_external_url": "",
    "spawn_policy": "conservative",
    "write_permission_mode": "workspace_only",
    "models": _DEFAULT_MODELS,
    "enabled_tools": _DEFAULT_ENABLED_TOOLS,
}


def _load() -> dict:
    if not _SETTINGS_PATH.exists():
        return copy.deepcopy(_DEFAULTS)
    try:
        data = json.loads(_SETTINGS_PATH.read_text(encoding="utf-8"))
    except Exception:
        logger.warning("Corrupted web_settings.json, using defaults")
        return copy.deepcopy(_DEFAULTS)
    merged = copy.deepcopy(_DEFAULTS)
    merged.update(data)
    return merged


def _save(data: dict) -> None:
    # Atomic write: write to temp then rename, so a crash mid-write never corrupts the file
    tmp = _SETTINGS_PATH.with_suffix(".json.tmp")
    try:
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(_SETTINGS_PATH)
    finally:
        # Clean up orphaned temp file if the rename itself crashed
        try:
            if tmp.exists():
                tmp.unlink()
        except Exception:
            pass


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


def reset_all() -> None:
    """Delete the persisted settings file so defaults apply again."""
    try:
        if _SETTINGS_PATH.exists():
            _SETTINGS_PATH.unlink()
    except Exception:
        logger.exception("Failed to reset web settings")


def get_spawn_policy() -> str:
    """Return subagent spawn policy normalized to a supported value."""
    value = str(_load().get("spawn_policy", "conservative") or "conservative").strip().lower()
    return value if value in {"aggressive", "conservative", "off"} else "conservative"


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


# ---------------------------------------------------------------------------
# Workspace history helpers
# ---------------------------------------------------------------------------

def get_workspace_history() -> list[str]:
    """Return list of previously used workspace directories."""
    return _load().get("workspace_history", [])


def add_workspace_to_history(path: str) -> None:
    """Record a workspace directory in history (most recent first, max 10)."""
    history = [p for p in get_workspace_history() if p != path]
    history.insert(0, path)
    if len(history) > 10:
        history = history[:10]
    set_("workspace_history", history)


def is_workspace_active() -> bool:
    """Check if workspace access is granted (filesystem tools allowed to execute)."""
    return _load().get("workspace_active", True)


def set_workspace_active(active: bool) -> None:
    """Grant or revoke workspace file access."""
    set_("workspace_active", active)


def get_write_permission_mode() -> str:
    value = str(_load().get("write_permission_mode", "workspace_only") or "workspace_only").strip().lower()
    return value if value in {"workspace_only", "full_access"} else "workspace_only"


def set_write_permission_mode(mode: str) -> None:
    normalized = str(mode or "workspace_only").strip().lower()
    if normalized not in {"workspace_only", "full_access"}:
        normalized = "workspace_only"
    set_("write_permission_mode", normalized)


def is_soul_active() -> bool:
    """Check if SOUL.md should be loaded into the agent context."""
    return _load().get("soul_active", True)


def set_soul_active(active: bool) -> None:
    """Enable or disable SOUL.md loading without touching the file content."""
    set_("soul_active", active)
