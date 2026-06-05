"""Agent coordinator: entry points, chat agent orchestration, execution agent.

Depends on all other ``agent.*`` modules.  ``_run_chat_agent`` is the
main orchestration function that sets up context, assembles the system
prompt, and delegates to ``agent._run_main_agent`` (the core two-phase
loop).
"""

import asyncio
import json
import logging
from datetime import datetime
from typing import Any

import cyrene.agent.state as _state

from cyrene.agent.commands import DEEP_REFLECT_COMMAND_ID, parse_deep_reflect_command
from cyrene.agent.deep_reflection import create_deep_reflection_record
from cyrene import debug
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
    _ensure_message_identity,
    _flush_intermediate_user_replies,
)
from cyrene.agent.prompts import (
    _CLAUDE_CODE_PROMPT,
    _DAILY_REVIEW_PROMPT,
    _DEEP_COMPARE_PROMPT,
    _DEEP_RESEARCH_PROMPT,
    _EXECUTION_SYSTEM_PROMPT,
    _HELP_ME_DECIDE_PROMPT,
    _LEARNING_PLAN_PROMPT,
    _MAIN_AGENT_PROMPT,
    _PHASE1_DECISION_PROMPT,
    _QUICK_ANSWER_PROMPT,
    _spawn_policy_prompt_block,
)
from cyrene.agent.session import (
    _expand_report_reference_history,
    _load_session_messages,
    _schedule_session_label_refresh,
    _save_session_messages,
    clear_session_id,
    get_session_labels,
)
from cyrene.agent.state import (
    _active_main_round_id,
    _active_main_round_prompt,
    _active_main_round_public_prompt,
    _active_main_round_started_at,
    _agent_lock,
    _AWAITING_USER_SENTINEL,
    _call_llm,
    _caller_type,
    _current_client_request_id,
    _current_command,
    _current_round_id,
    _deep_research_first_round,
    _deep_research_mode,
    _interrupt_event,
    _LIGHT_TOOL_DEFS,
    _MAX_TOOL_ROUNDS,
    _pending_interrupt_clearers,
    _pending_intermediate_user_replies,
    _persist_base_messages,
    _persist_history_prefix_len,
    _persist_insert_at,
    _persist_merge_live_state,
    _publish_runtime_event,
    _streaming_reply_requested,
    _tool_quit,
    _ui_round_assistant_meta,
    _ui_round_hide_initial_detail,
)
from cyrene.config import ASSISTANT_NAME
from cyrene.context_trace import context_block
from cyrene.llm import _assistant_text, _truncate
from cyrene.memory import get_memory_context
from cyrene.short_term import get_context
from cyrene.skills_registry import build_skill_prompt_block
from cyrene.settings_store import get_spawn_policy
from cyrene.tools import TOOL_HANDLERS, _execute_tool, get_active_tool_defs

logger = logging.getLogger(__name__)
_BACKGROUND_BEHAVIOR_TASKS: set[asyncio.Task[Any]] = set()


def _track_background_behavior_task(task: asyncio.Task[Any]) -> None:
    _BACKGROUND_BEHAVIOR_TASKS.add(task)

    def _done(completed: asyncio.Task[Any]) -> None:
        _BACKGROUND_BEHAVIOR_TASKS.discard(completed)
        try:
            completed.exception()
        except asyncio.CancelledError:
            return
        except Exception:
            logger.debug("background behavior-learning task finished with exception", exc_info=True)

    task.add_done_callback(_done)


async def _kick_behavior_learning_processing() -> None:
    from cyrene import behavior_learning as _behavior_learning

    task = asyncio.create_task(_behavior_learning.process_unprocessed_turns())
    _track_background_behavior_task(task)
    try:
        await asyncio.wait_for(asyncio.shield(task), timeout=0.2)
    except asyncio.TimeoutError:
        return


# ---------------------------------------------------------------------------
# Execution agent (internal, all tools)
# ---------------------------------------------------------------------------

