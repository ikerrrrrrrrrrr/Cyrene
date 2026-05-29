"""Session persistence: message I/O, pending questions, labels, lifecycle.

Depends on ``state`` (ContextVars, ``_call_llm``) and ``message``
(message utilities), but not on ``guidance``, ``coordinator``, or ``agent``.
"""

import asyncio
import json
import logging
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

import cyrene.agent.state as _state
from cyrene import debug
from cyrene.agent.message import (
    _dedupe_messages_by_id,
    _ensure_message_identity,
    _extract_json_object,
    _fallback_label,
    _is_replaceable_live_message,
    _merge_message_sequence,
    _message_suffix_after_persisted_prefix,
)
from cyrene.agent.state import (
    ASSISTANT_NAME,
    _call_llm,
    _caller_type,
    _current_agent_id,
    _current_round_id,
    _current_client_request_id,
    _MAX_HISTORY_MESSAGES,
    _pending_compressors,
    _pending_label_refreshes,
    _persist_base_messages,
    _persist_history_prefix_len,
    _persist_insert_at,
    _persist_merge_live_state,
    _publish_runtime_event,
    _REPORT_REF_MAX_PREVIEW,
    _REPORT_REF_PREFIX,
    _session_state_lock,
)
from cyrene.llm import _assistant_text

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Session state I/O
# ---------------------------------------------------------------------------

def _load_session_state() -> dict[str, Any]:
    if not _state.STATE_FILE.exists():
        return {}
    try:
        data = json.loads(_state.STATE_FILE.read_text(encoding="utf-8"))
    except Exception:
        logger.exception("Failed to read state file")
        return {}
    return data if isinstance(data, dict) else {}


def _write_session_state(state: dict[str, Any]) -> None:
    _state.DATA_DIR.mkdir(parents=True, exist_ok=True)
    _state.STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


def _ensure_archive_session_id(state: dict[str, Any]) -> str:
    archive_session_id = str(state.get("archive_session_id", "")).strip()
    if not archive_session_id:
        archive_session_id = f"session_{uuid4().hex[:12]}"
        state["archive_session_id"] = archive_session_id
    return archive_session_id


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


