"""Core two-phase agent loop.

This module contains ONLY ``_run_main_agent``, the heart of the agent:
Phase 1 (lightweight decision) → Phase 2 (full tool loop with subagent
monitoring and deep research Phase 3).
"""

import asyncio
import json
import logging
from typing import Any
from uuid import uuid4

from cyrene.agent.guidance import (
    _final_reply_from_history,
    _final_plain_reply_from_history,
    _final_user_reply_from_history,
    _is_placeholder_reply,
    _tool_result_fallback_text,
)
from cyrene.agent.message import (
    _apply_assistant_meta,
    _assistant_entry_from_response,
    _flush_intermediate_user_replies,
    _tool_result_requests_user_input,
)
from cyrene.agent.prompts import (
    _DEEP_RESEARCH_PHASE1_DECISION,
    _MAIN_AGENT_PROMPT,
    _PHASE1_DECISION_PROMPT,
)
from cyrene.agent.session import _append_session_message, _save_session_messages
from cyrene.agent.state import (
    _AWAITING_USER_SENTINEL,
    _call_llm,
    _caller_type,
    _current_command,
    _current_round_id,
    _DEEP_RESEARCH_LIGHT_TOOL_DEFS,
    _deep_research_first_round,
    _deep_research_mode,
    _interrupt_event,
    _LIGHT_TOOL_DEFS,
    _MAX_TOOL_ROUNDS,
    _publish_runtime_event,
    _streaming_reply_requested,
    _ui_round_hide_initial_detail,
)
from cyrene.llm import _assistant_text, _truncate
from cyrene.tools import _execute_tool, get_active_tool_defs

logger = logging.getLogger(__name__)