async def _run_execution_agent(task: str, bot: Any, chat_id: int, db_path: str, notify_state: dict[str, bool] | None = None) -> str:
    # 使用 agent_lock 防止与用户聊天并发执行
    if _agent_lock.locked():
        return ""
    async with _agent_lock:
        _interrupt_event.clear()
        return await _run_execution_agent_locked(task, bot, chat_id, db_path, notify_state)


async def _run_execution_agent_locked(task: str, bot: Any, chat_id: int, db_path: str, notify_state: dict[str, bool] | None = None) -> str:
    _caller_type.set("execution_agent")
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
# Chat agent (entry point with lock)
# ---------------------------------------------------------------------------

async def run_agent(
    user_message: str,
    bot: Any,
    chat_id: int,
    db_path: str,
    client_request_id: str = "",
    lang: str = "",
    command: str = "",
    public_user_message: str | None = None,
    public_attachments: list[dict[str, Any]] | None = None,
) -> str:
    """Main entry point. Runs the main agent loop with full tools."""
    if _agent_lock.locked():
        interrupt_active_run()
    async with _agent_lock:
        _interrupt_event.clear()
        return await _run_chat_agent(
            user_message, bot, chat_id, db_path,
            client_request_id=client_request_id, lang=lang, command=command,
            public_user_message=public_user_message, public_attachments=public_attachments,
        )


async def _clear_interrupt_when_idle() -> None:
    try:
        while _agent_lock.locked():
            await asyncio.sleep(0.05)
    finally:
        _interrupt_event.clear()


def interrupt_active_run() -> bool:
    if not _agent_lock.locked():
        _interrupt_event.clear()
        return False
    _interrupt_event.set()
    task = asyncio.create_task(_clear_interrupt_when_idle())
    _pending_interrupt_clearers.add(task)
    task.add_done_callback(_pending_interrupt_clearers.discard)
    return True


