"""Compatibility wrapper for behavior learning and learned skills."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from cyrene import behavior_learning as _behavior

logger = logging.getLogger(__name__)


def record_action(
    tool: str,
    args: dict[str, Any],
    caller: str,
    round_id: str,
    duration_ms: float,
    *,
    result: Any = "",
    success: bool = True,
    error: str = "",
) -> None:
    _behavior.record_action(
        tool,
        args,
        caller,
        round_id,
        duration_ms,
        result=result,
        success=success,
        error=error,
    )


def list_patterns(status: str = "all") -> list[dict[str, Any]]:
    return _behavior.list_patterns(status)


def list_learned_skills() -> list[dict[str, Any]]:
    return _behavior.list_learned_skills()


def get_learned_skill(skill_id: str) -> dict[str, Any] | None:
    return _behavior.get_learned_skill(skill_id)


def list_learned_skill_versions(skill_id: str) -> list[dict[str, Any]]:
    return _behavior.list_learned_skill_versions(skill_id)


def list_learned_skill_patches(skill_id: str, status: str = "all") -> list[dict[str, Any]]:
    return _behavior.list_learned_skill_patches(skill_id, status)


def list_learned_skill_runs(skill_id: str, limit: int = 50) -> list[dict[str, Any]]:
    return _behavior.list_learned_skill_runs(skill_id, limit)


def list_skill_replay_tests(skill_id: str) -> list[dict[str, Any]]:
    return _behavior.list_skill_replay_tests(skill_id)


def vocabulary_snapshot() -> dict[str, Any]:
    return _behavior.vocabulary_snapshot()


def create_vocabulary_label(**kwargs) -> dict[str, Any]:
    return _behavior.create_vocabulary_label(**kwargs)


def create_vocabulary_alias(**kwargs) -> dict[str, Any]:
    return _behavior.create_vocabulary_alias(**kwargs)


def promote_unknown_label(unknown_id: str, *, canonical_label: str = "", alias_label: str = "") -> dict[str, Any]:
    return _behavior.promote_unknown_label(unknown_id, canonical_label=canonical_label, alias_label=alias_label)


def dismiss_unknown_label(unknown_id: str) -> bool:
    return _behavior.dismiss_unknown_label(unknown_id)


def list_scripts(status: str = "all") -> list[dict[str, Any]]:
    return _behavior.list_compat_scripts(status)


def approve_script(script_id: str) -> bool:
    return _behavior.manual_activate_skill(script_id)


def reject_script(script_id: str) -> bool:
    return _behavior.manual_deprecate_skill(script_id)


async def run_script(script_id: str, param_overrides: dict[str, Any] | None = None) -> str:
    return await _behavior.run_learned_skill(script_id, param_overrides)


async def run_skill_replay_tests(skill_id: str) -> dict[str, Any]:
    return await _behavior.run_skill_replay_tests(skill_id)


async def update_learned_skill(skill_id: str, updates: dict[str, Any], *, reason: str = "Manual skill edit.") -> dict[str, Any] | None:
    return await _behavior.update_learned_skill(skill_id, updates, reason=reason)


async def apply_skill_patch(skill_id: str, patch_id: str) -> dict[str, Any]:
    return await _behavior.apply_skill_patch(skill_id, patch_id)


def reject_skill_patch(skill_id: str, patch_id: str) -> bool:
    return _behavior.reject_skill_patch(skill_id, patch_id)


async def rollback_learned_skill(skill_id: str, version: int) -> dict[str, Any]:
    return await _behavior.rollback_learned_skill(skill_id, version)


async def scan_for_session_start() -> dict[str, Any]:
    return await _behavior.scan_for_session_start()


async def scan_for_manual_learn() -> dict[str, Any]:
    return await _behavior.scan_for_manual_learn()


async def rebuild_learning_state(*, reprocess_all_turns: bool = True) -> dict[str, Any]:
    return await _behavior.rebuild_learning_state(reprocess_all_turns=reprocess_all_turns)


async def tick(bot: Any, db_path: str) -> None:
    await _behavior.tick(bot, db_path)


async def init(data_dir: Path, workspace_dir: Path) -> None:
    await _behavior.init(data_dir, workspace_dir)
    register_tools()


async def _tool_list_scripts(
    args: dict[str, Any],
    _bot: Any,
    _chat_id: int,
    _db_path: str,
    _notify_state: dict | None,
) -> str:
    status = str(args.get("status", "all"))
    skills = list_scripts(status)
    if not skills:
        return "No learned skills found."
    lines = []
    for skill in skills:
        lines.append(
            f"- [{skill['status']}] {skill['id']} {skill['name']} ({skill.get('type', '')}) "
            f"score>={skill.get('confidence', 0)}"
        )
    return "\n".join(lines)


async def _tool_run_script(
    args: dict[str, Any],
    _bot: Any,
    _chat_id: int,
    _db_path: str,
    _notify_state: dict | None,
) -> str:
    script_id = str(args.get("script_id", "")).strip()
    params = args.get("params")
    return await run_script(script_id, params if isinstance(params, dict) else None)


async def _tool_approve_script(
    args: dict[str, Any],
    _bot: Any,
    _chat_id: int,
    _db_path: str,
    _notify_state: dict | None,
) -> str:
    script_id = str(args.get("script_id", "")).strip()
    return f"Activated '{script_id}'." if approve_script(script_id) else f"Skill '{script_id}' not found."


async def _tool_reject_script(
    args: dict[str, Any],
    _bot: Any,
    _chat_id: int,
    _db_path: str,
    _notify_state: dict | None,
) -> str:
    script_id = str(args.get("script_id", "")).strip()
    return f"Deprecated '{script_id}'." if reject_script(script_id) else f"Skill '{script_id}' not found."


async def _tool_learn_patterns(
    _args: dict[str, Any],
    _bot: Any,
    _chat_id: int,
    _db_path: str,
    _notify_state: dict | None,
) -> str:
    stats = await scan_for_manual_learn()
    return (
        "Behavior learning completed. "
        f"processed={int(stats.get('processed_turns') or 0)} "
        f"merged={int(stats.get('merged_patterns') or 0)} "
        f"new_patterns={int(stats.get('new_patterns') or 0)} "
        f"skills_created={int(stats.get('skills_created') or 0)}"
    )


_PATTERN_TOOL_DEFS = [
    {
        "type": "function",
        "function": {
            "name": "ListScripts",
            "description": "List learned skills generated from behavior patterns.",
            "parameters": {
                "type": "object",
                "properties": {
                    "status": {
                        "type": "string",
                        "enum": ["draft", "shadow", "active", "refined", "deprecated", "all"],
                    },
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "RunScript",
            "description": "Run a learned skill by id with optional parameter overrides.",
            "parameters": {
                "type": "object",
                "properties": {
                    "script_id": {"type": "string"},
                    "params": {"type": "object"},
                },
                "required": ["script_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "ApproveScript",
            "description": "Manually activate a learned skill.",
            "parameters": {
                "type": "object",
                "properties": {"script_id": {"type": "string"}},
                "required": ["script_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "RejectScript",
            "description": "Manually deprecate a learned skill.",
            "parameters": {
                "type": "object",
                "properties": {"script_id": {"type": "string"}},
                "required": ["script_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "LearnPatterns",
            "description": "Process unlearned behavior turns and update behavior patterns plus learned skills immediately.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
]

_PATTERN_HANDLERS = {
    "ListScripts": _tool_list_scripts,
    "RunScript": _tool_run_script,
    "ApproveScript": _tool_approve_script,
    "RejectScript": _tool_reject_script,
    "LearnPatterns": _tool_learn_patterns,
}


def register_tools() -> bool:
    try:
        from cyrene.tools import TOOL_DEFS, TOOL_HANDLERS
    except ImportError:
        logger.debug("tools module not available, skipping behavior-learning tool registration")
        return False
    existing = {td["function"]["name"] for td in TOOL_DEFS}
    for td in _PATTERN_TOOL_DEFS:
        if td["function"]["name"] not in existing:
            TOOL_DEFS.append(td)
    for name, handler in _PATTERN_HANDLERS.items():
        if name not in TOOL_HANDLERS:
            TOOL_HANDLERS[name] = handler
    return True
