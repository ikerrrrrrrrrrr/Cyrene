"""Deep reflection: clean-context reframing and LLM-history projection."""

from __future__ import annotations

import json
import logging
import re
from typing import Any
from uuid import uuid4

from cyrene.agent.deep_reflection_prompts import (
    DEEP_REFLECTION_PROMPT_V1,
    DEEP_REFLECTION_SCHEMA,
    render_deep_reflection_packet,
)
from cyrene.agent.message import _ensure_message_identity
from cyrene.agent.state import _call_llm, _caller_type, _publish_runtime_event
from cyrene.context_trace import attach_context, content_fingerprint, context_block
from cyrene.llm import _assistant_text

_MAX_SOURCE_MESSAGES = 36
_MAX_TEXT_PREVIEW = 700
_SENSITIVE_ARG_KEYS = {"token", "key", "secret", "password", "authorization", "cookie", "api_key", "access_token", "refresh_token"}

logger = logging.getLogger(__name__)


def has_deep_reflection_record(messages: list[dict[str, Any]]) -> bool:
    return any(isinstance(message, dict) and bool(message.get("deep_reflection_record")) for message in messages)


def serialize_evidence(evidence: dict[str, Any]) -> str:
    """Return deterministic evidence JSON for prompt-cache-friendly calls."""
    return json.dumps(_json_safe(evidence), ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def build_reflection_evidence(
    messages: list[dict[str, Any]],
    *,
    scope: str = "current_round",
    goal_gap: str = "",
    user_requirement: str = "",
    focus: str = "",
) -> dict[str, Any]:
    """Mechanically compress visible transcript into clean reflection evidence."""
    _ensure_message_identity(messages)
    source_messages = _select_source_messages(messages, scope=scope)
    source_ids = _expand_source_ids_for_tool_episodes(messages, {
        str(message.get("message_id") or "").strip()
        for message in source_messages
        if str(message.get("message_id") or "").strip()
    })
    source_messages = [message for message in messages if str(message.get("message_id") or "").strip() in source_ids]

    user_messages = [
        _compact_text(message.get("content"), _MAX_TEXT_PREVIEW)
        for message in source_messages
        if message.get("role") == "user" and _compact_text(message.get("content"), _MAX_TEXT_PREVIEW)
    ]
    objective = user_messages[0] if user_messages else _compact_text(focus or user_requirement or goal_gap, _MAX_TEXT_PREVIEW)
    requirements = []
    if user_requirement:
        requirements.append(_compact_text(user_requirement, _MAX_TEXT_PREVIEW))
    requirements.extend(user_messages[-4:])

    attempts, tools_used = _compress_attempts(source_messages)
    source_round_ids = sorted({
        str(message.get("round_id") or "").strip()
        for message in source_messages
        if str(message.get("round_id") or "").strip()
    })

    evidence = {
        "schema": DEEP_REFLECTION_SCHEMA,
        "scope": str(scope or "current_round"),
        "objective": objective,
        "user_requirements": _dedupe_strings(requirements)[:8],
        "goal_gap": _compact_text(goal_gap or "The user or agent requested deep reflection because the current work may not satisfy the goal.", _MAX_TEXT_PREVIEW),
        "focus": _compact_text(focus, _MAX_TEXT_PREVIEW),
        "compressed_attempts": attempts[:12],
        "tools_used": tools_used[:16],
        "source_message_ids": sorted(source_ids),
        "source_round_ids": source_round_ids,
    }
    return evidence


async def create_deep_reflection_record(
    messages: list[dict[str, Any]],
    *,
    scope: str = "current_round",
    goal_gap: str = "",
    user_requirement: str = "",
    focus: str = "",
    lang_text: str = "",
) -> dict[str, Any]:
    """Create a visible transcript record from clean-context reflection."""
    await _publish_runtime_event({
        "type": "phase_transition",
        "from": "execution",
        "to": "deep_reflection",
        "detail": "正在进行深度反思" if re.search(r"[\u4e00-\u9fff]", str(lang_text or focus or user_requirement or goal_gap or "")) else "Running deep reflection",
    })
    evidence = build_reflection_evidence(
        messages,
        scope=scope,
        goal_gap=goal_gap,
        user_requirement=user_requirement,
        focus=focus,
    )
    packet, usage = await run_clean_reflection(evidence)
    source_message_ids = list(evidence.get("source_message_ids") or [])
    source_round_ids = list(evidence.get("source_round_ids") or [])
    record = make_reflection_record(
        packet,
        source_message_ids=source_message_ids,
        source_round_ids=source_round_ids,
        lang_text=lang_text or focus or user_requirement or goal_gap or evidence.get("objective", ""),
    )
    await _publish_runtime_event({
        "type": "deep_reflection",
        "reflection_id": record.get("reflection_id", ""),
        "source_message_count": len(source_message_ids),
        "source_round_ids": source_round_ids,
        "packet_hash": record.get("packet_hash", ""),
        "usage": usage,
    })
    return record


async def run_clean_reflection(evidence: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    token = _caller_type.set("deep_reflection")
    try:
        response = await _call_llm(
            [
                {"role": "system", "content": DEEP_REFLECTION_PROMPT_V1},
                {"role": "user", "content": serialize_evidence(evidence)},
            ],
            tools=None,
            max_tokens=1800,
            secondary=True,
            thinking="disabled",
        )
    finally:
        _caller_type.reset(token)

    text = (_assistant_text(response) or "").strip()
    payload = _extract_json_object(text)
    if not isinstance(payload, dict) or not payload:
        logger.warning(
            "Deep reflection worker returned invalid JSON; using evidence fallback. model=%s preview=%r",
            response.get("model") or (response.get("usage") or {}).get("model") or "",
            _compact_text(text, 500),
        )
        return _fallback_packet_from_evidence(evidence), dict(response.get("usage") or {})
    packet = _normalize_packet(payload, evidence)
    return packet, dict(response.get("usage") or {})


def make_reflection_record(
    packet: dict[str, Any],
    *,
    source_message_ids: list[str],
    source_round_ids: list[str],
    lang_text: str = "",
) -> dict[str, Any]:
    rendered = render_deep_reflection_packet(packet)
    reflection_id = f"reflect_{uuid4().hex[:12]}"
    chinese = bool(re.search(r"[\u4e00-\u9fff]", str(lang_text or "")))
    content = (
        "已完成深度反思。失败记录仍保留在对话中；后续上下文会使用压缩后的方向继续。"
        if chinese
        else "Deep reflection is complete. The failed transcript remains visible; future context will use the compressed direction."
    )
    record: dict[str, Any] = {
        "role": "assistant",
        "content": content,
        "system_initiated": True,
        "deep_reflection_record": True,
        "reflection_id": reflection_id,
        "source_message_ids": [str(item) for item in source_message_ids if str(item).strip()],
        "source_round_ids": [str(item) for item in source_round_ids if str(item).strip()],
        "packet": packet,
        "packet_hash": content_fingerprint(rendered),
    }
    _ensure_message_identity([record])
    return record


def project_history_for_llm(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Return an LLM-only projection that hides reflected failure details and permission elevation messages."""
    messages = [m for m in messages if isinstance(m, dict) and not bool(m.get("hidden_from_llm"))]

    records: list[tuple[int, dict[str, Any]]] = [
        (index, message)
        for index, message in enumerate(messages)
        if isinstance(message, dict) and bool(message.get("deep_reflection_record"))
    ]
    if not records:
        return messages

    suppressed_ids: set[str] = set()
    for _index, record in records:
        for message_id in record.get("source_message_ids") or []:
            message_id = str(message_id or "").strip()
            if message_id:
                suppressed_ids.add(message_id)
    suppressed_ids = _expand_source_ids_for_tool_episodes(messages, suppressed_ids)

    record_indexes = {index for index, _record in records}
    result: list[dict[str, Any]] = []
    for index, message in enumerate(messages):
        if not isinstance(message, dict):
            continue
        if index in record_indexes:
            mid = str(message.get("message_id") or "").strip()
            if mid and mid in suppressed_ids:
                continue
            result.append(_synthetic_reflection_message(message))
            continue
        mid = str(message.get("message_id") or "").strip()
        if mid and mid in suppressed_ids:
            continue
        result.append(message)
    return result


def _synthetic_reflection_message(record: dict[str, Any]) -> dict[str, Any]:
    packet = record.get("packet") if isinstance(record.get("packet"), dict) else {}
    content = render_deep_reflection_packet(packet)
    reflection_id = str(record.get("reflection_id") or record.get("message_id") or "unknown").strip()
    return attach_context(
        {"role": "system", "content": content},
        context_block(
            f"history.deep_reflection.{reflection_id}",
            "history_deep_reflection",
            source="cyrene.agent.deep_reflection.project_history_for_llm",
            reason="replace reflected failure transcript with clean-context packet",
            transforms=["projection", "suppress_source_messages"],
            content=content,
            metadata={
                "reflection_id": reflection_id,
                "source_message_count": len(record.get("source_message_ids") or []),
                "packet_hash": str(record.get("packet_hash") or content_fingerprint(content)),
            },
        ),
    )


def _select_source_messages(messages: list[dict[str, Any]], *, scope: str) -> list[dict[str, Any]]:
    candidates = [
        message for message in messages
        if isinstance(message, dict)
        and message.get("role") in {"user", "assistant", "tool"}
        and not bool(message.get("deep_reflection_record"))
        and not bool(message.get("hidden_from_ui"))
    ]
    if not candidates:
        return []

    if str(scope or "").strip() == "session_tail":
        return candidates[-_MAX_SOURCE_MESSAGES:]

    latest_round = next(
        (
            str(message.get("round_id") or "").strip()
            for message in reversed(candidates)
            if str(message.get("round_id") or "").strip()
        ),
        "",
    )
    if latest_round:
        round_messages = [
            message for message in candidates
            if str(message.get("round_id") or "").strip() == latest_round
        ]
        if round_messages:
            return round_messages[-_MAX_SOURCE_MESSAGES:]
    return candidates[-_MAX_SOURCE_MESSAGES:]


def _expand_source_ids_for_tool_episodes(messages: list[dict[str, Any]], source_ids: set[str]) -> set[str]:
    expanded = set(source_ids)
    call_to_assistant: dict[str, str] = {}
    call_to_tool: dict[str, str] = {}
    for index, message in enumerate(messages):
        if not isinstance(message, dict):
            continue
        mid = str(message.get("message_id") or "").strip()
        if message.get("role") == "assistant" and message.get("tool_calls"):
            for tool_call in message.get("tool_calls") or []:
                call_id = str(tool_call.get("id") or "").strip()
                if call_id and mid:
                    call_to_assistant[call_id] = mid
                tool_index = index + 1
                while tool_index < len(messages) and messages[tool_index].get("role") == "tool":
                    if str(messages[tool_index].get("tool_call_id") or "").strip() == call_id:
                        tool_mid = str(messages[tool_index].get("message_id") or "").strip()
                        if tool_mid:
                            call_to_tool[call_id] = tool_mid
                        break
                    tool_index += 1

    changed = True
    while changed:
        changed = False
        for call_id, assistant_mid in call_to_assistant.items():
            tool_mid = call_to_tool.get(call_id, "")
            if assistant_mid in expanded and tool_mid and tool_mid not in expanded:
                expanded.add(tool_mid)
                changed = True
            if tool_mid in expanded and assistant_mid and assistant_mid not in expanded:
                expanded.add(assistant_mid)
                changed = True
    return expanded


def _compress_attempts(source_messages: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    attempts: list[dict[str, Any]] = []
    tools_used: list[dict[str, Any]] = []
    tool_name_by_call_id: dict[str, str] = {}
    tool_args_by_call_id: dict[str, dict[str, Any]] = {}
    failed_unknown_call_ids = _failed_unknown_tool_call_ids(source_messages)

    for message in source_messages:
        if message.get("role") == "assistant":
            content = _compact_text(message.get("content"), _MAX_TEXT_PREVIEW)
            tools = []
            for tool_call in message.get("tool_calls") or []:
                fn = tool_call.get("function") if isinstance(tool_call, dict) else {}
                if not isinstance(fn, dict):
                    continue
                args = _safe_tool_args(fn.get("arguments"))
                call_id = str(tool_call.get("id") or "").strip()
                raw_name = str(fn.get("name") or "").strip()
                name = "unknown_tool" if call_id in failed_unknown_call_ids else raw_name
                if call_id:
                    tool_name_by_call_id[call_id] = name
                    tool_args_by_call_id[call_id] = args
                if name:
                    tool_entry = {"name": name, "args": args}
                    tools.append(tool_entry)
                    tools_used.append(tool_entry)
            if content or tools:
                attempts.append({
                    "attempt": content or "Called tools without assistant prose.",
                    "why_bad_for_goal": "Needs clean reflection; exact failure transcript suppressed.",
                    "tools": tools,
                })
        elif message.get("role") == "tool":
            call_id = str(message.get("tool_call_id") or "").strip()
            name = tool_name_by_call_id.get(call_id, "tool")
            args = tool_args_by_call_id.get(call_id, {})
            result = str(message.get("content") or "")
            status = "error" if _looks_like_error(result) else "result"
            attempts.append({
                "attempt": f"{name} returned {status}; full output suppressed.",
                "why_bad_for_goal": "Tool output is compressed because the full result should not remain in the next working context.",
                "tools": [{"name": name, "args": args}] if name else [],
            })

    return attempts, _dedupe_tool_entries(tools_used)


def _failed_unknown_tool_call_ids(messages: list[dict[str, Any]]) -> set[str]:
    result: set[str] = set()
    for message in messages:
        if not isinstance(message, dict) or message.get("role") != "tool":
            continue
        content = str(message.get("content") or "")
        if "Unknown tool:" not in content:
            continue
        call_id = str(message.get("tool_call_id") or "").strip()
        if call_id:
            result.add(call_id)
    return result


def _safe_tool_args(raw: Any) -> dict[str, Any]:
    if isinstance(raw, str):
        try:
            parsed = json.loads(raw or "{}")
        except Exception:
            return {"_raw": _compact_text(raw, 160)}
    elif isinstance(raw, dict):
        parsed = raw
    else:
        return {}
    return _redact_args(parsed)


def _redact_args(value: Any) -> Any:
    if isinstance(value, dict):
        result: dict[str, Any] = {}
        for key, item in sorted(value.items(), key=lambda pair: str(pair[0])):
            key_text = str(key)
            if key_text.lower() in _SENSITIVE_ARG_KEYS or any(token in key_text.lower() for token in _SENSITIVE_ARG_KEYS):
                result[key_text] = "[REDACTED]"
            elif key_text in {"content", "new_string", "old_string"} and isinstance(item, str):
                result[key_text] = f"[{len(item)} chars redacted]"
            else:
                result[key_text] = _redact_args(item)
        return result
    if isinstance(value, list):
        return [_redact_args(item) for item in value[:12]]
    if isinstance(value, str):
        return _compact_text(value, 220)
    return value


def _normalize_packet(payload: dict[str, Any], evidence: dict[str, Any]) -> dict[str, Any]:
    packet = {
        "schema": DEEP_REFLECTION_SCHEMA,
        "objective": _compact_text(payload.get("objective") or evidence.get("objective"), _MAX_TEXT_PREVIEW),
        "user_requirements": _list_of_strings(payload.get("user_requirements") or evidence.get("user_requirements")),
        "goal_gap": _compact_text(payload.get("goal_gap") or evidence.get("goal_gap"), _MAX_TEXT_PREVIEW),
        "current_state": _compact_text(payload.get("current_state") or "Previous attempts were insufficient; see compressed attempts.", _MAX_TEXT_PREVIEW),
        "compressed_attempts": _list_of_attempts(payload.get("compressed_attempts") or evidence.get("compressed_attempts")),
        "excluded_paths": _list_of_strings(payload.get("excluded_paths")),
        "tools_used": _list_of_tools(payload.get("tools_used") or evidence.get("tools_used")),
        "promising_directions": _list_of_strings(payload.get("promising_directions")),
        "next_step": _compact_text(payload.get("next_step") or "Continue from the highest-confidence promising direction.", _MAX_TEXT_PREVIEW),
        "open_questions": _list_of_strings(payload.get("open_questions")),
    }
    return packet


def _fallback_packet_from_evidence(evidence: dict[str, Any]) -> dict[str, Any]:
    focus = _compact_text(evidence.get("focus"), _MAX_TEXT_PREVIEW)
    objective = _compact_text(evidence.get("objective"), _MAX_TEXT_PREVIEW)
    promising = []
    if focus:
        promising.append(f"Use the user's reflection focus as the next direction: {focus}")
    if objective:
        promising.append(f"Re-anchor on the original objective: {objective}")
    promising.append("Continue from the compressed evidence and avoid repeating insufficient attempts as-is.")

    return _normalize_packet(
        {
            "current_state": "The reflection worker did not return strict JSON, so Cyrene generated this packet from deterministic compressed evidence.",
            "excluded_paths": ["Do not replay the compressed failed or insufficient attempts without a changed strategy."],
            "promising_directions": promising,
            "next_step": focus or "Choose the highest-leverage next action that directly closes the goal gap.",
            "open_questions": [],
        },
        evidence,
    )


def _extract_json_object(text: str) -> dict[str, Any]:
    stripped = text.strip()
    if stripped.startswith("```"):
        stripped = re.sub(r"^```(?:json)?\s*", "", stripped)
        stripped = re.sub(r"\s*```$", "", stripped)
    try:
        payload = json.loads(stripped)
        return payload if isinstance(payload, dict) else {}
    except Exception:
        pass
    start = stripped.find("{")
    end = stripped.rfind("}")
    if start >= 0 and end > start:
        try:
            payload = json.loads(stripped[start:end + 1])
            return payload if isinstance(payload, dict) else {}
        except Exception:
            return {}
    return {}


def _compact_text(value: Any, limit: int) -> str:
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 3)].rstrip() + "..."


def _looks_like_error(text: str) -> bool:
    lower = text.lower()
    return any(token in lower for token in ("tool failed", "error", "exception", "traceback", "failed", "denied"))


def _dedupe_strings(values: list[str]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        text = _compact_text(value, _MAX_TEXT_PREVIEW)
        if not text or text in seen:
            continue
        seen.add(text)
        result.append(text)
    return result


def _dedupe_tool_entries(values: list[dict[str, Any]]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    seen: set[str] = set()
    for value in values:
        key = json.dumps(_json_safe(value), ensure_ascii=False, sort_keys=True)
        if key in seen:
            continue
        seen.add(key)
        result.append(value)
    return result


def _list_of_strings(value: Any) -> list[str]:
    if not isinstance(value, list):
        value = [value] if value else []
    return _dedupe_strings([str(item) for item in value if str(item or "").strip()])[:12]


def _list_of_tools(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    result: list[dict[str, Any]] = []
    for item in value:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "").strip()
        if not name:
            continue
        args = item.get("args") if isinstance(item.get("args"), dict) else {}
        result.append({"name": name, "args": _redact_args(args)})
    return _dedupe_tool_entries(result)[:16]


def _list_of_attempts(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    result: list[dict[str, Any]] = []
    for item in value:
        if not isinstance(item, dict):
            continue
        result.append({
            "attempt": _compact_text(item.get("attempt"), _MAX_TEXT_PREVIEW),
            "why_bad_for_goal": _compact_text(item.get("why_bad_for_goal"), _MAX_TEXT_PREVIEW),
            "tools": _list_of_tools(item.get("tools")),
        })
    return result[:12]


def _json_safe(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_json_safe(item) for item in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)