# ---------------------------------------------------------------------------
# Chat agent coordinator
# ---------------------------------------------------------------------------

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
    public_user_message: str | None = None,
    public_attachments: list[dict[str, Any]] | None = None,
    public_prompt: str | None = None,
    refresh_labels: bool = True,
    hide_initial_detail: bool = False,
    assistant_message_meta: dict[str, Any] | None = None,
    lang: str = "",
    command: str = "",
) -> str:
    import time as _time

    original_user_message = str(user_message or "")
    deep_reflect_parse = parse_deep_reflect_command(original_user_message)
    if deep_reflect_parse.get("matched"):
        command = DEEP_REFLECT_COMMAND_ID
        user_message = str(deep_reflect_parse.get("focus") or "")
        if public_user_message is None:
            public_user_message = original_user_message
        if public_prompt is None:
            public_prompt = original_user_message

    round_id = str(forced_round_id or "").strip() or f"round_{int(_time.time() * 1000)}"
    round_token = _current_round_id.set(round_id)
    full_session_messages = _load_session_messages()
    # Update state module globals so reads via cyrene.agent.state are visible
    _state._active_main_round_id = round_id
    _state._active_main_round_prompt = user_message
    _state._active_main_round_public_prompt = user_message if public_prompt is None else str(public_prompt)
    _state._active_main_round_started_at = _time.time()
    raw_history = list(history_override) if history_override is not None else _load_session_messages()
    history = _expand_report_reference_history(raw_history, user_message)
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
    behavior_turn_context: dict[str, Any] | None = None
    final_output = ""
    try:
        restored_short_term = False
        if not history:
            st = get_context(max_chars=5000)
            if st:
                history = [{"role": "system", "content": "[Restored context]\n" + st}]
                restored_short_term = True
        if ephemeral_system:
            history = [*history, {"role": "system", "content": ephemeral_system}]

        if command != DEEP_REFLECT_COMMAND_ID:
            try:
                from cyrene import behavior_learning as _behavior_learning
                labels = get_session_labels(round_id)
                behavior_turn_context = await _behavior_learning.begin_turn(
                    session_id=labels.get("archive_session_id", ""),
                    round_id=round_id, user_message=user_message,
                    history=history, session_title=labels.get("session_title", ""),
                )
            except Exception:
                logger.warning("Failed to initialize behavior-learning turn context", exc_info=True)
                behavior_turn_context = None

        try:
            memory_context = get_memory_context(include_short_term=not restored_short_term)
        except TypeError as exc:
            if "include_short_term" not in str(exc):
                raise
            memory_context = get_memory_context()
        main_system = _MAIN_AGENT_PROMPT
        now = datetime.now().astimezone()
        temporal_context = (
            "## Current Date\n"
            f"- Current local date: {now:%Y-%m-%d} ({now:%A}).\n"
            "- Interpret relative phrases such as today, recently, this week, last week, 最近, 最近一周, 今天, 本周 relative to this date.\n"
            "- For current weather or travel recommendations, search for current forecast/current conditions. Do not invent or substitute old years unless the user explicitly asks for historical weather."
        )
        main_system += "\n\n" + temporal_context
        main_system_context = [
            context_block(
                "main.system.base",
                "system",
                source="cyrene.agent.prompts._MAIN_AGENT_PROMPT",
                reason="base main-agent instructions",
                content=_MAIN_AGENT_PROMPT,
            ),
            context_block(
                "runtime.temporal_context",
                "system",
                source="datetime.now().astimezone()",
                reason="anchor relative dates and current/recent searches",
                content=temporal_context,
                metadata={"date": f"{now:%Y-%m-%d}", "timezone": now.tzname()},
                transforms=["concat_into_system"],
            ),
        ]
        if lang and lang != "en":
            lang_prompt = f"The user has set their preferred language to {lang}. Reply in this language."
            main_system += "\n\n" + lang_prompt
            main_system_context.append(context_block(
                "main.system.language",
                "system",
                source="run_agent(lang)",
                reason="user selected preferred language",
                content=lang_prompt,
                metadata={"lang": lang},
            ))
        if memory_context:
            main_system = main_system + "\n\n## Memory Context\n" + memory_context
            main_system_context.append(context_block(
                "memory.context",
                "memory",
                source="cyrene.memory.get_memory_context",
                reason="main agent memory injection",
                transforms=["concat_into_system"],
                content=memory_context,
            ))
        skill_prompt_block = build_skill_prompt_block()
        if skill_prompt_block:
            main_system = main_system + "\n\n" + skill_prompt_block
            main_system_context.append(context_block(
                "skills.installed",
                "skills",
                source="cyrene.skills_registry.build_skill_prompt_block",
                reason="enabled external skills are visible to the agent",
                transforms=["preview", "concat_into_system"],
                content=skill_prompt_block,
            ))

        is_deep_research = command == "deep-research"
        dr_token = _deep_research_mode.set(is_deep_research)
        dr_first_token = _deep_research_first_round.set(is_deep_research and not bool(forced_round_id))
        cmd_token = _current_command.set(command)

        if command == DEEP_REFLECT_COMMAND_ID:
            visible_command_text = str(public_user_message if public_user_message is not None else original_user_message or "/deep-reflect").strip() or "/deep-reflect"
            visible_history = [
                message for message in history
                if isinstance(message, dict)
                and str(message.get("role") or "") != "system"
                and not bool(message.get("hidden_from_ui"))
            ]
            user_entry: dict[str, Any] = {
                "role": "user",
                "content": visible_command_text,
                "round_id": round_id,
            }
            if client_request_id:
                user_entry["client_request_id"] = client_request_id
            _ensure_message_identity([user_entry])
            try:
                reflection_record = await create_deep_reflection_record(
                    list(visible_history),
                    scope="current_round",
                    goal_gap="The user manually requested deep reflection because the current work may not be satisfying the goal.",
                    focus=user_message,
                    lang_text=visible_command_text or user_message,
                )
                reflection_record["round_id"] = round_id
                if client_request_id:
                    reflection_record["client_request_id"] = client_request_id
                main_text = str(reflection_record.get("content") or "Deep reflection is complete.")
                await _save_session_messages([*visible_history, user_entry, reflection_record])
            except Exception as exc:
                logger.warning("Manual deep reflection failed", exc_info=True)
                main_text = f"深度反思失败：{exc}" if any("\u4e00" <= ch <= "\u9fff" for ch in visible_command_text) else f"Deep reflection failed: {exc}"
                assistant_entry = _apply_assistant_meta({
                    "role": "assistant",
                    "content": main_text,
                    "round_id": round_id,
                })
                if client_request_id:
                    assistant_entry["client_request_id"] = client_request_id
                _ensure_message_identity([assistant_entry])
                await _save_session_messages([*visible_history, user_entry, assistant_entry])

            if refresh_labels:
                _schedule_session_label_refresh(visible_command_text, round_id)
            final_output = main_text
            await _publish_runtime_event({
                "type": "chat_message",
                "client_request_id": client_request_id,
            })
            if behavior_turn_context is not None:
                try:
                    from cyrene import behavior_learning as _behavior_learning
                    latest_labels = get_session_labels(round_id)
                    await _behavior_learning.complete_turn(
                        turn_id=behavior_turn_context["turn_id"],
                        assistant_response=final_output,
                        session_title=latest_labels.get("session_title", ""),
                        round_title=latest_labels.get("round_title", ""),
                    )
                    await _kick_behavior_learning_processing()
                except Exception:
                    logger.warning("Failed to finalize behavior-learning turn", exc_info=True)
            return final_output

        # Command-specific prompt injection
        if command == "deep-research":
            main_system = main_system + "\n\n" + _DEEP_RESEARCH_PROMPT
            main_system_context.append(context_block(
                "command.deep-research",
                "command_prompt",
                source="cyrene.agent.prompts._DEEP_RESEARCH_PROMPT",
                reason="deep-research command selected",
                transforms=["concat_into_system"],
                content=_DEEP_RESEARCH_PROMPT,
            ))
            deep_research_spawn_policy = (
                "\n\n## Subagent Spawn Policy\n"
                "Current policy: deep-research (maximum parallelism).\n"
                "- You MUST spawn subagents for EVERY research track. Never do research yourself — your only job is to decompose, delegate, and synthesize.\n"
                "- Launch ALL subagents at once in a single batch. Do not wait for some to finish before spawning others.\n"
                "- If a research track is broad, split it further into narrower sub-tracks and spawn additional subagents.\n"
                "- Err on the side of MORE subagents. 5–10 subagents is normal; 10+ is acceptable for complex questions.\n"
                "- Even small, focused questions within a track deserve their own subagent. Granularity beats breadth per agent.\n"
                "- If any subagent result is thin, contradictory, or incomplete, immediately spawn follow-up subagents to fill the gap.\n"
                "- The ONLY reason not to spawn a subagent is if the task is already fully answered with high confidence. When in doubt, spawn."
            )
            main_system += deep_research_spawn_policy
            main_system_context.append(context_block(
                "spawn_policy.deep-research",
                "spawn_policy",
                source="cyrene.agent.coordinator",
                reason="deep-research command forces maximum parallelism",
                transforms=["concat_into_system"],
                content=deep_research_spawn_policy,
            ))
        elif command == "quick-answer":
            main_system = main_system + "\n\n" + _QUICK_ANSWER_PROMPT
            main_system_context.append(context_block(
                "command.quick-answer",
                "command_prompt",
                source="cyrene.agent.prompts._QUICK_ANSWER_PROMPT",
                reason="quick-answer command selected",
                transforms=["concat_into_system"],
                content=_QUICK_ANSWER_PROMPT,
            ))
        elif command == "help-me-decide":
            main_system = main_system + "\n\n" + _HELP_ME_DECIDE_PROMPT
            main_system_context.append(context_block(
                "command.help-me-decide",
                "command_prompt",
                source="cyrene.agent.prompts._HELP_ME_DECIDE_PROMPT",
                reason="help-me-decide command selected",
                transforms=["concat_into_system"],
                content=_HELP_ME_DECIDE_PROMPT,
            ))
            help_me_decide_spawn_policy = (
                "\n\n## Subagent Spawn Policy\n"
                "Current policy: help-me-decide.\n"
                "- Spawn exactly ONE subagent per option. Launch all simultaneously.\n"
                "- Do NOT do any option research yourself — delegate every option to its own subagent.\n"
                "- After all subagents return, synthesize into a decision report."
            )
            main_system += help_me_decide_spawn_policy
            main_system_context.append(context_block(
                "spawn_policy.help-me-decide",
                "spawn_policy",
                source="cyrene.agent.coordinator",
                reason="help-me-decide command sets delegation policy",
                transforms=["concat_into_system"],
                content=help_me_decide_spawn_policy,
            ))
        elif command == "learning-plan":
            main_system = main_system + "\n\n" + _LEARNING_PLAN_PROMPT
            main_system_context.append(context_block(
                "command.learning-plan",
                "command_prompt",
                source="cyrene.agent.prompts._LEARNING_PLAN_PROMPT",
                reason="learning-plan command selected",
                transforms=["concat_into_system"],
                content=_LEARNING_PLAN_PROMPT,
            ))
            learning_plan_spawn_policy = (
                "\n\n## Subagent Spawn Policy\n"
                "Current policy: learning-plan.\n"
                "- Spawn exactly ONE subagent per knowledge module. Launch all simultaneously.\n"
                "- Do NOT research learning resources yourself — delegate every module to its own subagent.\n"
                "- After all subagents return, synthesize into a structured learning plan."
            )
            main_system += learning_plan_spawn_policy
            main_system_context.append(context_block(
                "spawn_policy.learning-plan",
                "spawn_policy",
                source="cyrene.agent.coordinator",
                reason="learning-plan command sets delegation policy",
                transforms=["concat_into_system"],
                content=learning_plan_spawn_policy,
            ))
        elif command == "daily-review":
            main_system = main_system + "\n\n" + _DAILY_REVIEW_PROMPT
            main_system_context.append(context_block(
                "command.daily-review",
                "command_prompt",
                source="cyrene.agent.prompts._DAILY_REVIEW_PROMPT",
                reason="daily-review command selected",
                transforms=["concat_into_system"],
                content=_DAILY_REVIEW_PROMPT,
            ))
            spawn_policy_block = _spawn_policy_prompt_block("off")
            main_system = main_system + "\n\n" + spawn_policy_block
            main_system_context.append(context_block(
                "spawn_policy.off",
                "spawn_policy",
                source="cyrene.agent.prompts._spawn_policy_prompt_block",
                reason="daily-review disables subagents",
                transforms=["concat_into_system"],
                content=spawn_policy_block,
                metadata={"policy": "off"},
            ))
        elif command == "deep-compare":
            main_system = main_system + "\n\n" + _DEEP_COMPARE_PROMPT
            main_system_context.append(context_block(
                "command.deep-compare",
                "command_prompt",
                source="cyrene.agent.prompts._DEEP_COMPARE_PROMPT",
                reason="deep-compare command selected",
                transforms=["concat_into_system"],
                content=_DEEP_COMPARE_PROMPT,
            ))
            deep_compare_spawn_policy = (
                "\n\n## Subagent Spawn Policy\n"
                "Current policy: deep-compare.\n"
                "- Spawn exactly ONE subagent per comparison dimension. Launch all simultaneously.\n"
                "- Do NOT do any comparison research yourself — delegate every dimension to its own subagent.\n"
                "- After all subagents return, synthesize into a comparison matrix and recommendation."
            )
            main_system += deep_compare_spawn_policy
            main_system_context.append(context_block(
                "spawn_policy.deep-compare",
                "spawn_policy",
                source="cyrene.agent.coordinator",
                reason="deep-compare command sets delegation policy",
                transforms=["concat_into_system"],
                content=deep_compare_spawn_policy,
            ))
        elif command == "claude-code":
            main_system = main_system + "\n\n" + _CLAUDE_CODE_PROMPT
            main_system_context.append(context_block(
                "command.claude-code",
                "command_prompt",
                source="cyrene.agent.prompts._CLAUDE_CODE_PROMPT",
                reason="claude-code command selected",
                transforms=["concat_into_system"],
                content=_CLAUDE_CODE_PROMPT,
            ))
        else:
            spawn_policy = get_spawn_policy()
            spawn_policy_block = _spawn_policy_prompt_block(spawn_policy)
            main_system = main_system + "\n\n" + spawn_policy_block
            main_system_context.append(context_block(
                f"spawn_policy.{spawn_policy}",
                "spawn_policy",
                source="cyrene.agent.prompts._spawn_policy_prompt_block",
                reason="configured spawn policy",
                transforms=["concat_into_system"],
                content=spawn_policy_block,
                metadata={"policy": spawn_policy},
            ))

        from cyrene.agent.agent import _run_main_agent

        main_text = await _run_main_agent(
            user_message, history, bot, chat_id, db_path, main_system,
            client_request_id=client_request_id, persist_user_message=persist_user_message,
            public_user_message=public_user_message, public_attachments=public_attachments, lang=lang,
            system_context=main_system_context,
        )

        if refresh_labels:
            _schedule_session_label_refresh(user_message, round_id)
        if main_text == _AWAITING_USER_SENTINEL:
            return main_text
        if main_text:
            final_output = main_text
        elif assistant_message_meta and assistant_message_meta.get("system_initiated"):
            # System-initiated rounds (e.g. the proactive heartbeat) must stay
            # silent when the agent chose not to speak — never substitute a
            # filler "Done." that would be delivered to the user.
            final_output = ""
        else:
            final_output = "Done."
        await _publish_runtime_event({
            "type": "chat_message",
            "client_request_id": client_request_id,
        })
        if behavior_turn_context is not None:
            try:
                from cyrene import behavior_learning as _behavior_learning
                latest_labels = get_session_labels(round_id)
                await _behavior_learning.complete_turn(
                    turn_id=behavior_turn_context["turn_id"],
                    assistant_response=final_output,
                    session_title=latest_labels.get("session_title", ""),
                    round_title=latest_labels.get("round_title", ""),
                )
                await _kick_behavior_learning_processing()
            except Exception:
                logger.warning("Failed to finalize behavior-learning turn", exc_info=True)
        return final_output
    finally:
        if behavior_turn_context is not None:
            try:
                from cyrene import behavior_learning as _behavior_learning
                _behavior_learning.clear_turn_context(behavior_turn_context)
            except Exception:
                logger.debug("Failed to clear behavior-learning context", exc_info=True)
        _current_command.reset(cmd_token)
        _deep_research_mode.reset(dr_token)
        _deep_research_first_round.reset(dr_first_token)
        _ui_round_assistant_meta.reset(assistant_meta_token)
        _ui_round_hide_initial_detail.reset(hide_initial_detail_token)
        _pending_intermediate_user_replies.reset(intermediate_reply_token)
        _current_client_request_id.reset(client_request_token)
        _persist_insert_at.reset(insert_token)
        _persist_history_prefix_len.reset(prefix_token)
        _persist_merge_live_state.reset(merge_live_token)
        _persist_base_messages.reset(base_token)
        _state._active_main_round_id = ""
        _state._active_main_round_prompt = ""
        _state._active_main_round_public_prompt = ""
        _state._active_main_round_started_at = 0.0
        _state._temporary_full_access.set(False)
        _current_round_id.reset(round_token)


