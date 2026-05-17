import asyncio
import json
import logging
import os
import re
from datetime import datetime, timezone
from typing import Any

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
# 当前调用者类型，用于 debug 日志
_caller_type: ContextVar[str] = ContextVar("_caller_type", default="main_agent")
_persist_base_messages: ContextVar[list[dict[str, Any]] | None] = ContextVar("_persist_base_messages", default=None)
_persist_merge_live_state: ContextVar[bool] = ContextVar("_persist_merge_live_state", default=False)
_persist_history_prefix_len: ContextVar[int] = ContextVar("_persist_history_prefix_len", default=0)
_persist_insert_at: ContextVar[int | None] = ContextVar("_persist_insert_at", default=None)
_agent_lock = asyncio.Lock()
_session_state_lock = asyncio.Lock()
_interrupt_event = asyncio.Event()
_MAX_HISTORY_MESSAGES = 40
_MAX_TOOL_ROUNDS = 12
# 后台 compressor 任务，防止被事件循环 GC
_pending_compressors: set[asyncio.Task] = set()
_pending_interrupt_clearers: set[asyncio.Task] = set()
_guidance_counter = 0
_round_guidance_queues: dict[str, list[dict[str, Any]]] = {}
_round_guidance_workers: dict[str, asyncio.Task] = {}
_active_main_round_id = ""
_active_main_round_prompt = ""
_active_main_round_started_at = 0.0

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
- Use tools when helpful: files, search, web, code, sub-agents, etc.
- When a task is complete, call the `quit` tool.
"""

_PHASE1_DECISION_PROMPT = """Decision phase rules:
- The only available tools right now are `use_tools` and `quit`.
- Never call concrete tools such as `WebSearch`, `Bash`, `Read`, or `spawn_subagent` directly in this phase.
- If you need any real tool work, call `use_tools` with the user's exact original message.
- If neither available tool fits, say clearly that there is no suitable tool in this phase.
"""

_EXECUTION_SYSTEM_PROMPT = """You are a capable execution agent. Your job is to complete tasks using tools.

Rules:
- Use tools to complete the task efficiently.
- Read/Write/Edit files, run Bash commands, search the web as needed.
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


