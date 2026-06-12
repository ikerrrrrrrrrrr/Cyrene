"""Guidance processing: inbox management, subagent coordination, result synthesis.

Depends on ``state``, ``session``, ``round`` (or merged session.py) and
``message``.  Inline-imports ``coordinator._run_chat_agent`` to break the
module-level cycle.
"""

import asyncio
import contextlib
import json
import logging
import re
from typing import Any
from uuid import uuid4

import httpx

from cyrene import debug
from cyrene.agent.message import _assistant_entry_from_response, _ensure_message_identity, _insert_intermediate_user_reply, _is_placeholder_reply
from cyrene.agent.state import (
    _agent_lock,
    _AWAITING_USER_SENTINEL,
    _call_llm,
    _call_llm_stream,
    _caller_type,
    _interrupt_event,
    _MAIN_INBOX_AGENT_ID,
    _pending_label_refreshes,
    _publish_runtime_event,
    _reply_stream_writer,
    _session_state_lock,
    _streaming_reply_requested,
)
from cyrene.agent.round import get_live_rounds, _main_inbox_pending_by_round
from cyrene.agent.session import (
    _append_session_message,
    _clear_pending_question,
    _guidance_persist_context_after_ack,
    _guidance_round_context,
    _load_pending_question,
    _load_session_messages,
    _load_session_state,
    _pending_question_resume_context,
    _pending_question_is_permission_elevation,
    _restore_pending_question,
    _save_session_messages,
    _schedule_session_label_refresh,
    _write_session_messages_locked,
    get_session_labels,
)
from cyrene.llm import _assistant_text

logger = logging.getLogger(__name__)

_VISIBLE_DSML_TOOL_BLOCK_RE = re.compile(
    r"<(?:｜｜|\|\|)DSML(?:｜｜|\|\|)tool_calls>.*?</(?:｜｜|\|\|)DSML(?:｜｜|\|\|)tool_calls>",
    re.DOTALL,
)


# ---------------------------------------------------------------------------
# Guidance ack / error text
# ---------------------------------------------------------------------------

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


async def _generate_guidance_ack(
    guidance: str,
    *,
    round_title: str = "",
    round_history: list[dict[str, Any]] | None = None,
) -> str:
    latest_assistant = next(
        (
            str(msg.get("content") or "").strip()
            for msg in reversed(round_history or [])
            if str(msg.get("role") or "").strip() == "assistant" and str(msg.get("content") or "").strip()
        ),
        "",
    )
    prompt_messages = [
        {
            "role": "system",
            "content": (
                "You are acknowledging new user guidance for an ongoing task.\n"
                "Reply with exactly one short sentence.\n"
                "Do not answer the task itself.\n"
                "Do not mention queues, rounds, internal state, or implementation details.\n"
                "Say that you understood the guidance and will adjust the current work accordingly.\n"
                "Match the user's language."
            ),
        },
        {
            "role": "user",
            "content": (
                f"Round title: {round_title or '—'}\n"
                f"Latest assistant reply: {latest_assistant or '—'}\n"
                f"New user guidance: {guidance}"
            ),
        },
    ]
    try:
        response = await _call_llm(prompt_messages, tools=None, max_tokens=80, secondary=True)
        ack_text = _assistant_text(response).strip()
        return ack_text or _guidance_ack_text()
    except Exception:
        logger.warning("Failed to generate guidance acknowledgement via LLM", exc_info=True)
        return _guidance_ack_text()


# ---------------------------------------------------------------------------
# Fan-out / wait helpers
# ---------------------------------------------------------------------------