# ---------------------------------------------------------------------------
# Backward-compatible public API
# ---------------------------------------------------------------------------

async def run_task_agent(prompt: str, bot: Any, chat_id: int, db_path: str, notify_state: dict[str, bool] | None = None) -> str:
    return await _run_execution_agent(prompt, bot, chat_id, db_path, notify_state=notify_state)


async def run_heartbeat_agent(prompt: str, bot: Any, chat_id: int, db_path: str) -> str:
    proactive_system = (
        "This round was initiated by the scheduler, not by a user chat message.\n"
        "The hidden task you receive is internal guidance, not text to answer literally.\n"
        "Your final assistant reply will be shown directly to the user in the Web UI.\n"
        "Write to the user in a natural, user-facing voice.\n"
        "Match the user's preferred language based on their past messages.\n"
        "Do not mention the scheduler, heartbeat, lottery, hidden prompt, or internal instructions.\n"
        "\n"
        "DECISION RULE — a warm, light-touch check-in:\n"
        "- This is a chance to reach out the way a thoughtful friend would. If something specific comes to mind — a topic, plan, or feeling the user shared — follow up on it warmly.\n"
        "- A brief, genuine hello is fine even without a concrete hook, as long as it feels caring rather than mechanical.\n"
        "- Lean toward reaching out. Only stay silent (call `quit`) when a message now would feel intrusive or repetitive — for example you just messaged, or there is truly nothing worth saying.\n"
        "- If the user did not reply to a recent proactive message, be more considerate: keep it lighter and don't pile on.\n"
        "- Keep it to 1–2 sentences. Be direct, warm, and specific.\n"
        "- If tools are useful, use them before composing the reply."
    )
    if _agent_lock.locked():
        return ""
    async with _agent_lock:
        _interrupt_event.clear()
        return await _run_chat_agent(
            prompt, bot, chat_id, db_path,
            ephemeral_system=proactive_system, persist_user_message=False,
            public_prompt="", refresh_labels=False, hide_initial_detail=True,
            assistant_message_meta={"proactive": True, "system_initiated": True},
        )