def _trim_session_messages(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    trimmed = messages[-_MAX_HISTORY_MESSAGES:]
    # 移除截断处孤立的 tool_calls（DeepSeek 要求 tool_calls 必须有对应的 tool response）
    for i in range(len(trimmed) - 1, -1, -1):
        if trimmed[i].get("tool_calls") and (i + 1 >= len(trimmed) or trimmed[i + 1].get("role") != "tool"):
            return trimmed[:i]
    return trimmed


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
        task = asyncio.create_task(_compress_old_messages(messages))
        _pending_compressors.add(task)
        task.add_done_callback(_pending_compressors.discard)



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
        await _write_session_messages_locked(state, full_messages)


async def _publish_runtime_event(event: dict[str, Any]) -> None:
    """Publish a UI/runtime event annotated with the current round when present."""
    round_id = _current_round_id.get()
    if round_id and not str(event.get("round_id", "")).strip():
        event = {**event, "round_id": round_id}
    await debug.publish_event(event)


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
    return entries


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

    for round_id, queue in _round_guidance_queues.items():
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
        entry["pending_guidance"] = len(queue)
        if queue:
            latest = queue[-1]
            entry["updated_at"] = latest.get("created_at") or entry.get("updated_at")
            if entry["status"] != "running":
                entry["status"] = "queued"

    if _active_main_round_id:
        entry = entries.setdefault(_active_main_round_id, {
            "id": _active_main_round_id,
            "title": "",
            "prompt": _active_main_round_prompt,
            "last_user": _active_main_round_prompt,
            "last_assistant": "",
            "status": "running",
            "pending_guidance": 0,
            "subagent_count": 0,
            "running_subagents": 0,
            "started_at": datetime.fromtimestamp(_active_main_round_started_at, tz=timezone.utc).isoformat() if _active_main_round_started_at else _round_started_iso(_active_main_round_id),
            "updated_at": datetime.now(timezone.utc).isoformat(),
        })
        entry["status"] = "running"
        if _active_main_round_prompt and not entry.get("prompt"):
            entry["prompt"] = _active_main_round_prompt
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


async def queue_round_guidance(target_round_id: str, content: str, bot: Any, chat_id: int, db_path: str) -> dict[str, Any]:
    """Queue a follow-up question for a live round without interrupting current output."""
    live = {item["id"]: item for item in get_live_rounds()}
    target = live.get(target_round_id)
    if target is None:
        raise ValueError(f"Round {target_round_id} is not live.")

    global _guidance_counter
    _guidance_counter += 1
    item = {
        "id": f"guide_{_guidance_counter}",
        "target_round_id": target_round_id,
        "content": content,
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    labels = get_session_labels(target_round_id)
    queued_user_entry: dict[str, Any] = {
        "role": "user",
        "content": content,
        "round_id": target_round_id,
        "queued_guidance_id": item["id"],
    }
    if labels.get("round_title"):
        queued_user_entry["round_title"] = labels["round_title"]
    await _append_session_message(queued_user_entry)
    _round_guidance_queues.setdefault(target_round_id, []).append(item)
    await _publish_round_guidance_update(target_round_id)

    worker = _round_guidance_workers.get(target_round_id)
    if worker is None or worker.done():
        _round_guidance_workers[target_round_id] = asyncio.create_task(
            _drain_round_guidance(target_round_id, bot, chat_id, db_path)
        )
    return item


async def _drain_round_guidance(target_round_id: str, bot: Any, chat_id: int, db_path: str) -> None:
    from cyrene.conversations import archive_exchange

    try:
        while _round_guidance_queues.get(target_round_id):
            item = _round_guidance_queues[target_round_id][0]
            title = next((live["title"] for live in get_live_rounds() if live.get("id") == target_round_id), target_round_id)
            full_messages = _load_session_messages()
            insert_at = next(
                (
                    idx
                    for idx, msg in enumerate(full_messages)
                    if str(msg.get("queued_guidance_id", "")).strip() == str(item.get("id", "")).strip()
                ),
                len(full_messages),
            )
            persist_base_messages = [
                msg
                for msg in full_messages
                if str(msg.get("queued_guidance_id", "")).strip() != str(item.get("id", "")).strip()
            ]
            round_history = [
                msg
                for msg in full_messages
                if str(msg.get("round_id", "")).strip() == target_round_id and not str(msg.get("queued_guidance_id", "")).strip()
            ]
            guidance_system = (
                "This user message is queued guidance for a still-running earlier round.\n"
                f"Target round id: {target_round_id}\n"
                f"Target round title: {title}\n"
                "Treat the user's message as steering or a remaining question for that background work. "
                "Do not pretend the target round has already concluded unless the evidence in context shows that it has."
            )
            async with _agent_lock:
                response = await _run_chat_agent(
                    str(item.get("content") or ""),
                    bot,
                    chat_id,
                    db_path,
                    ephemeral_system=guidance_system,
                    forced_round_id=target_round_id,
                    history_override=round_history,
                    persist_base_messages=persist_base_messages,
                    persist_insert_at=insert_at,
                )
            labels = get_session_labels(target_round_id)
            await archive_exchange(
                str(item.get("content") or ""),
                response,
                chat_id,
                session_title=labels.get("session_title", ""),
                round_title=labels.get("round_title", ""),
                round_id=labels.get("round_id", ""),
            )
            queue = _round_guidance_queues.get(target_round_id, [])
            if queue and queue[0].get("id") == item.get("id"):
                queue.pop(0)
            if not queue:
                _round_guidance_queues.pop(target_round_id, None)
            await _publish_round_guidance_update(target_round_id)
    except Exception:
        logger.exception("Failed to drain guidance queue for %s", target_round_id)
    finally:
        _round_guidance_workers.pop(target_round_id, None)
        if _round_guidance_queues.get(target_round_id):
            _round_guidance_workers[target_round_id] = asyncio.create_task(
                _drain_round_guidance(target_round_id, bot, chat_id, db_path)
            )


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

    for task in list(_pending_interrupt_clearers):
        task.cancel()
    _pending_interrupt_clearers.clear()
    _interrupt_event.clear()
    for task in list(_round_guidance_workers.values()):
        task.cancel()
    _round_guidance_workers.clear()
    _round_guidance_queues.clear()
    global _active_main_round_id, _active_main_round_prompt, _active_main_round_started_at
    _active_main_round_id = ""
    _active_main_round_prompt = ""
    _active_main_round_started_at = 0.0
    await _clear_subagents()
    await clear_all_inboxes()
    if STATE_FILE.exists():
        try:
            data = json.loads(STATE_FILE.read_text(encoding="utf-8"))
            msgs = data.get("messages", [])
            if msgs:
                await _compress_old_messages(msgs)
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


async def _call_llm(messages: list[dict], tools: list | None = None, max_tokens: int | None = 32000) -> dict:
    _t0 = __import__("time").monotonic()
    _phase = "phase1" if tools is _LIGHT_TOOL_DEFS else ("phase2" if tools else "no_tools")
    _model = os.environ.get("OPENAI_MODEL", "deepseek-chat")
    _base_url = os.environ.get("OPENAI_BASE_URL", "https://api.deepseek.com/v1")
    _api_key = os.environ.get("OPENAI_API_KEY", "")
    payload = {
        "model": _model,
        "messages": messages,
    }
    if max_tokens is not None:
        payload["max_tokens"] = max_tokens
    if "deepseek" in _model:
        payload["thinking"] = {"type": "enabled"}
    if tools:
        payload["tools"] = tools
        payload["tool_choice"] = "auto"

    headers = {"Content-Type": "application/json"}
    if _api_key and _api_key.lower() not in ("lmstudio", "dummy", ""):
        headers["Authorization"] = f"Bearer {_api_key}"

    transport = httpx.AsyncHTTPTransport(retries=1)
    async with httpx.AsyncClient(transport=transport, timeout=120.0) as client:
        resp = await client.post(
            f"{_base_url.rstrip('/')}/chat/completions",
            json=payload,
            headers=headers,
        )
        if resp.status_code != 200:
            logger.error("LLM API error %s: %s", resp.status_code, resp.text[:500])
            resp.raise_for_status()
        data = resp.json()
        msg = data["choices"][0]["message"]
        if data.get("usage"):
            msg["usage"] = data["usage"]
        if debug.VERBOSE:
            debug.log_llm_call(_caller_type.get(), _phase, messages, tools, msg, (__import__("time").monotonic() - _t0) * 1000)
        await _publish_runtime_event({
            "type": "llm_call", "caller": _caller_type.get(), "phase": _phase,
            "tools": [t.get("function", {}).get("name") for t in (tools or [])],
            "response": _assistant_text(msg)[:200],
            "tool_calls": [{"name": tc["function"]["name"], "args": tc["function"].get("arguments", "")[:100]}
                          for tc in (msg.get("tool_calls") or [])],
            "usage": data.get("usage") or {},
            "duration_ms": round((__import__("time").monotonic() - _t0) * 1000),
        })
        return msg


# ---------------------------------------------------------------------------
# Main agent (assistant tone + full tools + session persistence)
# ---------------------------------------------------------------------------


# 轻量 tool：只有 use_tools + quit，用于第一阶段判断是否进重循环
_LIGHT_TOOL_DEFS = [
    {"type": "function", "function": {"name": "use_tools", "description": "Call this when the user asks you to DO something (file ops, search, code, web, spawn_subagent, etc.). Not needed for chat only. IMPORTANT: set task to the user's EXACT original message, do not rewrite it.", "parameters": {"type": "object", "properties": {"task": {"type": "string"}}, "required": ["task"]}}},
    {"type": "function", "function": {"name": "quit", "description": "Call this when the interaction is done.", "parameters": {"type": "object", "properties": {}}}},
]


async def _run_main_agent(user_message: str, history: list, bot: Any, chat_id: int, db_path: str, system_prompt: str = "") -> str:
    """主 Agent：先轻量判断是否需工具，再决定是否进重循环。"""
    _caller_type.set("main_agent")
    round_id = _current_round_id.get()
    user_entry = {"role": "user", "content": user_message}
    if round_id:
        user_entry["round_id"] = round_id
    await _append_session_message(user_entry)
    effective_system = system_prompt or _MAIN_AGENT_PROMPT
    phase1_messages = [{"role": "system", "content": effective_system}, *history, user_entry, {"role": "user", "content": _PHASE1_DECISION_PROMPT}]

    # Phase 1: 轻量调用，无完整工具列表，只有 use_tools + quit
    response = await _call_llm(phase1_messages, tools=_LIGHT_TOOL_DEFS)
    tool_calls = response.get("tool_calls") or []
    invalid_phase1_tools = [
        str(tc.get("function", {}).get("name") or "").strip()
        for tc in tool_calls
        if str(tc.get("function", {}).get("name") or "").strip() not in {"use_tools", "quit", ""}
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
                    "Only `use_tools` and `quit` are available in this phase. "
                    "If real tool work is needed, call `use_tools` with the user's exact original message. "
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
    for tc in tool_calls:
        name = tc.get("function", {}).get("name")
        if name == "use_tools":
            use_tools_call = tc
        elif name == "quit":
            session_msgs = [m for m in messages[1:] if m["role"] != "system"]
            await _save_session_messages(session_msgs)
            return _assistant_text(response).strip() or "Done."

    if use_tools_call:
        await _publish_runtime_event({
            "type": "phase_transition",
            "from": "phase1_decision",
            "to": "phase2_execution",
            "detail": f"Phase 1 decided to use tools. Task: {user_message[:120]}",
        })
        # Phase 2: 重循环 — 全部工具。使用原始用户消息，不用 LLM 编的 task
        user_entry = {"role": "user", "content": user_message}
        if round_id:
            user_entry["round_id"] = round_id
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
            messages.append(entry)

            tcs = response.get("tool_calls") or []
            if any(t.get("function", {}).get("name") == "quit" for t in tcs):
                await _publish_runtime_event({
                    "type": "phase_transition",
                    "from": "execution",
                    "to": "done",
                    "detail": "Agent called quit",
                })
                await _save_session_messages([m for m in messages[1:] if m["role"] != "system"])
                return _assistant_text(response).strip() or "Done."
            if not tcs:
                await _save_session_messages([m for m in messages[1:] if m["role"] != "system"])
                return _assistant_text(response).strip() or "Done."

            spawned = False
            for t in tcs:
                try:
                    args = json.loads(t["function"].get("arguments") or "{}")
                    result = await _execute_tool(t["function"]["name"], args, bot, chat_id, db_path, None)
                except Exception as e:
                    result = f"Tool failed: {e}"
                tool_entry: dict[str, Any] = {"role": "tool", "tool_call_id": t["id"], "content": _truncate(result)}
                if round_id:
                    tool_entry["round_id"] = round_id
                messages.append(tool_entry)
                if t.get("function", {}).get("name") == "spawn_subagent":
                    spawned = True
            await _save_session_messages([m for m in messages[1:] if m["role"] != "system"])

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
                    await _save_session_messages([m for m in messages[1:] if m["role"] != "system"])
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
                # 用 LLM 综合结果
                synthesis = await _call_llm([
                    {"role": "system", "content": "You are a research synthesizer. Combine the following expert findings into a clear, structured answer. Preserve all factual claims and cite sources when provided."},
                    {"role": "user", "content": f"Task: {user_message}\n\nExpert findings:\n{summary}"}
                ], tools=None)
                final_text = _assistant_text(synthesis) or summary
                synthesis_entry: dict[str, Any] = {"role": "assistant", "content": final_text}
                if synthesis.get("usage"):
                    synthesis_entry["usage"] = synthesis["usage"]
                if round_id:
                    synthesis_entry["round_id"] = round_id
                messages.append(synthesis_entry)
                # 清空 registry，避免下一轮 spawn 把旧结果混入新 context
                await _sub_clear(round_id=round_id)
                await _save_session_messages([m for m in messages[1:] if m["role"] != "system"])
                return final_text

        await _save_session_messages([m for m in messages[1:] if m["role"] != "system"])
        return "Stopped after hitting the tool loop limit."

    await _publish_runtime_event({
        "type": "phase_transition",
        "from": "phase1_decision",
        "to": "chat_only",
        "detail": "Phase 1 decided chat-only, no tools needed",
    })
    # Phase 1 结束：纯聊天，无工具需要
    session_msgs = [m for m in messages[1:] if m["role"] != "system"]
    await _save_session_messages(session_msgs)
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


async def run_agent(user_message: str, bot: Any, chat_id: int, db_path: str) -> str:
    """Main entry point. Runs the main agent loop with full tools."""
    if _agent_lock.locked():
        interrupt_active_run()
    async with _agent_lock:
        _interrupt_event.clear()
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
) -> str:
    """Coordinator: main agent loop."""
    import time as _time

    round_id = str(forced_round_id or "").strip() or f"round_{int(_time.time() * 1000)}"
    round_token = _current_round_id.set(round_id)
    full_session_messages = _load_session_messages()
    global _active_main_round_id, _active_main_round_prompt, _active_main_round_started_at
    _active_main_round_id = round_id
    _active_main_round_prompt = user_message
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
    try:
        # 如果 history 为空（session 被重置），注入短期记忆
        if not history:
            st = get_context(max_chars=5000)
            if st:
                history = [{"role": "system", "content": "[Restored context]\n" + st}]
        if ephemeral_system:
            history = [*history, {"role": "system", "content": ephemeral_system}]

        # 组装记忆上下文注入主 Agent 的 system prompt
        memory_context = get_memory_context()
        main_system = _MAIN_AGENT_PROMPT
        if memory_context:
            main_system = _MAIN_AGENT_PROMPT + "\n\n## Memory Context\n" + memory_context

        # ====== 主 Agent ======
        main_text = await _run_main_agent(user_message, history, bot, chat_id, db_path, main_system)

        await _refresh_session_labels(user_message, round_id)
        await _publish_runtime_event({"type": "chat_message"})
        return main_text or "Done."
    finally:
        _persist_insert_at.reset(insert_token)
        _persist_history_prefix_len.reset(prefix_token)
        _persist_merge_live_state.reset(merge_live_token)
        _persist_base_messages.reset(base_token)
        _active_main_round_id = ""
        _active_main_round_prompt = ""
        _active_main_round_started_at = 0.0
        _current_round_id.reset(round_token)


# ---------------------------------------------------------------------------
# Backward-compatible public API
# ---------------------------------------------------------------------------


async def run_task_agent(prompt: str, bot: Any, chat_id: int, db_path: str, notify_state: dict[str, bool] | None = None) -> str:
    """Alias for execution agent (no session). Used by scheduler."""
    return await _run_execution_agent(prompt, bot, chat_id, db_path, notify_state=notify_state)


async def run_heartbeat_agent(prompt: str, bot: Any, chat_id: int, db_path: str) -> str:
    """Alias for execution agent (no session). Used by heartbeat."""
    return await _run_execution_agent(prompt, bot, chat_id, db_path)


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