def _trim_session_messages(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    if len(messages) <= _MAX_HISTORY_MESSAGES:
        return messages
    trimmed = messages[-_MAX_HISTORY_MESSAGES:]
    while trimmed and trimmed[0].get("role") == "tool":
        trimmed = trimmed[1:]
    for i in range(len(trimmed) - 1, -1, -1):
        if trimmed[i].get("tool_calls") and (i + 1 >= len(trimmed) or trimmed[i + 1].get("role") != "tool"):
            return trimmed[:i]
    return trimmed


# ---------------------------------------------------------------------------
# Token-budget compaction (append-only immutable compacted blocks)
# ---------------------------------------------------------------------------

_COMPACT_TRIGGER_RATIO = 0.6
_COMPACT_RECENT_RATIO = 0.3
_COMPACT_BLOCK_PREFIX = "[Compacted earlier context]"


def _is_compacted_block(message: dict[str, Any]) -> bool:
    return isinstance(message, dict) and bool(message.get("compacted_block"))


def _strip_tool_episode_text(messages: list[dict[str, Any]]) -> list[str]:
    """Render messages as compact text lines, stripping tool noise.

    Tool calls are reduced to ``[tool] name(args)``; tool *results* (role=="tool")
    are dropped entirely — we keep what was attempted, not the bulky output.
    """
    lines: list[str] = []
    for m in messages:
        role = m.get("role")
        if role == "tool":
            continue  # tool result body stripped
        content = str(m.get("content") or "").strip()
        if role == "user":
            if content:
                lines.append(f"User: {content[:500]}")
        elif role == "assistant":
            if content:
                lines.append(f"{ASSISTANT_NAME}: {content[:500]}")
            for tc in (m.get("tool_calls") or []):
                fn = tc.get("function", {}) if isinstance(tc, dict) else {}
                name = str(fn.get("name") or "").strip()
                args = str(fn.get("arguments") or "").strip()
                if name:
                    lines.append(f"  [tool] {name}({args[:200]})")
        elif role == "system":
            if content:
                lines.append(content[:300])
    return lines


def _safe_recent_start(live: list[dict[str, Any]], idx: int) -> int:
    """Move boundary forward so ``live[idx:]`` never starts on a tool result."""
    n = len(live)
    i = max(0, min(idx, n))
    while i < n and live[i].get("role") == "tool":
        i += 1
    return i


def _compact_messages_for_storage(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Token-budget compaction with an immutable, append-only compacted-block chain.

    - At or below 60% of the model context window: return unchanged (append-only
      → stable prefix → prompt-cache hits).
    - Above 60%: mechanically fold the older live messages into ONE new compacted
      block (tool results stripped, calls reduced to name+args), appended AFTER any
      existing compacted blocks (which are never rewritten). Recent messages are
      kept verbatim within ~30% of the window.

    Falls back to the count-based ``_trim_session_messages`` when the context
    window is unknown.
    """
    from cyrene.config_store import get_current_ctx_limit
    from cyrene.call_llm import _message_token_estimate

    ctx_limit = get_current_ctx_limit()
    if ctx_limit <= 0:
        return _trim_session_messages(messages)

    total = sum(_message_token_estimate(m) for m in messages)
    if total <= int(ctx_limit * _COMPACT_TRIGGER_RATIO):
        return messages

    head_blocks: list[dict[str, Any]] = []
    i = 0
    while i < len(messages) and _is_compacted_block(messages[i]):
        head_blocks.append(messages[i])
        i += 1
    live = messages[i:]

    recent_budget = int(ctx_limit * _COMPACT_RECENT_RATIO)
    acc = 0
    cut = 0
    for j in range(len(live) - 1, -1, -1):
        acc += _message_token_estimate(live[j])
        if acc > recent_budget:
            cut = j + 1
            break
    cut = _safe_recent_start(live, cut)

    to_compact = live[:cut]
    recent = live[cut:]
    if not to_compact:
        return messages

    block_lines = _strip_tool_episode_text(to_compact)
    if not block_lines:
        return messages
    block: dict[str, Any] = {
        "role": "system",
        "content": _COMPACT_BLOCK_PREFIX + "\n" + "\n".join(block_lines),
        "compacted_block": True,
    }
    _ensure_message_identity([block])
    return [*head_blocks, block, *recent]


# ---------------------------------------------------------------------------
# Pass 2 — background LLM distillation of mechanical compacted blocks
# ---------------------------------------------------------------------------

_pending_distill_task: asyncio.Task | None = None

_COMPACT_DISTILL_PROMPT = (
    "You are compressing archived conversation context into a dense, durable summary.\n"
    "Preserve: concrete facts about the user and their goals/preferences; decisions and "
    "their rationale; open threads / unfinished tasks; key tool actions taken (keep the "
    "[tool] name(args) lines that matter).\n"
    "Drop: filler, pleasantries, redundant restatements, raw tool output.\n"
    "Output a compact summary only — no preamble, no markdown headers. Be terse but complete.\n\n"
    "Archived context to compress:\n"
)


def _has_pending_compacted_block(messages: list[dict[str, Any]]) -> bool:
    return any(
        isinstance(m, dict) and m.get("compacted_block") and not m.get("llm_compacted")
        for m in messages
    )


def _schedule_compaction_distill() -> None:
    global _pending_distill_task
    if _pending_distill_task is not None and not _pending_distill_task.done():
        return
    _pending_distill_task = asyncio.create_task(_distill_pending_compacted_blocks())
    _pending_compressors.add(_pending_distill_task)
    _pending_distill_task.add_done_callback(_pending_compressors.discard)


async def _distill_pending_compacted_blocks() -> None:
    """Background: LLM-distill mechanical compacted blocks into denser ones.

    The LLM call runs WITHOUT holding the session lock; the result is swapped in
    by message_id (compacted blocks are immutable, so the id is a stable anchor
    even if new messages were appended meanwhile). A session-epoch guard prevents
    writing stale content into a session that was reset mid-distillation.
    """
    while True:
        async with _session_state_lock:
            snapshot_epoch = _state._session_epoch
            state = _load_session_state()
            messages = state.get("messages", [])
            if not isinstance(messages, list):
                return
            target = next(
                (
                    m for m in messages
                    if isinstance(m, dict) and m.get("compacted_block")
                    and not m.get("llm_compacted") and str(m.get("message_id", "")).strip()
                ),
                None,
            )
            if target is None:
                return
            target_id = str(target["message_id"]).strip()
            raw_content = str(target.get("content") or "")

        body = raw_content
        if body.startswith(_COMPACT_BLOCK_PREFIX):
            body = body[len(_COMPACT_BLOCK_PREFIX):].lstrip("\n")

        distilled = ""
        token = _caller_type.set("compactor")
        try:
            response = await _call_llm(
                [
                    {"role": "system", "content": "You compress conversation context. Be terse and faithful."},
                    {"role": "user", "content": _COMPACT_DISTILL_PROMPT + body},
                ],
                tools=None,
                max_tokens=1500,
                secondary=True,
            )
            distilled = (_assistant_text(response) or "").strip()
        except Exception:
            logger.warning("Compaction distillation failed", exc_info=True)
        finally:
            _caller_type.reset(token)

        async with _session_state_lock:
            if _state._session_epoch != snapshot_epoch:
                return  # session was reset mid-distillation
            state = _load_session_state()
            messages = state.get("messages", [])
            if not isinstance(messages, list):
                return
            updated = False
            for m in messages:
                if (
                    isinstance(m, dict)
                    and str(m.get("message_id", "")).strip() == target_id
                    and m.get("compacted_block")
                ):
                    if distilled:
                        m["content"] = _COMPACT_BLOCK_PREFIX + "\n" + distilled
                    m["llm_compacted"] = True  # mark done even on failure → no retry storm
                    updated = True
                    break
            if not updated:
                return
            state["messages"] = messages
            _write_session_state(state)


# ---------------------------------------------------------------------------
# Report reference helpers
# ---------------------------------------------------------------------------

def _report_title_from_text(text: str, fallback: str = "Deep Research Report") -> str:
    source = str(text or "").strip()
    if not source:
        return fallback
    for line in source.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.startswith("#"):
            stripped = stripped.lstrip("#").strip()
        if stripped:
            return _fallback_label(stripped, limit=120)
    return _fallback_label(source, limit=120)


def _report_reference_stub(
    *,
    round_id: str,
    round_title: str,
    archive_session_id: str,
    full_text: str,
    attachments: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    report_title = _report_title_from_text(full_text, fallback=round_title or "Deep Research Report")
    preview = ""
    body_lines = [line.strip() for line in str(full_text or "").splitlines() if line.strip()]
    for line in body_lines[1:]:
        if line.startswith("## "):
            break
        preview = line
        if preview:
            break
    preview = _fallback_label(preview, limit=_REPORT_REF_MAX_PREVIEW) if preview else ""
    content = f"{_REPORT_REF_PREFIX} {report_title}"
    if preview:
        content += f"\n{preview}"
    content += "\n完整报告已归档；仅在明确引用这篇报告时才会重新加载全文。"
    entry: dict[str, Any] = {
        "role": "assistant",
        "content": content,
        "report_ref": True,
        "report_title": report_title,
        "report_round_id": round_id,
        "report_archive_session_id": archive_session_id,
        "report_preview": preview,
    }
    if round_title:
        entry["round_title"] = round_title
    if attachments:
        entry["attachments"] = [dict(item) for item in attachments if isinstance(item, dict)]
    return entry


def _compress_report_messages_for_storage(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    state = _load_session_state()
    archive_session_id = _ensure_archive_session_id(state)
    result: list[dict[str, Any]] = []
    for message in messages:
        if not isinstance(message, dict) or not bool(message.get("deep_research_report")):
            result.append(message)
            continue
        compressed = _report_reference_stub(
            round_id=str(message.get("round_id", "")).strip(),
            round_title=str(message.get("round_title", "")).strip(),
            archive_session_id=archive_session_id,
            full_text=_assistant_text(message) or str(message.get("content") or ""),
            attachments=message.get("attachments") if isinstance(message.get("attachments"), list) else None,
        )
        for key in ("message_id", "client_request_id", "subagent_flow_snapshot"):
            if message.get(key):
                compressed[key] = message[key]
        result.append(compressed)
    return result


def _iter_report_refs(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    refs: list[dict[str, Any]] = []
    for message in messages:
        if not isinstance(message, dict) or not bool(message.get("report_ref")):
            continue
        if (
            str(message.get("report_archive_session_id", "")).strip()
            and str(message.get("report_round_id", "")).strip()
            and str(message.get("report_title", "")).strip()
        ):
            refs.append(message)
    return refs


def _looks_like_report_followup(user_message: str, report_refs: list[dict[str, Any]]) -> bool:
    text = str(user_message or "").strip()
    if not text or not report_refs:
        return False
    lowered = text.lower()
    direct_cues = (
        "基于", "根据", "引用", "那篇报告", "这篇报告", "之前的报告", "上次的报告",
        "研究报告", "深度研究", "那份研究", "这份研究", "继续", "延续", "接着", "展开",
        "summarize that report", "based on that report", "based on the report",
        "that report", "this report", "deep research report", "previous report",
        "continue from the report", "use the report", "refer to the report",
    )
    if any((cue in text) or (cue in lowered) for cue in direct_cues):
        return True
    for ref in reversed(report_refs):
        title = str(ref.get("report_title", "")).strip()
        if title and title.lower() in lowered:
            return True
    return False


def _select_report_ref(user_message: str, report_refs: list[dict[str, Any]]) -> dict[str, Any] | None:
    lowered = str(user_message or "").strip().lower()
    for ref in reversed(report_refs):
        title = str(ref.get("report_title", "")).strip()
        if title and title.lower() in lowered:
            return ref
    return report_refs[-1] if report_refs else None


def _expand_report_reference_history(history: list[dict[str, Any]], user_message: str) -> list[dict[str, Any]]:
    from cyrene.conversations import get_archived_round

    report_refs = _iter_report_refs(history)
    if not _looks_like_report_followup(user_message, report_refs):
        return history
    selected = _select_report_ref(user_message, report_refs)
    if not selected:
        return history
    archived = get_archived_round(
        str(selected.get("report_archive_session_id", "")).strip(),
        str(selected.get("report_round_id", "")).strip(),
    )
    if not archived:
        return history
    full_report = str(archived.get("assistant_body", "")).strip()
    if not full_report:
        return history
    report_title = str(selected.get("report_title", "")).strip() or "Deep Research Report"
    selected_message_id = str(selected.get("message_id", "")).strip()
    expanded_history: list[dict[str, Any]] = []
    replaced = False
    for message in history:
        if (
            isinstance(message, dict)
            and bool(message.get("report_ref"))
            and str(message.get("message_id", "")).strip() == selected_message_id
        ):
            replacement = dict(message)
            replacement["content"] = (
                f"{_REPORT_REF_PREFIX} {report_title}\n"
                "The user explicitly asked to use this archived report. "
                "The full report content is restored below for this turn only.\n\n"
                f"{full_report}"
            )
            replacement["report_expanded_for_turn"] = True
            expanded_history.append(replacement)
            replaced = True
            continue
        expanded_history.append(message)
    return expanded_history if replaced else history


# ---------------------------------------------------------------------------
# Session message write helpers
# ---------------------------------------------------------------------------

def _schedule_memory_compression(messages: list[dict[str, Any]]) -> None:
    task = asyncio.create_task(_compress_old_messages(list(messages)))
    _pending_compressors.add(task)
    task.add_done_callback(_pending_compressors.discard)


async def _write_session_messages_locked(state: dict[str, Any], messages: list[dict[str, Any]]) -> None:
    _ensure_archive_session_id(state)
    messages = _compress_report_messages_for_storage(messages)
    messages = _ensure_message_identity(messages)
    messages = _dedupe_messages_by_id(messages)
    trimmed = _compact_messages_for_storage(messages)
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

    if _has_pending_compacted_block(trimmed):
        _schedule_compaction_distill()


async def _save_session_messages(messages: list[dict[str, Any]]) -> None:
    messages = _compress_report_messages_for_storage(messages)
    messages = _ensure_message_identity(list(messages))
    async with _session_state_lock:
        state = _load_session_state()
        saved_epoch = state.get("_session_epoch")
        if saved_epoch is not None and saved_epoch != _state._session_epoch:
            logger.warning("Stale _save_session_messages skipped (session was reset)")
            return
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
            suffix = _message_suffix_after_persisted_prefix(messages, base_messages, prefix_len)
            round_id = str(_current_round_id.get() or "").strip()
            replace_end = insert_at
            while replace_end < len(base_messages) and _is_replaceable_live_message(base_messages[replace_end], round_id):
                replace_end += 1
            effective_messages = [
                *base_messages[:insert_at],
                *_merge_message_sequence(base_messages[insert_at:replace_end], suffix),
                *base_messages[replace_end:],
            ]
        elif base_messages is not None:
            current = state.get("messages", [])
            current_messages = list(current) if isinstance(current, list) else []
            prefix_len = max(0, min(_persist_history_prefix_len.get(), len(messages)))
            insert_at = _persist_insert_at.get()
            if insert_at is None:
                insert_at = len(base_messages)
            insert_at = max(0, min(insert_at, len(base_messages)))
            suffix = _message_suffix_after_persisted_prefix(messages, base_messages, prefix_len)
            existing_tail = current_messages[insert_at:] if insert_at < len(current_messages) else []
            effective_messages = [
                *base_messages[:insert_at],
                *_merge_message_sequence(existing_tail or base_messages[insert_at:], suffix),
            ]
        await _write_session_messages_locked(state, effective_messages)


async def _remove_last_exchange() -> None:
    """Remove the last user→assistant exchange from session messages.

    Used by the retry/regenerate feature to replace the previous exchange
    with a new one. Safe to call even when there is no exchange to remove.
    """
    async with _session_state_lock:
        state = _load_session_state()
        saved_epoch = state.get("_session_epoch")
        if saved_epoch is not None and saved_epoch != _state._session_epoch:
            return
        messages = state.get("messages", [])
        if not isinstance(messages, list) or len(messages) < 2:
            return
        msgs = list(messages)
        assistant_idx = None
        user_idx = None
        for i in range(len(msgs) - 1, -1, -1):
            role = str(msgs[i].get("role", "")).strip()
            if role == "assistant" and assistant_idx is None:
                assistant_idx = i
            elif role == "user" and user_idx is None and assistant_idx is not None and i < assistant_idx:
                user_idx = i
                break
        if user_idx is not None and assistant_idx is not None:
            del msgs[assistant_idx]
            del msgs[user_idx]
            await _write_session_messages_locked(state, msgs)


async def _append_session_message(entry: dict[str, Any]) -> None:
    async with _session_state_lock:
        state = _load_session_state()
        saved_epoch = state.get("_session_epoch")
        if saved_epoch is not None and saved_epoch != _state._session_epoch:
            logger.warning("Stale _append_session_message skipped (session was reset)")
            return
        messages = state.get("messages", [])
        full_messages = list(messages) if isinstance(messages, list) else []
        full_messages.append(entry)
        _ensure_message_identity(full_messages)
        await _write_session_messages_locked(state, full_messages)


async def append_system_message(
    content: str,
    *,
    message_meta: dict[str, Any] | None = None,
    publish_event: dict[str, Any] | None = None,
) -> dict[str, Any]:
    assistant_entry: dict[str, Any] = {
        "role": "assistant",
        "content": content,
        "system_initiated": True,
    }
    if message_meta:
        assistant_entry.update(message_meta)

    _ensure_message_identity([assistant_entry])
    await _append_session_message(dict(assistant_entry))

    event = {"type": "assistant_message", "system_initiated": True}
    if publish_event:
        event.update(publish_event)
    await _publish_runtime_event(event)
    return assistant_entry


# ---------------------------------------------------------------------------
# Pending question management
# ---------------------------------------------------------------------------

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
    meta = payload.get("meta")
    if isinstance(meta, dict) and meta:
        question["meta"] = dict(meta)
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
        saved_epoch = state.get("_session_epoch")
        if saved_epoch is not None and saved_epoch != _state._session_epoch:
            logger.warning("Stale _upsert_pending_question skipped (session was reset)")
            return question
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


# ---------------------------------------------------------------------------
# Guidance context helpers (return session snapshots, no guidance logic)
# ---------------------------------------------------------------------------

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
        "command": str((pending.get("meta") or {}).get("command", "") or "").strip(),
    }


# ---------------------------------------------------------------------------
# Session labels
# ---------------------------------------------------------------------------

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
    had_archive_session_id = bool(str(state.get("archive_session_id", "")).strip())
    archive_session_id = _ensure_archive_session_id(state)
    session_title = str(state.get("session_title", "") or "").strip()
    if not had_archive_session_id:
        _write_session_state(state)
    return {
        "session_title": session_title,
        "round_title": round_title,
        "round_id": target_round_id,
        "archive_session_id": archive_session_id,
    }


def _schedule_session_label_refresh(current_user_message: str, round_id: str) -> None:
    async def _runner() -> None:
        try:
            await _refresh_session_labels(current_user_message, round_id)
        except Exception:
            logger.warning("Async session naming failed for %s", round_id or "<unknown>", exc_info=True)

    task = asyncio.create_task(_runner())
    _pending_label_refreshes.add(task)
    task.add_done_callback(_pending_label_refreshes.discard)


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
        ], tools=None, secondary=True)
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


# ---------------------------------------------------------------------------
# Session lifecycle
# ---------------------------------------------------------------------------

async def _compress_old_messages(all_messages: list[dict[str, Any]]) -> None:
    from cyrene.short_term import touch_entry

    to_compress = [m for m in all_messages[:20] if m["role"] in ("user", "assistant")]
    if not to_compress:
        return

    lines = []
    for m in to_compress:
        role = "User" if m["role"] == "user" else ASSISTANT_NAME
        content = m.get("content", "")[:200]
        lines.append(f"{role}: {content}")
    text = "\n".join(lines)

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
    import cyrene.agent.state as _state
    from cyrene.subagent import clear as _clear_subagents
    from cyrene.inbox import clear_all_inboxes

    for task in list(_state._pending_interrupt_clearers):
        task.cancel()
    _state._pending_interrupt_clearers.clear()
    for task in list(_state._pending_label_refreshes):
        task.cancel()
    _state._pending_label_refreshes.clear()
    _state._interrupt_event.clear()
    if _state._main_inbox_worker is not None:
        _state._main_inbox_worker.cancel()
        _state._main_inbox_worker = None
    _state._active_main_round_id = ""
    _state._active_main_round_prompt = ""
    _state._active_main_round_public_prompt = ""
    _state._active_main_round_started_at = 0.0
    await _clear_subagents()
    await clear_all_inboxes()
    if _state.STATE_FILE.exists():
        try:
            data = json.loads(_state.STATE_FILE.read_text(encoding="utf-8"))
            msgs = data.get("messages", [])
            if msgs:
                _schedule_memory_compression(msgs)
        except Exception:
            pass
    async with _session_state_lock:
        _state._session_epoch += 1
        _state.STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        _state.STATE_FILE.write_text(
            json.dumps({"_session_epoch": _state._session_epoch}, ensure_ascii=False),
            encoding="utf-8",
        )
    try:
        from cyrene import pattern as _pattern_module
        _ = asyncio.create_task(_pattern_module.scan_for_session_start())
    except Exception:
        pass