async def run_steward_agent(conversation_text: str, soulmd_content: str, bot: Any, chat_id: int, db_path: str) -> str:
    # Query existing entity titles for LLM-level deduplication
    _existing_entity_hint = ""
    try:
        from cyrene.entities import list_entities
        _existing = await list_entities(db_path, limit=200)
        if _existing:
            _lines = [f"- [{e['type']}] {e['title']}" for e in _existing]
            _existing_entity_hint = "\n".join(_lines[:50])  # cap at 50 to keep prompt reasonable
    except Exception:
        pass

    steward_prompt = f"""You are a memory steward and entity extractor. Your job is twofold:

1. Update Cyrene's SOUL.md based on recent conversations (existing).
2. Extract entities (事务) from the conversation for background tracking.

Supported entity types: task, project, decision, knowledge, relationship, event, resource, idea, problem, habit.

### Part 1: SOUL.md updates
Read the recent conversation and current SOUL.md, then output:
- APPEND: what new information to add to SOUL.md
- ERASE: what old information to remove
- MERGE: what to consolidate
- Or SKIP if nothing important

### Part 2: Entity extraction
From the conversation, extract entities the user mentioned. Only extract when you are confident the user is talking about something real — not hypotheticals, jokes, or casual remarks.

CRITICAL: Check the existing entities list below. If the conversation mentions something semantically equivalent to an existing entity (same topic, same intent, different wording), SKIP it — do NOT output a duplicate. Use meaning, not just exact string match.

For each entity, output ENTITY with these fields:
ENTITY type="task" title="Buy groceries" confidence="0.85" content="User mentioned needing to buy groceries this weekend"

Confidence guidelines:
- ≥ 0.8: Clear actionable mention with specifics (dates, names, concrete actions)
- 0.5-0.7: Possible mention but lacks detail
- 0.2-0.5: Vague mention, store as low-confidence candidate
- < 0.2: Do not output (ignore)

Do NOT extract:
- Pure emotional expressions ("I'm so tired")
- Casual chit-chat ("I ate noodles")
- Hypothetical scenarios ("if I went to Mars")
- Anything semantically equivalent to an already-existing entity in the list below

### Existing entities (do NOT extract duplicates):
{_existing_entity_hint if _existing_entity_hint else "(none yet)"}

Output BOTH parts inline. Start with SOUL.md updates (APPEND/ERASE/MERGE/SKIP), then entity lines (ENTITY ...).

SOUL.md:
{soulmd_content}

Recent conversation:
{conversation_text}

Output only the modifications needed, one per line, prefixed with APPEND/ERASE/MERGE/SKIP/ENTITY."""
    return await _run_execution_agent(steward_prompt, bot, chat_id, db_path)