async def _run_main_agent(
    user_message: str,
    history: list,
    bot: Any,
    chat_id: int,
    db_path: str,
    system_prompt: str = "",
    client_request_id: str = "",
    persist_user_message: bool = True,
    public_user_message: str | None = None,
    public_attachments: list[dict[str, Any]] | None = None,
    lang: str = "",
) -> str:
    _caller_type.set("main_agent")
    suppress_initial_detail = _ui_round_hide_initial_detail.get()
    round_id = _current_round_id.get()
    visible_user_message = user_message if public_user_message is None else str(public_user_message)
    user_message_id = f"user_{uuid4().hex}"
    user_entry = {"role": "user", "content": visible_user_message, "message_id": user_message_id}
    if public_attachments:
        user_entry["attachments"] = [dict(item) for item in public_attachments if isinstance(item, dict)]
    if round_id:
        user_entry["round_id"] = round_id
    if client_request_id:
        user_entry["client_request_id"] = client_request_id
    if persist_user_message:
        await _append_session_message(user_entry)
    effective_system = system_prompt or _MAIN_AGENT_PROMPT
    llm_user_entry = dict(user_entry)
    llm_user_entry["content"] = user_message
    phase1_tools = _LIGHT_TOOL_DEFS
    if _deep_research_first_round.get():
        phase1_decision = _DEEP_RESEARCH_PHASE1_DECISION
        phase1_tools = _DEEP_RESEARCH_LIGHT_TOOL_DEFS
    elif _current_command.get() == "quick-answer":
        phase1_decision = (
            "Decision phase rules:\n"
            "- You are in Quick Answer mode. The user wants a fast, text-only answer.\n"
            "- Call `quit` immediately with your answer. Do NOT call `use_tools`.\n"
            "- Call `ask_user` ONLY if the question is genuinely unclear.\n"
            "- This mode is for pure conversation only — no tools, no research."
        )
    else:
        phase1_decision = _PHASE1_DECISION_PROMPT
    phase1_messages = [{"role": "system", "content": effective_system}, *history, llm_user_entry, {"role": "user", "content": phase1_decision}]

    async def _ensure_text_reply(
        response_obj: dict[str, Any],
        base_messages: list[dict[str, Any]],
        fallback: str = "Done.",
    ) -> str:
        text = _assistant_text(response_obj).strip()
        has_tool_results = any(
            (
                str(message.get("role") or "") == "tool"
                or (
                    str(message.get("role") or "") == "assistant"
                    and bool(message.get("tool_calls"))
                )
            )
            for message in base_messages
            if isinstance(message, dict)
        )
        if text and not (has_tool_results and _is_placeholder_reply(text)):
            return text
        if has_tool_results:
            final_user_text = (await _final_user_reply_from_history(base_messages, max_tokens=None)).strip()
            if final_user_text and not _is_placeholder_reply(final_user_text):
                return final_user_text
            fallback_from_tools = _tool_result_fallback_text(base_messages).strip()
            if fallback_from_tools:
                return fallback_from_tools
        else:
            final_plain_text = (await _final_plain_reply_from_history(base_messages, max_tokens=None)).strip()
            if final_plain_text and not _is_placeholder_reply(final_plain_text):
                return final_plain_text
        final_text = (await _final_reply_from_history(base_messages, max_tokens=None)).strip()
        if final_text and not _is_placeholder_reply(final_text):
            return final_text
        return fallback

    def _session_messages_to_save(current_messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
        _flush_intermediate_user_replies(current_messages)
        saved: list[dict[str, Any]] = []
        for message in current_messages[1:]:
            if message["role"] == "system":
                continue
            if bool(message.get("hidden_from_ui")):
                continue
            if not persist_user_message and message.get("message_id") == user_message_id:
                continue
            if message.get("role") == "user" and message.get("message_id") == user_message_id:
                saved.append(dict(user_entry))
                continue
            saved.append(message)
        return saved

    try:
        from cyrene import behavior_learning as _behavior_learning
        routed = await _behavior_learning.try_route_and_execute_skill(
            user_message=user_message, visible_user_entry=dict(user_entry),
            llm_user_entry=dict(llm_user_entry), history=history,
            bot=bot, chat_id=chat_id, db_path=db_path,
            effective_system=effective_system, client_request_id=client_request_id, round_id=round_id,
            lang=lang,
        )
    except Exception:
        logger.warning("Learned skill routing failed; falling back to main agent loop", exc_info=True)
        routed = None
    if routed is not None:
        await _publish_runtime_event({
            "type": "phase_transition", "from": "skill_router", "to": "learned_skill",
            "detail": f"Matched learned skill {routed['skill']['name']} ({routed['skill']['skill_type']})",
        })
        await _save_session_messages(_session_messages_to_save(routed["messages"]))
        return str(routed["final_text"] or "Done.")

    # Phase 1: lightweight decision
    response = await _call_llm(phase1_messages, tools=phase1_tools)
    tool_calls = response.get("tool_calls") or []
    dr_tools = {"ask_user", "quit"}
    general_tools = {"use_tools", "ask_user", "quit"}
    phase1_allowed = dr_tools if _deep_research_first_round.get() else general_tools
    invalid_phase1_tools = [
        str(tc.get("function", {}).get("name") or "").strip()
        for tc in tool_calls
        if str(tc.get("function", {}).get("name") or "").strip() not in phase1_allowed
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
                    + (f"Only `ask_user` and `quit` are available in this phase. You MUST ask the user about the report length before starting research."
                       if _deep_research_first_round.get()
                       else "Only `use_tools`, `ask_user`, and `quit` are available in this phase. "
                            "If real tool work is needed, call `use_tools` with the user's exact original message. "
                            "If clarification is needed before acting, call `ask_user`. "
                            "Otherwise say there is no suitable tool in this phase.")
                ),
            },
        ]
        response = await _call_llm(retry_messages, tools=phase1_tools)
    tool_calls = response.get("tool_calls") or []
    messages = [{"role": "system", "content": effective_system}, *history, llm_user_entry]
    assistant_entry = _assistant_entry_from_response(response, round_id)
    messages.append(assistant_entry)

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
            return await _ensure_text_reply(response, messages)

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
        return (await _ensure_text_reply(response, messages, fallback=str(result)))

    if use_tools_call:
        event = {"type": "phase_transition", "from": "phase1_decision", "to": "phase2_execution"}
        if not suppress_initial_detail:
            event["detail"] = f"Phase 1 decided to use tools. Task: {user_message[:120]}"
        await _publish_runtime_event(event)
        messages = [{"role": "system", "content": effective_system}, *history, dict(llm_user_entry)]

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
                await _publish_runtime_event({"type": "phase_transition", "from": "execution", "to": "done", "detail": "Agent called quit"})
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
                return await _ensure_text_reply(response, messages)
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
                return await _ensure_text_reply(response, messages)

            awaiting_user = False
            spawned = False
            for index, t in enumerate(tcs):
                tool_name = t.get("function", {}).get("name")
                if awaiting_user:
                    skipped_tool_entry: dict[str, Any] = {
                        "role": "tool", "tool_call_id": t["id"],
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

            # Subagent monitoring loop
            if spawned:
                await _publish_runtime_event({
                    "type": "phase_transition", "from": "phase2_execution", "to": "subagent_monitoring",
                    "detail": "Subagents spawned, entering monitoring loop",
                })
                from cyrene.subagent import (
                    _run_subagent, _spawn_subagent_task,
                    build_deep_research_source as _build_deep_research_source,
                    build_flow_snapshot as _build_subagent_flow_snapshot,
                    clear as _sub_clear, get_snapshot as _sub_snapshot,
                    get_raw_messages as _sub_raw_msgs, reactivate as _sub_reactivate,
                    run_summary_subagent as _run_summary_subagent,
                )
                from cyrene.inbox import get_unread_count as _inbox_unread
                from cyrene.modules.deep_research import (
                    deduplicate_references as _deduplicate_references,
                    deep_research_pdf_attachment as _deep_research_pdf_attachment,
                    expansion_pass as _expansion_pass,
                    extract_new_references as _extract_new_references,
                    generate_deep_research_outline as _generate_deep_research_outline,
                    load_research_template as _load_research_template,
                    parse_length_preference as _parse_length_preference,
                    assemble_report as _assemble_report,
                    write_section as _write_section,
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
                    snap = await _sub_snapshot(round_id=round_id)
                    if not snap:
                        break
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
                    snap2 = await _sub_snapshot(round_id=round_id)
                    all_truly_done = all(
                        info["status"] in ("done", "timeout") and _inbox_unread(aid) == 0
                        for aid, info in snap2.items()
                    )
                    if all_truly_done and not resurrected:
                        quiet_ticks += 1
                        if quiet_ticks >= 2:
                            break
                    else:
                        quiet_ticks = 0
                if interrupted:
                    # Don't return early — proceed to summary phase with whatever
                    # results the subagents have produced so far.
                    await _save_session_messages(_session_messages_to_save(messages))
                await asyncio.sleep(2)
                await _publish_runtime_event({
                    "type": "phase_transition", "from": "subagent_monitoring", "to": "synthesis",
                    "detail": "All subagents done, starting summary subagent",
                })
                summary_result = await _run_summary_subagent(
                    round_id=round_id, parent_task=user_message, round_history=messages,
                )

                # Deep research Phase 3
                if _deep_research_mode.get():
                    source_material = await _build_deep_research_source(round_id)
                    template = _load_research_template()
                    length_pref = _parse_length_preference(messages)
                    outline = await _generate_deep_research_outline(source_material, template, user_message, lang, length_pref)
                    units: list[dict] = outline.get("units", [])
                    if not units:
                        logger.warning("Deep research outline has no units, falling back to research materials")
                        final_text = source_material
                        synthesis_entry = {"role": "assistant", "content": final_text}
                    else:
                        sections_written: list[str] = []
                        references_accumulated: list[str] = []
                        for unit_no, unit_def in enumerate(units, 1):
                            section_text = await _write_section(
                                source_material=source_material, outline=outline,
                                report_so_far="\n\n".join(sections_written),
                                references_so_far="\n".join(references_accumulated),
                                unit_def=unit_def, unit_no=unit_no, total_units=len(units),
                                lang=lang, length_pref=length_pref,
                            )
                            body, new_refs = _extract_new_references(section_text)
                            sections_written.append(body)
                            references_accumulated.extend(new_refs)
                        total_len = sum(len(s) for s in sections_written)
                        expand_threshold = {"short": 4000, "medium": 8000, "long": 15000}.get(length_pref, 8000)
                        if total_len < expand_threshold:
                            sections_written = await _expansion_pass(
                                source_material, outline, sections_written, references_accumulated, lang,
                            )
                        references_accumulated, dedup_mapping = _deduplicate_references(references_accumulated)
                        final_text = _assemble_report(sections_written, references_accumulated, outline, dedup_mapping=dedup_mapping)
                    # Add a brief concluding message after the report
                    if lang and lang != "en":
                        closing_note = "\n\n---\n\n✅ **深度研究报告已生成完成。**"
                    else:
                        closing_note = "\n\n---\n\n✅ **Deep research report has been generated.**"
                    pdf_attachment = _deep_research_pdf_attachment(round_id, user_message, final_text)
                    if pdf_attachment:
                        pdf_name = pdf_attachment.get("name", "deep-research-report.pdf")
                        pdf_url = pdf_attachment.get("url", "")
                        if pdf_url:
                            closing_note += f"\n\n📎 [{pdf_name}]({pdf_url})"
                        synthesis_entry["attachments"] = [pdf_attachment]
                    final_text = final_text.rstrip() + closing_note
                    synthesis_entry = {"role": "assistant", "content": final_text, "deep_research_report": True}
                else:
                    final_text = summary_result
                    synthesis_entry = {"role": "assistant", "content": final_text}

                flow_snapshot = await _build_subagent_flow_snapshot(round_id)
                if client_request_id:
                    synthesis_entry["client_request_id"] = client_request_id
                if round_id:
                    synthesis_entry["round_id"] = round_id
                if flow_snapshot:
                    synthesis_entry["subagent_flow_snapshot"] = flow_snapshot
                # 弹出 Phase 2 的 assistant entry（content="" + tool_calls），避免
                # 流式输出时与 synthesis_entry 的 clientRequestId 重复导致前端去重异常
                if _streaming_reply_requested():
                    messages.pop()
                messages.append(_apply_assistant_meta(synthesis_entry))
                await _sub_clear(round_id=round_id)
                await _save_session_messages(_session_messages_to_save(messages))
                return final_text

        await _save_session_messages(_session_messages_to_save(messages))
        return "Stopped after hitting the tool loop limit."

    # Deep research first round: if LLM output text instead of calling ask_user, retry
    if _deep_research_first_round.get() and not ask_user_call and not use_tools_call:
        retry_messages = [
            *phase1_messages,
            {
                **_assistant_entry_from_response(response, round_id="", include_tool_calls=False),
                "content": _assistant_text(response) or (response.get("content") or ""),
            },
            {
                "role": "user",
                "content": (
                    "You replied with text. You MUST call the `ask_user` function. "
                    "Call `ask_user` with text=\"请选择报告篇幅\" and "
                    "options=[\"长（30+页）\", \"中（20+页）\", \"短（10+页）\"]."
                ),
            },
        ]
        response = await _call_llm(retry_messages, tools=phase1_tools)
        for tc in (response.get("tool_calls") or []):
            if tc.get("function", {}).get("name") == "ask_user":
                ask_user_call = tc
                break
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
            return (await _ensure_text_reply(response, messages, fallback=str(result)))

    # Chat-only path (no tools)
    event = {"type": "phase_transition", "from": "phase1_decision", "to": "chat_only"}
    if not suppress_initial_detail:
        event["detail"] = "Phase 1 decided chat-only, no tools needed"
    await _publish_runtime_event(event)
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
    return await _ensure_text_reply(response, messages)
