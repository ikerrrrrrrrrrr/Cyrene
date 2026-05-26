"""Behavior-tree learning, pattern mining, and learned skill execution.

This module implements the full behavior-learning pipeline:

- SQLite-backed behavior tree persistence
- behavior fingerprint generation + vocabulary normalization
- cross-session pattern mining and merging
- learned skill generation, versioning, shadow validation, and routing
- skill run logging, replay tests, and patch proposal scaffolding
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import sqlite3
import time

import aiosqlite
from collections import defaultdict
from contextvars import ContextVar
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

from cyrene.config import DB_PATH

logger = logging.getLogger(__name__)

_DATA_DIR: Path | None = None
_WORKSPACE_DIR: Path | None = None
_DB_FILE: Path = DB_PATH
_INIT_DONE = False
_PROCESS_LOCK = asyncio.Lock()

_current_session_id: ContextVar[str] = ContextVar("behavior_session_id", default="")
_current_turn_id: ContextVar[str] = ContextVar("behavior_turn_id", default="")
_current_round_id: ContextVar[str] = ContextVar("behavior_round_id", default="")

_VOCABULARY_VERSION = 1
_SHADOW_SUCCESS_THRESHOLD = 3
_SHADOW_CONSISTENCY_THRESHOLD = 0.85
_ROUTER_AUTO_THRESHOLD = 0.88
_ROUTER_JUDGE_THRESHOLD = 0.75
_PATTERN_STRONG_THRESHOLD = 0.85
_PATTERN_MEDIUM_THRESHOLD = 0.70
_MAX_PATTERN_EXAMPLES = 8

_CREATE_TABLES = """
CREATE TABLE IF NOT EXISTS behavior_sessions (
    session_id TEXT PRIMARY KEY,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    session_summary TEXT NOT NULL DEFAULT '',
    metadata_json TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_behavior_sessions_updated_at ON behavior_sessions(updated_at);

CREATE TABLE IF NOT EXISTS behavior_turns (
    turn_id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL,
    round_id TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    user_message TEXT NOT NULL,
    context_summary TEXT NOT NULL DEFAULT '',
    outcome_status TEXT NOT NULL DEFAULT 'success',
    user_feedback TEXT NOT NULL DEFAULT '',
    processed_status INTEGER NOT NULL DEFAULT 0,
    linked_skill_id TEXT NOT NULL DEFAULT '',
    metadata_json TEXT NOT NULL DEFAULT '{}',
    FOREIGN KEY (session_id) REFERENCES behavior_sessions(session_id)
);
CREATE INDEX IF NOT EXISTS idx_behavior_turns_session_id ON behavior_turns(session_id);
CREATE INDEX IF NOT EXISTS idx_behavior_turns_processed_status ON behavior_turns(processed_status, created_at);
CREATE INDEX IF NOT EXISTS idx_behavior_turns_round_id ON behavior_turns(round_id);

CREATE TABLE IF NOT EXISTS behavior_actions (
    action_id TEXT PRIMARY KEY,
    turn_id TEXT NOT NULL,
    session_id TEXT NOT NULL,
    round_id TEXT NOT NULL,
    created_at TEXT NOT NULL,
    action_index INTEGER NOT NULL,
    action_type TEXT NOT NULL,
    action_subtype TEXT NOT NULL,
    tool_name TEXT NOT NULL,
    input_summary TEXT NOT NULL DEFAULT '',
    output_summary TEXT NOT NULL DEFAULT '',
    success INTEGER NOT NULL DEFAULT 1,
    error_summary TEXT NOT NULL DEFAULT '',
    requires_llm INTEGER NOT NULL DEFAULT 0,
    risk_level TEXT NOT NULL DEFAULT 'none',
    metadata_json TEXT NOT NULL DEFAULT '{}',
    FOREIGN KEY (turn_id) REFERENCES behavior_turns(turn_id),
    FOREIGN KEY (session_id) REFERENCES behavior_sessions(session_id)
);
CREATE INDEX IF NOT EXISTS idx_behavior_actions_turn_id ON behavior_actions(turn_id, action_index);

CREATE TABLE IF NOT EXISTS behavior_fingerprints (
    turn_id TEXT PRIMARY KEY,
    fingerprint_content TEXT NOT NULL,
    vocabulary_version INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    FOREIGN KEY (turn_id) REFERENCES behavior_turns(turn_id)
);

CREATE TABLE IF NOT EXISTS behavior_patterns (
    pattern_id TEXT PRIMARY KEY,
    description TEXT NOT NULL DEFAULT '',
    prototype_fingerprint TEXT NOT NULL DEFAULT '{}',
    statistics_json TEXT NOT NULL DEFAULT '{}',
    skillability_json TEXT NOT NULL DEFAULT '{}',
    status TEXT NOT NULL DEFAULT 'candidate',
    linked_skill_list TEXT NOT NULL DEFAULT '[]',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_behavior_patterns_status ON behavior_patterns(status, updated_at);

CREATE TABLE IF NOT EXISTS behavior_pattern_turns (
    pattern_id TEXT NOT NULL,
    turn_id TEXT NOT NULL,
    similarity REAL NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    PRIMARY KEY (pattern_id, turn_id),
    FOREIGN KEY (pattern_id) REFERENCES behavior_patterns(pattern_id),
    FOREIGN KEY (turn_id) REFERENCES behavior_turns(turn_id)
);
CREATE INDEX IF NOT EXISTS idx_behavior_pattern_turns_turn_id ON behavior_pattern_turns(turn_id);

CREATE TABLE IF NOT EXISTS behavior_vocabulary_labels (
    label_id TEXT PRIMARY KEY,
    label_type TEXT NOT NULL,
    canonical_label TEXT NOT NULL,
    domain TEXT NOT NULL DEFAULT '',
    parent_label TEXT NOT NULL DEFAULT '',
    raw_description TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'active',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_behavior_vocabulary_labels_unique
    ON behavior_vocabulary_labels(label_type, canonical_label);

CREATE TABLE IF NOT EXISTS behavior_vocabulary_aliases (
    alias_id TEXT PRIMARY KEY,
    label_type TEXT NOT NULL,
    canonical_label TEXT NOT NULL,
    alias_label TEXT NOT NULL,
    created_at TEXT NOT NULL,
    vocabulary_version INTEGER NOT NULL DEFAULT 1
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_behavior_vocabulary_aliases_unique
    ON behavior_vocabulary_aliases(label_type, alias_label);

CREATE TABLE IF NOT EXISTS behavior_unknown_labels (
    unknown_id TEXT PRIMARY KEY,
    label_type TEXT NOT NULL,
    raw_description TEXT NOT NULL,
    proposed_domain TEXT NOT NULL DEFAULT '',
    proposed_type TEXT NOT NULL DEFAULT '',
    proposed_subtype TEXT NOT NULL DEFAULT '',
    reason TEXT NOT NULL DEFAULT '',
    seen_count INTEGER NOT NULL DEFAULT 1,
    example_turns TEXT NOT NULL DEFAULT '[]',
    status TEXT NOT NULL DEFAULT 'open',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_behavior_unknown_labels_status
    ON behavior_unknown_labels(status, seen_count DESC, updated_at DESC);

CREATE TABLE IF NOT EXISTS learned_skills (
    skill_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    current_version INTEGER NOT NULL DEFAULT 1,
    status TEXT NOT NULL DEFAULT 'draft',
    skill_type TEXT NOT NULL DEFAULT 'draft',
    risk_level TEXT NOT NULL DEFAULT 'none',
    requires_llm INTEGER NOT NULL DEFAULT 1,
    trigger_json TEXT NOT NULL DEFAULT '{}',
    input_schema_json TEXT NOT NULL DEFAULT '[]',
    parameter_extractor_json TEXT NOT NULL DEFAULT '{}',
    steps_json TEXT NOT NULL DEFAULT '[]',
    guards_json TEXT NOT NULL DEFAULT '{}',
    fallback_policy_json TEXT NOT NULL DEFAULT '{}',
    tests_json TEXT NOT NULL DEFAULT '[]',
    editable_fields_json TEXT NOT NULL DEFAULT '[]',
    created_from_json TEXT NOT NULL DEFAULT '{}',
    run_statistics_json TEXT NOT NULL DEFAULT '{}',
    pattern_id TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_learned_skills_status ON learned_skills(status, updated_at);
CREATE INDEX IF NOT EXISTS idx_learned_skills_pattern_id ON learned_skills(pattern_id);

CREATE TABLE IF NOT EXISTS learned_skill_versions (
    skill_id TEXT NOT NULL,
    version INTEGER NOT NULL,
    parent_version INTEGER,
    skill_definition TEXT NOT NULL,
    change_type TEXT NOT NULL DEFAULT '',
    change_summary TEXT NOT NULL DEFAULT '',
    patch_list TEXT NOT NULL DEFAULT '[]',
    created_at TEXT NOT NULL,
    test_result TEXT NOT NULL DEFAULT '{}',
    rollback_target INTEGER,
    PRIMARY KEY (skill_id, version),
    FOREIGN KEY (skill_id) REFERENCES learned_skills(skill_id)
);

CREATE TABLE IF NOT EXISTS learned_skill_runs (
    run_id TEXT PRIMARY KEY,
    skill_id TEXT NOT NULL,
    version INTEGER NOT NULL,
    turn_id TEXT NOT NULL DEFAULT '',
    match_score REAL NOT NULL DEFAULT 0,
    parameter_status TEXT NOT NULL DEFAULT '',
    execution_status TEXT NOT NULL DEFAULT '',
    failure_reason TEXT NOT NULL DEFAULT '',
    fallback_used INTEGER NOT NULL DEFAULT 0,
    user_feedback TEXT NOT NULL DEFAULT '',
    dry_run INTEGER NOT NULL DEFAULT 0,
    consistency_score REAL NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    FOREIGN KEY (skill_id) REFERENCES learned_skills(skill_id)
);
CREATE INDEX IF NOT EXISTS idx_learned_skill_runs_skill_id
    ON learned_skill_runs(skill_id, created_at DESC);

CREATE TABLE IF NOT EXISTS learned_skill_patches (
    patch_id TEXT PRIMARY KEY,
    skill_id TEXT NOT NULL,
    base_version INTEGER NOT NULL,
    patch_type TEXT NOT NULL,
    reason TEXT NOT NULL,
    patch_content TEXT NOT NULL DEFAULT '{}',
    risk_assessment TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'proposed',
    created_at TEXT NOT NULL,
    FOREIGN KEY (skill_id) REFERENCES learned_skills(skill_id)
);
CREATE INDEX IF NOT EXISTS idx_learned_skill_patches_skill_id
    ON learned_skill_patches(skill_id, created_at DESC);

CREATE TABLE IF NOT EXISTS behavior_replay_tests (
    test_id TEXT PRIMARY KEY,
    skill_id TEXT NOT NULL,
    turn_id TEXT NOT NULL DEFAULT '',
    test_type TEXT NOT NULL,
    input_payload TEXT NOT NULL DEFAULT '{}',
    expected_payload TEXT NOT NULL DEFAULT '{}',
    last_result TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    FOREIGN KEY (skill_id) REFERENCES learned_skills(skill_id)
);
CREATE INDEX IF NOT EXISTS idx_behavior_replay_tests_skill_id
    ON behavior_replay_tests(skill_id, created_at DESC);
"""

_CORE_DOMAINS = {
    "internal_reasoning",
    "local_resource_operation",
    "external_information_query",
    "external_service_operation",
    "content_generation",
    "content_transformation",
    "software_development",
    "system_operation",
    "communication",
    "schedule_management",
    "state_management",
    "user_interaction",
    "unknown",
}

_CORE_TYPES = {
    "observe_context",
    "read_resource",
    "search_resource",
    "query_realtime_info",
    "retrieve_external_knowledge",
    "parse_content",
    "extract_information",
    "compare_items",
    "transform_data",
    "calculate_result",
    "diagnose_problem",
    "plan_steps",
    "generate_content",
    "edit_resource",
    "create_resource",
    "manage_state",
    "manage_schedule",
    "send_communication",
    "operate_external_service",
    "run_command",
    "call_tool",
    "ask_clarification",
    "request_confirmation",
    "return_result",
    "unknown",
}

_STATIC_ALIASES = {
    "domain:software": "software_development",
    "domain:code": "software_development",
    "domain:filesystem": "local_resource_operation",
    "domain:file_system": "local_resource_operation",
    "domain:web": "external_information_query",
    "type:edit_file": "edit_resource",
    "type:write_file": "edit_resource",
    "type:create_file": "create_resource",
    "type:read_file": "read_resource",
    "type:list_files": "search_resource",
    "type:search_files": "search_resource",
    "type:search_web": "query_realtime_info",
    "type:fetch_web_page": "retrieve_external_knowledge",
    "type:run_shell_command": "run_command",
    "type:ask_user": "ask_clarification",
    "type:tool_call": "call_tool",
    "intent_type:search_weather": "query_realtime_info",
    "intent_subtype:search_weather": "weather_lookup",
    "intent_subtype:compare_weather": "weather_lookup",
    "object_type:weather_forecast": "weather_data",
}

_TOOL_ACTION_MAP: dict[str, tuple[str, str, str, int]] = {
    "Read": ("local_resource_operation", "read_resource", "read_file", 0),
    "Write": ("local_resource_operation", "edit_resource", "write_file", 0),
    "Edit": ("local_resource_operation", "edit_resource", "edit_file", 0),
    "Glob": ("local_resource_operation", "search_resource", "list_files", 0),
    "Grep": ("local_resource_operation", "search_resource", "search_file_content", 0),
    "Bash": ("system_operation", "run_command", "shell_command", 0),
    "WebSearch": ("external_information_query", "query_realtime_info", "search_web", 0),
    "WebFetch": ("external_information_query", "retrieve_external_knowledge", "fetch_web_page", 0),
    "AnalyzeAttachment": ("content_transformation", "parse_content", "analyze_attachment", 1),
    "spawn_subagent": ("internal_reasoning", "manage_state", "spawn_subagent", 1),
    "send_agent_message": ("communication", "send_communication", "send_agent_message", 0),
    "broadcast_agent_message": ("communication", "send_communication", "broadcast_agent_message", 0),
    "query_round": ("state_management", "observe_context", "query_round", 0),
    "recall_memory": ("state_management", "observe_context", "recall_memory", 1),
    "ask_user": ("user_interaction", "ask_clarification", "ask_user", 1),
    "schedule_task": ("schedule_management", "manage_schedule", "schedule_task", 0),
    "list_tasks": ("schedule_management", "manage_schedule", "list_tasks", 0),
    "pause_task": ("schedule_management", "manage_schedule", "pause_task", 0),
    "resume_task": ("schedule_management", "manage_schedule", "resume_task", 0),
    "cancel_task": ("schedule_management", "manage_schedule", "cancel_task", 0),
    "start_shell": ("system_operation", "run_command", "start_shell", 0),
    "send_shell": ("system_operation", "run_command", "send_shell", 0),
    "close_shell": ("system_operation", "manage_state", "close_shell", 0),
    "cc_launch": ("external_service_operation", "operate_external_service", "launch_claude_code", 0),
    "prompt_claude_code": ("external_service_operation", "operate_external_service", "prompt_claude_code", 1),
    "read_file": ("local_resource_operation", "read_resource", "read_file", 0),
    "write_file": ("local_resource_operation", "edit_resource", "write_file", 0),
    "edit_file": ("local_resource_operation", "edit_resource", "edit_file", 0),
    "list_files": ("local_resource_operation", "search_resource", "list_files", 0),
    "search_files": ("local_resource_operation", "search_resource", "search_file_content", 0),
    "run_shell": ("system_operation", "run_command", "shell_command", 0),
    "run_command": ("system_operation", "run_command", "shell_command", 0),
    "search_web": ("external_information_query", "query_realtime_info", "search_web", 0),
    "fetch_web_page": ("external_information_query", "retrieve_external_knowledge", "fetch_web_page", 0),
}

_CORRECTION_TERMS = (
    "不对", "不行", "错", "重来", "改一下", "重新", "fix", "wrong", "retry", "instead",
)

_SKILL_TYPE_ORDER = {
    "draft": 0,
    "workflow": 1,
    "parameterized": 2,
    "deterministic": 3,
}

_IO_FAMILIES = {
    "file": {
        "file", "file_path", "filepath", "path", "resource", "codebase", "module",
        "source_code", "modified_file", "workspace_file",
    },
    "text": {
        "text", "plain_text", "markdown", "report", "summary", "answer",
    },
    "code": {
        "code", "source_code", "patch", "diff", "modified_file",
    },
    "structured": {
        "json", "yaml", "csv", "table", "list",
    },
    "web": {
        "url", "web_page", "search_results", "external_data",
    },
    "weather": {
        "weather_report", "weather_info", "weather_forecast", "current_weather", "city_names",
    },
}

_CITY_ALIASES = {
    "beijing": "beijing",
    "北京": "beijing",
    "toronto": "toronto",
    "多伦多": "toronto",
}

_WEATHER_ENTITY_HINTS = tuple(_CITY_ALIASES.keys())

_NOISY_CONSTRAINTS = {
    "unknown",
    "search_returned_no_results",
    "tool_failure",
    "fetch_failed",
}

_GENERIC_ROUTER_ENTITIES = {
    "location",
    "locations",
    "city",
    "cities",
    "time",
    "date",
    "topic",
    "information",
}

_GENERIC_ROUTER_CONSTRAINTS = {
    "location_multi",
    "time_today",
    "time_now",
    "today",
    "now",
}

_SEMANTIC_FAMILIES = {
    "weather": {
        "weather", "forecast", "temperature", "humidity", "current_weather", "weather_data",
        "weather_report", "weather_forecast", "weather_lookup",
    },
    "realtime_info": {
        "query_realtime_info", "information_lookup", "search_weather", "compare_weather",
        "weather_lookup", "stock_lookup", "news_lookup", "price_lookup", "rate_lookup",
    },
    "information": {
        "information", "topic", "requested_output", "text_response", "general_request",
    },
    "code_change": {
        "edit_resource", "code_change", "source_code_file", "workspace_file", "codebase", "workspace",
    },
}


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _new_id(prefix: str) -> str:
    return f"{prefix}_{uuid4().hex[:12]}"


def _normalize_whitespace(text: str) -> str:
    return re.sub(r"\s+", " ", str(text or "")).strip()


def _truncate_text(text: Any, limit: int = 500) -> str:
    compact = _normalize_whitespace(str(text or ""))
    return compact[:limit]


def _json_dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False)


def _json_loads(raw: Any, fallback: Any) -> Any:
    if raw is None:
        return fallback
    if isinstance(raw, (dict, list)):
        return raw
    try:
        parsed = json.loads(str(raw))
    except Exception:
        return fallback
    if isinstance(fallback, dict):
        return parsed if isinstance(parsed, dict) else fallback
    if isinstance(fallback, list):
        return parsed if isinstance(parsed, list) else fallback
    return parsed


class _Conn:
    """Async context manager wrapping aiosqlite with sqlite3.Row row_factory."""
    def __init__(self):
        self._conn = aiosqlite.connect(str(_DB_FILE))

    async def __aenter__(self) -> aiosqlite.Connection:
        await self._conn.__aenter__()
        self._conn.row_factory = sqlite3.Row
        return self._conn

    async def __aexit__(self, *args):
        return await self._conn.__aexit__(*args)


_conn = _Conn


def _extract_json_object(text: str) -> dict[str, Any]:
    source = str(text or "").strip()
    if not source:
        return {}
    try:
        parsed = json.loads(source)
        return parsed if isinstance(parsed, dict) else {}
    except Exception:
        pass
    match = re.search(r"\{.*\}", source, re.DOTALL)
    if not match:
        return {}
    try:
        parsed = json.loads(match.group(0))
    except Exception:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _safe_slug(value: str, default: str = "unknown") -> str:
    slug = re.sub(r"[^a-z0-9_]+", "_", str(value or "").strip().lower())
    slug = re.sub(r"_+", "_", slug).strip("_")
    return slug or default


def _canonical_city_name(value: str) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    lowered = text.lower()
    for alias, canonical in _CITY_ALIASES.items():
        if alias.lower() in lowered:
            return canonical
    return ""


def _semantic_tokens(value: str) -> set[str]:
    normalized = _safe_slug(value, default="")
    if not normalized:
        return set()
    tokens = {token for token in normalized.split("_") if token and token not in {"current", "data", "requested"}}
    for family, members in _SEMANTIC_FAMILIES.items():
        if normalized in members or tokens & members:
            tokens.add(family)
    return tokens


def _extract_city_entities(*values: Any) -> list[str]:
    found: list[str] = []
    seen: set[str] = set()
    for value in values:
        if isinstance(value, dict):
            candidates = value.values()
        elif isinstance(value, (list, tuple, set)):
            candidates = value
        else:
            candidates = [value]
        for candidate in candidates:
            text = str(candidate or "")
            if not text:
                continue
            for hint in _WEATHER_ENTITY_HINTS:
                if hint.lower() in text.lower():
                    canonical = _canonical_city_name(hint)
                    if canonical and canonical not in seen:
                        seen.add(canonical)
                        found.append(canonical)
            canonical = _canonical_city_name(text)
            if canonical and canonical not in seen:
                seen.add(canonical)
                found.append(canonical)
    return found


def _normalize_entity_value(value: Any) -> list[str]:
    text = _normalize_whitespace(str(value or ""))
    if not text:
        return []
    city_entities = _extract_city_entities(text)
    if city_entities:
        return city_entities
    lowered = text.lower()
    normalized: list[str] = []
    if lowered.startswith("http://") or lowered.startswith("https://"):
        host_match = re.search(r"https?://([^/?#]+)", lowered)
        if host_match:
            host = host_match.group(1).replace("www.", "")
            normalized.append(_safe_slug(host))
        path_tokens = re.findall(r"[a-zA-Z]{3,}", lowered)
        for token in path_tokens[:6]:
            slug = _safe_slug(token, default="")
            if slug and slug not in normalized and slug not in {"https", "http", "www", "com", "cn"}:
                normalized.append(slug)
        return normalized[:6]
    if "/" in text or "." in text:
        slug = _safe_slug(text, default="")
        return [slug] if slug else []
    words = re.findall(r"[A-Za-z]{3,}|[\u4e00-\u9fff]{2,}", text)
    for word in words[:6]:
        slug = _safe_slug(word, default="")
        if slug and slug not in normalized:
            normalized.append(slug)
    return normalized[:6]


def _normalize_entities(values: list[Any]) -> list[str]:
    normalized: list[str] = []
    seen: set[str] = set()
    for value in values:
        for item in _normalize_entity_value(value):
            if item not in seen:
                seen.add(item)
                normalized.append(item)
    return normalized


def _coerce_short_text_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        text = _normalize_whitespace(value)
        if not text:
            return []
        return [text]
    if isinstance(value, (list, tuple, set)):
        result: list[str] = []
        for item in value:
            text = _normalize_whitespace(str(item or ""))
            if text:
                result.append(text)
        return result
    text = _normalize_whitespace(str(value))
    return [text] if text else []


def _compress_action_sequence(action_sequence: list[dict[str, Any]]) -> list[dict[str, Any]]:
    compressed: list[dict[str, Any]] = []
    previous_key: tuple[str, str, str] | None = None
    for action in action_sequence:
        if not isinstance(action, dict):
            continue
        key = (
            str(action.get("domain") or ""),
            str(action.get("type") or ""),
            str(action.get("subtype") or ""),
        )
        if key == previous_key:
            continue
        compressed.append(action)
        previous_key = key
    return compressed


def _looks_like_file_path(value: Any) -> bool:
    text = str(value or "").strip()
    return bool(
        text
        and not text.startswith("http://")
        and not text.startswith("https://")
        and ("/" in text or re.search(r"\.[A-Za-z0-9]{1,8}$", text))
    )


def _arg_value_family(value: Any) -> str:
    text = str(value or "").strip()
    lowered = text.lower()
    if not text:
        return "empty"
    if lowered.startswith("http://") or lowered.startswith("https://"):
        return "url"
    if _looks_like_file_path(text):
        return "file_path"
    if re.search(r"-?\d+(?:\.\d+)?", text):
        return "number"
    return "text"


def _arg_entities(value: Any) -> tuple[str, ...]:
    return tuple(_normalize_entities([value]))


def _should_parameterize_arg(key: str, observed_values: list[Any]) -> bool:
    values = [value for value in observed_values if value not in (None, "")]
    if len(values) <= 1:
        return False
    families = {_arg_value_family(value) for value in values}
    if "file_path" in families:
        return True
    if key in {"query", "url"}:
        entity_sets = {tuple(_arg_entities(value)) for value in values}
        if len(entity_sets) == 1 and next(iter(entity_sets), ()):
            return False
    return True


def _looks_like_weather_request(user_message: str, action_sequence: list[dict[str, Any]] | None = None) -> bool:
    text = str(user_message or "")
    lowered = text.lower()
    if re.search(r"(weather|forecast|temperature|humidity|天气|气温|预报)", lowered):
        return True
    action_types = {str((action or {}).get("type") or "") for action in (action_sequence or [])}
    action_subtypes = {str((action or {}).get("subtype") or "") for action in (action_sequence or [])}
    return bool(
        {"query_realtime_info", "retrieve_external_knowledge"} & action_types
        and {"search_web", "fetch_web_page"} & action_subtypes
        and _extract_city_entities(text)
    )


def _looks_like_referential_request(user_message: str) -> bool:
    text = _normalize_whitespace(user_message)
    if not text:
        return False
    lowered = text.lower()
    return bool(
        len(text) <= 18
        or re.search(r"(再|继续|还是|这个|那个|这些|这两个|those|them|it|again|retry|再试|重新|换个)", lowered)
    )


def _infer_context_entities(context_summary: str) -> list[str]:
    if not context_summary:
        return []
    return _normalize_entities([context_summary])


def _infer_context_domain_hints(context_summary: str) -> dict[str, str]:
    summary = str(context_summary or "")
    lowered = summary.lower()
    if _looks_like_weather_request(summary):
        return {
            "intent_type": "query_realtime_info",
            "intent_subtype": "weather_lookup",
            "object_type": "weather_data",
            "object_subtype": "current_weather",
            "domain": "external_information_query",
            "input_type": "city_names",
            "output_type": "weather_report",
        }
    if re.search(r"(stock|price|股价|市值|行情|latest price)", lowered):
        return {
            "intent_type": "query_realtime_info",
            "intent_subtype": "information_lookup",
            "object_type": "topic",
            "object_subtype": "information",
            "domain": "external_information_query",
            "input_type": "text",
            "output_type": "text",
        }
    if re.search(r"(refactor|fix|patch|rewrite|重构|修复|修改|补测试|测试用例|登录)", lowered):
        return {
            "intent_type": "edit_resource",
            "intent_subtype": "code_change",
            "object_type": "codebase",
            "object_subtype": "workspace",
            "domain": "software_development",
            "input_type": "text",
            "output_type": "modified_source_code_file",
        }
    return {}


def _history_summary(history: list[dict[str, Any]], limit: int = 6) -> str:
    snippets: list[str] = []
    for message in history[-limit:]:
        if not isinstance(message, dict):
            continue
        role = str(message.get("role") or "").strip()
        if role not in {"user", "assistant", "tool"}:
            continue
        content = _truncate_text(message.get("content") or "", 180)
        if not content:
            continue
        snippets.append(f"{role}: {content}")
    return "\n".join(snippets)


def _turn_feedback_from_message(message: str) -> str:
    lowered = str(message or "").lower()
    if any(term in lowered for term in _CORRECTION_TERMS):
        return "correction"
    return ""


def _default_pattern_stats() -> dict[str, Any]:
    return {
        "frequency": 0,
        "success_count": 0.0,
        "partial_success_count": 0.0,
        "failure_count": 0.0,
        "correction_count": 0.0,
        "success_rate": 0.0,
        "effective_count": 0.0,
        "action_stability": 0.0,
        "io_stability": 0.0,
        "last_seen_at": "",
    }


def _default_skill_stats() -> dict[str, Any]:
    return {
        "total_runs": 0,
        "shadow_success": 0,
        "shadow_failure": 0,
        "active_success": 0,
        "active_failure": 0,
        "last_run_at": "",
        "consistency_avg": 0.0,
    }


async def _ensure_tables() -> None:
    async with _conn() as conn:
        await conn.executescript(_CREATE_TABLES)
        await conn.commit()


async def _seed_core_vocabulary() -> None:
    now = _now_iso()
    async with _conn() as conn:
        for label in sorted(_CORE_DOMAINS):
            await conn.execute(
                """
                INSERT OR IGNORE INTO behavior_vocabulary_labels
                (label_id, label_type, canonical_label, domain, parent_label, raw_description, status, created_at, updated_at)
                VALUES (?, 'domain', ?, '', '', '', 'active', ?, ?)
                """,
                (f"domain:{label}", label, now, now),
            )
        for label in sorted(_CORE_TYPES):
            await conn.execute(
                """
                INSERT OR IGNORE INTO behavior_vocabulary_labels
                (label_id, label_type, canonical_label, domain, parent_label, raw_description, status, created_at, updated_at)
                VALUES (?, 'type', ?, '', '', '', 'active', ?, ?)
                """,
                (f"type:{label}", label, now, now),
            )
        for alias_key, canonical in _STATIC_ALIASES.items():
            label_type, alias = alias_key.split(":", 1)
            await conn.execute(
                """
                INSERT OR IGNORE INTO behavior_vocabulary_aliases
                (alias_id, label_type, canonical_label, alias_label, created_at, vocabulary_version)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (f"alias:{label_type}:{alias}", label_type, canonical, alias, now, _VOCABULARY_VERSION),
            )
        await conn.commit()


async def init(data_dir: Path, workspace_dir: Path) -> None:
    global _DATA_DIR, _WORKSPACE_DIR, _DB_FILE, _INIT_DONE
    _DATA_DIR = data_dir
    _WORKSPACE_DIR = workspace_dir
    _DB_FILE = DB_PATH
    await _ensure_tables()
    await _seed_core_vocabulary()
    await _refresh_generated_skill_names_with_llm()
    _INIT_DONE = True


def _pattern_dir() -> Path:
    base = _WORKSPACE_DIR or Path.cwd()
    path = base / "patterns"
    path.mkdir(parents=True, exist_ok=True)
    return path


async def begin_turn(
    *,
    session_id: str,
    round_id: str,
    user_message: str,
    history: list[dict[str, Any]],
    session_title: str = "",
) -> dict[str, Any]:
    if not _INIT_DONE:
        await _ensure_tables()
    now = _now_iso()
    normalized_session_id = str(session_id or "").strip() or _new_id("session")
    normalized_round_id = str(round_id or "").strip() or _new_id("round")
    turn_id = _new_id("turn")
    feedback = _turn_feedback_from_message(user_message)
    context_summary = _history_summary(history)
    metadata = {
        "round_id": normalized_round_id,
        "session_title": str(session_title or "").strip(),
        "correction_feedback": False,
        "round_title": "",
    }
    async with _conn() as conn:
        cursor = await conn.execute(
            "SELECT session_id FROM behavior_sessions WHERE session_id = ?",
            (normalized_session_id,),
        )
        row = await cursor.fetchone()
        if row is None:
            await conn.execute(
                """
                INSERT INTO behavior_sessions
                (session_id, created_at, updated_at, session_summary, metadata_json)
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    normalized_session_id,
                    now,
                    now,
                    _truncate_text(session_title or user_message, 240),
                    _json_dumps({"source": "live_session"}),
                ),
            )
        else:
            await conn.execute(
                """
                UPDATE behavior_sessions
                SET updated_at = ?, session_summary = COALESCE(NULLIF(?, ''), session_summary)
                WHERE session_id = ?
                """,
                (now, _truncate_text(session_title, 240), normalized_session_id),
            )
        if feedback:
            cursor = await conn.execute(
                """
                SELECT turn_id, user_feedback, metadata_json
                FROM behavior_turns
                WHERE session_id = ?
                ORDER BY created_at DESC
                LIMIT 1
                """,
                (normalized_session_id,),
            )
            latest_turn = await cursor.fetchone()
            if latest_turn is not None:
                latest_meta = _json_loads(latest_turn["metadata_json"], {})
                latest_meta["correction_feedback"] = True
                await conn.execute(
                    """
                    UPDATE behavior_turns
                    SET user_feedback = ?, metadata_json = ?, updated_at = ?
                    WHERE turn_id = ?
                    """,
                    ("correction", _json_dumps(latest_meta), now, latest_turn["turn_id"]),
                )
        await conn.execute(
            """
            INSERT INTO behavior_turns
            (turn_id, session_id, round_id, created_at, updated_at, user_message, context_summary,
             outcome_status, user_feedback, processed_status, linked_skill_id, metadata_json)
            VALUES (?, ?, ?, ?, ?, ?, ?, 'success', '', 0, '', ?)
            """,
            (
                turn_id,
                normalized_session_id,
                normalized_round_id,
                now,
                now,
                str(user_message or ""),
                context_summary,
                _json_dumps(metadata),
            ),
        )
        await conn.commit()
    session_token = _current_session_id.set(normalized_session_id)
    turn_token = _current_turn_id.set(turn_id)
    round_token = _current_round_id.set(normalized_round_id)
    return {
        "turn_id": turn_id,
        "session_id": normalized_session_id,
        "round_id": normalized_round_id,
        "session_token": session_token,
        "turn_token": turn_token,
        "round_token": round_token,
    }


def clear_turn_context(context: dict[str, Any]) -> None:
    try:
        _current_session_id.reset(context["session_token"])
        _current_turn_id.reset(context["turn_token"])
        _current_round_id.reset(context["round_token"])
    except Exception:
        logger.debug("Failed to reset behavior context", exc_info=True)


def current_turn_id() -> str:
    return _current_turn_id.get()


def _map_tool_to_action(tool_name: str) -> tuple[str, str, str, int]:
    if tool_name in _TOOL_ACTION_MAP:
        return _TOOL_ACTION_MAP[tool_name]
    return ("state_management", "call_tool", _safe_slug(tool_name), 0)


async def record_action(
    tool_name: str,
    args: dict[str, Any],
    caller: str,
    round_id: str,
    duration_ms: float,
    *,
    result: Any = "",
    success: bool = True,
    error: str = "",
) -> None:
    session_id = _current_session_id.get()
    turn_id = _current_turn_id.get()
    if not session_id or not turn_id:
        return
    now = _now_iso()
    action_id = _new_id("action")
    domain, action_type, action_subtype, requires_llm = _map_tool_to_action(tool_name)
    metadata = {
        "caller": str(caller or "unknown"),
        "round_id": str(round_id or _current_round_id.get()),
        "duration_ms": round(float(duration_ms or 0), 2),
        "raw_args": dict(args or {}),
        "action_domain": domain,
    }
    async with _conn() as conn:
        cursor = await conn.execute(
            "SELECT COALESCE(MAX(action_index), -1) AS max_idx FROM behavior_actions WHERE turn_id = ?",
            (turn_id,),
        )
        row = await cursor.fetchone()
        next_index = int(row["max_idx"] or -1) + 1
        await conn.execute(
            """
            INSERT INTO behavior_actions
            (action_id, turn_id, session_id, round_id, created_at, action_index, action_type, action_subtype,
             tool_name, input_summary, output_summary, success, error_summary, requires_llm, risk_level, metadata_json)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'none', ?)
            """,
            (
                action_id,
                turn_id,
                session_id,
                str(round_id or _current_round_id.get()),
                now,
                next_index,
                action_type,
                action_subtype,
                tool_name,
                _truncate_text(_json_dumps(args or {}), 500),
                _truncate_text(result, 500),
                1 if success else 0,
                _truncate_text(error, 400),
                requires_llm,
                _json_dumps(metadata),
            ),
        )
        await conn.execute(
            "UPDATE behavior_turns SET updated_at = ? WHERE turn_id = ?",
            (now, turn_id),
        )
        await conn.execute(
            "UPDATE behavior_sessions SET updated_at = ? WHERE session_id = ?",
            (now, session_id),
        )
        await conn.commit()


async def mark_turn_skill_routed(skill_id: str) -> None:
    turn_id = _current_turn_id.get()
    if not turn_id:
        return
    async with _conn() as conn:
        await conn.execute(
            "UPDATE behavior_turns SET linked_skill_id = ?, updated_at = ? WHERE turn_id = ?",
            (str(skill_id or ""), _now_iso(), turn_id),
        )
        await conn.commit()


async def _classify_turn_outcome(turn_id: str) -> str:
    async with _conn() as conn:
        cursor = await conn.execute(
            "SELECT success FROM behavior_actions WHERE turn_id = ?",
            (turn_id,),
        )
        rows = await cursor.fetchall()
        if not rows:
            return "success"
        success_count = sum(1 for row in rows if int(row["success"] or 0) == 1)
        failure_count = len(rows) - success_count
        if failure_count == 0:
            return "success"
        if success_count == 0:
            return "failure"
        return "partial_success"


async def complete_turn(
    *,
    turn_id: str,
    assistant_response: str,
    session_title: str = "",
    round_title: str = "",
) -> None:
    now = _now_iso()
    outcome = await _classify_turn_outcome(turn_id)
    async with _conn() as conn:
        cursor = await conn.execute(
            "SELECT metadata_json, session_id FROM behavior_turns WHERE turn_id = ?",
            (turn_id,),
        )
        row = await cursor.fetchone()
        if row is None:
            return
        metadata = _json_loads(row["metadata_json"], {})
        metadata["assistant_preview"] = _truncate_text(assistant_response, 240)
        if session_title:
            metadata["session_title"] = session_title
        if round_title:
            metadata["round_title"] = round_title
        await conn.execute(
            """
            UPDATE behavior_turns
            SET updated_at = ?, outcome_status = ?, metadata_json = ?
            WHERE turn_id = ?
            """,
            (now, outcome, _json_dumps(metadata), turn_id),
        )
        if session_title:
            await conn.execute(
                """
                UPDATE behavior_sessions
                SET updated_at = ?, session_summary = ?
                WHERE session_id = ?
                """,
                (now, _truncate_text(session_title, 240), row["session_id"]),
            )
        await conn.commit()


async def _alias_lookup(label_type: str, label: str) -> str:
    normalized = _safe_slug(label)
    if not normalized:
        return ""
    static_key = f"{label_type}:{normalized}"
    if static_key in _STATIC_ALIASES:
        return _STATIC_ALIASES[static_key]
    async with _conn() as conn:
        cursor = await conn.execute(
            """
            SELECT canonical_label
            FROM behavior_vocabulary_aliases
            WHERE label_type = ? AND alias_label = ?
            """,
            (label_type, normalized),
        )
        row = await cursor.fetchone()
        if row is not None:
            return str(row["canonical_label"] or "")
    return normalized


async def _record_unknown_label(
    *,
    turn_id: str,
    label_type: str,
    raw_description: str,
    proposed_domain: str = "",
    proposed_type: str = "",
    proposed_subtype: str = "",
    reason: str = "",
) -> None:
    normalized_raw = _normalize_whitespace(raw_description)
    if not normalized_raw:
        return
    now = _now_iso()
    async with _conn() as conn:
        cursor = await conn.execute(
            """
            SELECT unknown_id, seen_count, example_turns
            FROM behavior_unknown_labels
            WHERE label_type = ? AND raw_description = ?
            """,
            (label_type, normalized_raw),
        )
        row = await cursor.fetchone()
        if row is None:
            await conn.execute(
                """
                INSERT INTO behavior_unknown_labels
                (unknown_id, label_type, raw_description, proposed_domain, proposed_type, proposed_subtype,
                 reason, seen_count, example_turns, status, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, 1, ?, 'open', ?, ?)
                """,
                (
                    _new_id("unknown"),
                    label_type,
                    normalized_raw,
                    proposed_domain,
                    proposed_type,
                    proposed_subtype,
                    reason,
                    _json_dumps([turn_id] if turn_id else []),
                    now,
                    now,
                ),
            )
        else:
            examples = _json_loads(row["example_turns"], [])
            if turn_id and turn_id not in examples:
                examples = [turn_id, *examples][:12]
            await conn.execute(
                """
                UPDATE behavior_unknown_labels
                SET proposed_domain = COALESCE(NULLIF(?, ''), proposed_domain),
                    proposed_type = COALESCE(NULLIF(?, ''), proposed_type),
                    proposed_subtype = COALESCE(NULLIF(?, ''), proposed_subtype),
                    reason = COALESCE(NULLIF(?, ''), reason),
                    seen_count = ?,
                    example_turns = ?,
                    updated_at = ?
                WHERE unknown_id = ?
                """,
                (
                    proposed_domain,
                    proposed_type,
                    proposed_subtype,
                    reason,
                    int(row["seen_count"] or 0) + 1,
                    _json_dumps(examples),
                    now,
                    row["unknown_id"],
                ),
            )
        await conn.commit()


async def _normalize_domain(value: str, turn_id: str = "") -> str:
    normalized = await _alias_lookup("domain", value)
    if normalized in _CORE_DOMAINS:
        return normalized
    if normalized and normalized in _CORE_TYPES:
        return "unknown"
    await _record_unknown_label(
        turn_id=turn_id,
        label_type="domain",
        raw_description=value,
        proposed_domain=normalized,
        reason="domain_not_in_core",
    )
    return "unknown"


async def _normalize_type(value: str, turn_id: str = "") -> str:
    normalized = await _alias_lookup("type", value)
    if normalized in _CORE_TYPES:
        return normalized
    await _record_unknown_label(
        turn_id=turn_id,
        label_type="type",
        raw_description=value,
        proposed_type=normalized,
        reason="type_not_in_core",
    )
    return "unknown"


async def _normalize_subtype(value: str, turn_id: str = "") -> str:
    normalized = await _alias_lookup("subtype", value)
    if normalized == "unknown":
        await _record_unknown_label(
            turn_id=turn_id,
            label_type="subtype",
            raw_description=value,
            proposed_subtype=normalized,
            reason="subtype_unknown",
        )
    return normalized


async def _normalize_semantic_label(value: str, *, label_type: str, turn_id: str = "") -> str:
    normalized = await _alias_lookup(label_type, value)
    if not normalized:
        normalized = "unknown"
    if normalized not in {"", "unknown"}:
        await _record_unknown_label(
            turn_id=turn_id,
            label_type=label_type,
            raw_description=value,
            proposed_type=normalized if label_type.endswith("_type") else "",
            proposed_subtype=normalized if label_type.endswith("_subtype") else "",
            reason="open_semantic_label",
        )
    elif value:
        await _record_unknown_label(
            turn_id=turn_id,
            label_type=label_type,
            raw_description=value,
            reason="semantic_label_unknown",
        )
    return normalized or "unknown"


def _normalize_slot(slot: dict[str, Any]) -> dict[str, Any]:
    return {
        "name": _safe_slug(slot.get("name") or slot.get("parameter_name") or "param"),
        "type": _safe_slug(slot.get("type") or "text"),
        "required": bool(slot.get("required", False)),
        "examples": [str(item) for item in (slot.get("examples") or [])[:6]],
        "default_value": slot.get("default_value"),
        "aliases": [str(item) for item in (slot.get("aliases") or [])[:6]],
    }


async def normalize_fingerprint(fingerprint: dict[str, Any], *, turn_id: str = "") -> dict[str, Any]:
    fp = dict(fingerprint or {})
    intent = fp.get("intent") or {}
    obj = fp.get("object") or {}
    if not isinstance(intent, dict):
        intent = {}
    if not isinstance(obj, dict):
        obj = {}
    action_sequence = fp.get("action_sequence") or []

    normalized_intent_type = await _normalize_semantic_label(
        str(intent.get("type") or "unknown"),
        label_type="intent_type",
        turn_id=turn_id,
    )
    normalized_intent_subtype = await _normalize_semantic_label(
        str(intent.get("subtype") or "unknown"),
        label_type="intent_subtype",
        turn_id=turn_id,
    )
    normalized_object_type = await _normalize_semantic_label(
        str(obj.get("type") or "unknown"),
        label_type="object_type",
        turn_id=turn_id,
    )
    normalized_object_subtype = await _normalize_semantic_label(
        str(obj.get("subtype") or "unknown"),
        label_type="object_subtype",
        turn_id=turn_id,
    )
    normalized_domain = await _normalize_domain(str(fp.get("domain") or "unknown"), turn_id)
    raw_text = " ".join(
        str(part or "")
        for part in (
            (intent or {}).get("raw_description"),
            (obj or {}).get("raw_description"),
            fp.get("domain"),
            " ".join(str(item) for item in (fp.get("constraints") or [])),
            " ".join(str(item) for item in (fp.get("entities") or [])),
        )
    )

    normalized_actions: list[dict[str, Any]] = []
    has_unknown = False
    for action in action_sequence:
        if not isinstance(action, dict):
            continue
        action_domain = await _normalize_domain(str(action.get("domain") or normalized_domain), turn_id)
        action_type = await _normalize_type(str(action.get("type") or "unknown"), turn_id)
        action_subtype = await _normalize_subtype(str(action.get("subtype") or "unknown"), turn_id)
        if "unknown" in {action_domain, action_type, action_subtype}:
            has_unknown = True
        normalized_actions.append(
            {
                "domain": action_domain,
                "type": action_type,
                "subtype": action_subtype,
                "raw_description": _truncate_text(action.get("raw_description") or "", 180),
            }
        )
    normalized_actions = _compress_action_sequence(normalized_actions)

    if _looks_like_weather_request(raw_text, normalized_actions):
        normalized_intent_type = "query_realtime_info"
        normalized_intent_subtype = "weather_lookup"
        normalized_object_type = "weather_data"
        normalized_object_subtype = "current_weather"
        normalized_domain = "external_information_query"

    normalized = {
        "intent": {
            "type": normalized_intent_type,
            "subtype": normalized_intent_subtype,
            "raw_description": _truncate_text(intent.get("raw_description") or "", 180),
        },
        "object": {
            "type": normalized_object_type,
            "subtype": normalized_object_subtype,
            "raw_description": _truncate_text(obj.get("raw_description") or "", 180),
        },
        "input_type": _safe_slug(str(fp.get("input_type") or "unknown")),
        "output_type": _safe_slug(str(fp.get("output_type") or "unknown")),
        "domain": normalized_domain,
        "constraints": sorted(
            {
                _safe_slug(item)
                for item in _coerce_short_text_list(fp.get("constraints"))
                if str(item).strip() and _safe_slug(item) not in _NOISY_CONSTRAINTS
            }
        ),
        "entities": sorted(_normalize_entities(fp.get("entities") or [])),
        "action_sequence": normalized_actions,
        "parameter_slots": [_normalize_slot(slot) for slot in (fp.get("parameter_slots") or []) if isinstance(slot, dict)],
        "llm_dependency": _safe_slug(str(fp.get("llm_dependency") or "medium")),
        "risk_level": _safe_slug(str(fp.get("risk_level") or "none")),
        "vocabulary_status": {
            "uses_core_vocabulary": normalized_domain in _CORE_DOMAINS and all(
                action.get("type") in _CORE_TYPES for action in normalized_actions
            ),
            "uses_learned_vocabulary": any(
                label not in {"unknown", ""} and label not in _CORE_TYPES and label not in _CORE_DOMAINS
                for label in (
                    normalized_intent_type,
                    normalized_intent_subtype,
                    normalized_object_type,
                    normalized_object_subtype,
                )
            ),
            "has_unknown": has_unknown
            or normalized_intent_type == "unknown"
            or normalized_object_type == "unknown"
            or normalized_domain == "unknown",
            "proposed_new_labels": [],
        },
        "vocabulary_version": _VOCABULARY_VERSION,
    }
    return normalized


async def _heuristic_request_fingerprint(
    user_message: str,
    action_sequence: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    text = str(user_message or "")
    lowered = text.lower()
    weather_request = _looks_like_weather_request(text, action_sequence)
    intent_type = "generate_content"
    intent_subtype = "general_request"
    domain = "content_generation"
    output_type = "text"
    object_type = "topic"
    object_subtype = "information"
    action_types = {str((action or {}).get("type") or "") for action in (action_sequence or [])}
    action_subtypes = {str((action or {}).get("subtype") or "") for action in (action_sequence or [])}
    code_action_detected = bool(
        {"read_resource", "search_resource", "edit_resource", "run_command"} & action_types
        or {
            "read_file",
            "write_file",
            "edit_file",
            "list_files",
            "search_file_content",
            "shell_command",
        }
        & action_subtypes
    )
    realtime_query_detected = bool(
        {"query_realtime_info", "retrieve_external_knowledge"} & action_types
        or {"search_web", "fetch_web_page"} & action_subtypes
    )
    code_request = bool(
        re.search(r"(refactor|implement|fix|patch|rewrite|重构|实现|修复|修改|优化|补充|新增|调整|测试|test)", lowered)
        or code_action_detected
    )
    code_analysis_request = bool(
        re.search(r"(review|inspect|analy[sz]e|explain|检查|分析|解释|看看|看一下|阅读)", lowered)
        and (
            code_action_detected
            or bool(re.search(r"(src/|\.py\b|\.js\b|\.ts\b|\.tsx\b|\.jsx\b|\.java\b|\.go\b|\.rs\b|\.swift\b|\.cpp\b|\.c\b)", lowered))
        )
    )
    realtime_request = bool(
        realtime_query_detected
        or re.search(r"(weather|stock|news|price|rate|score|latest|实时|最新|天气|股票|汇率|新闻|比分|价格)", lowered)
        or re.search(r"(搜索|查询)", text)
    )
    if code_request:
        intent_type = "edit_resource"
        intent_subtype = "code_change"
        domain = "software_development"
        output_type = "modified_source_code_file"
    elif code_analysis_request:
        intent_type = "extract_information"
        intent_subtype = "code_explanation"
        domain = "software_development"
    elif weather_request:
        intent_type = "query_realtime_info"
        intent_subtype = "weather_lookup"
        domain = "external_information_query"
        object_type = "weather_data"
        object_subtype = "current_weather"
        output_type = "weather_report"
    elif realtime_request:
        intent_type = "query_realtime_info"
        intent_subtype = "information_lookup"
        domain = "external_information_query"
    elif re.search(r"(总结|总结一下|summari[sz]e|summary)", lowered):
        intent_type = "generate_content"
        intent_subtype = "summary"
        domain = "content_generation"
    entities = _extract_city_entities(text)
    for match in re.findall(r"(?:[A-Za-z0-9_.-]+/[A-Za-z0-9_./-]+|[A-Za-z0-9_.-]+\.[A-Za-z0-9]{1,8})", text):
        if match not in entities:
            entities.append(match)
    if domain == "software_development":
        object_type = "source_code_file" if entities else "codebase"
        object_subtype = "workspace_file" if entities else "workspace"
    elif domain == "content_generation":
        object_type = "requested_output"
        object_subtype = "text_response"
    if not action_sequence:
        action_sequence = []
        if domain == "software_development":
            action_sequence = [
                {"domain": "local_resource_operation", "type": "search_resource", "subtype": "list_files", "raw_description": "Inspect workspace files"},
                {"domain": "local_resource_operation", "type": "read_resource", "subtype": "read_file", "raw_description": "Read relevant source files"},
                {"domain": "software_development", "type": "edit_resource", "subtype": "edit_file", "raw_description": "Update implementation"},
            ]
        elif domain == "external_information_query":
            action_sequence = [
                {"domain": "external_information_query", "type": "query_realtime_info", "subtype": "search_web", "raw_description": "Search current information"},
            ]
    return await normalize_fingerprint(
        {
            "intent": {
                "type": intent_type,
                "subtype": intent_subtype,
                "raw_description": _truncate_text(text, 180),
            },
            "object": {
                "type": object_type,
                "subtype": object_subtype,
                "raw_description": _truncate_text(text, 180),
            },
            "input_type": "city_names" if weather_request else ("file_path" if entities and domain == "software_development" else "text"),
            "output_type": output_type,
            "domain": domain,
            "constraints": (
                ["multiple_cities"] if len(_extract_city_entities(text)) >= 2 else []
            ) + (["chinese"] if re.search(r"[\u4e00-\u9fff]", text) else []),
            "entities": entities,
            "action_sequence": action_sequence,
            "parameter_slots": [],
            "llm_dependency": "medium",
            "risk_level": "none",
        }
    )


async def _call_llm_json(prompt: str, *, caller: str = "behavior_learning") -> dict[str, Any]:
    from cyrene.agent.state import _call_llm, _caller_type
    from cyrene.llm import _assistant_text

    token = _caller_type.set(caller)
    try:
        response = await _call_llm([{"role": "user", "content": prompt}], tools=None, max_tokens=2000)
        return _extract_json_object(_assistant_text(response))
    except Exception:
        logger.debug("behavior learning LLM JSON call failed", exc_info=True)
        return {}
    finally:
        _caller_type.reset(token)


async def _action_rows_for_turn(turn_id: str) -> list[dict[str, Any]]:
    async with _conn() as conn:
        cursor = await conn.execute(
            """
            SELECT *
            FROM behavior_actions
            WHERE turn_id = ?
            ORDER BY action_index ASC
            """,
            (turn_id,),
        )
        rows = await cursor.fetchall()
    actions: list[dict[str, Any]] = []
    for row in rows:
        item = dict(row)
        item["metadata_json"] = _json_loads(item.get("metadata_json"), {})
        actions.append(item)
    return actions


async def build_turn_fingerprint(turn_id: str) -> dict[str, Any]:
    async with _conn() as conn:
        cursor = await conn.execute(
            "SELECT fingerprint_content FROM behavior_fingerprints WHERE turn_id = ?",
            (turn_id,),
        )
        existing = await cursor.fetchone()
        if existing is not None:
            return _json_loads(existing["fingerprint_content"], {})
        cursor = await conn.execute(
            """
            SELECT turn_id, session_id, round_id, user_message, context_summary
            FROM behavior_turns
            WHERE turn_id = ?
            """,
            (turn_id,),
        )
        turn_row = await cursor.fetchone()
    if turn_row is None:
        return {}
    action_rows = await _action_rows_for_turn(turn_id)
    action_summary = []
    deterministic_actions = []
    deterministic_entities: list[str] = []
    for row in action_rows:
        raw_args = (row.get("metadata_json") or {}).get("raw_args") or {}
        deterministic_entities.extend(_normalize_entities(list(raw_args.values())))
        action_summary.append(
            {
                "tool_name": row["tool_name"],
                "action_type": row["action_type"],
                "action_subtype": row["action_subtype"],
                "input_summary": row["input_summary"],
                "output_summary": row["output_summary"],
                "success": bool(row["success"]),
            }
        )
        deterministic_actions.append(
            {
                "domain": str((row.get("metadata_json") or {}).get("action_domain") or "state_management"),
                "type": str(row["action_type"]),
                "subtype": str(row["action_subtype"]),
                "raw_description": str(row["tool_name"]),
            }
        )
    prompt = f"""You are building a structured behavior fingerprint for an autonomous coding agent turn.

Return exactly one JSON object with these keys:
intent, object, input_type, output_type, domain, constraints, entities, action_sequence, parameter_slots, llm_dependency, risk_level

Rules:
- intent.type and object.type must use short snake_case labels.
- domain should prefer one of:
  {sorted(_CORE_DOMAINS)}
- action_sequence items must have: domain, type, subtype, raw_description
- type labels should prefer one of:
  {sorted(_CORE_TYPES)}
- parameter_slots items should have: name, type, required, examples
- constraints/entities are arrays of short strings
- keep labels stable and reusable
- infer from the actual tool sequence, not just the user wording

User message:
{turn_row["user_message"]}

Context summary:
{turn_row["context_summary"]}

Observed agent actions:
{json.dumps(action_summary, ensure_ascii=False, indent=2)}

JSON only.
"""
    payload = await _call_llm_json(prompt)
    heuristic_fp = await _heuristic_request_fingerprint(
        str(turn_row["user_message"]),
        action_sequence=deterministic_actions,
    )
    if not payload:
        payload = heuristic_fp
    else:
        if not isinstance(payload.get("intent"), dict):
            payload["intent"] = {}
        if not isinstance(payload.get("object"), dict):
            payload["object"] = {}
        payload.setdefault("intent", heuristic_fp.get("intent") or {})
        payload.setdefault("object", heuristic_fp.get("object") or {})
        if str((payload.get("intent") or {}).get("type") or "").strip().lower() in {"", "unknown"}:
            payload["intent"] = heuristic_fp.get("intent") or {}
        if str((payload.get("object") or {}).get("type") or "").strip().lower() in {"", "unknown"}:
            payload["object"] = heuristic_fp.get("object") or {}
        if deterministic_actions:
            payload["input_type"] = heuristic_fp.get("input_type")
            payload["output_type"] = heuristic_fp.get("output_type")
        else:
            if not str(payload.get("input_type") or "").strip():
                payload["input_type"] = heuristic_fp.get("input_type")
            if not str(payload.get("output_type") or "").strip():
                payload["output_type"] = heuristic_fp.get("output_type")
        if not str(payload.get("domain") or "").strip():
            payload["domain"] = heuristic_fp.get("domain")
        payload["action_sequence"] = deterministic_actions or payload.get("action_sequence") or []
        merged_entities = list(payload.get("entities") or [])
        merged_entities.extend(deterministic_entities)
        payload["entities"] = _normalize_entities(merged_entities)
    fingerprint = await normalize_fingerprint(payload, turn_id=turn_id)
    now = _now_iso()
    async with _conn() as conn:
        await conn.execute(
            """
            INSERT OR REPLACE INTO behavior_fingerprints
            (turn_id, fingerprint_content, vocabulary_version, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (turn_id, _json_dumps(fingerprint), _VOCABULARY_VERSION, now, now),
        )
        await conn.commit()
    return fingerprint


async def build_request_fingerprint(user_message: str, history: list[dict[str, Any]]) -> dict[str, Any]:
    context_summary = _history_summary(history)
    heuristic = await _heuristic_request_fingerprint(user_message)
    prompt = f"""You are matching a new user request against learned automation skills.

Return exactly one JSON object with these keys:
intent, object, input_type, output_type, domain, constraints, entities, action_sequence, parameter_slots, llm_dependency, risk_level

Rules:
- Predict the likely abstract action sequence needed to satisfy the request.
- Use short snake_case labels.
- Prefer stable reusable categories.
- action_sequence items must contain domain, type, subtype, raw_description.

User message:
{user_message}

Recent context:
{context_summary}

JSON only.
"""
    payload = await _call_llm_json(prompt, caller="skill_router")
    if not payload:
        return heuristic
    normalized = await normalize_fingerprint(payload)
    heuristic_actions = heuristic.get("action_sequence") or []
    normalized_actions = normalized.get("action_sequence") or []
    if (
        not normalized_actions
        or all(str((item or {}).get("type") or "") in {"", "unknown"} for item in normalized_actions)
        or all(str((item or {}).get("domain") or "") in {"", "unknown"} for item in normalized_actions)
    ):
        normalized["action_sequence"] = heuristic_actions
    if not normalized.get("constraints") and heuristic.get("constraints"):
        normalized["constraints"] = list(heuristic.get("constraints") or [])
    elif heuristic.get("constraints"):
        normalized_constraints = {str(item) for item in (normalized.get("constraints") or [])}
        heuristic_constraints = {str(item) for item in (heuristic.get("constraints") or [])}
        if normalized_constraints and heuristic_constraints and (
            normalized_constraints <= _GENERIC_ROUTER_CONSTRAINTS
            or not (normalized_constraints & heuristic_constraints)
        ):
            normalized["constraints"] = list(heuristic.get("constraints") or [])
    if not normalized.get("entities") and heuristic.get("entities"):
        normalized["entities"] = list(heuristic.get("entities") or [])
    elif heuristic.get("entities"):
        normalized_entities = {str(item) for item in (normalized.get("entities") or [])}
        heuristic_entities = {str(item) for item in (heuristic.get("entities") or [])}
        if normalized_entities and heuristic_entities and (
            normalized_entities <= _GENERIC_ROUTER_ENTITIES
            or not (normalized_entities & heuristic_entities)
        ):
            normalized["entities"] = list(heuristic.get("entities") or [])
    if str(normalized.get("input_type") or "unknown") == "unknown" and heuristic.get("input_type"):
        normalized["input_type"] = heuristic.get("input_type")
    elif str(normalized.get("input_type") or "") in {"text", "text_query", "query"} and heuristic.get("input_type") not in {"", "text", "text_query", "query"}:
        normalized["input_type"] = heuristic.get("input_type")
    if str(normalized.get("output_type") or "unknown") == "unknown" and heuristic.get("output_type"):
        normalized["output_type"] = heuristic.get("output_type")
    elif str(normalized.get("output_type") or "") in {"text", "text_response", "answer"} and heuristic.get("output_type") not in {"", "text", "text_response", "answer"}:
        normalized["output_type"] = heuristic.get("output_type")
    if str((normalized.get("intent") or {}).get("type") or "unknown") == "unknown":
        normalized["intent"] = dict(heuristic.get("intent") or {})
    if str((normalized.get("object") or {}).get("type") or "unknown") == "unknown":
        normalized["object"] = dict(heuristic.get("object") or {})
    if str(normalized.get("domain") or "unknown") == "unknown" and heuristic.get("domain"):
        normalized["domain"] = heuristic.get("domain")
    if heuristic.get("parameter_slots") == [] and (normalized.get("parameter_slots") or []):
        slot_names = {_safe_slug(str((slot or {}).get("name") or ""), default="") for slot in (normalized.get("parameter_slots") or [])}
        if slot_names <= {"location", "locations", "time", "date", "city", "cities"}:
            normalized["parameter_slots"] = []
    return normalized


def _node_similarity(node_a: dict[str, Any], node_b: dict[str, Any]) -> float:
    type_a = str(node_a.get("type") or "")
    type_b = str(node_b.get("type") or "")
    subtype_a = str(node_a.get("subtype") or "")
    subtype_b = str(node_b.get("subtype") or "")
    if type_a and type_a == type_b and subtype_a and subtype_a == subtype_b:
        return 1.0
    if type_a and type_a == type_b:
        return 0.75
    type_tokens_a = _semantic_tokens(type_a) | _semantic_tokens(subtype_a)
    type_tokens_b = _semantic_tokens(type_b) | _semantic_tokens(subtype_b)
    if type_tokens_a and type_tokens_b:
        overlap = len(type_tokens_a & type_tokens_b) / max(len(type_tokens_a), len(type_tokens_b))
        if overlap >= 0.6:
            return 0.75
        if overlap >= 0.34:
            return 0.5
    if type_a and type_b and type_a == "unknown" and type_b == "unknown":
        return 0.25
    return 0.0


def _scalar_similarity(left: str, right: str) -> float:
    if not left and not right:
        return 1.0
    if not left or not right:
        return 0.0
    if left == right:
        return 1.0
    left_families = {name for name, values in _IO_FAMILIES.items() if left in values}
    right_families = {name for name, values in _IO_FAMILIES.items() if right in values}
    if not left_families:
        if "file" in left or "path" in left:
            left_families.add("file")
        if "code" in left or "source" in left or "patch" in left or "diff" in left:
            left_families.add("code")
        if "text" in left or "summary" in left or "report" in left or "answer" in left:
            left_families.add("text")
        if "json" in left or "yaml" in left or "csv" in left or "table" in left:
            left_families.add("structured")
        if "url" in left or "web" in left or "search" in left:
            left_families.add("web")
    if not right_families:
        if "file" in right or "path" in right:
            right_families.add("file")
        if "code" in right or "source" in right or "patch" in right or "diff" in right:
            right_families.add("code")
        if "text" in right or "summary" in right or "report" in right or "answer" in right:
            right_families.add("text")
        if "json" in right or "yaml" in right or "csv" in right or "table" in right:
            right_families.add("structured")
        if "url" in right or "web" in right or "search" in right:
            right_families.add("web")
    if left_families & right_families:
        return 0.75
    if left in right or right in left:
        return 0.50
    return 1.0 if left == right else 0.0


def _set_similarity(left: list[str], right: list[str]) -> float:
    a = {str(item) for item in left if str(item).strip()}
    b = {str(item) for item in right if str(item).strip()}
    if not a and not b:
        return 1.0
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def _slot_similarity(left: list[dict[str, Any]], right: list[dict[str, Any]]) -> float:
    left_keys = {f"{item.get('name')}:{item.get('type')}" for item in left}
    right_keys = {f"{item.get('name')}:{item.get('type')}" for item in right}
    return _set_similarity(sorted(left_keys), sorted(right_keys))


def _action_item_similarity(left: dict[str, Any], right: dict[str, Any]) -> float:
    if left.get("type") == right.get("type") and left.get("subtype") == right.get("subtype"):
        return 1.0
    if left.get("type") == right.get("type"):
        return 0.75
    if left.get("domain") == right.get("domain"):
        return 0.50
    return 0.0


def _lcs_similarity(left: list[dict[str, Any]], right: list[dict[str, Any]]) -> float:
    if not left and not right:
        return 1.0
    if not left or not right:
        return 0.0
    dp = [[0] * (len(right) + 1) for _ in range(len(left) + 1)]
    for i in range(1, len(left) + 1):
        for j in range(1, len(right) + 1):
            if _action_item_similarity(left[i - 1], right[j - 1]) >= 0.75:
                dp[i][j] = dp[i - 1][j - 1] + 1
            else:
                dp[i][j] = max(dp[i - 1][j], dp[i][j - 1])
    lcs = dp[len(left)][len(right)]
    return (2 * lcs) / (len(left) + len(right))


def compute_fingerprint_similarity(fp_a: dict[str, Any], fp_b: dict[str, Any]) -> dict[str, Any]:
    intent_sim = _node_similarity(fp_a.get("intent") or {}, fp_b.get("intent") or {})
    object_sim = _node_similarity(fp_a.get("object") or {}, fp_b.get("object") or {})
    io_sim = (_scalar_similarity(str(fp_a.get("input_type") or ""), str(fp_b.get("input_type") or ""))
              + _scalar_similarity(str(fp_a.get("output_type") or ""), str(fp_b.get("output_type") or ""))) / 2
    domain_sim = _scalar_similarity(str(fp_a.get("domain") or ""), str(fp_b.get("domain") or ""))
    constraint_sim = _set_similarity(fp_a.get("constraints") or [], fp_b.get("constraints") or [])
    entity_sim = _set_similarity(fp_a.get("entities") or [], fp_b.get("entities") or [])
    action_sim = _lcs_similarity(fp_a.get("action_sequence") or [], fp_b.get("action_sequence") or [])
    slot_sim = _slot_similarity(fp_a.get("parameter_slots") or [], fp_b.get("parameter_slots") or [])
    total = (
        0.18 * intent_sim
        + 0.10 * object_sim
        + 0.10 * io_sim
        + 0.10 * domain_sim
        + 0.10 * constraint_sim
        + 0.10 * entity_sim
        + 0.25 * action_sim
        + 0.07 * slot_sim
    )
    return {
        "total": round(total, 4),
        "breakdown": {
            "intent": round(intent_sim, 4),
            "object": round(object_sim, 4),
            "io": round(io_sim, 4),
            "domain": round(domain_sim, 4),
            "constraints": round(constraint_sim, 4),
            "entities": round(entity_sim, 4),
            "action_sequence": round(action_sim, 4),
            "parameter_slots": round(slot_sim, 4),
        },
        "hard_fail": action_sim < 0.50 or io_sim < 0.25,
    }


async def _llm_should_merge(
    turn_fp: dict[str, Any],
    pattern_fp: dict[str, Any],
    similarity: dict[str, Any],
) -> bool:
    prompt = f"""Decide whether a new turn fingerprint should merge into an existing learned behavior pattern.

Return JSON only:
{{"should_merge": true|false, "confidence": 0-1, "same_skill_possible": true|false, "reason": "..."}}

New turn fingerprint:
{json.dumps(turn_fp, ensure_ascii=False, indent=2)}

Existing pattern prototype:
{json.dumps(pattern_fp, ensure_ascii=False, indent=2)}

Similarity breakdown:
{json.dumps(similarity, ensure_ascii=False, indent=2)}
"""
    result = await _call_llm_json(prompt, caller="pattern_merger")
    return bool(result.get("should_merge"))


async def _fingerprint_for_turn(turn_id: str) -> dict[str, Any]:
    async with _conn() as conn:
        cursor = await conn.execute(
            "SELECT fingerprint_content FROM behavior_fingerprints WHERE turn_id = ?",
            (turn_id,),
        )
        row = await cursor.fetchone()
    return _json_loads(row["fingerprint_content"], {}) if row else {}


async def _member_turn_ids(pattern_id: str) -> list[str]:
    async with _conn() as conn:
        cursor = await conn.execute(
            """
            SELECT turn_id
            FROM behavior_pattern_turns
            WHERE pattern_id = ?
            ORDER BY created_at ASC
            """,
            (pattern_id,),
        )
        rows = await cursor.fetchall()
    return [str(row["turn_id"]) for row in rows]


def _choose_pattern_prototype(fingerprints: list[dict[str, Any]]) -> dict[str, Any]:
    if not fingerprints:
        return {}
    if len(fingerprints) == 1:
        return fingerprints[0]
    best_index = 0
    best_score = -1.0
    for idx, left in enumerate(fingerprints):
        total = 0.0
        for jdx, right in enumerate(fingerprints):
            if idx == jdx:
                continue
            total += compute_fingerprint_similarity(left, right)["total"]
        avg = total / max(1, len(fingerprints) - 1)
        if avg > best_score:
            best_index = idx
            best_score = avg
    return fingerprints[best_index]


def _pattern_description(prototype: dict[str, Any]) -> str:
    intent = prototype.get("intent") or {}
    obj = prototype.get("object") or {}
    return " / ".join(
        part for part in [
            str(intent.get("type") or ""),
            str(intent.get("subtype") or ""),
            str(obj.get("type") or ""),
            str(obj.get("subtype") or ""),
        ]
        if part
    ) or "behavior_pattern"


async def _fetch_turn_rows(turn_ids: list[str]) -> list[dict[str, Any]]:
    if not turn_ids:
        return []
    placeholders = ",".join("?" for _ in turn_ids)
    async with _conn() as conn:
        cursor = await conn.execute(
            f"""
            SELECT *
            FROM behavior_turns
            WHERE turn_id IN ({placeholders})
            ORDER BY created_at ASC
            """,
            tuple(turn_ids),
        )
        rows = await cursor.fetchall()
    return [dict(row) for row in rows]


async def _compute_pattern_stats(turn_ids: list[str], prototype: dict[str, Any]) -> dict[str, Any]:
    turns = await _fetch_turn_rows(turn_ids)
    fingerprints = [await _fingerprint_for_turn(turn_id) for turn_id in turn_ids]
    success_count = 0.0
    partial_count = 0.0
    failure_count = 0.0
    correction_count = 0.0
    action_stability_values: list[float] = []
    io_stability_values: list[float] = []
    last_seen = ""
    for turn, fp in zip(turns, fingerprints):
        outcome = str(turn.get("outcome_status") or "success")
        if outcome == "success":
            success_count += 1
        elif outcome == "partial_success":
            partial_count += 1
        elif outcome == "failure":
            failure_count += 1
        metadata = _json_loads(turn.get("metadata_json"), {})
        if bool(metadata.get("correction_feedback")) or str(turn.get("user_feedback") or "") == "correction":
            correction_count += 1
        sim = compute_fingerprint_similarity(fp, prototype)
        action_stability_values.append(float(sim["breakdown"]["action_sequence"]))
        io_stability_values.append(
            (float(sim["breakdown"]["io"]) + float(sim["breakdown"]["domain"])) / 2
        )
        last_seen = str(turn.get("updated_at") or turn.get("created_at") or last_seen)
    frequency = len(turn_ids)
    effective_count = success_count + (0.5 * partial_count) - (1.0 * failure_count) - (1.5 * correction_count)
    success_rate = success_count / frequency if frequency else 0.0
    return {
        "frequency": frequency,
        "success_count": success_count,
        "partial_success_count": partial_count,
        "failure_count": failure_count,
        "correction_count": correction_count,
        "success_rate": round(success_rate, 4),
        "effective_count": round(effective_count, 4),
        "action_stability": round(sum(action_stability_values) / len(action_stability_values), 4) if action_stability_values else 0.0,
        "io_stability": round(sum(io_stability_values) / len(io_stability_values), 4) if io_stability_values else 0.0,
        "last_seen_at": last_seen,
    }


def _pattern_skillability(stats: dict[str, Any], prototype: dict[str, Any]) -> dict[str, Any]:
    effective = float(stats.get("effective_count") or 0)
    llm_dependency = str(prototype.get("llm_dependency") or "medium")
    return {
        "draft": effective >= 2,
        "workflow": effective >= 3,
        "parameterized": effective >= 5,
        "deterministic": effective >= 8 and llm_dependency in {"low", "none"},
    }


def _pattern_status(stats: dict[str, Any], linked_skill_ids: list[str]) -> str:
    effective = float(stats.get("effective_count") or 0)
    frequency = int(stats.get("frequency") or 0)
    if linked_skill_ids:
        return "linked_to_skill"
    if effective >= 2:
        return "skill_candidate"
    if frequency >= 2:
        return "stable"
    return "candidate"


async def _upsert_pattern(pattern_id: str) -> None:
    turn_ids = await _member_turn_ids(pattern_id)
    fingerprints = [await _fingerprint_for_turn(turn_id) for turn_id in turn_ids]
    prototype = _choose_pattern_prototype([fp for fp in fingerprints if fp])
    stats = await _compute_pattern_stats(turn_ids, prototype)
    skillability = _pattern_skillability(stats, prototype)
    async with _conn() as conn:
        cursor = await conn.execute(
            "SELECT skill_id FROM learned_skills WHERE pattern_id = ? ORDER BY created_at ASC",
            (pattern_id,),
        )
        linked_skill_rows = await cursor.fetchall()
        linked_skill_ids = [str(row["skill_id"]) for row in linked_skill_rows]
        status = _pattern_status(stats, linked_skill_ids)
        await conn.execute(
            """
            UPDATE behavior_patterns
            SET description = ?, prototype_fingerprint = ?, statistics_json = ?, skillability_json = ?,
                status = ?, linked_skill_list = ?, updated_at = ?
            WHERE pattern_id = ?
            """,
            (
                _pattern_description(prototype),
                _json_dumps(prototype),
                _json_dumps(stats),
                _json_dumps(skillability),
                status,
                _json_dumps(linked_skill_ids),
                _now_iso(),
                pattern_id,
            ),
        )
        await conn.commit()


async def _merge_turn_into_pattern(turn_id: str, fingerprint: dict[str, Any]) -> tuple[str, bool]:
    async with _conn() as conn:
        cursor = await conn.execute(
            """
            SELECT pattern_id, prototype_fingerprint
            FROM behavior_patterns
            WHERE status != 'deprecated'
            ORDER BY updated_at DESC
            """
        )
        rows = await cursor.fetchall()
    best_pattern_id = ""
    best_similarity: dict[str, Any] = {"total": 0.0, "hard_fail": False, "breakdown": {}}
    best_prototype: dict[str, Any] = {}
    for row in rows:
        prototype = _json_loads(row["prototype_fingerprint"], {})
        sim = compute_fingerprint_similarity(fingerprint, prototype)
        if sim["total"] > float(best_similarity["total"]):
            best_pattern_id = str(row["pattern_id"])
            best_similarity = sim
            best_prototype = prototype
    should_merge = False
    if best_pattern_id and not best_similarity["hard_fail"] and float(best_similarity["total"]) >= _PATTERN_STRONG_THRESHOLD:
        should_merge = True
    elif best_pattern_id and not best_similarity["hard_fail"] and float(best_similarity["total"]) >= _PATTERN_MEDIUM_THRESHOLD:
        breakdown = best_similarity.get("breakdown") or {}
        if (
            float(breakdown.get("action_sequence") or 0.0) >= 0.85
            and float(breakdown.get("intent") or 0.0) >= 0.75
            and float(breakdown.get("object") or 0.0) >= 0.75
            and float(breakdown.get("domain") or 0.0) >= 0.75
        ):
            should_merge = True
        else:
            should_merge = await _llm_should_merge(fingerprint, best_prototype, best_similarity)
    if not should_merge:
        pattern_id = _new_id("pattern")
        now = _now_iso()
        async with _conn() as conn:
            await conn.execute(
                """
                INSERT INTO behavior_patterns
                (pattern_id, description, prototype_fingerprint, statistics_json, skillability_json, status, linked_skill_list, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, 'candidate', '[]', ?, ?)
                """,
                (
                    pattern_id,
                    _pattern_description(fingerprint),
                    _json_dumps(fingerprint),
                    _json_dumps(_default_pattern_stats()),
                    _json_dumps({}),
                    now,
                    now,
                ),
            )
            await conn.execute(
                """
                INSERT INTO behavior_pattern_turns
                (pattern_id, turn_id, similarity, created_at)
                VALUES (?, ?, ?, ?)
                """,
                (pattern_id, turn_id, 1.0, now),
            )
            await conn.commit()
        await _upsert_pattern(pattern_id)
        return pattern_id, False
    async with _conn() as conn:
        await conn.execute(
            """
            INSERT OR REPLACE INTO behavior_pattern_turns
            (pattern_id, turn_id, similarity, created_at)
            VALUES (?, ?, ?, ?)
            """,
            (best_pattern_id, turn_id, float(best_similarity["total"]), _now_iso()),
        )
        await conn.commit()
    await _upsert_pattern(best_pattern_id)
    return best_pattern_id, True


async def _derive_parameter_templates(turn_ids: list[str]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    action_groups: list[list[dict[str, Any]]] = []
    for turn_id in turn_ids[:_MAX_PATTERN_EXAMPLES]:
        group: list[dict[str, Any]] = []
        for action in await _action_rows_for_turn(turn_id):
            metadata = action.get("metadata_json") or {}
            group.append(
                {
                    "tool_name": action["tool_name"],
                    "action_type": action["action_type"],
                    "action_subtype": action["action_subtype"],
                    "args": metadata.get("raw_args") or {},
                }
            )
        if group:
            compressed_group: list[dict[str, Any]] = []
            previous_signature: tuple[str, str] | None = None
            for item in group:
                signature = (str(item.get("action_type") or ""), str(item.get("action_subtype") or ""))
                if signature == previous_signature:
                    continue
                compressed_group.append(item)
                previous_signature = signature
            action_groups.append(compressed_group)
    if not action_groups:
        return [], []
    grouped_by_signature: dict[tuple[tuple[str, str], ...], list[list[dict[str, Any]]]] = defaultdict(list)
    for group in action_groups:
        signature = tuple(
            (str(item.get("action_type") or ""), str(item.get("action_subtype") or ""))
            for item in group
        )
        grouped_by_signature[signature].append(group)
    template_group = max(
        grouped_by_signature.values(),
        key=lambda groups: (len(groups), -len(groups[0]), -sum(len(g) for g in groups)),
    )[0]
    steps: list[dict[str, Any]] = []
    schema: dict[str, dict[str, Any]] = {}
    schema_reuse: dict[tuple[str, str, tuple[str, ...]], str] = {}
    param_index = 1
    for step_index, template in enumerate(template_group):
        args_template = dict(template.get("args") or {})
        for key, value in list(args_template.items()):
            observed_values = [
                group[step_index].get("args", {}).get(key)
                for group in action_groups
                if step_index < len(group)
            ]
            values = {json.dumps(item, ensure_ascii=False) for item in observed_values}
            if len(values) > 1 and _should_parameterize_arg(key, observed_values):
                examples = []
                for item in sorted(values):
                    try:
                        examples.append(str(json.loads(item)))
                    except Exception:
                        examples.append(str(item))
                param_type = "path" if _arg_value_family(value) == "file_path" else ("url" if _arg_value_family(value) == "url" else "text")
                reuse_key = (_safe_slug(key), param_type, tuple(examples[:6]))
                param_name = schema_reuse.get(reuse_key, "")
                if not param_name:
                    param_name = f"param_{_safe_slug(key)}_{param_index}"
                    param_index += 1
                    schema_reuse[reuse_key] = param_name
                    schema[param_name] = {
                        "parameter_name": param_name,
                        "type": param_type,
                        "required": True,
                        "default_value": str(value) if value is not None else "",
                        "default_strategy": "use_first_observed",
                        "validation_rule": "",
                        "examples": examples[:6],
                        "aliases": [key],
                    }
                args_template[key] = f"{{{{{param_name}}}}}"
        steps.append(
            {
                "step_id": f"step_{step_index + 1}",
                "type": template["action_type"],
                "subtype": template["action_subtype"],
                "description": f"{template['tool_name']} via learned pattern",
                "enabled": True,
                "requires_llm": False,
                "implementation_kind": "tool_call",
                "implementation_reference": {
                    "tool_name": template["tool_name"],
                    "args_template": args_template,
                },
                "failure_policy": "fail",
            }
        )
    return steps, list(schema.values())


def _sanitize_skill_name(name: str) -> str:
    text = _normalize_whitespace(name)
    text = re.sub(r"[\r\n\t]+", " ", text)
    text = re.sub(r"\s{2,}", " ", text).strip(" .,:;|-_")
    if not text:
        return "学习技能"
    if len(text) > 24:
        text = text[:24].rstrip(" .,:;|-_")
    return text or "学习技能"


def _sanitize_skill_description(description: str) -> str:
    text = _normalize_whitespace(description)
    text = re.sub(r"[\r\n\t]+", " ", text).strip()
    if len(text) > 120:
        text = text[:120].rstrip(" .,:;|-_")
    return text or "从重复行为中学到的自动技能。"


def _looks_like_generated_skill_name(name: str) -> bool:
    text = str(name or "").strip()
    if not text:
        return True
    if re.fullmatch(r"[a-z0-9]+(?:_[a-z0-9]+){2,}(?:_[0-9]{4})?", text):
        return True
    return text.startswith("skill_")


async def _unique_skill_name(conn: aiosqlite.Connection, preferred_name: str, *, skill_id: str = "") -> str:
    base = str(preferred_name or "").strip() or "学习技能"
    candidate = base
    counter = 2
    while True:
        if skill_id:
            cursor = await conn.execute(
                "SELECT skill_id FROM learned_skills WHERE name = ? AND skill_id != ?",
                (candidate, skill_id),
            )
            row = await cursor.fetchone()
        else:
            cursor = await conn.execute(
                "SELECT skill_id FROM learned_skills WHERE name = ?",
                (candidate,),
            )
            row = await cursor.fetchone()
        if row is None:
            return candidate
        candidate = f"{base} {counter}"
        counter += 1


async def _generate_skill_identity_with_llm(
    *,
    pattern_id: str,
    prototype: dict[str, Any],
    turn_examples: list[dict[str, Any]],
    skill_type: str,
    current_name: str = "",
    current_description: str = "",
) -> tuple[str, str]:
    example_messages = [
        _truncate_text(str(turn.get("user_message") or ""), 120)
        for turn in turn_examples
        if str(turn.get("user_message") or "").strip()
    ][:5]
    payload = {
        "pattern_id": pattern_id,
        "skill_type": skill_type,
        "prototype": prototype,
        "example_requests": example_messages,
        "current_name": current_name,
        "current_description": current_description,
    }
    prompt = f"""You are naming a learned automation skill for end users.

Return JSON with:
- name: a short user-facing skill name in Chinese, ideally 4-12 characters, natural and concrete.
- description: one short Chinese sentence describing what this skill can do for the user.

Requirements:
- Do not output internal taxonomy labels, snake_case, tool names, URLs, IDs, random numbers, or implementation details.
- Do not use generic filler like "技能", "任务", "自动化流程" unless absolutely necessary.
- Prefer what the user would recognize as the task itself.
- If there are concrete entities in the examples, you may use them when they make the skill clearer.
- Keep the name concise and natural.

Context JSON:
{json.dumps(payload, ensure_ascii=False, indent=2)}
"""
    result = await _call_llm_json(prompt, caller="skill_namer")
    proposed_name = _sanitize_skill_name(str(result.get("name") or ""))
    proposed_description = _sanitize_skill_description(str(result.get("description") or ""))
    return proposed_name, proposed_description


async def _refresh_generated_skill_names_with_llm() -> None:
    async with _conn() as conn:
        cursor = await conn.execute(
            "SELECT skill_id, name, description, pattern_id, skill_type, trigger_json FROM learned_skills ORDER BY created_at ASC"
        )
        rows = await cursor.fetchall()
    updates: list[tuple[str, str, str]] = []
    seen_names = {str(row["name"] or "").strip() for row in rows if str(row["name"] or "").strip()}
    for row in rows:
        current_name = str(row["name"] or "")
        if not _looks_like_generated_skill_name(current_name):
            continue
        trigger = _json_loads(row["trigger_json"], {})
        prototype = (trigger or {}).get("base_fingerprint") or {}
        if not prototype:
            continue
        turn_examples = await _fetch_turn_rows(await _member_turn_ids(str(row["pattern_id"] or "")))
        proposed_name, proposed_description = await _generate_skill_identity_with_llm(
            pattern_id=str(row["pattern_id"] or ""),
            prototype=prototype,
            turn_examples=turn_examples,
            skill_type=str(row["skill_type"] or "draft"),
            current_name=current_name,
            current_description=str(row["description"] or ""),
        )
        unique_name = proposed_name
        suffix = 2
        while unique_name in seen_names - {current_name}:
            unique_name = f"{proposed_name} {suffix}"
            suffix += 1
        seen_names.discard(current_name)
        seen_names.add(unique_name)
        if unique_name != current_name or proposed_description != str(row["description"] or ""):
            updates.append((unique_name, proposed_description, str(row["skill_id"] or "")))
    if not updates:
        return
    async with _conn() as conn:
        now = _now_iso()
        for name, description, skill_id in updates:
            await conn.execute(
                "UPDATE learned_skills SET name = ?, description = ?, updated_at = ? WHERE skill_id = ?",
                (name, description, now, skill_id),
            )
        await conn.commit()


def _skill_trigger_from_prototype(prototype: dict[str, Any], turn_examples: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "intent_types": [str((prototype.get("intent") or {}).get("type") or "")],
        "intent_subtypes": [str((prototype.get("intent") or {}).get("subtype") or "")],
        "object_types": [str((prototype.get("object") or {}).get("type") or "")],
        "object_subtypes": [str((prototype.get("object") or {}).get("subtype") or "")],
        "positive_examples": [_truncate_text(turn.get("user_message") or "", 200) for turn in turn_examples[:6]],
        "negative_examples": [],
        "min_match_score": _ROUTER_JUDGE_THRESHOLD,
        "base_fingerprint": prototype,
    }


async def _skill_definition_from_pattern(pattern_id: str, skill_type: str) -> dict[str, Any]:
    turn_ids = await _member_turn_ids(pattern_id)
    turn_examples = await _fetch_turn_rows(turn_ids)
    async with _conn() as conn:
        cursor = await conn.execute(
            "SELECT prototype_fingerprint, description FROM behavior_patterns WHERE pattern_id = ?",
            (pattern_id,),
        )
        row = await cursor.fetchone()
    prototype = _json_loads(row["prototype_fingerprint"], {}) if row else {}
    steps, input_schema = await _derive_parameter_templates(turn_ids)
    if not input_schema:
        input_schema = [
            {
                "parameter_name": slot.get("name"),
                "type": slot.get("type"),
                "required": bool(slot.get("required")),
                "default_value": slot.get("default_value"),
                "default_strategy": "use_observed_examples",
                "validation_rule": "",
                "examples": slot.get("examples") or [],
                "aliases": slot.get("aliases") or [],
            }
            for slot in prototype.get("parameter_slots") or []
        ]
    trigger = _skill_trigger_from_prototype(prototype, turn_examples)
    fallback_description = str(row["description"] if row else "") or _pattern_description(prototype)
    name, description = await _generate_skill_identity_with_llm(
        pattern_id=pattern_id,
        prototype=prototype,
        turn_examples=turn_examples,
        skill_type=skill_type,
        current_description=fallback_description,
    )
    status = "draft" if skill_type == "draft" else "shadow"
    return {
        "name": name,
        "description": description or fallback_description,
        "status": status,
        "skill_type": skill_type,
        "risk_level": "none",
        "requires_llm": prototype.get("llm_dependency") not in {"low", "none"},
        "trigger": trigger,
        "input_schema": input_schema,
        "parameter_extractor": {
            "mode": "hybrid",
            "rule_list": [
                {"kind": "path"},
                {"kind": "quoted_string"},
                {"kind": "number"},
                {"kind": "date"},
                {"kind": "url"},
            ],
            "llm_fallback": True,
        },
        "steps": steps,
        "guards": {
            "risk_level": "none",
            "required_context": [],
            "forbidden_conditions": [],
            "confidence_threshold": _ROUTER_JUDGE_THRESHOLD,
        },
        "fallback_policy": {
            "on_missing_args": "fallback_to_agent",
            "on_low_confidence": "fallback_to_agent",
            "on_step_failure": "fallback_to_agent",
            "on_user_reject": "fallback_to_agent",
        },
        "tests": [],
        "editable_fields": [
            "trigger",
            "input_schema",
            "parameter_extractor",
            "steps",
            "guards",
            "fallback_policy",
        ],
        "created_from": {
            "pattern_list": [pattern_id],
            "turn_list": turn_ids[:_MAX_PATTERN_EXAMPLES],
            "failure_case_list": [],
        },
    }


def _skill_row_to_definition(row: sqlite3.Row | dict[str, Any]) -> dict[str, Any]:
    data = dict(row)
    return {
        "skill_id": data["skill_id"],
        "name": data["name"],
        "description": data["description"],
        "version": int(data["current_version"]),
        "status": data["status"],
        "skill_type": data["skill_type"],
        "risk_level": data["risk_level"],
        "requires_llm": bool(data["requires_llm"]),
        "trigger": _json_loads(data["trigger_json"], {}),
        "input_schema": _json_loads(data["input_schema_json"], []),
        "parameter_extractor": _json_loads(data["parameter_extractor_json"], {}),
        "steps": _json_loads(data["steps_json"], []),
        "guards": _json_loads(data["guards_json"], {}),
        "fallback_policy": _json_loads(data["fallback_policy_json"], {}),
        "tests": _json_loads(data["tests_json"], []),
        "editable_fields": _json_loads(data["editable_fields_json"], []),
        "created_from": _json_loads(data["created_from_json"], {}),
        "run_statistics": _json_loads(data["run_statistics_json"], {}),
        "pattern_id": data["pattern_id"],
        "created_at": data["created_at"],
        "updated_at": data["updated_at"],
    }


async def _save_skill_version(
    *,
    conn: aiosqlite.Connection,
    skill_id: str,
    version: int,
    parent_version: int | None,
    definition: dict[str, Any],
    change_type: str,
    change_summary: str,
    patch_list: list[dict[str, Any]] | None = None,
    test_result: dict[str, Any] | None = None,
    rollback_target: int | None = None,
) -> None:
    await conn.execute(
        """
        INSERT OR REPLACE INTO learned_skill_versions
        (skill_id, version, parent_version, skill_definition, change_type, change_summary,
         patch_list, created_at, test_result, rollback_target)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            skill_id,
            version,
            parent_version,
            _json_dumps(definition),
            change_type,
            change_summary,
            _json_dumps(patch_list or []),
            _now_iso(),
            _json_dumps(test_result or {}),
            rollback_target,
        ),
    )


async def _insert_replay_tests(conn: aiosqlite.Connection, skill_id: str, turn_ids: list[str], trigger: dict[str, Any]) -> list[str]:
    created_ids: list[str] = []
    now = _now_iso()
    for turn_id in turn_ids[:_MAX_PATTERN_EXAMPLES]:
        test_id = _new_id("replay")
        expected = {
            "trigger": trigger,
            "turn_id": turn_id,
        }
        await conn.execute(
            """
            INSERT OR REPLACE INTO behavior_replay_tests
            (test_id, skill_id, turn_id, test_type, input_payload, expected_payload, last_result, created_at, updated_at)
            VALUES (?, ?, ?, 'regression', ?, ?, '{}', ?, ?)
            """,
            (test_id, skill_id, turn_id, "{}", _json_dumps(expected), now, now),
        )
        created_ids.append(test_id)
    return created_ids


async def _create_skill(pattern_id: str) -> str | None:
    async with _conn() as conn:
        cursor = await conn.execute(
            "SELECT statistics_json FROM behavior_patterns WHERE pattern_id = ?",
            (pattern_id,),
        )
        pattern_row = await cursor.fetchone()
        if pattern_row is None:
            return None
        stats = _json_loads(pattern_row["statistics_json"], {})
        if float(stats.get("effective_count") or 0) < 2:
            return None
        cursor = await conn.execute(
            "SELECT skill_id FROM learned_skills WHERE pattern_id = ?",
            (pattern_id,),
        )
        existing = await cursor.fetchone()
        if existing is not None:
            return str(existing["skill_id"])
        definition = await _skill_definition_from_pattern(pattern_id, "draft")
        definition["name"] = await _unique_skill_name(conn, str(definition.get("name") or "学习技能"))
        skill_id = _new_id("learned_skill")
        now = _now_iso()
        replay_ids = await _insert_replay_tests(conn, skill_id, definition["created_from"]["turn_list"], definition["trigger"])
        definition["tests"] = replay_ids
        await conn.execute(
            """
            INSERT INTO learned_skills
            (skill_id, name, description, current_version, status, skill_type, risk_level, requires_llm,
             trigger_json, input_schema_json, parameter_extractor_json, steps_json, guards_json, fallback_policy_json,
             tests_json, editable_fields_json, created_from_json, run_statistics_json, pattern_id, created_at, updated_at)
            VALUES (?, ?, ?, 1, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                skill_id,
                definition["name"],
                definition["description"],
                definition["status"],
                definition["skill_type"],
                definition["risk_level"],
                1 if definition["requires_llm"] else 0,
                _json_dumps(definition["trigger"]),
                _json_dumps(definition["input_schema"]),
                _json_dumps(definition["parameter_extractor"]),
                _json_dumps(definition["steps"]),
                _json_dumps(definition["guards"]),
                _json_dumps(definition["fallback_policy"]),
                _json_dumps(definition["tests"]),
                _json_dumps(definition["editable_fields"]),
                _json_dumps(definition["created_from"]),
                _json_dumps(_default_skill_stats()),
                pattern_id,
                now,
                now,
            ),
        )
        persisted = {
            "skill_id": skill_id,
            **definition,
            "version": 1,
            "run_statistics": _default_skill_stats(),
            "pattern_id": pattern_id,
            "created_at": now,
            "updated_at": now,
        }
        await _save_skill_version(
            conn=conn,
            skill_id=skill_id,
            version=1,
            parent_version=None,
            definition=persisted,
            change_type="create",
            change_summary="Initial draft learned skill generated from pattern evidence.",
        )
        await conn.commit()
    await _upsert_pattern(pattern_id)
    return skill_id


def _target_skill_type(stats: dict[str, Any], prototype: dict[str, Any]) -> str:
    effective = float(stats.get("effective_count") or 0)
    llm_dependency = str(prototype.get("llm_dependency") or "medium")
    if effective >= 8 and llm_dependency in {"low", "none"}:
        return "deterministic"
    if effective >= 5:
        return "parameterized"
    if effective >= 3:
        return "workflow"
    return "draft"


async def _update_skill_to_type(skill_id: str, target_type: str, reason: str) -> None:
    async with _conn() as conn:
        cursor = await conn.execute(
            "SELECT * FROM learned_skills WHERE skill_id = ?", (skill_id,)
        )
        row = await cursor.fetchone()
        if row is None:
            return
        current = _skill_row_to_definition(row)
        current_type = current["skill_type"]
        if _SKILL_TYPE_ORDER.get(target_type, 0) <= _SKILL_TYPE_ORDER.get(current_type, 0):
            return
        next_version = int(row["current_version"]) + 1
        pattern_id = current["pattern_id"]
        definition = await _skill_definition_from_pattern(pattern_id, target_type)
        definition["status"] = "shadow"
        definition["tests"] = current["tests"]
        persisted = {
            "skill_id": skill_id,
            **definition,
            "version": next_version,
            "run_statistics": current["run_statistics"],
            "pattern_id": pattern_id,
            "created_at": current["created_at"],
            "updated_at": _now_iso(),
        }
        await conn.execute(
            """
            UPDATE learned_skills
            SET name = ?, description = ?, current_version = ?, status = 'shadow', skill_type = ?,
                risk_level = ?, requires_llm = ?, trigger_json = ?, input_schema_json = ?,
                parameter_extractor_json = ?, steps_json = ?, guards_json = ?, fallback_policy_json = ?,
                tests_json = ?, editable_fields_json = ?, created_from_json = ?, updated_at = ?
            WHERE skill_id = ?
            """,
            (
                definition["name"],
                definition["description"],
                next_version,
                target_type,
                definition["risk_level"],
                1 if definition["requires_llm"] else 0,
                _json_dumps(definition["trigger"]),
                _json_dumps(definition["input_schema"]),
                _json_dumps(definition["parameter_extractor"]),
                _json_dumps(definition["steps"]),
                _json_dumps(definition["guards"]),
                _json_dumps(definition["fallback_policy"]),
                _json_dumps(definition["tests"]),
                _json_dumps(definition["editable_fields"]),
                _json_dumps(definition["created_from"]),
                _now_iso(),
                skill_id,
            ),
        )
        await _save_skill_version(
            conn=conn,
            skill_id=skill_id,
            version=next_version,
            parent_version=int(row["current_version"]),
            definition=persisted,
            change_type="promote_type",
            change_summary=reason,
        )
        await conn.commit()


async def _activate_skill(skill_id: str, reason: str) -> None:
    async with _conn() as conn:
        cursor = await conn.execute(
            "SELECT * FROM learned_skills WHERE skill_id = ?", (skill_id,)
        )
        row = await cursor.fetchone()
        if row is None:
            return
        current = _skill_row_to_definition(row)
        if current["status"] == "active":
            return
        next_version = int(row["current_version"]) + 1
        current["status"] = "active"
        current["version"] = next_version
        current["updated_at"] = _now_iso()
        await conn.execute(
            "UPDATE learned_skills SET status = 'active', current_version = ?, updated_at = ? WHERE skill_id = ?",
            (next_version, current["updated_at"], skill_id),
        )
        await _save_skill_version(
            conn=conn,
            skill_id=skill_id,
            version=next_version,
            parent_version=int(row["current_version"]),
            definition=current,
            change_type="activate",
            change_summary=reason,
        )
        await conn.commit()


async def manual_activate_skill(skill_id: str) -> bool:
    async with _conn() as conn:
        cursor = await conn.execute(
            "SELECT * FROM learned_skills WHERE skill_id = ?", (skill_id,)
        )
        row = await cursor.fetchone()
    if row is None:
        return False
    await _activate_skill(skill_id, "Manually activated from evolution UI.")
    return True


async def manual_deprecate_skill(skill_id: str) -> bool:
    async with _conn() as conn:
        cursor = await conn.execute(
            "SELECT * FROM learned_skills WHERE skill_id = ?", (skill_id,)
        )
        row = await cursor.fetchone()
        if row is None:
            return False
        current = _skill_row_to_definition(row)
        next_version = int(row["current_version"]) + 1
        current["status"] = "deprecated"
        current["version"] = next_version
        current["updated_at"] = _now_iso()
        await conn.execute(
            "UPDATE learned_skills SET status = 'deprecated', current_version = ?, updated_at = ? WHERE skill_id = ?",
            (next_version, current["updated_at"], skill_id),
        )
        await _save_skill_version(
            conn=conn,
            skill_id=skill_id,
            version=next_version,
            parent_version=int(row["current_version"]),
            definition=current,
            change_type="deprecate",
            change_summary="Manually deprecated from evolution UI.",
        )
        await conn.commit()
    return True


async def _update_shadow_promotion(skill_id: str) -> None:
    async with _conn() as conn:
        cursor = await conn.execute(
            "SELECT * FROM learned_skills WHERE skill_id = ?", (skill_id,)
        )
        row = await cursor.fetchone()
        if row is None or str(row["status"]) != "shadow":
            return
        stats = _json_loads(row["run_statistics_json"], {})
        shadow_success = int(stats.get("shadow_success") or 0)
        shadow_failure = int(stats.get("shadow_failure") or 0)
        consistency_avg = float(stats.get("consistency_avg") or 0.0)
    if shadow_success >= _SHADOW_SUCCESS_THRESHOLD and shadow_failure <= 1 and consistency_avg >= _SHADOW_CONSISTENCY_THRESHOLD:
        await _activate_skill(skill_id, "Shadow validation passed and skill promoted to active.")


async def _update_skill_run_stats(skill_id: str, *, execution_status: str, consistency_score: float = 0.0) -> None:
    async with _conn() as conn:
        cursor = await conn.execute(
            "SELECT * FROM learned_skills WHERE skill_id = ?", (skill_id,)
        )
        row = await cursor.fetchone()
        if row is None:
            return
        stats = _json_loads(row["run_statistics_json"], _default_skill_stats())
        stats["total_runs"] = int(stats.get("total_runs") or 0) + 1
        stats["last_run_at"] = _now_iso()
        total_runs = stats["total_runs"]
        old_consistency = float(stats.get("consistency_avg") or 0.0)
        stats["consistency_avg"] = round(((old_consistency * (total_runs - 1)) + consistency_score) / total_runs, 4)
        if execution_status == "shadow_success":
            stats["shadow_success"] = int(stats.get("shadow_success") or 0) + 1
        elif execution_status == "shadow_failure":
            stats["shadow_failure"] = int(stats.get("shadow_failure") or 0) + 1
        elif execution_status == "success":
            stats["active_success"] = int(stats.get("active_success") or 0) + 1
        elif execution_status in {"failure", "fallback"}:
            stats["active_failure"] = int(stats.get("active_failure") or 0) + 1
        await conn.execute(
            "UPDATE learned_skills SET run_statistics_json = ?, updated_at = ? WHERE skill_id = ?",
            (_json_dumps(stats), _now_iso(), skill_id),
        )
        await conn.commit()
    await _update_shadow_promotion(skill_id)


async def _create_patch_proposal(skill_id: str, base_version: int, patch_type: str, reason: str, patch_content: dict[str, Any]) -> None:
    async with _conn() as conn:
        await conn.execute(
            """
            INSERT INTO learned_skill_patches
            (patch_id, skill_id, base_version, patch_type, reason, patch_content, risk_assessment, status, created_at)
            VALUES (?, ?, ?, ?, ?, ?, '', 'proposed', ?)
            """,
            (
                _new_id("patch"),
                skill_id,
                base_version,
                patch_type,
                reason,
                _json_dumps(patch_content),
                _now_iso(),
            ),
        )
        await conn.commit()


async def _maybe_propose_patch(skill_id: str, version: int, failure_reason: str) -> None:
    reason = str(failure_reason or "")
    if not reason:
        return
    skill = await get_learned_skill(skill_id)
    if skill is None:
        return
    lowered = reason.lower()
    if "missing" in lowered or "parameter" in lowered or "参数" in reason:
        patch_type = "update_input_schema"
    elif "low_confidence" in lowered or "misfire" in lowered:
        patch_type = "update_trigger"
    else:
        patch_type = "replace_step"
    await _create_patch_proposal(
        skill_id,
        version,
        patch_type,
        reason,
        {
            "failure_reason": reason,
            "change_list": _build_patch_change_list(skill, patch_type, reason),
        },
    )


async def _read_patterns() -> list[dict[str, Any]]:
    async with _conn() as conn:
        cursor = await conn.execute(
            "SELECT * FROM behavior_patterns ORDER BY updated_at DESC"
        )
        rows = await cursor.fetchall()
    patterns: list[dict[str, Any]] = []
    for row in rows:
        item = dict(row)
        item["prototype_fingerprint"] = _json_loads(item.get("prototype_fingerprint"), {})
        item["statistics"] = _json_loads(item.get("statistics_json"), _default_pattern_stats())
        item["skillability"] = _json_loads(item.get("skillability_json"), {})
        item["linked_skill_list"] = _json_loads(item.get("linked_skill_list"), [])
        patterns.append(item)
    return patterns


async def list_patterns(status: str = "all") -> list[dict[str, Any]]:
    patterns = await _read_patterns()
    if status != "all":
        patterns = [item for item in patterns if item.get("status") == status]
    result: list[dict[str, Any]] = []
    for item in patterns:
        prototype = item.get("prototype_fingerprint") or {}
        stats = item.get("statistics") or {}
        result.append(
            {
                "id": item["pattern_id"],
                "description": item.get("description", ""),
                "status": item.get("status", ""),
                "frequency": int(stats.get("frequency") or 0),
                "effective_count": float(stats.get("effective_count") or 0.0),
                "success_rate": float(stats.get("success_rate") or 0.0),
                "action_stability": float(stats.get("action_stability") or 0.0),
                "io_stability": float(stats.get("io_stability") or 0.0),
                "last_seen_at": stats.get("last_seen_at", ""),
                "linked_skill_list": item.get("linked_skill_list") or [],
                "prototype_fingerprint": prototype,
                "action_sequence": prototype.get("action_sequence") or [],
                "skillability": item.get("skillability") or {},
            }
        )
    return result


async def list_learned_skills() -> list[dict[str, Any]]:
    async with _conn() as conn:
        cursor = await conn.execute(
            "SELECT * FROM learned_skills ORDER BY updated_at DESC"
        )
        rows = await cursor.fetchall()
    skills: list[dict[str, Any]] = []
    for row in rows:
        definition = _skill_row_to_definition(row)
        trigger = definition["trigger"]
        stats = definition["run_statistics"]
        skills.append(
            {
                "id": definition["skill_id"],
                "name": definition["name"],
                "description": definition["description"],
                "status": definition["status"],
                "skill_type": definition["skill_type"],
                "version": definition["version"],
                "pattern_id": definition["pattern_id"],
                "requires_llm": definition["requires_llm"],
                "trigger": trigger,
                "input_schema": definition["input_schema"],
                "steps": definition["steps"],
                "run_statistics": stats,
                "updated_at": definition["updated_at"],
                "created_at": definition["created_at"],
                "positive_examples": trigger.get("positive_examples") or [],
                "min_match_score": trigger.get("min_match_score", _ROUTER_JUDGE_THRESHOLD),
            }
        )
    return skills


async def get_learned_skill(skill_id: str) -> dict[str, Any] | None:
    async with _conn() as conn:
        cursor = await conn.execute(
            "SELECT * FROM learned_skills WHERE skill_id = ?", (skill_id,)
        )
        row = await cursor.fetchone()
    return _skill_row_to_definition(row) if row is not None else None


async def list_learned_skill_versions(skill_id: str) -> list[dict[str, Any]]:
    async with _conn() as conn:
        cursor = await conn.execute(
            """
            SELECT skill_id, version, parent_version, change_type, change_summary, patch_list, created_at,
                   test_result, rollback_target
            FROM learned_skill_versions
            WHERE skill_id = ?
            ORDER BY version DESC
            """,
            (skill_id,),
        )
        rows = await cursor.fetchall()
    return [
        {
            "skill_id": str(row["skill_id"]),
            "version": int(row["version"]),
            "parent_version": int(row["parent_version"]) if row["parent_version"] is not None else None,
            "change_type": str(row["change_type"] or ""),
            "change_summary": str(row["change_summary"] or ""),
            "patch_list": _json_loads(row["patch_list"], []),
            "created_at": str(row["created_at"] or ""),
            "test_result": _json_loads(row["test_result"], {}),
            "rollback_target": int(row["rollback_target"]) if row["rollback_target"] is not None else None,
        }
        for row in rows
    ]


async def list_learned_skill_patches(skill_id: str, status: str = "all") -> list[dict[str, Any]]:
    async with _conn() as conn:
        if status == "all":
            cursor = await conn.execute(
                """
                SELECT *
                FROM learned_skill_patches
                WHERE skill_id = ?
                ORDER BY created_at DESC
                """,
                (skill_id,),
            )
            rows = await cursor.fetchall()
        else:
            cursor = await conn.execute(
                """
                SELECT *
                FROM learned_skill_patches
                WHERE skill_id = ? AND status = ?
                ORDER BY created_at DESC
                """,
                (skill_id, status),
            )
            rows = await cursor.fetchall()
    return [
        {
            "patch_id": str(row["patch_id"]),
            "skill_id": str(row["skill_id"]),
            "base_version": int(row["base_version"]),
            "patch_type": str(row["patch_type"] or ""),
            "reason": str(row["reason"] or ""),
            "patch_content": _json_loads(row["patch_content"], {}),
            "risk_assessment": str(row["risk_assessment"] or ""),
            "status": str(row["status"] or ""),
            "created_at": str(row["created_at"] or ""),
        }
        for row in rows
    ]


async def list_learned_skill_runs(skill_id: str, limit: int = 50) -> list[dict[str, Any]]:
    async with _conn() as conn:
        cursor = await conn.execute(
            """
            SELECT *
            FROM learned_skill_runs
            WHERE skill_id = ?
            ORDER BY created_at DESC
            LIMIT ?
            """,
            (skill_id, max(1, int(limit))),
        )
        rows = await cursor.fetchall()
    return [dict(row) for row in rows]


async def list_skill_replay_tests(skill_id: str) -> list[dict[str, Any]]:
    return await _replay_tests_for_skill(skill_id)


async def vocabulary_snapshot() -> dict[str, Any]:
    async with _conn() as conn:
        cursor = await conn.execute(
            """
            SELECT label_type, canonical_label, domain, parent_label, raw_description, status, updated_at
            FROM behavior_vocabulary_labels
            ORDER BY label_type ASC, canonical_label ASC
            """
        )
        label_rows = await cursor.fetchall()
        cursor = await conn.execute(
            """
            SELECT label_type, canonical_label, alias_label, vocabulary_version, created_at
            FROM behavior_vocabulary_aliases
            ORDER BY label_type ASC, canonical_label ASC, alias_label ASC
            """
        )
        alias_rows = await cursor.fetchall()
        cursor = await conn.execute(
            """
            SELECT *
            FROM behavior_unknown_labels
            ORDER BY seen_count DESC, updated_at DESC
            """
        )
        unknown_rows = await cursor.fetchall()
    return {
        "labels": [dict(row) for row in label_rows],
        "aliases": [dict(row) for row in alias_rows],
        "unknown_labels": [
            {
                **dict(row),
                "example_turns": _json_loads(row["example_turns"], []),
            }
            for row in unknown_rows
        ],
        "vocabulary_version": _VOCABULARY_VERSION,
    }


async def create_vocabulary_label(
    *,
    label_type: str,
    canonical_label: str,
    domain: str = "",
    parent_label: str = "",
    raw_description: str = "",
    status: str = "active",
) -> dict[str, Any]:
    normalized_type = _safe_slug(label_type)
    normalized_label = _safe_slug(canonical_label)
    if not normalized_type or not normalized_label:
        raise ValueError("label_type and canonical_label are required")
    now = _now_iso()
    label_id = f"{normalized_type}:{normalized_label}"
    async with _conn() as conn:
        await conn.execute(
            """
            INSERT OR REPLACE INTO behavior_vocabulary_labels
            (label_id, label_type, canonical_label, domain, parent_label, raw_description, status, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, COALESCE((SELECT created_at FROM behavior_vocabulary_labels WHERE label_id = ?), ?), ?)
            """,
            (
                label_id,
                normalized_type,
                normalized_label,
                _safe_slug(domain, default=""),
                _safe_slug(parent_label, default=""),
                _normalize_whitespace(raw_description),
                _safe_slug(status),
                label_id,
                now,
                now,
            ),
        )
        await conn.commit()
    return {
        "label_id": label_id,
        "label_type": normalized_type,
        "canonical_label": normalized_label,
    }


async def create_vocabulary_alias(*, label_type: str, canonical_label: str, alias_label: str) -> dict[str, Any]:
    normalized_type = _safe_slug(label_type)
    normalized_canonical = _safe_slug(canonical_label)
    normalized_alias = _safe_slug(alias_label)
    if not normalized_type or not normalized_canonical or not normalized_alias:
        raise ValueError("label_type, canonical_label, and alias_label are required")
    now = _now_iso()
    alias_id = f"alias:{normalized_type}:{normalized_alias}"
    async with _conn() as conn:
        await conn.execute(
            """
            INSERT OR REPLACE INTO behavior_vocabulary_aliases
            (alias_id, label_type, canonical_label, alias_label, created_at, vocabulary_version)
            VALUES (?, ?, ?, ?, COALESCE((SELECT created_at FROM behavior_vocabulary_aliases WHERE alias_id = ?), ?), ?)
            """,
            (
                alias_id,
                normalized_type,
                normalized_canonical,
                normalized_alias,
                alias_id,
                now,
                _VOCABULARY_VERSION,
            ),
        )
        await conn.commit()
    return {
        "alias_id": alias_id,
        "label_type": normalized_type,
        "canonical_label": normalized_canonical,
        "alias_label": normalized_alias,
    }


async def promote_unknown_label(unknown_id: str, *, canonical_label: str = "", alias_label: str = "") -> dict[str, Any]:
    async with _conn() as conn:
        cursor = await conn.execute(
            "SELECT * FROM behavior_unknown_labels WHERE unknown_id = ?",
            (unknown_id,),
        )
        row = await cursor.fetchone()
        if row is None:
            raise ValueError("unknown label not found")
        label_type = _safe_slug(str(row["label_type"] or "unknown"))
        proposed = _safe_slug(
            canonical_label
            or row["proposed_subtype"]
            or row["proposed_type"]
            or row["proposed_domain"]
            or row["raw_description"]
        )
        if not proposed:
            raise ValueError("canonical label is required")
        await conn.execute(
            """
            INSERT OR IGNORE INTO behavior_vocabulary_labels
            (label_id, label_type, canonical_label, domain, parent_label, raw_description, status, created_at, updated_at)
            VALUES (?, ?, ?, '', '', ?, 'active', ?, ?)
            """,
            (f"{label_type}:{proposed}", label_type, proposed, str(row["raw_description"] or ""), _now_iso(), _now_iso()),
        )
        alias_source = _safe_slug(alias_label or str(row["raw_description"] or ""))
        if alias_source:
            await conn.execute(
                """
                INSERT OR REPLACE INTO behavior_vocabulary_aliases
                (alias_id, label_type, canonical_label, alias_label, created_at, vocabulary_version)
                VALUES (?, ?, ?, ?, COALESCE((SELECT created_at FROM behavior_vocabulary_aliases WHERE alias_id = ?), ?), ?)
                """,
                (
                    f"alias:{label_type}:{alias_source}",
                    label_type,
                    proposed,
                    alias_source,
                    f"alias:{label_type}:{alias_source}",
                    _now_iso(),
                    _VOCABULARY_VERSION,
                ),
            )
        await conn.execute(
            "UPDATE behavior_unknown_labels SET status = 'promoted', updated_at = ? WHERE unknown_id = ?",
            (_now_iso(), unknown_id),
        )
        await conn.commit()
    return {
        "unknown_id": unknown_id,
        "label_type": label_type,
        "canonical_label": proposed,
        "alias_label": alias_source,
    }


async def dismiss_unknown_label(unknown_id: str) -> bool:
    async with _conn() as conn:
        cursor = await conn.execute(
            "SELECT unknown_id FROM behavior_unknown_labels WHERE unknown_id = ?",
            (unknown_id,),
        )
        row = await cursor.fetchone()
        if row is None:
            return False
        await conn.execute(
            "UPDATE behavior_unknown_labels SET status = 'dismissed', updated_at = ? WHERE unknown_id = ?",
            (_now_iso(), unknown_id),
        )
        await conn.commit()
    return True


def _clone_json_value(value: Any) -> Any:
    return json.loads(json.dumps(value, ensure_ascii=False))


def _path_parts(target_path: str) -> list[str | int]:
    parts: list[str | int] = []
    for raw in str(target_path or "").split("."):
        raw = raw.strip()
        if not raw:
            continue
        parts.append(int(raw) if raw.isdigit() else raw)
    return parts


def _walk_to_parent(root: Any, parts: list[str | int], *, create: bool = False) -> tuple[Any, str | int | None]:
    if not parts:
        return root, None
    current = root
    for index, part in enumerate(parts[:-1]):
        next_part = parts[index + 1]
        if isinstance(part, int):
            if not isinstance(current, list):
                raise KeyError(f"Path segment {part} requires list container")
            while create and part >= len(current):
                current.append({} if not isinstance(next_part, int) else [])
            current = current[part]
            continue
        if not isinstance(current, dict):
            raise KeyError(f"Path segment {part} requires dict container")
        if part not in current or current[part] is None:
            if not create:
                raise KeyError(part)
            current[part] = [] if isinstance(next_part, int) else {}
        current = current[part]
    return current, parts[-1]


def _set_path_value(root: Any, target_path: str, value: Any, *, create: bool = True) -> None:
    parent, leaf = _walk_to_parent(root, _path_parts(target_path), create=create)
    if leaf is None:
        raise KeyError("empty target path")
    if isinstance(leaf, int):
        if not isinstance(parent, list):
            raise KeyError(f"Leaf {leaf} requires list container")
        while create and leaf >= len(parent):
            parent.append(None)
        parent[leaf] = value
        return
    if not isinstance(parent, dict):
        raise KeyError(f"Leaf {leaf} requires dict container")
    parent[leaf] = value


def _remove_path_value(root: Any, target_path: str) -> None:
    parent, leaf = _walk_to_parent(root, _path_parts(target_path), create=False)
    if leaf is None:
        raise KeyError("empty target path")
    if isinstance(leaf, int):
        if not isinstance(parent, list):
            raise KeyError(f"Leaf {leaf} requires list container")
        parent.pop(leaf)
        return
    if not isinstance(parent, dict):
        raise KeyError(f"Leaf {leaf} requires dict container")
    parent.pop(leaf, None)


def _build_patch_change_list(skill: dict[str, Any], patch_type: str, reason: str, extra: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    extra = extra or {}
    if patch_type == "update_trigger":
        current_score = float((skill.get("trigger") or {}).get("min_match_score") or _ROUTER_JUDGE_THRESHOLD)
        next_score = round(min(0.95, current_score + 0.05), 2)
        if next_score != current_score:
            return [
                {
                    "operation": "replace",
                    "target_path": "trigger.min_match_score",
                    "old_value": current_score,
                    "new_value": next_score,
                }
            ]
    if patch_type == "update_input_schema":
        current_policy = str((skill.get("fallback_policy") or {}).get("on_missing_args") or "fallback_to_agent")
        if current_policy != "ask_user":
            return [
                {
                    "operation": "replace",
                    "target_path": "fallback_policy.on_missing_args",
                    "old_value": current_policy,
                    "new_value": "ask_user",
                }
            ]
    if patch_type == "replace_step":
        failing_tool = ""
        match = re.search(r"([A-Za-z_][A-Za-z0-9_]*)", reason or "")
        if match:
            failing_tool = match.group(1)
        for index, step in enumerate(skill.get("steps") or []):
            reference = step.get("implementation_reference") or {}
            if failing_tool and str(reference.get("tool_name") or "") != failing_tool:
                continue
            current_policy = str(step.get("failure_policy") or "fail")
            if current_policy != "fallback_to_agent":
                return [
                    {
                        "operation": "replace",
                        "target_path": f"steps.{index}.failure_policy",
                        "old_value": current_policy,
                        "new_value": "fallback_to_agent",
                    }
                ]
            break
    return extra.get("change_list") or []


def _apply_change_list(definition: dict[str, Any], change_list: list[dict[str, Any]]) -> list[dict[str, Any]]:
    applied: list[dict[str, Any]] = []
    for change in change_list:
        operation = str(change.get("operation") or "replace")
        target_path = str(change.get("target_path") or "").strip()
        if not target_path:
            continue
        if operation in {"add", "replace", "enable", "disable"}:
            new_value = change.get("new_value")
            if operation == "enable":
                new_value = True
            elif operation == "disable":
                new_value = False
            _set_path_value(definition, target_path, _clone_json_value(new_value), create=True)
        elif operation == "remove":
            _remove_path_value(definition, target_path)
        else:
            continue
        applied.append(change)
    return applied


async def _sanitize_skill_definition(definition: dict[str, Any]) -> dict[str, Any]:
    sanitized = _clone_json_value(definition)
    if isinstance(sanitized.get("trigger"), dict):
        base_fp = (sanitized["trigger"] or {}).get("base_fingerprint")
        if isinstance(base_fp, dict):
            sanitized["trigger"]["base_fingerprint"] = await normalize_fingerprint(base_fp)
    if isinstance(sanitized.get("input_schema"), list):
        sanitized["input_schema"] = [
            _normalize_slot(item)
            for item in sanitized["input_schema"]
            if isinstance(item, dict)
        ]
    for key in ("parameter_extractor", "guards", "fallback_policy", "created_from", "run_statistics"):
        if not isinstance(sanitized.get(key), dict):
            sanitized[key] = {}
    for key in ("steps", "tests", "editable_fields"):
        if not isinstance(sanitized.get(key), list):
            sanitized[key] = []
    return sanitized


async def _persist_skill_version(
    conn: aiosqlite.Connection,
    *,
    skill_id: str,
    current_row: sqlite3.Row,
    definition: dict[str, Any],
    change_type: str,
    change_summary: str,
    patch_list: list[dict[str, Any]] | None = None,
    test_result: dict[str, Any] | None = None,
    rollback_target: int | None = None,
) -> dict[str, Any]:
    now = _now_iso()
    next_version = int(current_row["current_version"]) + 1
    persisted = {
        "skill_id": skill_id,
        **definition,
        "version": next_version,
        "pattern_id": definition.get("pattern_id") or str(current_row["pattern_id"] or ""),
        "created_at": definition.get("created_at") or str(current_row["created_at"] or now),
        "updated_at": now,
        "run_statistics": definition.get("run_statistics") or _json_loads(
            current_row["run_statistics_json"], _default_skill_stats()
        ),
    }
    await conn.execute(
        """
        UPDATE learned_skills
        SET name = ?, description = ?, current_version = ?, status = ?, skill_type = ?, risk_level = ?,
            requires_llm = ?, trigger_json = ?, input_schema_json = ?, parameter_extractor_json = ?,
            steps_json = ?, guards_json = ?, fallback_policy_json = ?, tests_json = ?, editable_fields_json = ?,
            created_from_json = ?, run_statistics_json = ?, updated_at = ?
        WHERE skill_id = ?
        """,
        (
            str(persisted.get("name") or ""),
            str(persisted.get("description") or ""),
            next_version,
            str(persisted.get("status") or "draft"),
            str(persisted.get("skill_type") or "draft"),
            str(persisted.get("risk_level") or "none"),
            1 if bool(persisted.get("requires_llm")) else 0,
            _json_dumps(persisted.get("trigger") or {}),
            _json_dumps(persisted.get("input_schema") or []),
            _json_dumps(persisted.get("parameter_extractor") or {}),
            _json_dumps(persisted.get("steps") or []),
            _json_dumps(persisted.get("guards") or {}),
            _json_dumps(persisted.get("fallback_policy") or {}),
            _json_dumps(persisted.get("tests") or []),
            _json_dumps(persisted.get("editable_fields") or []),
            _json_dumps(persisted.get("created_from") or {}),
            _json_dumps(persisted.get("run_statistics") or _default_skill_stats()),
            now,
            skill_id,
        ),
    )
    await _save_skill_version(
        conn=conn,
        skill_id=skill_id,
        version=next_version,
        parent_version=int(current_row["current_version"]),
        definition=persisted,
        change_type=change_type,
        change_summary=change_summary,
        patch_list=patch_list,
        test_result=test_result,
        rollback_target=rollback_target,
    )
    return persisted


def _extract_with_rules(user_message: str, schema_item: dict[str, Any]) -> tuple[Any, float]:
    text = str(user_message or "")
    aliases = [str(item).lower() for item in (schema_item.get("aliases") or []) if str(item).strip()]
    schema_type = str(schema_item.get("type") or "text")
    examples = [str(item) for item in (schema_item.get("examples") or [])]
    if examples:
        for example in examples:
            if example and example in text:
                return example, 0.95
    if schema_type in {"path", "file", "filepath"} or any(alias in {"path", "file", "file_path"} for alias in aliases):
        match = re.search(r"(~?/?[A-Za-z0-9_.-][A-Za-z0-9_./-]*\.[A-Za-z0-9]{1,8}|~?/?[A-Za-z0-9_.-][A-Za-z0-9_./-]*/[A-Za-z0-9_./-]+)", text)
        if match:
            return match.group(1), 0.85
    if schema_type in {"number", "int", "float"}:
        match = re.search(r"-?\d+(?:\.\d+)?", text)
        if match:
            raw = match.group(0)
            return (float(raw) if "." in raw else int(raw)), 0.80
    if schema_type == "date":
        match = re.search(r"\b\d{4}-\d{2}-\d{2}\b", text)
        if match:
            return match.group(0), 0.90
    if schema_type == "url":
        match = re.search(r"https?://\S+", text)
        if match:
            return match.group(0), 0.90
    quoted = re.findall(r'"([^"]+)"|\'([^\']+)\'', text)
    if quoted:
        first = next((item[0] or item[1] for item in quoted if item[0] or item[1]), "")
        if first:
            return first, 0.65
    return None, 0.0


async def _extract_with_llm(
    *,
    user_message: str,
    context_summary: str,
    input_schema: list[dict[str, Any]],
    partial_params: dict[str, Any],
) -> dict[str, Any]:
    prompt = f"""Extract parameters for a learned automation skill.

Return JSON only:
{{"params": {{"name": "value"}}}}

User message:
{user_message}

Context summary:
{context_summary}

Input schema:
{json.dumps(input_schema, ensure_ascii=False, indent=2)}

Already extracted params:
{json.dumps(partial_params, ensure_ascii=False, indent=2)}
"""
    result = await _call_llm_json(prompt, caller="skill_param_extractor")
    params = result.get("params")
    return params if isinstance(params, dict) else {}


async def extract_skill_parameters(
    *,
    user_message: str,
    context_summary: str,
    input_schema: list[dict[str, Any]],
    llm_fallback: bool = True,
    overrides: dict[str, Any] | None = None,
) -> dict[str, Any]:
    params: dict[str, Any] = {}
    confidence_scores: list[float] = []
    overrides = overrides or {}
    for item in input_schema:
        name = str(item.get("parameter_name") or item.get("name") or "").strip()
        if not name:
            continue
        if name in overrides:
            params[name] = overrides[name]
            confidence_scores.append(1.0)
            continue
        value, score = _extract_with_rules(user_message, item)
        if value is not None:
            params[name] = value
            confidence_scores.append(score)
            continue
        default_value = item.get("default_value")
        if default_value not in (None, "") and not item.get("required", False):
            params[name] = default_value
            confidence_scores.append(0.55)
    missing_required = [
        str(item.get("parameter_name") or item.get("name") or "")
        for item in input_schema
        if bool(item.get("required", False))
        and str(item.get("parameter_name") or item.get("name") or "")
        and str(item.get("parameter_name") or item.get("name") or "") not in params
    ]
    if missing_required and llm_fallback:
        llm_params = await _extract_with_llm(
            user_message=user_message,
            context_summary=context_summary,
            input_schema=input_schema,
            partial_params=params,
        )
        for key, value in llm_params.items():
            if key not in params and value not in (None, ""):
                params[key] = value
                confidence_scores.append(0.70)
        missing_required = [item for item in missing_required if item not in params]
    confidence = round(sum(confidence_scores) / len(confidence_scores), 4) if confidence_scores else 0.0
    return {
        "params": params,
        "missing_required": missing_required,
        "complete": not missing_required,
        "confidence": confidence,
    }


def _resolve_value_template(value: Any, params: dict[str, Any]) -> Any:
    if isinstance(value, str):
        resolved = value
        for key, param in params.items():
            resolved = resolved.replace(f"{{{{{key}}}}}", str(param))
        return resolved
    if isinstance(value, list):
        return [_resolve_value_template(item, params) for item in value]
    if isinstance(value, dict):
        return {key: _resolve_value_template(item, params) for key, item in value.items()}
    return value


async def _llm_confirm_skill_match(request_fp: dict[str, Any], skill: dict[str, Any], similarity: dict[str, Any]) -> bool:
    prompt = f"""Decide whether a learned automation skill should handle a new request.

Return JSON only:
{{"should_use": true|false, "confidence": 0-1, "reason": "..."}}

Request fingerprint:
{json.dumps(request_fp, ensure_ascii=False, indent=2)}

Skill definition:
{json.dumps({"name": skill["name"], "trigger": skill["trigger"], "steps": skill["steps"]}, ensure_ascii=False, indent=2)}

Similarity:
{json.dumps(similarity, ensure_ascii=False, indent=2)}
"""
    result = await _call_llm_json(prompt, caller="skill_match_judge")
    return bool(result.get("should_use"))


async def match_active_skill(user_message: str, history: list[dict[str, Any]]) -> dict[str, Any] | None:
    async with _conn() as conn:
        cursor = await conn.execute(
            "SELECT * FROM learned_skills WHERE status = 'active' ORDER BY updated_at DESC"
        )
        rows = await cursor.fetchall()
    if not rows:
        return None
    request_fp = await build_request_fingerprint(user_message, history)
    best: dict[str, Any] | None = None
    for row in rows:
        skill = _skill_row_to_definition(row)
        trigger = skill["trigger"]
        base_fp = trigger.get("base_fingerprint") or {}
        similarity = compute_fingerprint_similarity(request_fp, base_fp)
        min_score = float(trigger.get("min_match_score") or _ROUTER_JUDGE_THRESHOLD)
        if similarity["hard_fail"]:
            continue
        if best is None or float(similarity["total"]) > float(best["similarity"]["total"]):
            best = {
                "skill": skill,
                "request_fingerprint": request_fp,
                "similarity": similarity,
                "min_score": min_score,
            }
    if best is None:
        return None
    total = float(best["similarity"]["total"])
    if total >= max(_ROUTER_AUTO_THRESHOLD, best["min_score"]):
        return best
    if total >= max(_ROUTER_JUDGE_THRESHOLD, best["min_score"]):
        if await _llm_confirm_skill_match(best["request_fingerprint"], best["skill"], best["similarity"]):
            return best
    return None


async def try_route_and_execute_skill(
    *,
    user_message: str,
    visible_user_entry: dict[str, Any],
    llm_user_entry: dict[str, Any],
    history: list[dict[str, Any]],
    bot: Any,
    chat_id: int,
    db_path: str,
    effective_system: str,
    client_request_id: str,
    round_id: str,
) -> dict[str, Any] | None:
    match = await match_active_skill(user_message, history)
    if match is None:
        return None
    skill = match["skill"]
    similarity = match["similarity"]
    input_schema = skill["input_schema"]
    current_turn = _current_turn_id.get()
    async with _conn() as conn:
        cursor = await conn.execute(
            "SELECT context_summary FROM behavior_turns WHERE turn_id = ?",
            (current_turn,),
        )
        turn_row = await cursor.fetchone()
    context_summary = str(turn_row["context_summary"] or "") if turn_row else ""
    extraction = await extract_skill_parameters(
        user_message=user_message,
        context_summary=context_summary,
        input_schema=input_schema,
        llm_fallback=bool((skill.get("parameter_extractor") or {}).get("llm_fallback", True)),
    )
    if not extraction["complete"]:
        run_id = _new_id("skill_run")
        async with _conn() as conn:
            await conn.execute(
                """
                INSERT INTO learned_skill_runs
                (run_id, skill_id, version, turn_id, match_score, parameter_status, execution_status, failure_reason,
                 fallback_used, user_feedback, dry_run, consistency_score, created_at)
                VALUES (?, ?, ?, ?, ?, ?, 'fallback', ?, 1, '', 0, 0, ?)
                """,
                (
                    run_id,
                    skill["skill_id"],
                    skill["version"],
                    current_turn,
                    float(similarity["total"]),
                    "missing_required",
                    f"missing parameters: {', '.join(extraction['missing_required'])}",
                    _now_iso(),
                ),
            )
            await conn.commit()
        await _update_skill_run_stats(skill["skill_id"], execution_status="fallback")
        await _maybe_propose_patch(skill["skill_id"], int(skill["version"]), "missing_required_parameters")
        return None
    params = extraction["params"]
    await mark_turn_skill_routed(skill["skill_id"])
    from cyrene.agent.guidance import _final_user_reply_from_history
    from cyrene.agent.message import _apply_assistant_meta
    from cyrene.tools import _execute_tool

    messages: list[dict[str, Any]] = [
        {"role": "system", "content": effective_system},
        *history,
        dict(llm_user_entry),
    ]
    tool_calls: list[dict[str, Any]] = []
    for step in skill["steps"]:
        if not bool(step.get("enabled", True)):
            continue
        reference = step.get("implementation_reference") or {}
        if str(step.get("implementation_kind") or "") != "tool_call":
            continue
        tool_name = str(reference.get("tool_name") or "")
        args_template = reference.get("args_template") or {}
        call_id = _new_id("tc")
        resolved_args = _resolve_value_template(args_template, params)
        tool_calls.append(
            {
                "id": call_id,
                "type": "function",
                "function": {
                    "name": tool_name,
                    "arguments": _json_dumps(resolved_args),
                },
            }
        )
    assistant_entry = {
        "role": "assistant",
        "content": f"Using learned skill `{skill['name']}`.",
        "tool_calls": tool_calls,
    }
    if round_id:
        assistant_entry["round_id"] = round_id
    messages.append(_apply_assistant_meta(assistant_entry))
    for call, step in zip(tool_calls, [step for step in skill["steps"] if bool(step.get("enabled", True)) and str((step.get("implementation_reference") or {}).get("tool_name") or "")]):
        tool_name = str(call["function"]["name"])
        try:
            resolved_args = json.loads(call["function"]["arguments"])
            result = await _execute_tool(tool_name, resolved_args, bot, chat_id, db_path, None)
            tool_success = not str(result).lower().startswith("tool failed:")
            failure_reason = "" if tool_success else str(result)
        except Exception as exc:
            result = f"Tool failed: {exc}"
            tool_success = False
            failure_reason = str(exc)
        tool_entry = {"role": "tool", "tool_call_id": call["id"], "content": _truncate_text(result, 6000)}
        if round_id:
            tool_entry["round_id"] = round_id
        messages.append(tool_entry)
        if not tool_success:
            run_id = _new_id("skill_run")
            async with _conn() as conn:
                await conn.execute(
                    """
                    INSERT INTO learned_skill_runs
                    (run_id, skill_id, version, turn_id, match_score, parameter_status, execution_status, failure_reason,
                     fallback_used, user_feedback, dry_run, consistency_score, created_at)
                    VALUES (?, ?, ?, ?, ?, 'complete', 'failure', ?, 1, '', 0, 0, ?)
                    """,
                    (
                        run_id,
                        skill["skill_id"],
                        skill["version"],
                        current_turn,
                        float(similarity["total"]),
                        failure_reason or f"{tool_name} failed",
                        _now_iso(),
                    ),
                )
                await conn.commit()
            await _update_skill_run_stats(skill["skill_id"], execution_status="failure")
            await _maybe_propose_patch(skill["skill_id"], int(skill["version"]), failure_reason or f"{tool_name}_failed")
            return None
    final_text = await _final_user_reply_from_history(messages, max_tokens=None)
    final_entry = {"role": "assistant", "content": final_text}
    if client_request_id:
        final_entry["client_request_id"] = client_request_id
    if round_id:
        final_entry["round_id"] = round_id
    messages.append(_apply_assistant_meta(final_entry))
    run_id = _new_id("skill_run")
    async with _conn() as conn:
        await conn.execute(
            """
            INSERT INTO learned_skill_runs
            (run_id, skill_id, version, turn_id, match_score, parameter_status, execution_status, failure_reason,
             fallback_used, user_feedback, dry_run, consistency_score, created_at)
            VALUES (?, ?, ?, ?, ?, 'complete', 'success', '', 0, '', 0, ?, ?)
            """,
            (
                run_id,
                skill["skill_id"],
                skill["version"],
                current_turn,
                float(similarity["total"]),
                round(extraction["confidence"], 4),
                _now_iso(),
            ),
        )
        await conn.commit()
    await _update_skill_run_stats(skill["skill_id"], execution_status="success", consistency_score=round(extraction["confidence"], 4))
    return {
        "skill": skill,
        "messages": messages,
        "final_text": final_text,
        "match_score": similarity["total"],
    }


async def _validate_shadow_skill_for_turn(skill: dict[str, Any], turn_row: dict[str, Any], fingerprint: dict[str, Any]) -> None:
    trigger = skill["trigger"]
    similarity = compute_fingerprint_similarity(fingerprint, trigger.get("base_fingerprint") or {})
    if similarity["hard_fail"] or float(similarity["total"]) < max(_ROUTER_JUDGE_THRESHOLD, float(trigger.get("min_match_score") or 0.0)):
        return
    step_actions = []
    for step in skill["steps"]:
        if not bool(step.get("enabled", True)):
            continue
        step_actions.append(
            {
                "domain": str((trigger.get("base_fingerprint") or {}).get("domain") or "state_management"),
                "type": str(step.get("type") or "call_tool"),
                "subtype": str(step.get("subtype") or "unknown"),
                "raw_description": str(step.get("description") or ""),
            }
        )
    consistency = _lcs_similarity(step_actions, fingerprint.get("action_sequence") or [])
    extraction = await extract_skill_parameters(
        user_message=str(turn_row.get("user_message") or ""),
        context_summary=str(turn_row.get("context_summary") or ""),
        input_schema=skill["input_schema"],
        llm_fallback=bool((skill.get("parameter_extractor") or {}).get("llm_fallback", True)),
    )
    success = extraction["complete"] and (
        consistency >= _SHADOW_CONSISTENCY_THRESHOLD
        or (
            float(similarity["total"]) >= _PATTERN_STRONG_THRESHOLD
            and consistency >= 0.50
        )
    )
    run_id = _new_id("skill_run")
    async with _conn() as conn:
        await conn.execute(
            """
            INSERT INTO learned_skill_runs
            (run_id, skill_id, version, turn_id, match_score, parameter_status, execution_status, failure_reason,
             fallback_used, user_feedback, dry_run, consistency_score, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0, '', 1, ?, ?)
            """,
            (
                run_id,
                skill["skill_id"],
                skill["version"],
                str(turn_row["turn_id"]),
                float(similarity["total"]),
                "complete" if extraction["complete"] else "missing_required",
                "shadow_success" if success else "shadow_failure",
                "" if success else "shadow_validation_failed",
                round(consistency, 4),
                _now_iso(),
            ),
        )
        await conn.commit()
    await _update_skill_run_stats(
        skill["skill_id"],
        execution_status="shadow_success" if success else "shadow_failure",
        consistency_score=round(consistency, 4),
    )


async def _validate_shadow_skills_for_turn(turn_id: str, fingerprint: dict[str, Any]) -> None:
    async with _conn() as conn:
        cursor = await conn.execute(
            "SELECT * FROM learned_skills WHERE status = 'shadow' ORDER BY updated_at DESC"
        )
        rows = await cursor.fetchall()
        cursor = await conn.execute(
            "SELECT * FROM behavior_turns WHERE turn_id = ?",
            (turn_id,),
        )
        turn_row = await cursor.fetchone()
    if turn_row is None:
        return
    for row in rows:
        skill = _skill_row_to_definition(row)
        await _validate_shadow_skill_for_turn(skill, dict(turn_row), fingerprint)


async def _backfill_shadow_validation(skill_id: str) -> None:
    skill = await get_learned_skill(skill_id)
    if skill is None or str(skill.get("status") or "") != "shadow":
        return
    turn_ids = list((skill.get("created_from") or {}).get("turn_list") or [])
    if not turn_ids and skill.get("pattern_id"):
        turn_ids = await _member_turn_ids(str(skill["pattern_id"]))
    for turn_id in turn_ids:
        async with _conn() as conn:
            cursor = await conn.execute(
                """
                SELECT 1
                FROM learned_skill_runs
                WHERE skill_id = ? AND version = ? AND turn_id = ? AND dry_run = 1
                LIMIT 1
                """,
                (skill_id, int(skill["version"]), str(turn_id)),
            )
            existing = await cursor.fetchone()
            cursor = await conn.execute(
                "SELECT * FROM behavior_turns WHERE turn_id = ?",
                (str(turn_id),),
            )
            turn_row = await cursor.fetchone()
        if existing is not None or turn_row is None:
            continue
        fingerprint = await _fingerprint_for_turn(str(turn_id))
        if not fingerprint:
            continue
        await _validate_shadow_skill_for_turn(skill, dict(turn_row), fingerprint)
        skill = await get_learned_skill(skill_id)
        if skill is None or str(skill.get("status") or "") != "shadow":
            return


async def _replay_tests_for_skill(skill_id: str) -> list[dict[str, Any]]:
    async with _conn() as conn:
        cursor = await conn.execute(
            "SELECT * FROM behavior_replay_tests WHERE skill_id = ? ORDER BY created_at ASC",
            (skill_id,),
        )
        rows = await cursor.fetchall()
    return [dict(row) for row in rows]


async def _run_replay_tests(skill_id: str) -> dict[str, Any]:
    skill = await get_learned_skill(skill_id)
    if skill is None:
        return {"passed": 0, "total": 0, "pass_rate": 0.0}
    tests = await _replay_tests_for_skill(skill_id)
    passed = 0
    total = 0
    now = _now_iso()
    for test in tests:
        total += 1
        turn_id = str(test.get("turn_id") or "")
        async with _conn() as conn:
            cursor = await conn.execute(
                "SELECT user_message, context_summary FROM behavior_turns WHERE turn_id = ?", (turn_id,)
            )
            turn_row = await cursor.fetchone()
        if turn_row is None:
            continue
        request_fp = await build_request_fingerprint(str(turn_row["user_message"]), [{"role": "system", "content": str(turn_row["context_summary"])}])
        similarity = compute_fingerprint_similarity(request_fp, (skill["trigger"] or {}).get("base_fingerprint") or {})
        ok = not similarity["hard_fail"] and float(similarity["total"]) >= _ROUTER_JUDGE_THRESHOLD
        if ok:
            passed += 1
        async with _conn() as conn:
            await conn.execute(
                "UPDATE behavior_replay_tests SET last_result = ?, updated_at = ? WHERE test_id = ?",
                (_json_dumps({"ok": ok, "similarity": similarity}), now, test["test_id"]),
            )
            await conn.commit()
    return {
        "passed": passed,
        "total": total,
        "pass_rate": round((passed / total) if total else 0.0, 4),
    }


async def run_skill_replay_tests(skill_id: str) -> dict[str, Any]:
    return await _run_replay_tests(skill_id)


async def update_learned_skill(
    skill_id: str,
    updates: dict[str, Any],
    *,
    reason: str = "Manual skill edit.",
) -> dict[str, Any] | None:
    if not isinstance(updates, dict):
        return None
    async with _conn() as conn:
        cursor = await conn.execute(
            "SELECT * FROM learned_skills WHERE skill_id = ?", (skill_id,)
        )
        row = await cursor.fetchone()
        if row is None:
            return None
        current = _skill_row_to_definition(row)
        definition = _clone_json_value(current)
        allowed_fields = {
            "name",
            "description",
            "status",
            "skill_type",
            "risk_level",
            "requires_llm",
            "trigger",
            "input_schema",
            "parameter_extractor",
            "steps",
            "guards",
            "fallback_policy",
            "editable_fields",
            "created_from",
        }
        changed_fields = {key for key in updates.keys() if key in allowed_fields}
        for field in changed_fields:
            definition[field] = _clone_json_value(updates[field])
        structural_fields = {
            "trigger",
            "input_schema",
            "parameter_extractor",
            "steps",
            "guards",
            "fallback_policy",
            "skill_type",
        }
        if structural_fields & changed_fields and "status" not in changed_fields:
            definition["status"] = "shadow"
        definition["pattern_id"] = current["pattern_id"]
        definition["created_at"] = current["created_at"]
        definition["run_statistics"] = current["run_statistics"]
        sanitized = await _sanitize_skill_definition(definition)
        valid_statuses = {"draft", "shadow", "active", "refined", "deprecated"}
        if str(sanitized.get("status") or "") not in valid_statuses:
            sanitized["status"] = current["status"]
        if str(sanitized.get("skill_type") or "") not in _SKILL_TYPE_ORDER:
            sanitized["skill_type"] = current["skill_type"]
        sanitized["requires_llm"] = bool(sanitized.get("requires_llm"))
        persisted = await _persist_skill_version(
            conn,
            skill_id=skill_id,
            current_row=row,
            definition=sanitized,
            change_type="manual_edit",
            change_summary=reason,
        )
        await conn.commit()
    replay_result = await _run_replay_tests(skill_id)
    return {
        **persisted,
        "test_result": replay_result,
    }


async def apply_skill_patch(skill_id: str, patch_id: str) -> dict[str, Any]:
    async with _conn() as conn:
        cursor = await conn.execute(
            "SELECT * FROM learned_skills WHERE skill_id = ?", (skill_id,)
        )
        skill_row = await cursor.fetchone()
        cursor = await conn.execute(
            "SELECT * FROM learned_skill_patches WHERE skill_id = ? AND patch_id = ?",
            (skill_id, patch_id),
        )
        patch_row = await cursor.fetchone()
        if skill_row is None or patch_row is None:
            return {"ok": False, "error": "Skill or patch not found."}
        if str(patch_row["status"] or "") != "proposed":
            return {"ok": False, "error": "Patch is not in proposed state."}
        current = _skill_row_to_definition(skill_row)
        patch_content = _json_loads(patch_row["patch_content"], {})
        change_list = patch_content.get("change_list") or []
        if not change_list:
            change_list = _build_patch_change_list(
                current,
                str(patch_row["patch_type"] or ""),
                str(patch_row["reason"] or ""),
                patch_content,
            )
        if not change_list:
            return {"ok": False, "error": "Patch is advisory only and needs manual editing."}
        definition = _clone_json_value(current)
        applied_changes = _apply_change_list(definition, change_list)
        definition["status"] = "shadow"
        definition["pattern_id"] = current["pattern_id"]
        definition["created_at"] = current["created_at"]
        definition["run_statistics"] = current["run_statistics"]
        sanitized = await _sanitize_skill_definition(definition)
        persisted = await _persist_skill_version(
            conn,
            skill_id=skill_id,
            current_row=skill_row,
            definition=sanitized,
            change_type="apply_patch",
            change_summary=str(patch_row["reason"] or "Applied skill patch."),
            patch_list=applied_changes,
        )
        await conn.execute(
            "UPDATE learned_skill_patches SET status = 'applied' WHERE patch_id = ?",
            (patch_id,),
        )
        await conn.commit()
    replay_result = await _run_replay_tests(skill_id)
    return {
        "ok": True,
        "skill": persisted,
        "patch_id": patch_id,
        "applied_changes": applied_changes,
        "test_result": replay_result,
    }


async def reject_skill_patch(skill_id: str, patch_id: str) -> bool:
    async with _conn() as conn:
        cursor = await conn.execute(
            "SELECT patch_id FROM learned_skill_patches WHERE skill_id = ? AND patch_id = ?",
            (skill_id, patch_id),
        )
        row = await cursor.fetchone()
        if row is None:
            return False
        await conn.execute(
            "UPDATE learned_skill_patches SET status = 'rejected' WHERE patch_id = ?",
            (patch_id,),
        )
        await conn.commit()
    return True


async def rollback_learned_skill(skill_id: str, rollback_version: int) -> dict[str, Any]:
    async with _conn() as conn:
        cursor = await conn.execute(
            "SELECT * FROM learned_skills WHERE skill_id = ?", (skill_id,)
        )
        current_row = await cursor.fetchone()
        cursor = await conn.execute(
            """
            SELECT skill_definition
            FROM learned_skill_versions
            WHERE skill_id = ? AND version = ?
            """,
            (skill_id, int(rollback_version)),
        )
        version_row = await cursor.fetchone()
        if current_row is None or version_row is None:
            return {"ok": False, "error": "Skill or target version not found."}
        definition = _json_loads(version_row["skill_definition"], {})
        if not isinstance(definition, dict):
            return {"ok": False, "error": "Stored version is invalid."}
        definition["status"] = str(definition.get("status") or "shadow")
        definition["pattern_id"] = str(current_row["pattern_id"] or definition.get("pattern_id") or "")
        definition["created_at"] = str(current_row["created_at"] or definition.get("created_at") or _now_iso())
        definition["run_statistics"] = _json_loads(current_row["run_statistics_json"], _default_skill_stats())
        sanitized = await _sanitize_skill_definition(definition)
        persisted = await _persist_skill_version(
            conn,
            skill_id=skill_id,
            current_row=current_row,
            definition=sanitized,
            change_type="rollback",
            change_summary=f"Rolled back skill to version {rollback_version}.",
            rollback_target=int(rollback_version),
        )
        await conn.commit()
    replay_result = await _run_replay_tests(skill_id)
    return {
        "ok": True,
        "skill": persisted,
        "rollback_target": int(rollback_version),
        "test_result": replay_result,
    }


async def _maybe_create_or_update_skill(pattern_id: str) -> None:
    async with _conn() as conn:
        cursor = await conn.execute(
            "SELECT prototype_fingerprint, statistics_json FROM behavior_patterns WHERE pattern_id = ?",
            (pattern_id,),
        )
        pattern_row = await cursor.fetchone()
        cursor = await conn.execute(
            "SELECT skill_id FROM learned_skills WHERE pattern_id = ?",
            (pattern_id,),
        )
        skill_row = await cursor.fetchone()
    if pattern_row is None:
        return
    prototype = _json_loads(pattern_row["prototype_fingerprint"], {})
    stats = _json_loads(pattern_row["statistics_json"], _default_pattern_stats())
    effective = float(stats.get("effective_count") or 0.0)
    if effective < 2:
        return
    skill_id = str(skill_row["skill_id"]) if skill_row is not None else await _create_skill(pattern_id)
    if not skill_id:
        return
    target_type = _target_skill_type(stats, prototype)
    skill = await get_learned_skill(skill_id)
    if skill is None:
        return
    if _SKILL_TYPE_ORDER.get(target_type, 0) > _SKILL_TYPE_ORDER.get(skill["skill_type"], 0):
        await _update_skill_to_type(skill_id, target_type, f"Promoted to {target_type} based on stronger pattern evidence.")
        replay_result = await _run_replay_tests(skill_id)
        if replay_result["total"] and replay_result["pass_rate"] < 0.50:
            refreshed = await get_learned_skill(skill_id)
            if refreshed is not None:
                await _create_patch_proposal(
                    skill_id,
                    int(refreshed["version"]),
                    "update_trigger",
                    "Replay tests show low pass rate after promotion.",
                    {
                        **replay_result,
                        "change_list": _build_patch_change_list(
                            refreshed,
                            "update_trigger",
                            "Replay tests show low pass rate after promotion.",
                            replay_result,
                        ),
                    },
                )
    await _backfill_shadow_validation(skill_id)


async def _promote_unknown_pool() -> None:
    async with _conn() as conn:
        cursor = await conn.execute(
            """
            SELECT *
            FROM behavior_unknown_labels
            WHERE status = 'open' AND seen_count >= 3
            ORDER BY seen_count DESC, updated_at DESC
            """
        )
        rows = await cursor.fetchall()
        now = _now_iso()
        for row in rows:
            label_type = str(row["label_type"] or "")
            raw = str(row["raw_description"] or "")
            proposed = _safe_slug(
                row["proposed_subtype"] or row["proposed_type"] or row["proposed_domain"] or raw
            )
            if not proposed:
                continue
            await conn.execute(
                """
                INSERT OR IGNORE INTO behavior_vocabulary_aliases
                (alias_id, label_type, canonical_label, alias_label, created_at, vocabulary_version)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    _new_id("alias"),
                    label_type,
                    proposed,
                    _safe_slug(raw),
                    now,
                    _VOCABULARY_VERSION,
                ),
            )
            await conn.execute(
                "UPDATE behavior_unknown_labels SET status = 'promoted', updated_at = ? WHERE unknown_id = ?",
                (now, row["unknown_id"]),
            )
        await conn.commit()


async def process_unprocessed_turns(force: bool = False) -> dict[str, Any]:
    async with _PROCESS_LOCK:
        async with _conn() as conn:
            cursor = await conn.execute(
                """
                SELECT turn_id
                FROM behavior_turns
                WHERE processed_status = 0
                ORDER BY created_at ASC
                """
            )
            turn_rows = await cursor.fetchall()
        stats = {
            "processed_turns": 0,
            "merged_patterns": 0,
            "new_patterns": 0,
            "skills_created": 0,
            "skills_updated": 0,
            "shadow_checks": 0,
        }
        for row in turn_rows:
            turn_id = str(row["turn_id"])
            fingerprint = await build_turn_fingerprint(turn_id)
            if not fingerprint:
                continue
            before_skills = {item["id"]: item for item in await list_learned_skills()}
            pattern_id, merged = await _merge_turn_into_pattern(turn_id, fingerprint)
            await _maybe_create_or_update_skill(pattern_id)
            after_skills = {item["id"]: item for item in await list_learned_skills()}
            if merged:
                stats["merged_patterns"] += 1
            else:
                stats["new_patterns"] += 1
            created_skill_ids = set(after_skills) - set(before_skills)
            if created_skill_ids:
                stats["skills_created"] += len(created_skill_ids)
            updated_skill_ids = {
                skill_id
                for skill_id in set(after_skills) & set(before_skills)
                if (
                    int(after_skills[skill_id].get("version") or 0) != int(before_skills[skill_id].get("version") or 0)
                    or str(after_skills[skill_id].get("status") or "") != str(before_skills[skill_id].get("status") or "")
                    or str(after_skills[skill_id].get("skill_type") or "") != str(before_skills[skill_id].get("skill_type") or "")
                )
            }
            if updated_skill_ids:
                stats["skills_updated"] += len(updated_skill_ids)
            await _validate_shadow_skills_for_turn(turn_id, fingerprint)
            stats["shadow_checks"] += 1
            async with _conn() as conn:
                await conn.execute(
                    "UPDATE behavior_turns SET processed_status = 1, updated_at = ? WHERE turn_id = ?",
                    (_now_iso(), turn_id),
                )
                await conn.commit()
            stats["processed_turns"] += 1
        await _promote_unknown_pool()
        return stats


async def tick(_bot: Any, _db_path: str) -> None:
    try:
        await process_unprocessed_turns()
    except Exception:
        logger.debug("behavior learning tick failed", exc_info=True)


async def scan_for_session_start() -> dict[str, Any]:
    return await process_unprocessed_turns()


async def scan_for_manual_learn() -> dict[str, Any]:
    return await process_unprocessed_turns(force=True)


async def rebuild_learning_state(*, reprocess_all_turns: bool = True) -> dict[str, Any]:
    async with _conn() as conn:
        await conn.execute("DELETE FROM behavior_pattern_turns")
        await conn.execute("DELETE FROM behavior_patterns")
        await conn.execute("DELETE FROM behavior_fingerprints")
        await conn.execute("DELETE FROM behavior_replay_tests")
        await conn.execute("DELETE FROM learned_skill_patches")
        await conn.execute("DELETE FROM learned_skill_runs")
        await conn.execute("DELETE FROM learned_skill_versions")
        await conn.execute("DELETE FROM learned_skills")
        if reprocess_all_turns:
            await conn.execute("UPDATE behavior_turns SET processed_status = 0, linked_skill_id = '', updated_at = updated_at")
        await conn.commit()
    stats = await process_unprocessed_turns(force=True)
    learned = await list_learned_skills()
    return {
        **stats,
        "patterns": await list_patterns("all"),
        "learned_skills": learned,
    }


async def run_learned_skill(skill_id: str, param_overrides: dict[str, Any] | None = None) -> str:
    skill = await get_learned_skill(skill_id)
    if skill is None:
        return f"Learned skill '{skill_id}' not found."
    from cyrene.tools import _execute_tool

    context_summary = ""
    extraction = {
        "params": param_overrides or {},
        "complete": True,
        "missing_required": [],
        "confidence": 1.0,
    }
    if skill["input_schema"]:
        extraction = await extract_skill_parameters(
            user_message=" ".join(str(value) for value in (param_overrides or {}).values()),
            context_summary=context_summary,
            input_schema=skill["input_schema"],
            llm_fallback=False,
            overrides=param_overrides,
        )
    if not extraction["complete"]:
        return f"Skill '{skill_id}' is missing required params: {', '.join(extraction['missing_required'])}"
    results: list[str] = []
    for step in skill["steps"]:
        if not bool(step.get("enabled", True)):
            continue
        reference = step.get("implementation_reference") or {}
        if str(step.get("implementation_kind") or "") != "tool_call":
            continue
        tool_name = str(reference.get("tool_name") or "")
        resolved_args = _resolve_value_template(reference.get("args_template") or {}, extraction["params"])
        try:
            result = await _execute_tool(tool_name, resolved_args, None, 0, "", None)
        except Exception as exc:
            result = f"Tool failed: {exc}"
        results.append(f"{tool_name}: {_truncate_text(result, 500)}")
    return "\n".join(results) if results else f"Skill '{skill_id}' has no executable steps."


async def list_compat_scripts(status: str = "all") -> list[dict[str, Any]]:
    learned = await list_learned_skills()
    if status != "all":
        learned = [item for item in learned if item.get("status") == status]
    return [
        {
            "id": item["id"],
            "name": item["name"],
            "description": item["description"],
            "status": item["status"],
            "type": item["skill_type"],
            "occurrences": len(item.get("positive_examples") or []),
            "confidence": float(item.get("min_match_score") or 0.0),
            "last_used": item.get("run_statistics", {}).get("last_run_at", ""),
            "steps": [
                {"tool": str((step.get("implementation_reference") or {}).get("tool_name") or "")}
                for step in item.get("steps") or []
                if (step.get("implementation_reference") or {}).get("tool_name")
            ],
        }
        for item in learned
    ]
