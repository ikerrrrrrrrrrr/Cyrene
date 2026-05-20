import asyncio
import json
import logging
import os
import re
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable
from uuid import uuid4

import httpx

from contextvars import ContextVar

from cyrene.config import ASSISTANT_NAME, DATA_DIR, STATE_FILE
from cyrene.memory import get_memory_context
from cyrene.short_term import get_context, touch_entry
from cyrene import debug
from cyrene.llm import _assistant_text, _truncate
from cyrene.tools import get_active_tool_defs, TOOL_HANDLERS, _execute_tool
from cyrene.subagent import (
    clear as _clear_subagents,
)

logger = logging.getLogger(__name__)

# 当前 agent ID，用于 send_agent_message 识别发送者
_current_agent_id: ContextVar[str] = ContextVar("_current_agent_id", default="main")
# 当前对话轮次 ID，用于隔离多轮 flow / inbox 通信
_current_round_id: ContextVar[str] = ContextVar("_current_round_id", default="")
_current_client_request_id: ContextVar[str] = ContextVar("_current_client_request_id", default="")
# 当前调用者类型，用于 debug 日志
_caller_type: ContextVar[str] = ContextVar("_caller_type", default="main_agent")
_persist_base_messages: ContextVar[list[dict[str, Any]] | None] = ContextVar("_persist_base_messages", default=None)
_persist_merge_live_state: ContextVar[bool] = ContextVar("_persist_merge_live_state", default=False)
_persist_history_prefix_len: ContextVar[int] = ContextVar("_persist_history_prefix_len", default=0)
_persist_insert_at: ContextVar[int | None] = ContextVar("_persist_insert_at", default=None)
_pending_intermediate_user_replies: ContextVar[list[dict[str, Any]] | None] = ContextVar("_pending_intermediate_user_replies", default=None)
_reply_stream_writer: ContextVar[Callable[[dict[str, Any]], Awaitable[None]] | None] = ContextVar("_reply_stream_writer", default=None)
_agent_lock = asyncio.Lock()
_session_state_lock = asyncio.Lock()
_interrupt_event = asyncio.Event()
_MAX_HISTORY_MESSAGES = 40
_MAX_TOOL_ROUNDS = 16
# 后台 compressor 任务，防止被事件循环 GC
_pending_compressors: set[asyncio.Task] = set()
_pending_label_refreshes: set[asyncio.Task] = set()
_pending_interrupt_clearers: set[asyncio.Task] = set()
_main_inbox_worker: asyncio.Task | None = None
_active_main_round_id = ""
_active_main_round_prompt = ""
_active_main_round_public_prompt = ""
_active_main_round_started_at = 0.0
_MAIN_INBOX_AGENT_ID = "main"
_AWAITING_USER_SENTINEL = "[[cyrene.awaiting_user]]"
_ui_round_hide_initial_detail: ContextVar[bool] = ContextVar("_ui_round_hide_initial_detail", default=False)
_ui_round_assistant_meta: ContextVar[dict[str, Any] | None] = ContextVar("_ui_round_assistant_meta", default=None)

# ---------------------------------------------------------------------------
# System prompts
# ---------------------------------------------------------------------------

_MAIN_AGENT_PROMPT = """You are a capable AI assistant. Get things done efficiently.

## Values
- **Ownership**: Take responsibility end-to-end. Do not stop at analysis — implement, verify, and confirm.
- **Honesty over deference**: If something is wrong or risky, say so directly. Do not fabricate results.
- **Clarity > Speed**: When a decision has non-obvious consequences, pause and explain. For routine tasks, just do it.

## Communication
- Respond clearly and directly. No conversational interjections ("Got it", "Sure", "Great question").
- No emoji. Never.
- While working, give brief progress updates (1-2 sentences). After completion, give a concise final answer.
- Final answer: prefer 1-2 short paragraphs. Use lists only when the content is inherently list-shaped. Keep it flat.

## Tools
- **You have full tool access** — use it proactively. Any request that involves files, search, web, code, shell commands, scheduling, data, or sub-agents REQUIRES tools. Do NOT try to answer with text alone when a tool would help.
- The ONLY exception is pure conversation (opinions, greetings, explanations, or questions about concepts that don't need real-world data).
- When in doubt, use tools. A tool-backed answer is always better than a guess.
- If it helps the user stay oriented during a long task, you may call `send_message` to post a brief in-progress update before the final answer. Use it sparingly and only when there is real new information.
- If the user's request is ambiguous or missing a key detail, call `ask_user` instead of guessing. Use it either as a freeform question or with a short option list when structured choices would help.
- When a task is complete, call the `quit` tool.
"""

_PHASE1_DECISION_PROMPT = """Decision phase rules:
- The only available tools right now are `use_tools`, `ask_user`, and `quit`. You cannot call concrete tools (WebSearch, Bash, Read, etc.) directly — you must use `use_tools` to unlock them.
- ALWAYS call `use_tools` when the user asks you to DO anything — file ops, search, web, code, shell, scheduling, data queries, sub-agents, etc.
- Call `quit` ONLY when the request is pure conversation (opinions, greetings, conceptual explanations) AND you are completely sure no tool could improve the answer.
- Call `ask_user` when the request is genuinely ambiguous and you need clarification before acting.
- When in doubt between answering directly or calling `use_tools`, call `use_tools`. It is always better to have tools available than to answer blindly.
"""

_EXECUTION_SYSTEM_PROMPT = """You are a capable execution agent. Your job is to complete tasks using tools.

Rules:
- Use tools to complete the task efficiently.
- Read/Write/Edit files, run Bash commands, search the web as needed.
- You may call `send_message` to post a brief user-visible progress reply mid-run when helpful, but do not overuse it and do not treat it as the final answer.
- If you cannot continue safely without user clarification, call `ask_user` and stop until the user answers.
- Return the RESULT of what you did, not a conversation.
- Be concise in tool usage.
- When done, call the `quit` tool.
- Do not fabricate results. If a tool fails or returns nothing useful, state that clearly.
"""

# ---------------------------------------------------------------------------
# Session helpers
# ---------------------------------------------------------------------------


def _load_session_messages() -> list[dict[str, Any]]:
    state = _load_session_state()
    messages = state.get("messages", [])
    return messages if isinstance(messages, list) else []


def _load_pending_question() -> dict[str, Any]:
    state = _load_session_state()
    pending = state.get("pending_question", {})
    return dict(pending) if isinstance(pending, dict) else {}


def get_pending_question() -> dict[str, Any]:
    return _load_pending_question()


def _load_round_messages(round_id: str) -> list[dict[str, Any]]:
    target_round_id = str(round_id or "").strip()
    messages = _load_session_messages()
    if not target_round_id:
        return messages
    return [
        msg
        for msg in messages
        if str(msg.get("round_id", "")).strip() == target_round_id
    ]


def _load_session_state() -> dict[str, Any]:
    if not STATE_FILE.exists():
        return {}
    try:
        data = json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except Exception:
        logger.exception("Failed to read state file")
        return {}
    return data if isinstance(data, dict) else {}


def _write_session_state(state: dict[str, Any]) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


def _ensure_archive_session_id(state: dict[str, Any]) -> str:
    archive_session_id = str(state.get("archive_session_id", "")).strip()
    if not archive_session_id:
        archive_session_id = f"session_{uuid4().hex[:12]}"
        state["archive_session_id"] = archive_session_id
    return archive_session_id