async def _insert_guidance_reply(
    target_round_id: str,
    guidance_id: str,
    content: str,
    round_title: str = "",
    client_request_id: str = "",
    subagent_flow_snapshot: dict[str, Any] | None = None,
) -> None:
    from cyrene.agent.message import _ensure_message_identity

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
    if subagent_flow_snapshot:
        assistant_entry["subagent_flow_snapshot"] = subagent_flow_snapshot

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
    content: str,
    round_title: str = "",
    client_request_id: str = "",
) -> None:
    from cyrene.agent.message import _ensure_message_identity

    assistant_entry: dict[str, Any] = {
        "role": "assistant",
        "content": content,
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


# ---------------------------------------------------------------------------
# Synthesis and final replies
# ---------------------------------------------------------------------------

async def _synthesize_subagent_results(
    task: str,
    summary: str,
    round_title: str = "",
    guidance: str = "",
    round_history: list[dict[str, Any]] | None = None,
) -> str:
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
                            context_lines.append("[Spawned subagent]")
                    elif name == "send_agent_message":
                        try:
                            a = json.loads(args)
                            context_lines.append(f"[Subagent msg: {a.get('from', '?')} -> {a.get('to', '?')}]")
                        except Exception:
                            pass
    context_block = "\n\n".join(context_lines) if context_lines else "—"

    experts_block = summary.strip() or "(No subagent results.)"

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


def _is_placeholder_reply(text: str) -> bool:
    normalized = str(text or "").strip().lower()
    return normalized in {
        "", "done", "done.", "finished", "finished.",
        "ok", "ok.", "okay", "okay.",
        "完成", "完成。", "已完成", "已完成。",
    }


async def _final_user_reply_from_history(messages: list[dict], max_tokens: int | None = None) -> str:
    last_user_text = next(
        (
            str(message.get("content") or "").strip()
            for message in reversed(messages)
            if isinstance(message, dict) and str(message.get("role") or "") == "user" and str(message.get("content") or "").strip()
        ),
        "",
    )
    prompt_messages = [
        *messages,
        {
            "role": "user",
            "content": (
                ("Now answer the user's request directly using the gathered tool results.\n" if last_user_text else
                 "The user uploaded one or more attachments without extra text. Summarize the attachment contents directly using the gathered tool results.\n")
                + "Do not call tools.\n"
                + "Do not reply with only 'Done'.\n"
                + "If the tools extracted file or attachment contents, quote or summarize those contents in your answer."
            ),
        },
    ]
    return await _validated_final_no_tool_reply(prompt_messages, max_tokens=max_tokens)


async def _final_plain_reply_from_history(messages: list[dict], max_tokens: int | None = None) -> str:
    prompt_messages = [
        *messages,
        {
            "role": "user",
            "content": (
                "Answer the latest user message directly.\n"
                "Do not call tools.\n"
                "Do not reply with only 'Done'."
            ),
        },
    ]
    return await _validated_final_no_tool_reply(prompt_messages, max_tokens=max_tokens)


def _tool_result_fallback_text(messages: list[dict]) -> str:
    for message in reversed(messages):
        if not isinstance(message, dict) or str(message.get("role") or "") != "tool":
            continue
        raw = str(message.get("content") or "").strip()
        if not raw:
            continue
        try:
            payload = json.loads(raw)
        except Exception:
            payload = None
        if isinstance(payload, dict):
            text_preview = str(payload.get("text_preview") or "").strip()
            if text_preview:
                return f"我从附件中提取到的内容是：\n\n{text_preview}"
            stdout = str(payload.get("stdout") or "").strip()
            if stdout:
                return f"我从附件中提取到的内容是：\n\n{stdout[:4000]}"
            preview = str(payload.get("preview") or "").strip()
            if preview and "no built-in parser" not in preview.lower():
                return f"我从附件中提取到的内容是：\n\n{preview}"
        elif raw and not raw.lower().startswith("tool failed:"):
            return f"我从附件中提取到的内容是：\n\n{raw[:4000]}"
    return ""


async def _final_reply_from_history(messages: list[dict], max_tokens: int | None = None) -> str:
    return (await _validated_final_no_tool_reply(messages, max_tokens=max_tokens)) or "Done."


def _strip_visible_dsml_tool_blocks(text: str) -> str:
    return _VISIBLE_DSML_TOOL_BLOCK_RE.sub("", str(text or "")).strip()


def _record_final_reply_usage(*responses: Any) -> None:
    """Stash the merged usage of the final-reply call(s) for the persist layer.

    Streaming finals return plain text to their callers, so without this the
    token usage of the reply call never reaches the saved assistant entry.
    """
    from cyrene.agent.state import _last_final_reply_usage
    merged: dict[str, Any] = {}
    for response in responses:
        usage = response.get("usage") if isinstance(response, dict) else None
        if not isinstance(usage, dict):
            continue
        for key, value in usage.items():
            if isinstance(value, (int, float)) and isinstance(merged.get(key), (int, float)):
                merged[key] = merged[key] + value
            else:
                merged.setdefault(key, value)
    _last_final_reply_usage.set(merged or None)


async def _validated_final_no_tool_reply(messages: list[dict], max_tokens: int | None = None) -> str:
    """Generate final user-visible text without leaking textual DSML tool markup."""
    if _streaming_reply_requested():
        response = await _call_llm_stream(messages, max_tokens=max_tokens)
    else:
        response = await _call_llm(messages, tools=None, max_tokens=max_tokens)
    _record_final_reply_usage(response)
    text = _assistant_text(response).strip()
    if not _VISIBLE_DSML_TOOL_BLOCK_RE.search(text):
        return text

    retry_messages = [
        *messages,
        {"role": "assistant", "content": text},
        {
            "role": "user",
            "content": (
                "Your previous message was DSML/tool-call markup, but tools are not available in this final-answer step. "
                "Write the final answer to the user in plain text only, using the already gathered context. "
                "Do not output XML, DSML, JSON tool calls, or any tool-call markup."
            ),
        },
    ]
    retry_response = await _call_llm(retry_messages, tools=None, max_tokens=max_tokens)
    _record_final_reply_usage(response, retry_response)
    retry_text = _assistant_text(retry_response).strip()
    if _VISIBLE_DSML_TOOL_BLOCK_RE.search(retry_text):
        return _strip_visible_dsml_tool_blocks(retry_text)
    return retry_text


# ---------------------------------------------------------------------------
# Main inbox processing
# ---------------------------------------------------------------------------

async def _process_main_inbox_message(message: dict[str, Any], bot: Any, chat_id: int, db_path: str) -> str:
    from cyrene.agent.coordinator import _run_chat_agent
    from cyrene.subagent import clear as _sub_clear, get_snapshot as _sub_snapshot

    target_round_id = str(message.get("round_id", "")).strip()
    guidance_id = str(message.get("message_id", "")).strip()
    content = str(message.get("content") or "").strip()
    if not target_round_id or not guidance_id or not content:
        return ""

    context = _guidance_round_context(target_round_id, guidance_id)
    live_round = next((live for live in get_live_rounds() if live.get("id") == target_round_id), None)
    round_title = context["round_title"] or str((live_round or {}).get("title") or "").strip() or target_round_id
    ack_text = await _generate_guidance_ack(
        content,
        round_title=round_title,
        round_history=context["round_history"],
    )
    snapshot = await _sub_snapshot(round_id=target_round_id)
    await _insert_guidance_ack(
        target_round_id,
        guidance_id,
        ack_text,
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
        interrupted, _summary = await _wait_for_subagent_round(target_round_id, bot, chat_id, db_path)
        if interrupted:
            reply = "[Sub-agents are still working in the background. The guidance was delivered and the round is continuing.]"
        else:
            from cyrene.subagent import run_summary_subagent as _run_summary_subagent
            from cyrene.subagent import build_flow_snapshot as _build_subagent_flow_snapshot

            parent_task = next(
                (
                    str(msg.get("content") or "").strip()
                    for msg in context["round_history"]
                    if str(msg.get("role") or "").strip() == "user" and str(msg.get("content") or "").strip()
                ),
                content,
            )
            reply = await _run_summary_subagent(
                round_id=target_round_id,
                parent_task=parent_task,
                guidance=content,
                round_history=context["round_history"],
            )
            flow_snapshot = await _build_subagent_flow_snapshot(target_round_id)
            await _sub_clear(round_id=target_round_id)
        await _insert_guidance_reply(
            target_round_id,
            guidance_id,
            reply,
            round_title=round_title,
            client_request_id=context["client_request_id"],
            subagent_flow_snapshot=flow_snapshot if not interrupted else None,
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
        assistant_message_meta={"in_reply_to_guidance_id": guidance_id},
    )


def _ensure_main_inbox_worker(bot: Any, chat_id: int, db_path: str) -> None:
    import cyrene.agent.state as _state
    _def_ctx = _state._ensure_session("")
    if _def_ctx.main_inbox_worker is None or _def_ctx.main_inbox_worker.done():
        _def_ctx.main_inbox_worker = asyncio.create_task(_drain_main_inbox(bot, chat_id, db_path))


async def queue_round_guidance(
    target_round_id: str,
    content: str,
    bot: Any,
    chat_id: int,
    db_path: str,
    client_request_id: str = "",
) -> dict[str, Any]:
    from cyrene.inbox import send_message as _send_inbox
    from datetime import datetime, timezone

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

    import cyrene.agent.state as _state
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
        _state._ensure_session("").main_inbox_worker = None
        if get_live_rounds() and _main_inbox_pending_by_round():
            _ensure_main_inbox_worker(bot, chat_id, db_path)


# ---------------------------------------------------------------------------
# answer_pending_question (moved here from coordinator to keep it close to guidance)
# ---------------------------------------------------------------------------

async def answer_pending_question(
    question_id: str,
    answer_text: str,
    bot: Any,
    chat_id: int,
    db_path: str,
    client_request_id: str = "",
) -> str:
    from cyrene.agent.coordinator import _run_chat_agent

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

    pending_meta = cleared.get("meta")
    if isinstance(pending_meta, dict) and str(pending_meta.get("kind", "")).strip() == "claude_code_prompt_confirmation":
        try:
            return await _handle_claude_code_prompt_answer(
                round_id=round_id,
                pending=cleared,
                answer_text=content,
                client_request_id=client_request_id,
            )
        except Exception:
            await _restore_pending_question(pending)
            raise
    if isinstance(pending_meta, dict) and _pending_question_is_permission_elevation(cleared):
        try:
            return await _handle_permission_elevation_answer(
                round_id=round_id,
                pending=cleared,
                answer_text=content,
                client_request_id=client_request_id,
                context=context,
            )
        except Exception:
            await _restore_pending_question(pending)
            raise

    if isinstance(pending_meta, dict) and str(pending_meta.get("kind", "")).strip() == "plan_confirmation":
        try:
            return await _handle_plan_confirmation_answer(
                round_id=round_id,
                pending=cleared,
                answer_text=content,
                client_request_id=client_request_id,
                context=context,
            )
        except Exception:
            await _restore_pending_question(pending)
            raise

    if isinstance(pending_meta, dict) and str(pending_meta.get("kind", "")).strip() == "browser_takeover":
        # The user finished logging in via the native window. Return the browser
        # session to headless (same profile → now authenticated), then fall through
        # to resume the round normally with the user's confirmation.
        try:
            from cyrene.browser import end_browser_takeover
            await end_browser_takeover(str(pending_meta.get("url", "") or ""))
        except Exception:
            logger.warning("browser end_takeover failed during resume", exc_info=True)

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
            command=str(context.get("command", "") or "").strip(),
        )
    except Exception:
        await _restore_pending_question(pending)
        raise


def _is_affirmative_answer(text: str) -> bool:
    normalized = str(text or "").strip().lower()
    return normalized in {
        "同意并发送", "同意", "发送", "确认", "确认发送", "好", "好的", "可以", "行", "yes", "y", "ok", "okay", "send", "confirm",
    }


def _is_negative_answer(text: str) -> bool:
    normalized = str(text or "").strip().lower()
    return normalized in {
        "取消", "不用", "不发", "停止", "算了", "cancel", "no", "n", "stop",
    }


async def _handle_claude_code_prompt_answer(
    round_id: str,
    pending: dict[str, Any],
    answer_text: str,
    client_request_id: str = "",
) -> str:
    from cyrene.cc_bridge import send_prompt_to_cc
    from cyrene.agent.prompts import _contains_cjk

    meta = pending.get("meta", {})
    optimized_prompt = str(meta.get("optimized_prompt") or "").strip()
    task = str(meta.get("task") or "").strip()
    user_answer = str(answer_text or "").strip()
    chinese = _contains_cjk(task or optimized_prompt or user_answer)

    user_entry: dict[str, Any] = {
        "role": "user",
        "content": user_answer,
        "round_id": round_id,
    }
    if client_request_id:
        user_entry["client_request_id"] = client_request_id
    await _append_session_message(user_entry)

    if _is_negative_answer(user_answer):
        reply = "已取消，Claude Code 没有收到这条提示词。" if chinese else "Cancelled. The prompt was not sent to Claude Code."
        await _insert_intermediate_user_reply(reply, round_id=round_id, client_request_id=client_request_id)
        return reply

    prompt_to_send = optimized_prompt if _is_affirmative_answer(user_answer) else user_answer
    if not prompt_to_send:
        reply = "没有可发送的提示词。" if chinese else "There is no prompt to send."
        await _insert_intermediate_user_reply(reply, round_id=round_id, client_request_id=client_request_id)
        return reply

    result = send_prompt_to_cc(prompt_to_send)
    if not result.get("ok"):
        reason = str(result.get("reason") or "unknown error").strip()
        reply = (
            f"没有成功发送到 Claude Code：{reason}"
            if chinese else
            f"Failed to send the prompt to Claude Code: {reason}"
        )
        await _insert_intermediate_user_reply(reply, round_id=round_id, client_request_id=client_request_id)
        return reply

    reply = (
        "已把提示词输入到 Claude Code，任务已经开始运行。"
        if chinese else
        "I sent the prompt to Claude Code and it is now running."
    )
    await _insert_intermediate_user_reply(reply, round_id=round_id, client_request_id=client_request_id)
    await _publish_runtime_event({
        "type": "chat_message",
        "client_request_id": client_request_id,
        "round_id": round_id,
    })
    return reply


async def _handle_write_permission_answer(
    *,
    round_id: str,
    pending: dict[str, Any],
    answer_text: str,
    client_request_id: str,
    context: dict[str, Any],
) -> str:
    return await _handle_permission_elevation_answer(
        round_id=round_id,
        pending=pending,
        answer_text=answer_text,
        client_request_id=client_request_id,
        context=context,
    )


def _permission_answer_granted(text: str) -> bool:
    normalized = str(text or "").strip().lower()
    if not normalized:
        return False
    if normalized in {
        "拒绝", "保持仅限 workspace", "拒绝，保持 workspace_only", "workspace_only",
        "cancel", "no", "n", "stop",
    }:
        return False
    return normalized in {
        "仅这次允许", "allow once", "仅此次", "这次", "once",
        "始终允许", "always allow", "always", "永久允许", "allow",
        "允许这次", "允许这次读取", "允许执行", "允许删除", "仅此任务允许 full_access",
        "同意", "确认", "好", "好的", "可以", "行", "yes", "y", "ok", "okay",
        "allow_once",
    }


async def _handle_permission_elevation_answer(
    *,
    round_id: str,
    pending: dict[str, Any],
    answer_text: str,
    client_request_id: str,
    context: dict[str, Any],
) -> str:
    from cyrene.agent.coordinator import _run_chat_agent
    from cyrene.settings_store import set_write_permission_mode
    from cyrene.agent.state import _temporary_full_access

    normalized = str(answer_text or "").strip().lower()
    meta = pending.get("meta") if isinstance(pending.get("meta"), dict) else {}
    permission_kind = str(meta.get("kind", "")).strip()
    tool_name = str(meta.get("tool_name", "") or "").strip()
    operation = str(meta.get("operation", "") or "").strip()
    path_hint = str(meta.get("path_hint", "") or "").strip()
    reason = str(meta.get("reason", "") or "").strip()

    granted = _permission_answer_granted(answer_text)
    if permission_kind == "write_permission_request":
        # "仅这次允许" —— 只在此 round 内有效，round 结束时自动清理
        if normalized in {"仅这次允许", "allow once", "仅此次", "这次", "once"}:
            _temporary_full_access.set(True)
            system = (
                "The user granted elevated write/delete permission for this round only. "
                "Retry the blocked action if it is still required."
            )
        # "始终允许" —— 全局永久生效
        elif normalized in {"始终允许", "always allow", "always", "永久允许", "allow"}:
            set_write_permission_mode("full_access")
            system = (
                "The user granted permanent elevated write/delete permission. "
                "Retry the blocked action if it is still required."
            )
        else:
            set_write_permission_mode("workspace_only")
            system = (
                "The user denied elevated write/delete permission. "
                "Stay within the workspace and choose a safer alternative."
            )
    elif permission_kind == "read_elevation":
        if granted:
            _temporary_full_access.set(True)
            system = (
                "The user granted temporary read access to paths outside the workspace for this round. "
                "Retry the blocked read action if it is still required."
            )
        else:
            system = (
                "The user denied read access outside the workspace. "
                "Do not retry; stay within the workspace and choose a safe alternative."
            )
    elif granted:
        _temporary_full_access.set(True)
        system = (
            "The user granted the internal permission/confirmation request for this round. "
            "Retry the blocked action if it is still required."
        )
    else:
        system = (
            "The user denied the internal permission/confirmation request for this round. "
            "Do not retry the blocked action; stay within the current safety constraints and choose a safer alternative."
        )
    details = []
    if permission_kind:
        details.append(f"Permission kind: {permission_kind}")
    if tool_name:
        details.append(f"Tool: {tool_name}")
    if operation:
        details.append(f"Operation: {operation}")
    if path_hint:
        details.append(f"Target/path hint: {path_hint}")
    if reason:
        details.append(f"Reason/request detail: {reason}")
    if details:
        system += "\n" + "\n".join(details)

    return await _run_chat_agent(
        "[Internal permission decision received. Continue the same round using the system instruction above.]",
        None,
        0,
        "",
        ephemeral_system=system,
        forced_round_id=round_id,
        history_override=context.get("round_history") or [],
        persist_base_messages=context.get("persist_base_messages") or [],
        persist_insert_at=context.get("persist_insert_at"),
        client_request_id=client_request_id,
        persist_user_message=False,
        command=str(context.get("command", "") or "").strip(),
    )


async def _handle_plan_confirmation_answer(
    *,
    round_id: str,
    pending: dict[str, Any],
    answer_text: str,
    client_request_id: str,
    context: dict[str, Any],
) -> str:
    """处理「计划模式」确认回答：同意并开始 / 拒绝 / 修改。"""
    from cyrene.agent.coordinator import _run_chat_agent
    from cyrene.agent.state import _publish_runtime_event
    from cyrene.agent.planning import _plan_to_text

    meta = pending.get("meta") if isinstance(pending.get("meta"), dict) else {}
    plan = meta.get("plan") if isinstance(meta.get("plan"), dict) else {}
    user_message = str(meta.get("user_message") or "").strip()
    raw = str(answer_text or "").strip()
    normalized = raw.lower()

    approve = raw in {"同意并开始", "同意并开始执行", "同意并执行", "同意", "开始"} or normalized in {"approve", "start", "yes", "ok", "okay", "go"}
    reject = raw in {"拒绝", "取消", "算了", "不用了"} or normalized in {"reject", "cancel", "no", "stop"}

    if approve:
        await _publish_runtime_event({"type": "plan", "status": "accepted", "plan": plan, "round_id": round_id})
        exec_system = (
            "用户已同意以下计划，请严格按计划执行。当前为默认权限模式：碰到 workspace 之外或写/删操作时，"
            "再按需向用户申请提权。完成后用一段话总结结果。\n\n" + _plan_to_text(plan)
        )
        return await _run_chat_agent(
            user_message or "[按已同意的计划执行]",
            None, 0, "",
            ephemeral_system=exec_system,
            forced_round_id=round_id,
            history_override=context.get("round_history") or [],
            persist_base_messages=context.get("persist_base_messages") or [],
            persist_insert_at=context.get("persist_insert_at"),
            client_request_id=client_request_id,
            persist_user_message=False,
            command=str(context.get("command", "") or "").strip(),
            permission_mode="default",
        )

    if reject:
        await _publish_runtime_event({"type": "plan", "status": "rejected", "plan": plan, "round_id": round_id})
        reject_system = (
            "用户拒绝了刚才的计划，不要执行任何操作。用一句话礼貌确认已取消，"
            "并邀请用户提出新的方向或调整后的需求。"
        )
        return await _run_chat_agent(
            "[用户拒绝了计划]",
            None, 0, "",
            ephemeral_system=reject_system,
            forced_round_id=round_id,
            history_override=context.get("round_history") or [],
            persist_base_messages=context.get("persist_base_messages") or [],
            persist_insert_at=context.get("persist_insert_at"),
            client_request_id=client_request_id,
            persist_user_message=False,
            command=str(context.get("command", "") or "").strip(),
            permission_mode="default",
        )

    # 其他（含「修改」或任意自定义意见）→ 带着修改意见重新规划
    return await _run_chat_agent(
        user_message or raw,
        None, 0, "",
        forced_round_id=round_id,
        history_override=context.get("round_history") or [],
        persist_base_messages=context.get("persist_base_messages") or [],
        persist_insert_at=context.get("persist_insert_at"),
        client_request_id=client_request_id,
        persist_user_message=True,
        command=str(context.get("command", "") or "").strip(),
        permission_mode="plan",
        plan_modification=raw,
    )