def _trim_session_messages(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    if len(messages) <= _MAX_HISTORY_MESSAGES:
        return messages
    trimmed = messages[-_MAX_HISTORY_MESSAGES:]
    # Strip orphan tool messages from the start (their tool_calls were trimmed off)
    while trimmed and trimmed[0].get("role") == "tool":
        trimmed = trimmed[1:]
    # Strip orphan tool_calls from the end (their tool responses were trimmed off)
    for i in range(len(trimmed) - 1, -1, -1):
        if trimmed[i].get("tool_calls") and (i + 1 >= len(trimmed) or trimmed[i + 1].get("role") != "tool"):
            return trimmed[:i]
    return trimmed


def _schedule_memory_compression(messages: list[dict[str, Any]]) -> None:
    """Compress older conversation state without blocking the active request path."""
    task = asyncio.create_task(_compress_old_messages(list(messages)))
    _pending_compressors.add(task)
    task.add_done_callback(_pending_compressors.discard)


def _is_replaceable_live_message(entry: dict[str, Any], round_id: str) -> bool:
    """Return True for persisted messages that belong to the active live run.

    Queued guidance messages are intentionally excluded so they stay behind the
    current run transcript instead of being replaced by incremental saves.
    """
    if not round_id:
        return False
    if str(entry.get("round_id", "")).strip() != round_id:
        return False
    return not str(entry.get("queued_guidance_id", "")).strip()


async def _write_session_messages_locked(state: dict[str, Any], messages: list[dict[str, Any]]) -> None:
    _ensure_archive_session_id(state)
    messages = _ensure_message_identity(messages)
    trimmed = _trim_session_messages(messages)
    state["messages"] = trimmed
    if not str(state.get("session_title", "")).strip():
        state.pop("session_title", None)
    _write_session_state(state)
    await debug.publish_event({
        "type": "session_update",
        "message_count": len(trimmed),
        "last_role": trimmed[-1].get("role") if trimmed else "",
        "round_id": next((str(m.get("round_id", "")).strip() for m in reversed(trimmed) if m.get("round_id")), ""),
    })

    if len(messages) >= _MAX_HISTORY_MESSAGES + 5:
        _schedule_memory_compression(messages)



async def _save_session_messages(messages: list[dict[str, Any]]) -> None:
    """保存 session 消息。如果超过上限，触发后台压缩。"""
    async with _session_state_lock:
        state = _load_session_state()
        effective_messages = messages
        base_messages = _persist_base_messages.get()
        if base_messages is None and _persist_merge_live_state.get():
            current = state.get("messages", [])
            base_messages = list(current) if isinstance(current, list) else []
            prefix_len = max(0, min(_persist_history_prefix_len.get(), len(messages)))
            insert_at = _persist_insert_at.get()
            if insert_at is None:
                insert_at = len(base_messages)
            insert_at = max(0, min(insert_at, len(base_messages)))
            suffix = messages[prefix_len:]
            round_id = str(_current_round_id.get() or "").strip()
            replace_end = insert_at
            while replace_end < len(base_messages) and _is_replaceable_live_message(base_messages[replace_end], round_id):
                replace_end += 1
            effective_messages = [*base_messages[:insert_at], *suffix, *base_messages[replace_end:]]
        elif base_messages is not None:
            prefix_len = max(0, min(_persist_history_prefix_len.get(), len(messages)))
            insert_at = _persist_insert_at.get()
            if insert_at is None:
                insert_at = len(base_messages)
            insert_at = max(0, min(insert_at, len(base_messages)))
            suffix = messages[prefix_len:]
            effective_messages = [*base_messages[:insert_at], *suffix, *base_messages[insert_at:]]
        await _write_session_messages_locked(state, effective_messages)


async def _append_session_message(entry: dict[str, Any]) -> None:
    async with _session_state_lock:
        state = _load_session_state()
        messages = state.get("messages", [])
        full_messages = list(messages) if isinstance(messages, list) else []
        full_messages.append(entry)
        _ensure_message_identity(full_messages)
        await _write_session_messages_locked(state, full_messages)


def _flush_intermediate_user_replies(messages: list[dict[str, Any]]) -> None:
    pending = _pending_intermediate_user_replies.get()
    if not pending:
        return
    existing_ids = {
        str(message.get("message_id", "")).strip()
        for message in messages
        if isinstance(message, dict)
    }
    for entry in pending:
        message_id = str(entry.get("message_id", "")).strip()
        if message_id and message_id in existing_ids:
            continue
        messages.append(dict(entry))
        if message_id:
            existing_ids.add(message_id)
    pending.clear()


async def _insert_intermediate_user_reply(
    content: str,
    round_id: str,
    client_request_id: str = "",
) -> dict[str, Any]:
    assistant_entry: dict[str, Any] = {
        "role": "assistant",
        "content": content,
        "round_id": round_id,
        "intermediate_reply": True,
    }
    if client_request_id:
        assistant_entry["client_request_id"] = client_request_id

    labels = get_session_labels(round_id)
    if labels.get("round_title"):
        assistant_entry["round_title"] = labels["round_title"]

    _ensure_message_identity([assistant_entry])

    pending = _pending_intermediate_user_replies.get()
    if pending is not None:
        pending.append(dict(assistant_entry))

    async with _session_state_lock:
        state = _load_session_state()
        existing = state.get("messages", [])
        full_messages = list(existing) if isinstance(existing, list) else []
        full_messages.append(dict(assistant_entry))
        _ensure_message_identity(full_messages)
        await _write_session_messages_locked(state, full_messages)

    await _publish_runtime_event({
        "type": "assistant_message",
        "round_id": round_id,
        "client_request_id": client_request_id,
        "intermediate": True,
        "message_id": assistant_entry.get("message_id", ""),
    })
    return assistant_entry


def _normalize_pending_question(payload: dict[str, Any]) -> dict[str, Any]:
    question_id = str(payload.get("id", "")).strip() or f"question_{uuid4().hex[:12]}"
    text = str(payload.get("text", "") or "").strip()
    round_id = str(payload.get("round_id", "") or "").strip()
    client_request_id = str(payload.get("client_request_id", "") or "").strip()
    asked_at = str(payload.get("asked_at", "") or "").strip() or datetime.now(timezone.utc).isoformat()
    allow_custom = bool(payload.get("allow_custom", True))
    options: list[dict[str, str]] = []
    raw_options = payload.get("options", [])
    if isinstance(raw_options, list):
        for index, item in enumerate(raw_options, start=1):
            if isinstance(item, dict):
                label = str(item.get("label", "") or "").strip()
                option_id = str(item.get("id", "") or "").strip() or f"option_{index}"
            else:
                label = str(item or "").strip()
                option_id = f"option_{index}"
            if not label:
                continue
            options.append({"id": option_id, "label": label})
    question: dict[str, Any] = {
        "id": question_id,
        "text": text,
        "round_id": round_id,
        "client_request_id": client_request_id,
        "options": options[:6],
        "allow_custom": allow_custom,
        "asked_at": asked_at,
    }
    round_title = str(payload.get("round_title", "") or "").strip()
    if round_title:
        question["round_title"] = round_title
    return question


async def _upsert_pending_question(payload: dict[str, Any]) -> dict[str, Any]:
    question = _normalize_pending_question(payload)
    assistant_entry: dict[str, Any] = {
        "role": "assistant",
        "content": question["text"],
        "round_id": question["round_id"],
        "question_prompt": True,
        "question_id": question["id"],
    }
    if question.get("client_request_id"):
        assistant_entry["client_request_id"] = question["client_request_id"]
    if question.get("round_title"):
        assistant_entry["round_title"] = question["round_title"]
    if question["options"]:
        assistant_entry["question_options"] = list(question["options"])

    _ensure_message_identity([assistant_entry])
    question["message_id"] = assistant_entry["message_id"]

    async with _session_state_lock:
        state = _load_session_state()
        existing = state.get("messages", [])
        full_messages = list(existing) if isinstance(existing, list) else []
        replacement_index = next(
            (
                idx
                for idx, msg in enumerate(full_messages)
                if str(msg.get("question_id", "")).strip() == question["id"]
            ),
            -1,
        )
        if replacement_index >= 0:
            assistant_entry["message_id"] = str(full_messages[replacement_index].get("message_id", "")).strip() or assistant_entry["message_id"]
            question["message_id"] = assistant_entry["message_id"]
            full_messages[replacement_index] = assistant_entry
        else:
            full_messages.append(assistant_entry)
        state["pending_question"] = question
        await _write_session_messages_locked(state, full_messages)

    await _publish_runtime_event({
        "type": "user_question",
        "question_id": question["id"],
        "client_request_id": question.get("client_request_id", ""),
        "round_id": question.get("round_id", ""),
    })
    return question


async def _restore_pending_question(question: dict[str, Any]) -> None:
    normalized = _normalize_pending_question(question)
    async with _session_state_lock:
        state = _load_session_state()
        state["pending_question"] = normalized
        _write_session_state(state)
    await _publish_runtime_event({
        "type": "user_question",
        "question_id": normalized["id"],
        "client_request_id": normalized.get("client_request_id", ""),
        "round_id": normalized.get("round_id", ""),
    })


async def _clear_pending_question(question_id: str) -> dict[str, Any]:
    target_question_id = str(question_id or "").strip()
    async with _session_state_lock:
        state = _load_session_state()
        pending = state.get("pending_question", {})
        pending_dict = dict(pending) if isinstance(pending, dict) else {}
        if not pending_dict:
            return {}
        if target_question_id and str(pending_dict.get("id", "")).strip() != target_question_id:
            return {}
        state.pop("pending_question", None)
        _write_session_state(state)

    await _publish_runtime_event({
        "type": "user_question_answered",
        "question_id": str(pending_dict.get("id", "")).strip(),
        "client_request_id": str(pending_dict.get("client_request_id", "")).strip(),
        "round_id": str(pending_dict.get("round_id", "")).strip(),
    })
    return pending_dict


async def _publish_runtime_event(event: dict[str, Any]) -> None:
    """Publish a UI/runtime event annotated with the current round when present."""
    round_id = _current_round_id.get()
    if round_id and not str(event.get("round_id", "")).strip():
        event = {**event, "round_id": round_id}
    await debug.publish_event(event)


async def _emit_reply_stream_event(event: dict[str, Any]) -> None:
    writer = _reply_stream_writer.get()
    if writer is None:
        return
    await writer(dict(event))


def _streaming_reply_requested() -> bool:
    return _reply_stream_writer.get() is not None


def _assistant_entry_from_response(response: dict[str, Any], round_id: str, include_tool_calls: bool = True) -> dict[str, Any]:
    entry: dict[str, Any] = {"role": "assistant", "content": response.get("content") or ""}
    if response.get("reasoning_content"):
        entry["reasoning_content"] = response["reasoning_content"]
    if include_tool_calls and response.get("tool_calls"):
        entry["tool_calls"] = response["tool_calls"]
    if response.get("usage"):
        entry["usage"] = response["usage"]
    if round_id:
        entry["round_id"] = round_id
    extra_meta = _ui_round_assistant_meta.get()
    if extra_meta:
        entry.update(extra_meta)
    return entry


def _apply_assistant_meta(entry: dict[str, Any]) -> dict[str, Any]:
    extra_meta = _ui_round_assistant_meta.get()
    if extra_meta:
        entry.update(extra_meta)
    return entry


def _fallback_label(text: str, limit: int = 48) -> str:
    compact = re.sub(r"\s+", " ", str(text or "")).strip().strip("[](){}<>\"'`，。！？；：,.;!?")
    return compact[:limit] or "Untitled"


def _extract_json_object(text: str) -> dict[str, Any]:
    source = str(text or "").strip()
    if not source:
        return {}
    try:
        data = json.loads(source)
        return data if isinstance(data, dict) else {}
    except Exception:
        pass
    match = re.search(r"\{.*\}", source, re.DOTALL)
    if not match:
        return {}
    try:
        data = json.loads(match.group(0))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _tool_result_requests_user_input(result: str) -> bool:
    payload = _extract_json_object(result)
    return str(payload.get("status", "")).strip() == "awaiting_user"


def _ensure_message_identity(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    for message in messages:
        if not isinstance(message, dict):
            continue
        if not str(message.get("message_id", "")).strip():
            message["message_id"] = f"msg_{uuid4().hex}"
    return messages


def _round_epoch_ms(round_id: str) -> int | None:
    match = re.fullmatch(r"round_(\d+)", str(round_id or "").strip())
    if not match:
        return None
    try:
        return int(match.group(1))
    except Exception:
        return None


def _round_started_iso(round_id: str) -> str | None:
    epoch_ms = _round_epoch_ms(round_id)
    if epoch_ms is None:
        return None
    try:
        return datetime.fromtimestamp(epoch_ms / 1000, tz=timezone.utc).isoformat()
    except Exception:
        return None


def _round_title_from_entry(entry: dict[str, Any]) -> str:
    return (
        str(entry.get("title", "")).strip()
        or _fallback_label(entry.get("last_user") or entry.get("prompt") or entry.get("id"), limit=40)
    )


def _session_round_entries() -> dict[str, dict[str, Any]]:
    entries: dict[str, dict[str, Any]] = {}
    messages = _load_session_messages()
    for msg in messages:
        round_id = str(msg.get("round_id", "")).strip()
        if not round_id:
            continue
        entry = entries.setdefault(round_id, {
            "id": round_id,
            "title": "",
            "prompt": "",
            "last_user": "",
            "last_assistant": "",
            "status": "done",
            "pending_guidance": 0,
            "subagent_count": 0,
            "running_subagents": 0,
            "started_at": _round_started_iso(round_id),
            "updated_at": _round_started_iso(round_id),
        })
        if msg.get("round_title"):
            entry["title"] = str(msg.get("round_title") or "").strip()
        role = str(msg.get("role") or "")
        content = str(msg.get("content") or "").strip()
        if role == "user" and content:
            if not entry["prompt"]:
                entry["prompt"] = content
            entry["last_user"] = content
        elif role == "assistant" and content:
            entry["last_assistant"] = content
            if not entry["title"] and bool(msg.get("system_initiated")):
                entry["title"] = "proactive check-in"
    return entries


def _main_inbox_pending_by_round() -> dict[str, int]:
    from cyrene.inbox import get_unread_messages

    counts: dict[str, int] = {}
    for message in get_unread_messages(_MAIN_INBOX_AGENT_ID):
        if str(message.get("type", "")).strip() != "guidance":
            continue
        round_id = str(message.get("round_id", "")).strip()
        if not round_id:
            continue
        counts[round_id] = counts.get(round_id, 0) + 1
    return counts


def _pending_question_live_entry() -> dict[str, Any]:
    pending = _load_pending_question()
    round_id = str(pending.get("round_id", "")).strip()
    if not round_id:
        return {}
    return {
        "id": round_id,
        "title": str(pending.get("round_title", "")).strip(),
        "prompt": str(pending.get("text", "")).strip(),
        "last_user": "",
        "last_assistant": str(pending.get("text", "")).strip(),
        "status": "queued",
        "pending_guidance": 0,
        "subagent_count": 0,
        "running_subagents": 0,
        "started_at": _round_started_iso(round_id),
        "updated_at": str(pending.get("asked_at", "")).strip() or datetime.now(timezone.utc).isoformat(),
    }


def get_live_rounds() -> list[dict[str, Any]]:
    """Return live round summaries for UI context selection and tooling."""
    entries = _session_round_entries()

    from cyrene.subagent import _registry  # noqa: WPS437

    for info in _registry.values():
        round_id = str(info.get("round_id", "")).strip()
        if not round_id:
            continue
        entry = entries.setdefault(round_id, {
            "id": round_id,
            "title": "",
            "prompt": str(info.get("task") or "").strip(),
            "last_user": "",
            "last_assistant": "",
            "status": "done",
            "pending_guidance": 0,
            "subagent_count": 0,
            "running_subagents": 0,
            "started_at": _round_started_iso(round_id) or info.get("created_at"),
            "updated_at": info.get("updated_at") or _round_started_iso(round_id),
        })
        entry["subagent_count"] += 1
        sub_status = str(info.get("status") or "done")
        if sub_status in ("running", "waiting", "resumed"):
            entry["running_subagents"] += 1
            entry["status"] = "running"
        if not entry.get("prompt"):
            entry["prompt"] = str(info.get("task") or "").strip()
        if info.get("updated_at"):
            entry["updated_at"] = info.get("updated_at")
        if info.get("created_at") and not entry.get("started_at"):
            entry["started_at"] = info.get("created_at")

    for round_id, pending_count in _main_inbox_pending_by_round().items():
        entry = entries.setdefault(round_id, {
            "id": round_id,
            "title": "",
            "prompt": "",
            "last_user": "",
            "last_assistant": "",
            "status": "queued",
            "pending_guidance": 0,
            "subagent_count": 0,
            "running_subagents": 0,
            "started_at": _round_started_iso(round_id),
            "updated_at": _round_started_iso(round_id),
        })
        entry["pending_guidance"] = pending_count
        entry["updated_at"] = datetime.now(timezone.utc).isoformat()
        if entry["status"] != "running":
            entry["status"] = "queued"

    pending_question_entry = _pending_question_live_entry()
    if pending_question_entry:
        round_id = pending_question_entry["id"]
        entry = entries.setdefault(round_id, pending_question_entry)
        if not entry.get("title"):
            entry["title"] = pending_question_entry["title"]
        if not entry.get("prompt"):
            entry["prompt"] = pending_question_entry["prompt"]
        if not entry.get("last_assistant"):
            entry["last_assistant"] = pending_question_entry["last_assistant"]
        if entry.get("status") != "running":
            entry["status"] = "queued"
        entry["updated_at"] = pending_question_entry["updated_at"]

    if _active_main_round_id:
        entry = entries.setdefault(_active_main_round_id, {
            "id": _active_main_round_id,
            "title": "",
            "prompt": _active_main_round_public_prompt,
            "last_user": _active_main_round_public_prompt,
            "last_assistant": "",
            "status": "running",
            "pending_guidance": 0,
            "subagent_count": 0,
            "running_subagents": 0,
            "started_at": datetime.fromtimestamp(_active_main_round_started_at, tz=timezone.utc).isoformat() if _active_main_round_started_at else _round_started_iso(_active_main_round_id),
            "updated_at": datetime.now(timezone.utc).isoformat(),
        })
        entry["status"] = "running"
        if _active_main_round_public_prompt and not entry.get("prompt"):
            entry["prompt"] = _active_main_round_public_prompt
        if _active_main_round_started_at and not entry.get("started_at"):
            entry["started_at"] = datetime.fromtimestamp(_active_main_round_started_at, tz=timezone.utc).isoformat()
        entry["updated_at"] = datetime.now(timezone.utc).isoformat()

    live_entries: list[dict[str, Any]] = []
    now = datetime.now(timezone.utc)
    for entry in entries.values():
        if entry.get("status") not in ("running", "queued") and not entry.get("pending_guidance", 0):
            continue
        started_at = entry.get("started_at")
        updated_at = entry.get("updated_at")
        elapsed = "—"
        if started_at:
            try:
                started_dt = datetime.fromisoformat(str(started_at)).astimezone(timezone.utc)
                elapsed = _format_duration((now - started_dt).total_seconds())
            except Exception:
                elapsed = "—"
        live_entries.append({
            "id": entry["id"],
            "title": _round_title_from_entry(entry),
            "prompt": entry.get("prompt", ""),
            "lastUser": entry.get("last_user", ""),
            "lastAssistant": entry.get("last_assistant", ""),
            "status": entry.get("status", "queued"),
            "pendingGuidance": int(entry.get("pending_guidance", 0) or 0),
            "subagentCount": int(entry.get("subagent_count", 0) or 0),
            "runningSubagents": int(entry.get("running_subagents", 0) or 0),
            "startedAt": started_at or "",
            "updatedAt": updated_at or "",
            "elapsed": elapsed,
        })

    live_entries.sort(key=lambda item: item.get("startedAt") or "", reverse=True)
    return live_entries


def query_live_rounds(round_id: str = "") -> str:
    """Summarize currently live rounds for the main agent."""
    rounds = get_live_rounds()
    if round_id:
        rounds = [item for item in rounds if item.get("id") == round_id]
    if not rounds:
        if round_id:
            return f"No live round found for {round_id}."
        return "No live rounds are currently running."

    lines = []
    for item in rounds:
        lines.append(
            f"- {item['id']} | {item['status']} | {item['title']} | elapsed {item['elapsed']} | "
            f"subagents {item['runningSubagents']}/{item['subagentCount']} | pending guidance {item['pendingGuidance']}"
        )
        prompt = item.get("prompt") or item.get("lastUser") or ""
        if prompt:
            lines.append(f"  prompt: {_fallback_label(prompt, limit=120)}")
        last_answer = item.get("lastAssistant") or ""
        if last_answer:
            lines.append(f"  latest reply: {_fallback_label(last_answer, limit=160)}")
    return "\n".join(lines)


async def _publish_round_guidance_update(target_round_id: str) -> None:
    live = next((item for item in get_live_rounds() if item.get("id") == target_round_id), None)
    await debug.publish_event({
        "type": "round_guidance_update",
        "target_round_id": target_round_id,
        "pending_guidance": int(live.get("pendingGuidance", 0) if live else 0),
        "status": live.get("status", "") if live else "",
        "title": live.get("title", "") if live else "",
    })


def _guidance_error_text(exc: Exception) -> str:
    if isinstance(exc, httpx.TimeoutException):
        reason = "the upstream model timed out"
    elif isinstance(exc, httpx.HTTPError):
        reason = "the upstream model request failed"
    else:
        reason = "an internal error occurred while applying the guidance"
    return f"Guidance could not be applied because {reason}."


def format_httpx_error(exc: Exception) -> str:
    parts: list[str] = [type(exc).__name__]
    detail = str(exc or "").strip()
    if detail:
        parts.append(detail)

    request = getattr(exc, "request", None)
    if request is not None:
        method = str(getattr(request, "method", "") or "").strip()
        url = str(getattr(request, "url", "") or "").strip()
        request_part = "request="
        if method:
            request_part += method
        if url:
            request_part += f" {url}" if method else url
        parts.append(request_part)

    response = getattr(exc, "response", None)
    if response is not None:
        parts.append(f"status={response.status_code}")
        try:
            body = str(response.text or "").strip()
        except Exception:
            body = ""
        if body:
            body_preview = re.sub(r"\s+", " ", body)[:500]
            parts.append(f"body={body_preview}")

    cause = getattr(exc, "__cause__", None)
    if cause is not None:
        cause_text = str(cause or "").strip()
        if cause_text:
            parts.append(f"cause={type(cause).__name__}: {cause_text}")
        else:
            parts.append(f"cause={type(cause).__name__}")

    return " | ".join(parts)


def _guidance_ack_text() -> str:
    return "已接受引导。我会按这条新要求调整当前这一轮的工作，并在完成后给你更新。"


def _schedule_session_label_refresh(current_user_message: str, round_id: str) -> None:
    async def _runner() -> None:
        try:
            await _refresh_session_labels(current_user_message, round_id)
        except Exception:
            logger.warning("Async session naming failed for %s", round_id or "<unknown>", exc_info=True)

    task = asyncio.create_task(_runner())
    _pending_label_refreshes.add(task)
    task.add_done_callback(_pending_label_refreshes.discard)


def _guidance_round_context(target_round_id: str, guidance_id: str) -> dict[str, Any]:
    full_messages = _load_session_messages()
    queued_entry = next(
        (
            msg
            for msg in full_messages
            if str(msg.get("queued_guidance_id", "")).strip() == guidance_id
        ),
        {},
    )
    insert_at = next(
        (
            idx
            for idx, msg in enumerate(full_messages)
            if str(msg.get("queued_guidance_id", "")).strip() == guidance_id
        ),
        len(full_messages),
    )
    return {
        "full_messages": full_messages,
        "queued_entry": queued_entry,
        "insert_at": insert_at,
        "persist_base_messages": [
            msg
            for msg in full_messages
            if str(msg.get("queued_guidance_id", "")).strip() != guidance_id
        ],
        "round_history": [
            msg
            for msg in full_messages
            if str(msg.get("round_id", "")).strip() == target_round_id
            and not str(msg.get("queued_guidance_id", "")).strip()
        ],
        "round_title": str(queued_entry.get("round_title", "")).strip(),
        "client_request_id": str(queued_entry.get("client_request_id", "")).strip(),
    }


def _guidance_persist_context_after_ack(guidance_id: str) -> dict[str, Any]:
    full_messages = _load_session_messages()
    ack_index = next(
        (
            idx
            for idx, msg in enumerate(full_messages)
            if str(msg.get("guidance_ack_for_guidance_id", "")).strip() == guidance_id
        ),
        -1,
    )
    queued_index = next(
        (
            idx
            for idx, msg in enumerate(full_messages)
            if str(msg.get("queued_guidance_id", "")).strip() == guidance_id
        ),
        len(full_messages) - 1,
    )
    insert_at = ack_index + 1 if ack_index >= 0 else queued_index + 1
    insert_at = max(0, min(insert_at, len(full_messages)))
    return {
        "persist_base_messages": full_messages,
        "persist_insert_at": insert_at,
    }


def _pending_question_resume_context(question_id: str) -> dict[str, Any]:
    full_messages = _load_session_messages()
    pending = _load_pending_question()
    target_question_id = str(question_id or "").strip()
    if not pending:
        return {}
    if target_question_id and str(pending.get("id", "")).strip() != target_question_id:
        return {}

    target_round_id = str(pending.get("round_id", "")).strip()
    insert_at = next(
        (
            idx + 1
            for idx, msg in enumerate(full_messages)
            if str(msg.get("question_id", "")).strip() == str(pending.get("id", "")).strip()
        ),
        len(full_messages),
    )
    return {
        "pending_question": pending,
        "full_messages": full_messages,
        "persist_base_messages": full_messages,
        "persist_insert_at": insert_at,
        "round_history": [
            msg
            for msg in full_messages
            if str(msg.get("round_id", "")).strip() == target_round_id
        ],
        "round_id": target_round_id,
        "round_title": str(pending.get("round_title", "")).strip(),
        "client_request_id": str(pending.get("client_request_id", "")).strip(),
    }


async def _insert_guidance_reply(
    target_round_id: str,
    guidance_id: str,
    content: str,
    round_title: str = "",
    client_request_id: str = "",
) -> None:
    assistant_entry: dict[str, Any] = {
        "role": "assistant",
        "content": content,
        "round_id": target_round_id,
        "in_reply_to_guidance_id": guidance_id,
    }
    if round_title:
        assistant_entry["round_title"] = round_title
    if client_request_id:
        assistant_entry["client_request_id"] = client_request_id

    async with _session_state_lock:
        state = _load_session_state()
        existing = state.get("messages", [])
        full_messages = list(existing) if isinstance(existing, list) else []
        _ensure_message_identity([assistant_entry])
        replacement_index = next(
            (
                idx
                for idx, msg in enumerate(full_messages)
                if str(msg.get("in_reply_to_guidance_id", "")).strip() == guidance_id
            ),
            -1,
        )
        if replacement_index >= 0:
            full_messages[replacement_index] = assistant_entry
        else:
            ack_index = next(
                (
                    idx
                    for idx, msg in enumerate(full_messages)
                    if str(msg.get("guidance_ack_for_guidance_id", "")).strip() == guidance_id
                ),
                -1,
            )
            insert_at = ack_index if ack_index >= 0 else next(
                (
                    idx
                    for idx, msg in enumerate(full_messages)
                    if str(msg.get("queued_guidance_id", "")).strip() == guidance_id
                ),
                len(full_messages) - 1,
            )
            full_messages.insert(max(0, insert_at + 1), assistant_entry)
        await _write_session_messages_locked(state, full_messages)
    await _publish_runtime_event({
        "type": "chat_message",
        "round_id": target_round_id,
        "client_request_id": client_request_id,
        "guidance_id": guidance_id,
    })


async def _insert_guidance_ack(
    target_round_id: str,
    guidance_id: str,
    round_title: str = "",
    client_request_id: str = "",
) -> None:
    assistant_entry: dict[str, Any] = {
        "role": "assistant",
        "content": _guidance_ack_text(),
        "round_id": target_round_id,
        "guidance_ack_for_guidance_id": guidance_id,
    }
    if round_title:
        assistant_entry["round_title"] = round_title
    async with _session_state_lock:
        state = _load_session_state()
        existing = state.get("messages", [])
        full_messages = list(existing) if isinstance(existing, list) else []
        _ensure_message_identity([assistant_entry])
        replacement_index = next(
            (
                idx
                for idx, msg in enumerate(full_messages)
                if str(msg.get("guidance_ack_for_guidance_id", "")).strip() == guidance_id
            ),
            -1,
        )
        if replacement_index >= 0:
            full_messages[replacement_index] = assistant_entry
        else:
            insert_at = next(
                (
                    idx
                    for idx, msg in enumerate(full_messages)
                    if str(msg.get("queued_guidance_id", "")).strip() == guidance_id
                ),
                len(full_messages) - 1,
            )
            full_messages.insert(max(0, insert_at + 1), assistant_entry)
        await _write_session_messages_locked(state, full_messages)
    await _publish_runtime_event({
        "type": "guidance_acknowledged",
        "round_id": target_round_id,
        "client_request_id": client_request_id,
        "guidance_id": guidance_id,
        "ack_text": assistant_entry["content"],
    })


async def _fan_out_guidance_to_subagents(target_round_id: str, content: str, bot: Any, chat_id: int, db_path: str) -> list[str]:
    from cyrene.inbox import send_message as _send_inbox
    from cyrene.subagent import (
        _run_subagent,
        _spawn_subagent_task,
        get_raw_messages as _sub_raw_msgs,
        get_snapshot as _sub_snapshot,
        reactivate as _sub_reactivate,
    )

    guidance_text = (
        "Main agent received new user guidance for this round.\n"
        "Adjust your work accordingly and revise your result if needed.\n\n"
        f"User guidance:\n{content}"
    )
    snapshot = await _sub_snapshot(round_id=target_round_id)
    if not snapshot:
        return []

    sent: list[str] = []
    for agent_id in snapshot:
        await _send_inbox(_MAIN_INBOX_AGENT_ID, agent_id, "guidance", guidance_text, round_id=target_round_id)
        sent.append(agent_id)

    for agent_id, info in snapshot.items():
        if info.get("status") not in ("done", "timeout"):
            continue
        if await _sub_reactivate(agent_id):
            raw_messages = await _sub_raw_msgs(agent_id)
            _spawn_subagent_task(
                _run_subagent(agent_id, str(info.get("task") or ""), bot, chat_id, db_path, resume_messages=raw_messages),
                agent_id,
            )
    return sent


async def _wait_for_subagent_round(round_id: str, bot: Any, chat_id: int, db_path: str) -> tuple[bool, str]:
    from cyrene.inbox import get_unread_count as _inbox_unread
    from cyrene.subagent import (
        _run_subagent,
        _spawn_subagent_task,
        collect_results as _sub_collect,
        get_raw_messages as _sub_raw_msgs,
        get_snapshot as _sub_snapshot,
        reactivate as _sub_reactivate,
    )

    _interrupt_event.clear()
    interrupted = False
    quiet_ticks = 0
    for _ in range(120):
        try:
            await asyncio.wait_for(_interrupt_event.wait(), timeout=5)
            _interrupt_event.clear()
            interrupted = True
            break
        except asyncio.TimeoutError:
            pass

        snapshot = await _sub_snapshot(round_id=round_id)
        if not snapshot:
            break

        resurrected = False
        for agent_id, info in snapshot.items():
            if info.get("status") not in ("done", "timeout") or _inbox_unread(agent_id) == 0:
                continue
            if await _sub_reactivate(agent_id):
                raw_messages = await _sub_raw_msgs(agent_id)
                _spawn_subagent_task(
                    _run_subagent(agent_id, str(info.get("task") or ""), bot, chat_id, db_path, resume_messages=raw_messages),
                    agent_id,
                )
                resurrected = True

        snapshot = await _sub_snapshot(round_id=round_id)
        all_truly_done = all(
            info.get("status") in ("done", "timeout") and _inbox_unread(agent_id) == 0
            for agent_id, info in snapshot.items()
        )
        if all_truly_done and not resurrected:
            quiet_ticks += 1
            if quiet_ticks >= 2:
                break
        else:
            quiet_ticks = 0

    if interrupted:
        return True, ""

    await asyncio.sleep(2)
    return False, await _sub_collect(round_id=round_id)


async def _synthesize_subagent_results(
    task: str,
    summary: str,
    round_title: str = "",
    guidance: str = "",
    round_history: list[dict[str, Any]] | None = None,
) -> str:
    # Include the main agent's own reasoning and spawn context so the LLM
    # understands what each subagent was asked to do and how we got here.
    context_lines: list[str] = []
    if round_history:
        for msg in round_history[-16:]:
            role = str(msg.get("role", "")).strip()
            if role == "system":
                continue
            content = str(msg.get("content", "")).strip()
            tool_calls = msg.get("tool_calls") or []
            if role == "user" and content:
                label = "User query" if not context_lines else "User"
                context_lines.append(f"[{label}]\n{content[:800]}")
            elif role == "assistant":
                if content:
                    context_lines.append(f"[Assistant reasoning]\n{content[:600]}")
                for tc in tool_calls:
                    fn = tc.get("function", {})
                    name = fn.get("name", "")
                    args = fn.get("arguments", "{}")
                    if name == "spawn_subagent":
                        try:
                            a = json.loads(args)
                            context_lines.append(f"[Spawned subagent: {a.get('agent_id', '?')}]\nTask: {a.get('task', '')[:300]}")
                        except Exception:
                            context_lines.append(f"[Spawned subagent]")
                    elif name == "send_agent_message":
                        try:
                            a = json.loads(args)
                            context_lines.append(f"[Subagent msg: {a.get('from', '?')} -> {a.get('to', '?')}]")
                        except Exception:
                            pass
    context_block = "\n\n".join(context_lines) if context_lines else "—"

    # Build the expert findings block from subagent results
    experts_block = summary.strip() or "(No subagent results.)"

    # Only call LLM synthesis when there are actual multi-subagent findings
    if len(experts_block) < 50:
        return experts_block

    prompt_messages = [
        {
            "role": "system",
            "content": (
                "You are presenting the final answer after subagents completed their tasks.\n\n"
                "Rules:\n"
                "1. First, present EACH subagent's original output in full — verbatim, under their own heading.\n"
                "   This is mandatory. Do not rewrite, truncate, or summarize their work.\n"
                "2. After all subagent outputs, you MAY add a brief synthesis section that connects"
                " or contrasts their perspectives.\n"
                "3. For creative work (poems, code, art descriptions): quote the original completely.\n"
                "4. For research or analysis: present each expert's findings in full, then synthesize.\n\n"
                "Output format:\n"
                "--- <subagent name> ---\n"
                "<their complete original output>\n"
                "...\n"
                "--- Synthesis ---\n"
                "<your synthesis, if needed>"
            ),
        },
        {
            "role": "user",
            "content": (
                f"Task: {task}\n\n"
                f"Round context:\n{context_block}\n\n"
                f"Expert findings from subagents:\n{experts_block}\n\n"
                "Present the final answer following the rules above."
            ),
        },
    ]
    response = await (_call_llm_stream(prompt_messages, max_tokens=None) if _streaming_reply_requested() else _call_llm(prompt_messages, tools=None, max_tokens=None))
    llm_text = _assistant_text(response).strip()
    return llm_text or experts_block


async def _final_reply_from_history(messages: list[dict[str, Any]], max_tokens: int | None = None) -> str:
    response = await (_call_llm_stream(messages, max_tokens=max_tokens) if _streaming_reply_requested() else _call_llm(messages, tools=None, max_tokens=max_tokens))
    return _assistant_text(response).strip() or "Done."


async def _process_main_inbox_message(message: dict[str, Any], bot: Any, chat_id: int, db_path: str) -> str:
    from cyrene.subagent import clear as _sub_clear, get_snapshot as _sub_snapshot

    target_round_id = str(message.get("round_id", "")).strip()
    guidance_id = str(message.get("message_id", "")).strip()
    content = str(message.get("content") or "").strip()
    if not target_round_id or not guidance_id or not content:
        return ""

    context = _guidance_round_context(target_round_id, guidance_id)
    live_round = next((live for live in get_live_rounds() if live.get("id") == target_round_id), None)
    round_title = context["round_title"] or str((live_round or {}).get("title") or "").strip() or target_round_id
    snapshot = await _sub_snapshot(round_id=target_round_id)
    await _insert_guidance_ack(
        target_round_id,
        guidance_id,
        round_title=round_title,
        client_request_id=context["client_request_id"],
    )
    has_live_subagents = bool(
        live_round
        and (
            int(live_round.get("subagentCount", 0) or 0) > 0
            or int(live_round.get("runningSubagents", 0) or 0) > 0
        )
    )
    if has_live_subagents or (live_round is None and snapshot):
        await _publish_runtime_event({
            "type": "phase_transition",
            "round_id": target_round_id,
            "from": "guidance_queue",
            "to": "subagent_guidance",
            "detail": f"Main agent is applying guidance to {len(snapshot)} subagent(s).",
        })
        await _fan_out_guidance_to_subagents(target_round_id, content, bot, chat_id, db_path)
        interrupted, summary = await _wait_for_subagent_round(target_round_id, bot, chat_id, db_path)
        if interrupted:
            reply = "[Sub-agents are still working in the background. The guidance was delivered and the round is continuing.]"
        else:
            reply = await _synthesize_subagent_results(
                task=content,
                summary=summary,
                round_title=round_title,
                guidance=content,
                round_history=context["round_history"],
            )
            await _sub_clear(round_id=target_round_id)
        await _insert_guidance_reply(
            target_round_id,
            guidance_id,
            reply,
            round_title=round_title,
            client_request_id=context["client_request_id"],
        )
        _schedule_session_label_refresh(content, target_round_id)
        return reply

    guidance_system = (
        "This user message came from the main-agent inbox for an earlier round.\n"
        f"Target round id: {target_round_id}\n"
        f"Target round title: {round_title}\n"
        "Treat it as steering or a follow-up for that round. Continue the round instead of starting a fresh topic."
    )
    await _publish_runtime_event({
        "type": "phase_transition",
        "round_id": target_round_id,
        "from": "guidance_queue",
        "to": "guided_round_continuation",
        "detail": "Main agent is continuing the same round with the new guidance.",
    })
    persist_context = _guidance_persist_context_after_ack(guidance_id)
    return await _run_chat_agent(
        content,
        bot,
        chat_id,
        db_path,
        ephemeral_system=guidance_system,
        forced_round_id=target_round_id,
        history_override=context["round_history"],
        persist_base_messages=persist_context["persist_base_messages"],
        persist_insert_at=persist_context["persist_insert_at"],
        client_request_id=context["client_request_id"],
        persist_user_message=False,
    )


async def answer_pending_question(
    question_id: str,
    answer_text: str,
    bot: Any,
    chat_id: int,
    db_path: str,
    client_request_id: str = "",
) -> str:
    context = _pending_question_resume_context(question_id)
    pending = context.get("pending_question", {})
    if not pending:
        raise ValueError("Pending question not found.")

    content = str(answer_text or "").strip()
    if not content:
        raise ValueError("Answer cannot be empty.")

    round_id = str(context.get("round_id", "")).strip()
    if not round_id:
        raise ValueError("Pending question has no round context.")

    cleared = await _clear_pending_question(str(pending.get("id", "")).strip())
    if not cleared:
        raise ValueError("Pending question not found.")

    answer_system = (
        "This user message answers your earlier clarification question for the same round.\n"
        f"Target round id: {round_id}\n"
        f"Original clarification question: {str(pending.get('text', '')).strip()}\n"
        "Treat the new user message as the answer and continue the same round."
    )
    try:
        return await _run_chat_agent(
            content,
            bot,
            chat_id,
            db_path,
            ephemeral_system=answer_system,
            forced_round_id=round_id,
            history_override=context.get("round_history") or [],
            persist_base_messages=context.get("persist_base_messages") or [],
            persist_insert_at=context.get("persist_insert_at"),
            client_request_id=client_request_id,
            persist_user_message=True,
        )
    except Exception:
        await _restore_pending_question(pending)
        raise


def _ensure_main_inbox_worker(bot: Any, chat_id: int, db_path: str) -> None:
    global _main_inbox_worker
    if _main_inbox_worker is None or _main_inbox_worker.done():
        _main_inbox_worker = asyncio.create_task(_drain_main_inbox(bot, chat_id, db_path))


async def queue_round_guidance(
    target_round_id: str,
    content: str,
    bot: Any,
    chat_id: int,
    db_path: str,
    client_request_id: str = "",
) -> dict[str, Any]:
    """Send a follow-up question to the main-agent inbox for a live round."""
    from cyrene.inbox import send_message as _send_inbox

    live = {item["id"]: item for item in get_live_rounds()}
    target = live.get(target_round_id)
    if target is None:
        raise ValueError(f"Round {target_round_id} is not live.")

    created_at = datetime.now(timezone.utc).isoformat()
    guidance_id = await _send_inbox("user", _MAIN_INBOX_AGENT_ID, "guidance", content, round_id=target_round_id)
    if not guidance_id:
        raise ValueError("Failed to send guidance to the main-agent inbox.")
    item = {
        "id": guidance_id,
        "target_round_id": target_round_id,
        "content": content,
        "created_at": created_at,
    }
    labels = get_session_labels(target_round_id)
    queued_user_entry: dict[str, Any] = {
        "role": "user",
        "content": content,
        "round_id": target_round_id,
        "queued_guidance_id": guidance_id,
    }
    if labels.get("round_title"):
        queued_user_entry["round_title"] = labels["round_title"]
    if client_request_id:
        queued_user_entry["client_request_id"] = client_request_id
    await _append_session_message(queued_user_entry)
    await _publish_round_guidance_update(target_round_id)
    _ensure_main_inbox_worker(bot, chat_id, db_path)
    return item


async def _drain_main_inbox(bot: Any, chat_id: int, db_path: str) -> None:
    from cyrene.conversations import archive_exchange
    from cyrene.inbox import get_unread_messages, mark_read_count

    global _main_inbox_worker
    try:
        while True:
            unread = [
                message
                for message in get_unread_messages(_MAIN_INBOX_AGENT_ID)
                if str(message.get("type", "")).strip() == "guidance"
            ]
            if not unread:
                break

            item = unread[0]
            target_round_id = str(item.get("round_id", "")).strip()
            guidance_id = str(item.get("message_id", "")).strip()
            response = ""
            try:
                await _publish_runtime_event({
                    "type": "phase_transition",
                    "round_id": target_round_id,
                    "from": "queued_guidance",
                    "to": "guidance_execution",
                    "detail": "Main agent is now applying the queued guidance.",
                })
                async with _agent_lock:
                    _interrupt_event.clear()
                    response = await _process_main_inbox_message(item, bot, chat_id, db_path)
            except Exception as exc:
                logger.exception("Failed to process main inbox guidance for %s", target_round_id or "<unknown>")
                if target_round_id and guidance_id:
                    context = _guidance_round_context(target_round_id, guidance_id)
                    round_title = context.get("round_title") or next(
                        (live["title"] for live in get_live_rounds() if live.get("id") == target_round_id),
                        target_round_id,
                    )
                    response = _guidance_error_text(exc)
                    await _insert_guidance_reply(
                        target_round_id,
                        guidance_id,
                        response,
                        round_title=round_title,
                        client_request_id=str(context.get("client_request_id") or ""),
                    )
            finally:
                await mark_read_count(_MAIN_INBOX_AGENT_ID, 1)
                if target_round_id:
                    await _publish_round_guidance_update(target_round_id)
            if response and response != _AWAITING_USER_SENTINEL:
                labels = get_session_labels(target_round_id)
                await archive_exchange(
                    str(item.get("content") or ""),
                    response,
                    chat_id,
                    session_title=labels.get("session_title", ""),
                    round_title=labels.get("round_title", ""),
                    round_id=labels.get("round_id", ""),
                    archive_session_id=labels.get("archive_session_id", ""),
                )
    except Exception:
        logger.exception("Failed to drain main inbox")
    finally:
        _main_inbox_worker = None
        if get_live_rounds() and _main_inbox_pending_by_round():
            _ensure_main_inbox_worker(bot, chat_id, db_path)


def get_session_labels(round_id: str = "") -> dict[str, str]:
    state = _load_session_state()
    messages = state.get("messages", []) if isinstance(state.get("messages"), list) else []
    last_round_id = next((str(m.get("round_id", "")).strip() for m in reversed(messages) if m.get("round_id")), "")
    target_round_id = str(round_id or "").strip() or last_round_id
    round_title = next(
        (
            str(m.get("round_title", "")).strip()
            for m in messages
            if str(m.get("round_id", "")).strip() == target_round_id and m.get("round_title")
        ),
        "",
    )
    return {
        "session_title": str(state.get("session_title", "")).strip(),
        "round_title": round_title,
        "round_id": target_round_id,
        "archive_session_id": _ensure_archive_session_id(state),
    }


async def _refresh_session_labels(current_user_message: str, round_id: str) -> None:
    state = _load_session_state()
    messages = state.get("messages", []) if isinstance(state.get("messages"), list) else []
    if not messages:
        return

    session_user_inputs = [
        str(msg.get("content", "")).strip()
        for msg in messages
        if msg.get("role") == "user" and str(msg.get("content", "")).strip()
    ]
    round_user_inputs = [
        str(msg.get("content", "")).strip()
        for msg in messages
        if msg.get("role") == "user"
        and str(msg.get("round_id", "")).strip() == round_id
        and str(msg.get("content", "")).strip()
    ]
    if not round_user_inputs:
        round_user_inputs = [_fallback_label(current_user_message, limit=80)]
    if not session_user_inputs:
        session_user_inputs = round_user_inputs

    round_fallback = _fallback_label(" / ".join(round_user_inputs), limit=40)
    session_fallback = _fallback_label(" / ".join(session_user_inputs), limit=56)
    token = _caller_type.set("session_namer")
    try:
        response = await _call_llm([
            {
                "role": "system",
                "content": (
                    "You generate concise UI labels for chat sessions and rounds. "
                    "Return strict JSON with keys round_title and session_title only."
                ),
            },
            {
                "role": "user",
                "content": (
                    "Summarize the following chat inputs into compact labels.\n"
                    "Rules:\n"
                    "- round_title: summarize only the current round's user input(s)\n"
                    "- session_title: summarize all user inputs in the session so far\n"
                    "- Keep each label under 12 words\n"
                    "- Use the user's language when obvious\n"
                    "- No quotes, markdown, numbering, or trailing punctuation\n\n"
                    f"Current round user inputs:\n{json.dumps(round_user_inputs, ensure_ascii=False)}\n\n"
                    f"All session user inputs:\n{json.dumps(session_user_inputs, ensure_ascii=False)}\n\n"
                    "Return JSON only."
                ),
            },
        ], tools=None)
        payload = _extract_json_object(_assistant_text(response))
    except Exception:
        logger.warning("Session naming failed", exc_info=True)
        payload = {}
    finally:
        _caller_type.reset(token)

    round_title = _fallback_label(payload.get("round_title") or round_fallback, limit=40)
    session_title = _fallback_label(payload.get("session_title") or session_fallback, limit=56)

    async with _session_state_lock:
        latest_state = _load_session_state()
        latest_messages = latest_state.get("messages", [])
        full_messages = list(latest_messages) if isinstance(latest_messages, list) else []
        for msg in full_messages:
            if str(msg.get("round_id", "")).strip() == round_id:
                msg["round_title"] = round_title
        latest_state["messages"] = full_messages
        latest_state["session_title"] = session_title
        _write_session_state(latest_state)


async def _compress_old_messages(all_messages: list[dict]) -> None:
    """
    压缩最早的一部分消息到短期记忆。
    在后台运行，不阻塞对话。
    """
    # 取前 20 条用户+助理消息
    to_compress = [m for m in all_messages[:20] if m["role"] in ("user", "assistant")]
    if not to_compress:
        return

    # 格式化成文本
    lines = []
    for m in to_compress:
        role = "User" if m["role"] == "user" else ASSISTANT_NAME
        content = m.get("content", "")[:200]
        lines.append(f"{role}: {content}")
    text = "\n".join(lines)

    # LLM 调用压缩
    prompt = f"""Extract key information from this conversation. Focus on:
1. Facts about the user (job, preferences, habits)
2. Emotional patterns or recurring topics
3. Action items or decisions made

For each finding, classify as: fact | pattern | preference | emotion

Conversation:
{text}

Output format (one per line, no explanations):
[fact] user works at a tech company
[emotion] user was frustrated about a project deadline
[preference] user likes casual short replies
"""

    try:
        response = await _call_llm([
            {"role": "system", "content": "You extract structured memories from conversations. Be concise."},
            {"role": "user", "content": prompt}
        ], tools=None)
        compressed = _assistant_text(response) or ""
    except Exception:
        logger.warning("Memory compression failed", exc_info=True)
        return

    # 解析并写入短期记忆
    for line in compressed.split("\n"):
        line = line.strip()
        if not line or not line.startswith("["):
            continue
        try:
            closing = line.index("]")
            entry_type = line[1:closing]
            content = line[closing + 1:].strip()
            if content and len(content) > 3:
                touch_entry(content, {
                    "content": content,
                    "type": entry_type,
                    "emotional_valence": -2 if "frustrat" in content.lower() or "stress" in content.lower() or "angry" in content.lower()
                    else 2 if "happy" in content.lower() or "love" in content.lower() or "excit" in content.lower()
                    else 0,
                })
        except (ValueError, IndexError):
            continue


async def clear_session_id() -> None:
    """Clear session, subagent registry, and compress conversation to short-term memory before discarding."""
    from cyrene.inbox import clear_all_inboxes

    global _main_inbox_worker
    for task in list(_pending_interrupt_clearers):
        task.cancel()
    _pending_interrupt_clearers.clear()
    for task in list(_pending_label_refreshes):
        task.cancel()
    _pending_label_refreshes.clear()
    _interrupt_event.clear()
    if _main_inbox_worker is not None:
        _main_inbox_worker.cancel()
        _main_inbox_worker = None
    global _active_main_round_id, _active_main_round_prompt, _active_main_round_public_prompt, _active_main_round_started_at
    _active_main_round_id = ""
    _active_main_round_prompt = ""
    _active_main_round_public_prompt = ""
    _active_main_round_started_at = 0.0
    await _clear_subagents()
    await clear_all_inboxes()
    if STATE_FILE.exists():
        try:
            data = json.loads(STATE_FILE.read_text(encoding="utf-8"))
            msgs = data.get("messages", [])
            if msgs:
                # Session reset should not block on a provider round-trip just to
                # preserve memory. Queue compression and clear the live state now.
                _schedule_memory_compression(msgs)
        except Exception:
            pass
        STATE_FILE.unlink()
    # 不清短期记忆。它用于在 session 重置后注入上下文。


# ---------------------------------------------------------------------------
# Tool: quit (stays here to avoid circular imports — added to TOOL_HANDLERS below)
# ---------------------------------------------------------------------------


async def _tool_quit(args: dict[str, Any], _bot: Any, _chat_id: int, _db_path: str, _notify_state: dict[str, bool] | None) -> str:
    return "Interaction ended."


# Add quit handler to the shared TOOL_HANDLERS dict (from tools.py)
TOOL_HANDLERS["quit"] = _tool_quit


# ---------------------------------------------------------------------------
# LLM call (accepts tools as parameter)
# ---------------------------------------------------------------------------


def _sanitize_messages_for_llm(messages: list[dict]) -> list[dict]:
    """Ensure valid tool_calls/tool message pairing with unique tool_call_ids.

    Handles three classes of corruption that cause LLM APIs to reject the
    conversation history:
    1. Duplicate tool_call_ids (e.g. after a retry round) — regenerated uniquely.
    2. Orphan tool_calls (assistant tool_calls without matching tool responses).
    3. Orphan tool messages (tool messages without a preceding tool_calls).
    """
    import uuid as _uuid

    seen_ids: set[str] = set()
    result: list[dict[str, Any]] = []
    i = 0
    while i < len(messages):
        msg = messages[i]
        role = str(msg.get("role", ""))

        if role == "assistant" and msg.get("tool_calls"):
            tc_list = msg["tool_calls"]
            all_valid = True
            for j, tc in enumerate(tc_list):
                idx = i + 1 + j
                if idx >= len(messages):
                    all_valid = False
                    break
                tm = messages[idx]
                if tm.get("role") != "tool" or tm.get("tool_call_id") != tc.get("id", ""):
                    all_valid = False
                    break

            if all_valid:
                old_ids = [tc.get("id", "") for tc in tc_list]
                has_dupes = any(oid in seen_ids for oid in old_ids)

                if has_dupes:
                    new_msg = dict(msg)
                    new_tc_list = []
                    new_ids = []
                    for tc in tc_list:
                        new_tc = dict(tc)
                        new_id = f"call_{_uuid.uuid4().hex[:12]}"
                        new_tc["id"] = new_id
                        new_tc_list.append(new_tc)
                        new_ids.append(new_id)
                        seen_ids.add(new_id)
                    new_msg["tool_calls"] = new_tc_list
                    result.append(new_msg)
                    for j, new_id in enumerate(new_ids):
                        tool_msg = dict(messages[i + 1 + j])
                        tool_msg["tool_call_id"] = new_id
                        result.append(tool_msg)
                else:
                    for oid in old_ids:
                        seen_ids.add(oid)
                    result.append(msg)
                    for j in range(len(tc_list)):
                        result.append(messages[i + 1 + j])

                i += 1 + len(tc_list)
            else:
                # Orphan tool_calls — skip this assistant message
                i += 1
        elif role == "tool":
            # Orphan tool message — skip
            i += 1
        else:
            result.append(msg)
            i += 1

    return result


def _llm_phase_name(tools: list | None) -> str:
    return "phase1" if tools is _LIGHT_TOOL_DEFS else ("phase2" if tools else "no_tools")


def _build_llm_request(
    messages: list[dict],
    tools: list | None,
    max_tokens: int | None,
    *,
    stream: bool,
) -> tuple[str, str, dict[str, Any], dict[str, str]]:
    model = os.environ.get("OPENAI_MODEL", "deepseek-chat")
    base_url = os.environ.get("OPENAI_BASE_URL", "https://api.deepseek.com/v1")
    api_key = os.environ.get("OPENAI_API_KEY", "")
    endpoint = f"{base_url.rstrip('/')}/chat/completions"
    payload: dict[str, Any] = {
        "model": model,
        "messages": _sanitize_messages_for_llm(messages),
    }
    if max_tokens is not None:
        payload["max_tokens"] = max_tokens
    if "deepseek" in model:
        payload["thinking"] = {"type": "enabled"}
    if tools:
        payload["tools"] = tools
        payload["tool_choice"] = "auto"
    if stream:
        payload["stream"] = True
        payload["stream_options"] = {"include_usage": True}
    headers = {"Content-Type": "application/json"}
    if api_key and api_key.lower() not in ("lmstudio", "dummy", ""):
        headers["Authorization"] = f"Bearer {api_key}"
    return endpoint, model, payload, headers


def _extract_stream_delta_text(delta: dict[str, Any]) -> str:
    content = delta.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, dict) and item.get("type") == "text":
                text = item.get("text")
                if isinstance(text, str):
                    parts.append(text)
        return "".join(parts)
    return ""


async def _call_llm(messages: list[dict], tools: list | None = None, max_tokens: int | None = 32000) -> dict:
    _t0 = __import__("time").monotonic()
    _phase = _llm_phase_name(tools)
    endpoint, _model, payload, headers = _build_llm_request(messages, tools, max_tokens, stream=False)

    transport = httpx.AsyncHTTPTransport(retries=1)
    try:
        async with httpx.AsyncClient(transport=transport, timeout=120.0) as client:
            resp = await client.post(
                endpoint,
                json=payload,
                headers=headers,
            )
            if resp.status_code != 200:
                try:
                    resp.raise_for_status()
                except httpx.HTTPStatusError as exc:
                    logger.error(
                        "Upstream LLM returned non-200 [caller=%s phase=%s model=%s]: %s",
                        _caller_type.get(),
                        _phase,
                        _model,
                        format_httpx_error(exc),
                    )
                    raise
            data = resp.json()
            msg = data["choices"][0]["message"]
            if data.get("usage"):
                msg["usage"] = data["usage"]
            if debug.VERBOSE:
                debug.log_llm_call(_caller_type.get(), _phase, messages, tools, msg, (__import__("time").monotonic() - _t0) * 1000)
            await _publish_runtime_event({
                "type": "llm_call", "caller": _caller_type.get(), "phase": _phase,
                "tools": [t.get("function", {}).get("name") for t in (tools or [])],
                "messages": _sanitize_messages_for_llm(messages),
                "response": msg,
                "usage": data.get("usage") or {},
                "duration_ms": round((__import__("time").monotonic() - _t0) * 1000),
            })
            return msg
    except httpx.TimeoutException as exc:
        logger.exception(
            "Upstream LLM timeout [caller=%s phase=%s model=%s endpoint=%s]: %s",
            _caller_type.get(),
            _phase,
            _model,
            endpoint,
            format_httpx_error(exc),
        )
        raise
    except httpx.HTTPError as exc:
        logger.exception(
            "Upstream LLM HTTP error [caller=%s phase=%s model=%s endpoint=%s]: %s",
            _caller_type.get(),
            _phase,
            _model,
            endpoint,
            format_httpx_error(exc),
        )
        raise


async def _call_llm_stream(messages: list[dict], max_tokens: int | None = 32000) -> dict[str, Any]:
    _t0 = __import__("time").monotonic()
    _phase = _llm_phase_name(None)
    endpoint, _model, payload, headers = _build_llm_request(messages, None, max_tokens, stream=True)

    accumulated: list[str] = []
    usage: dict[str, Any] = {}
    started = False
    transport = httpx.AsyncHTTPTransport(retries=1)
    try:
        async with httpx.AsyncClient(transport=transport, timeout=120.0) as client:
            async with client.stream("POST", endpoint, json=payload, headers=headers) as resp:
                if resp.status_code != 200:
                    try:
                        resp.raise_for_status()
                    except httpx.HTTPStatusError as exc:
                        logger.error(
                            "Upstream LLM returned non-200 [caller=%s phase=%s model=%s stream=true]: %s",
                            _caller_type.get(),
                            _phase,
                            _model,
                            format_httpx_error(exc),
                        )
                        raise
                async for raw_line in resp.aiter_lines():
                    line = str(raw_line or "").strip()
                    if not line:
                        continue
                    if line.startswith("data:"):
                        line = line[5:].strip()
                    if not line:
                        continue
                    if line == "[DONE]":
                        break
                    try:
                        data = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if isinstance(data.get("usage"), dict):
                        usage = data["usage"]
                    for choice in data.get("choices") or []:
                        delta = choice.get("delta") or {}
                        text = _extract_stream_delta_text(delta)
                        if not text:
                            continue
                        if not started:
                            await _emit_reply_stream_event({"type": "reply_start"})
                            started = True
                        accumulated.append(text)
                        await _emit_reply_stream_event({"type": "reply_delta", "delta": text})
        full_text = "".join(accumulated)
        if not started:
            await _emit_reply_stream_event({"type": "reply_start"})
        await _emit_reply_stream_event({"type": "reply_done", "response": full_text})
        msg: dict[str, Any] = {"role": "assistant", "content": full_text}
        if usage:
            msg["usage"] = usage
        if debug.VERBOSE:
            debug.log_llm_call(_caller_type.get(), _phase, messages, None, msg, (__import__("time").monotonic() - _t0) * 1000)
        await _publish_runtime_event({
            "type": "llm_call",
            "caller": _caller_type.get(),
            "phase": _phase,
            "tools": [],
            "response": full_text[:200],
            "tool_calls": [],
            "usage": usage,
            "duration_ms": round((__import__("time").monotonic() - _t0) * 1000),
        })
        return msg
    except httpx.TimeoutException as exc:
        logger.exception(
            "Upstream LLM timeout [caller=%s phase=%s model=%s endpoint=%s stream=true]: %s",
            _caller_type.get(),
            _phase,
            _model,
            endpoint,
            format_httpx_error(exc),
        )
        raise
    except httpx.HTTPError as exc:
        logger.exception(
            "Upstream LLM HTTP error [caller=%s phase=%s model=%s endpoint=%s stream=true]: %s",
            _caller_type.get(),
            _phase,
            _model,
            endpoint,
            format_httpx_error(exc),
        )
        raise


# ---------------------------------------------------------------------------
# Main agent (assistant tone + full tools + session persistence)
# ---------------------------------------------------------------------------


# 轻量 tool：只有 use_tools + quit，用于第一阶段判断是否进重循环
_LIGHT_TOOL_DEFS = [
    {"type": "function", "function": {"name": "use_tools", "description": "MANDATORY gateway to full tool access. Call this for ANY request that involves doing things — file ops, search, web, code, shell, scheduling, sub-agents, data, etc. This is the ONLY way to reach real tools. Skip ONLY for pure conversation (opinions, greetings, conceptual explanations). IMPORTANT: set task to the user's EXACT original message, do not rewrite it.", "parameters": {"type": "object", "properties": {"task": {"type": "string"}}, "required": ["task"]}}},
    {"type": "function", "function": {"name": "ask_user", "description": "Ask the user a clarification question when their request is ambiguous or missing a critical detail. Use freeform by sending only text, or add a short options array when offering structured choices would help. Do not combine this with other tools in the same assistant turn.", "parameters": {"type": "object", "properties": {"text": {"type": "string"}, "options": {"type": "array", "items": {"type": "string"}}}, "required": ["text"]}}},
    {"type": "function", "function": {"name": "quit", "description": "Call this when the interaction is done.", "parameters": {"type": "object", "properties": {}}}},
]


async def _run_main_agent(
    user_message: str,
    history: list,
    bot: Any,
    chat_id: int,
    db_path: str,
    system_prompt: str = "",
    client_request_id: str = "",
    persist_user_message: bool = True,
) -> str:
    """主 Agent：先轻量判断是否需工具，再决定是否进重循环。"""
    _caller_type.set("main_agent")
    suppress_initial_detail = _ui_round_hide_initial_detail.get()
    round_id = _current_round_id.get()
    user_entry = {"role": "user", "content": user_message}
    if round_id:
        user_entry["round_id"] = round_id
    if client_request_id:
        user_entry["client_request_id"] = client_request_id
    if persist_user_message:
        await _append_session_message(user_entry)
    effective_system = system_prompt or _MAIN_AGENT_PROMPT
    phase1_messages = [{"role": "system", "content": effective_system}, *history, user_entry, {"role": "user", "content": _PHASE1_DECISION_PROMPT}]

    def _session_messages_to_save(current_messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
        _flush_intermediate_user_replies(current_messages)
        return [
            message
            for message in current_messages[1:]
            if message["role"] != "system" and (persist_user_message or message is not user_entry)
        ]

    # Phase 1: 轻量调用，无完整工具列表，只有 use_tools + quit
    response = await _call_llm(phase1_messages, tools=_LIGHT_TOOL_DEFS)
    tool_calls = response.get("tool_calls") or []
    invalid_phase1_tools = [
        str(tc.get("function", {}).get("name") or "").strip()
        for tc in tool_calls
        if str(tc.get("function", {}).get("name") or "").strip() not in {"use_tools", "ask_user", "quit", ""}
    ]
    if invalid_phase1_tools:
        retry_messages = [
            *phase1_messages,
            {
                **_assistant_entry_from_response(response, round_id="", include_tool_calls=False),
                "content": _assistant_text(response) or (response.get("content") or ""),
            },
            {
                "role": "user",
                "content": (
                    f"[Decision-phase correction] You attempted unavailable tool(s): {', '.join(invalid_phase1_tools)}. "
                    "Only `use_tools`, `ask_user`, and `quit` are available in this phase. "
                    "If real tool work is needed, call `use_tools` with the user's exact original message. "
                    "If clarification is needed before acting, call `ask_user`. "
                    "Otherwise say there is no suitable tool in this phase."
                ),
            },
        ]
        response = await _call_llm(retry_messages, tools=_LIGHT_TOOL_DEFS)
    tool_calls = response.get("tool_calls") or []
    messages = [{"role": "system", "content": effective_system}, *history, user_entry]
    assistant_entry = _assistant_entry_from_response(response, round_id)
    messages.append(assistant_entry)

    # 如果 LLM 调了 use_tools → 进入重循环（含全部工具）
    use_tools_call = None
    ask_user_call = None
    for tc in tool_calls:
        name = tc.get("function", {}).get("name")
        if name == "use_tools":
            use_tools_call = tc
        elif name == "ask_user":
            ask_user_call = tc
        elif name == "quit":
            if client_request_id:
                messages[-1]["client_request_id"] = client_request_id
            await _save_session_messages(_session_messages_to_save(messages))
            return _assistant_text(response).strip() or "Done."

    if ask_user_call:
        try:
            args = json.loads(ask_user_call["function"].get("arguments") or "{}")
            result = await _execute_tool("ask_user", args, bot, chat_id, db_path, None)
        except Exception as exc:
            result = f"Tool failed: {exc}"
        tool_entry: dict[str, Any] = {"role": "tool", "tool_call_id": ask_user_call["id"], "content": _truncate(result)}
        if round_id:
            tool_entry["round_id"] = round_id
        messages.append(tool_entry)
        if _tool_result_requests_user_input(result):
            return _AWAITING_USER_SENTINEL
        await _save_session_messages(_session_messages_to_save(messages))
        return _assistant_text(response).strip() or str(result)

    if use_tools_call:
        event = {
            "type": "phase_transition",
            "from": "phase1_decision",
            "to": "phase2_execution",
        }
        if not suppress_initial_detail:
            event["detail"] = f"Phase 1 decided to use tools. Task: {user_message[:120]}"
        await _publish_runtime_event(event)
        # Phase 2: 重循环 — 全部工具。使用原始用户消息，不用 LLM 编的 task
        user_entry = {"role": "user", "content": user_message}
        if round_id:
            user_entry["round_id"] = round_id
        if client_request_id:
            user_entry["client_request_id"] = client_request_id
        messages = [{"role": "system", "content": effective_system}, *history, user_entry]

        for _ in range(_MAX_TOOL_ROUNDS):
            response = await _call_llm(messages, tools=get_active_tool_defs())
            entry: dict = {"role": "assistant", "content": response.get("content") or ""}
            if response.get("reasoning_content"):
                entry["reasoning_content"] = response["reasoning_content"]
            if response.get("tool_calls"):
                entry["tool_calls"] = response["tool_calls"]
            if response.get("usage"):
                entry["usage"] = response["usage"]
            if round_id:
                entry["round_id"] = round_id
            messages.append(_apply_assistant_meta(entry))

            tcs = response.get("tool_calls") or []
            if any(t.get("function", {}).get("name") == "quit" for t in tcs):
                await _publish_runtime_event({
                    "type": "phase_transition",
                    "from": "execution",
                    "to": "done",
                    "detail": "Agent called quit",
                })
                if _streaming_reply_requested():
                    messages.pop()
                    final_text = await _final_reply_from_history(messages, max_tokens=None)
                    final_entry: dict[str, Any] = {"role": "assistant", "content": final_text}
                    if client_request_id:
                        final_entry["client_request_id"] = client_request_id
                    if round_id:
                        final_entry["round_id"] = round_id
                    messages.append(_apply_assistant_meta(final_entry))
                    await _save_session_messages(_session_messages_to_save(messages))
                    return final_text
                if client_request_id:
                    messages[-1]["client_request_id"] = client_request_id
                await _save_session_messages(_session_messages_to_save(messages))
                return _assistant_text(response).strip() or "Done."
            if not tcs:
                if _streaming_reply_requested():
                    messages.pop()
                    final_text = await _final_reply_from_history(messages, max_tokens=None)
                    final_entry = {"role": "assistant", "content": final_text}
                    if client_request_id:
                        final_entry["client_request_id"] = client_request_id
                    if round_id:
                        final_entry["round_id"] = round_id
                    messages.append(_apply_assistant_meta(final_entry))
                    await _save_session_messages(_session_messages_to_save(messages))
                    return final_text
                if client_request_id:
                    messages[-1]["client_request_id"] = client_request_id
                await _save_session_messages(_session_messages_to_save(messages))
                return _assistant_text(response).strip() or "Done."

            awaiting_user = False
            spawned = False
            for index, t in enumerate(tcs):
                tool_name = t.get("function", {}).get("name")
                if awaiting_user:
                    skipped_tool_entry: dict[str, Any] = {
                        "role": "tool",
                        "tool_call_id": t["id"],
                        "content": "Skipped because ask_user paused the round until the user answers.",
                    }
                    if round_id:
                        skipped_tool_entry["round_id"] = round_id
                    messages.append(skipped_tool_entry)
                    continue
                try:
                    args = json.loads(t["function"].get("arguments") or "{}")
                    result = await _execute_tool(tool_name, args, bot, chat_id, db_path, None)
                except Exception as e:
                    result = f"Tool failed: {e}"
                tool_entry: dict[str, Any] = {"role": "tool", "tool_call_id": t["id"], "content": _truncate(result)}
                if round_id:
                    tool_entry["round_id"] = round_id
                messages.append(tool_entry)
                if tool_name == "ask_user" and _tool_result_requests_user_input(str(result)):
                    awaiting_user = True
                if tool_name == "spawn_subagent":
                    spawned = True
            if awaiting_user:
                return _AWAITING_USER_SENTINEL
            await _save_session_messages(_session_messages_to_save(messages))

            # 调用了 spawn_subagent → 进入监控模式，不调 LLM，等 subagent 全部安静
            if spawned:
                await _publish_runtime_event({
                    "type": "phase_transition",
                    "from": "phase2_execution",
                    "to": "subagent_monitoring",
                    "detail": "Subagents spawned, entering monitoring loop",
                })
                from cyrene.subagent import (
                    _run_subagent,
                    _spawn_subagent_task,
                    collect_results as _sub_collect,
                    clear as _sub_clear,
                    get_snapshot as _sub_snapshot,
                    get_raw_messages as _sub_raw_msgs,
                    reactivate as _sub_reactivate,
                )
                from cyrene.inbox import get_unread_count as _inbox_unread

                # 新退出条件：所有 agent 都 DONE/TIMEOUT 且 inbox 全部清空。
                # 监控期间，DONE agent 如果收到消息就唤醒它继续处理。
                # 如果用户发来新消息，中断监控让主 agent 立即处理。
                _interrupt_event.clear()
                interrupted = False
                quiet_ticks = 0
                for _ in range(120):  # max 10 min 硬上限
                    try:
                        await asyncio.wait_for(_interrupt_event.wait(), timeout=5)
                        _interrupt_event.clear()
                        interrupted = True
                        break
                    except asyncio.TimeoutError:
                        pass
                    snap = await _sub_snapshot(round_id=round_id)
                    if not snap:
                        break

                    # 1) 唤醒：DONE/TIMEOUT 的 agent 有未读消息 → 重启它的 loop
                    resurrected = False
                    for aid, info in snap.items():
                        if info["status"] in ("done", "timeout") and _inbox_unread(aid) > 0:
                            if await _sub_reactivate(aid):
                                raw = await _sub_raw_msgs(aid)
                                _spawn_subagent_task(
                                    _run_subagent(aid, info["task"], bot, chat_id, db_path, resume_messages=raw),
                                    aid,
                                )
                                resurrected = True

                    # 2) 真正退出条件：所有 agent 都 DONE/TIMEOUT 且没有未读消息
                    snap2 = await _sub_snapshot(round_id=round_id)
                    all_truly_done = all(
                        info["status"] in ("done", "timeout") and _inbox_unread(aid) == 0
                        for aid, info in snap2.items()
                    )
                    if all_truly_done and not resurrected:
                        quiet_ticks += 1
                        if quiet_ticks >= 2:  # 连续两次 tick 都安静 → 真退出
                            break
                    else:
                        quiet_ticks = 0
                if interrupted:
                    await _save_session_messages(_session_messages_to_save(messages))
                    return "[Sub-agents are still working in the background. You can continue the conversation.]"
                # 等 quiescent 后，收集结果
                await asyncio.sleep(2)  # 给 subagent 一点时间写 registry
                summary = await _sub_collect(round_id=round_id)
                await _publish_runtime_event({
                    "type": "phase_transition",
                    "from": "subagent_monitoring",
                    "to": "synthesis",
                    "detail": "All subagents done, synthesizing results",
                })
                final_text = await _synthesize_subagent_results(task=user_message, summary=summary, round_history=messages)
                synthesis_entry: dict[str, Any] = {"role": "assistant", "content": final_text}
                if client_request_id:
                    synthesis_entry["client_request_id"] = client_request_id
                if round_id:
                    synthesis_entry["round_id"] = round_id
                messages.append(_apply_assistant_meta(synthesis_entry))
                # 清空 registry，避免下一轮 spawn 把旧结果混入新 context
                await _sub_clear(round_id=round_id)
                await _save_session_messages(_session_messages_to_save(messages))
                return final_text

        await _save_session_messages(_session_messages_to_save(messages))
        return "Stopped after hitting the tool loop limit."

    event = {
        "type": "phase_transition",
        "from": "phase1_decision",
        "to": "chat_only",
    }
    if not suppress_initial_detail:
        event["detail"] = "Phase 1 decided chat-only, no tools needed"
    await _publish_runtime_event(event)
    # Phase 1 结束：纯聊天，无工具需要
    if _streaming_reply_requested():
        messages = [{"role": "system", "content": effective_system}, *history, user_entry]
        final_text = await _final_reply_from_history(messages, max_tokens=None)
        final_entry = {"role": "assistant", "content": final_text}
        if client_request_id:
            final_entry["client_request_id"] = client_request_id
        if round_id:
            final_entry["round_id"] = round_id
        messages.append(_apply_assistant_meta(final_entry))
        await _save_session_messages(_session_messages_to_save(messages))
        return final_text
    if client_request_id:
        messages[-1]["client_request_id"] = client_request_id
    await _save_session_messages(_session_messages_to_save(messages))
    return _assistant_text(response).strip() or "Done."


# ---------------------------------------------------------------------------
# Execution agent (internal, all tools)
# ---------------------------------------------------------------------------


async def _run_execution_agent(task: str, bot: Any, chat_id: int, db_path: str, notify_state: dict[str, bool] | None = None) -> str:
    _caller_type.set("execution_agent")
    """Execution agent with all tools. Used internally by chat agent."""
    messages = [
        {"role": "system", "content": _EXECUTION_SYSTEM_PROMPT},
        {"role": "user", "content": task},
    ]

    final_text = "Done."
    for _ in range(_MAX_TOOL_ROUNDS):
        response = await _call_llm(messages, tools=get_active_tool_defs())

        assistant_entry: dict[str, Any] = {"role": "assistant"}
        if response.get("content"):
            assistant_entry["content"] = response["content"]
        else:
            assistant_entry["content"] = ""
        if response.get("tool_calls"):
            assistant_entry["tool_calls"] = response["tool_calls"]
        if response.get("reasoning_content"):
            assistant_entry["reasoning_content"] = response["reasoning_content"]
        if response.get("usage"):
            assistant_entry["usage"] = response["usage"]
        messages.append(assistant_entry)

        tool_calls = response.get("tool_calls") or []

        # Check for quit
        if any(tc.get("function", {}).get("name") == "quit" for tc in tool_calls):
            final_text = _assistant_text(response) or "Done."
            break

        if not tool_calls:
            return _assistant_text(response) or "Done."

        for tc in tool_calls:
            call_id = tc["id"]
            fn = tc["function"]
            name = fn["name"]
            try:
                args = json.loads(fn.get("arguments") or "{}")
                result = await _execute_tool(name, args, bot, chat_id, db_path, notify_state)
            except Exception as e:
                result = f"Tool {name} failed: {e}"
            messages.append({"role": "tool", "tool_call_id": call_id, "content": _truncate(result)})

    return final_text


# ---------------------------------------------------------------------------
# Chat agent (entry point)
# ---------------------------------------------------------------------------


async def run_agent(user_message: str, bot: Any, chat_id: int, db_path: str, client_request_id: str = "") -> str:
    """Main entry point. Runs the main agent loop with full tools."""
    if _agent_lock.locked():
        interrupt_active_run()
    async with _agent_lock:
        _interrupt_event.clear()
        if client_request_id:
            return await _run_chat_agent(user_message, bot, chat_id, db_path, client_request_id=client_request_id)
        return await _run_chat_agent(user_message, bot, chat_id, db_path)


async def _clear_interrupt_when_idle() -> None:
    try:
        while _agent_lock.locked():
            await asyncio.sleep(0.05)
    finally:
        _interrupt_event.clear()


def interrupt_active_run() -> bool:
    """Best-effort interrupt for the currently running main-agent request."""
    if not _agent_lock.locked():
        _interrupt_event.clear()
        return False
    _interrupt_event.set()
    task = asyncio.create_task(_clear_interrupt_when_idle())
    _pending_interrupt_clearers.add(task)
    task.add_done_callback(_pending_interrupt_clearers.discard)
    return True


async def _run_chat_agent(
    user_message: str,
    bot: Any,
    chat_id: int,
    db_path: str,
    ephemeral_system: str = "",
    forced_round_id: str = "",
    history_override: list[dict[str, Any]] | None = None,
    persist_base_messages: list[dict[str, Any]] | None = None,
    persist_insert_at: int | None = None,
    client_request_id: str = "",
    persist_user_message: bool = True,
    public_prompt: str | None = None,
    refresh_labels: bool = True,
    hide_initial_detail: bool = False,
    assistant_message_meta: dict[str, Any] | None = None,
) -> str:
    """Coordinator: main agent loop."""
    import time as _time

    round_id = str(forced_round_id or "").strip() or f"round_{int(_time.time() * 1000)}"
    round_token = _current_round_id.set(round_id)
    full_session_messages = _load_session_messages()
    global _active_main_round_id, _active_main_round_prompt, _active_main_round_public_prompt, _active_main_round_started_at
    _active_main_round_id = round_id
    _active_main_round_prompt = user_message
    _active_main_round_public_prompt = user_message if public_prompt is None else str(public_prompt)
    _active_main_round_started_at = _time.time()
    history = list(history_override) if history_override is not None else _load_session_messages()
    merge_base = persist_base_messages
    merge_insert_at = persist_insert_at
    merge_live_state = history_override is None
    if history_override is not None and merge_base is None:
        merge_base = list(full_session_messages)
        merge_insert_at = len(merge_base)
        merge_live_state = False
    elif merge_live_state and merge_insert_at is None:
        merge_insert_at = len(history)
    base_token = _persist_base_messages.set(merge_base)
    merge_live_token = _persist_merge_live_state.set(merge_live_state and merge_base is None)
    prefix_token = _persist_history_prefix_len.set(len(history) if (merge_base is not None or merge_live_state) else 0)
    insert_token = _persist_insert_at.set(merge_insert_at if (merge_base is not None or merge_live_state) else None)
    client_request_token = _current_client_request_id.set(client_request_id)
    intermediate_reply_token = _pending_intermediate_user_replies.set([])
    hide_initial_detail_token = _ui_round_hide_initial_detail.set(bool(hide_initial_detail))
    assistant_meta_token = _ui_round_assistant_meta.set(dict(assistant_message_meta) if assistant_message_meta else None)
    try:
        # 如果 history 为空（session 被重置），注入短期记忆
        restored_short_term = False
        if not history:
            st = get_context(max_chars=5000)
            if st:
                history = [{"role": "system", "content": "[Restored context]\n" + st}]
                restored_short_term = True
        if ephemeral_system:
            history = [*history, {"role": "system", "content": ephemeral_system}]

        # 组装记忆上下文注入主 Agent 的 system prompt
        try:
            memory_context = get_memory_context(include_short_term=not restored_short_term)
        except TypeError as exc:
            if "include_short_term" not in str(exc):
                raise
            memory_context = get_memory_context()
        main_system = _MAIN_AGENT_PROMPT
        if memory_context:
            main_system = _MAIN_AGENT_PROMPT + "\n\n## Memory Context\n" + memory_context

        # ====== 主 Agent ======
        main_text = await _run_main_agent(
            user_message,
            history,
            bot,
            chat_id,
            db_path,
            main_system,
            client_request_id=client_request_id,
            persist_user_message=persist_user_message,
        )

        if refresh_labels:
            await _refresh_session_labels(user_message, round_id)
        if main_text == _AWAITING_USER_SENTINEL:
            return main_text
        await _publish_runtime_event({
            "type": "chat_message",
            "client_request_id": client_request_id,
        })
        return main_text or "Done."
    finally:
        _ui_round_assistant_meta.reset(assistant_meta_token)
        _ui_round_hide_initial_detail.reset(hide_initial_detail_token)
        _pending_intermediate_user_replies.reset(intermediate_reply_token)
        _current_client_request_id.reset(client_request_token)
        _persist_insert_at.reset(insert_token)
        _persist_history_prefix_len.reset(prefix_token)
        _persist_merge_live_state.reset(merge_live_token)
        _persist_base_messages.reset(base_token)
        _active_main_round_id = ""
        _active_main_round_prompt = ""
        _active_main_round_public_prompt = ""
        _active_main_round_started_at = 0.0
        _current_round_id.reset(round_token)


# ---------------------------------------------------------------------------
# Backward-compatible public API
# ---------------------------------------------------------------------------


async def run_task_agent(prompt: str, bot: Any, chat_id: int, db_path: str, notify_state: dict[str, bool] | None = None) -> str:
    """Alias for execution agent (no session). Used by scheduler."""
    return await _run_execution_agent(prompt, bot, chat_id, db_path, notify_state=notify_state)


async def run_heartbeat_agent(prompt: str, bot: Any, chat_id: int, db_path: str) -> str:
    """Run a full main-agent loop for proactive user-visible check-ins.

    The internal scheduler prompt is hidden from the Web UI. The final reply is
    persisted as a normal assistant message and must read like a direct message
    to the user, not a report about the hidden task.
    """
    proactive_system = (
        "This round was initiated by the scheduler, not by a user chat message.\n"
        "The hidden task you receive is internal guidance, not text to answer literally.\n"
        "Your final assistant reply will be shown directly to the user in the Web UI.\n"
        "Write to the user in a natural, user-facing voice.\n"
        "Do not mention the scheduler, heartbeat, lottery, hidden prompt, or internal instructions.\n"
        "If you decide to speak, send one concise, useful proactive message to the user.\n"
        "If tools are useful, use the normal main-agent loop and let the UI show the later details."
    )
    if _agent_lock.locked():
        return ""
    async with _agent_lock:
        _interrupt_event.clear()
        return await _run_chat_agent(
            prompt,
            bot,
            chat_id,
            db_path,
            ephemeral_system=proactive_system,
            persist_user_message=False,
            public_prompt="",
            refresh_labels=False,
            hide_initial_detail=True,
            assistant_message_meta={"proactive": True, "system_initiated": True},
        )


async def run_steward_agent(conversation_text: str, soulmd_content: str, bot: Any, chat_id: int, db_path: str) -> str:
    """Steward Agent call. Reads recent conversation + current SOUL.md, outputs modification instructions.
    Uses a different system prompt and no session persistence.
    """
    steward_prompt = f"""You are a memory steward. Your job is to update Cyrene's SOUL.md based on recent conversations.

Read the recent conversation and current SOUL.md, then output:
- APPEND: what new information to add
- ERASE: what old information to remove
- MERGE: what to consolidate
- Or SKIP if nothing important

SOUL.md:
{soulmd_content}

Recent conversation:
{conversation_text}

Output only the modifications needed, one per line, prefixed with APPEND/ERASE/MERGE/SKIP."""

    return await _run_execution_agent(steward_prompt, bot, chat_id, db_path)
