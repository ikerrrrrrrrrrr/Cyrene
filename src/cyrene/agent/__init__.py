"""Agent subpackage — split from the monolithic agent.py.

``from cyrene.agent import run_agent`` still works via the re-exports below.
"""
# Backward-compat: re-export from cyrene.agent.state so that the same
# mutable references are shared by all sub-modules (tests that write
# ``agent.STATE_FILE = path`` must use ``cyrene.agent.state.STATE_FILE``
# instead if they need sub-modules to see the update).
from cyrene.agent.state import DATA_DIR, STATE_FILE

from cyrene.agent.state import (
    _active_main_round_id,
    _active_main_round_prompt,
    _active_main_round_public_prompt,
    _active_main_round_started_at,
    _agent_lock,
    _AWAITING_USER_SENTINEL,
    _call_llm,
    _call_llm_stream,
    _caller_type,
    _current_agent_id,
    _current_client_request_id,
    _current_command,
    _current_round_id,
    _deep_research_mode,
    _emit_reply_stream_event,
    _init_session_epoch,
    _interrupt_event,
    _LIGHT_TOOL_DEFS,
    _llm_phase_name,
    _MAIN_INBOX_AGENT_ID,
    _main_inbox_worker,
    _MAX_HISTORY_MESSAGES,
    _MAX_TOOL_ROUNDS,
    _pending_compressors,
    _pending_interrupt_clearers,
    _pending_label_refreshes,
    _pending_intermediate_user_replies,
    _persist_base_messages,
    _persist_history_prefix_len,
    _persist_insert_at,
    _persist_merge_live_state,
    _publish_runtime_event,
    _REPORT_REF_MAX_PREVIEW,
    _REPORT_REF_PREFIX,
    _reply_stream_writer,
    _session_epoch,
    _session_state_lock,
    _streaming_reply_requested,
    _tool_quit,
    _ui_round_assistant_meta,
    _ui_round_hide_initial_detail,
)

from cyrene.agent.prompts import (
    _CLAUDE_CODE_PROMPT,
    _COMPARE_SUBAGENT_PROMPT,
    _contains_cjk,
    _DAILY_REVIEW_PROMPT,
    _DECISION_SUBAGENT_PROMPT,
    _DEEP_COMPARE_PROMPT,
    _DEEP_RESEARCH_PROMPT,
    _DEEP_RESEARCH_SUBAGENT_PROMPT,
    _DEFAULT_TEMPLATE,
    _EXECUTION_SYSTEM_PROMPT,
    _EXPANSION_PROMPT,
    _HELP_ME_DECIDE_PROMPT,
    _LEARNING_PLAN_PROMPT,
    _LEARNING_SUBAGENT_PROMPT,
    _MAIN_AGENT_PROMPT,
    _OUTLINE_GENERATION_PROMPT,
    _PHASE1_DECISION_PROMPT,
    _QUICK_ANSWER_PROMPT,
    _SECTION_WRITE_PROMPT,
    _spawn_policy_prompt_block,
    build_claude_code_question_payload,
    _fallback_claude_code_prompt,
    optimize_claude_code_prompt,
)

from cyrene.agent.message import (
    _apply_assistant_meta,
    _assistant_entry_from_response,
    _dedupe_messages_by_id,
    _ensure_message_identity,
    _extract_json_object,
    _fallback_label,
    _flush_intermediate_user_replies,
    _insert_intermediate_user_reply,
    _is_replaceable_live_message,
    _merge_message_sequence,
    _message_suffix_after_persisted_prefix,
    _round_epoch_ms,
    _round_started_iso,
    _round_title_from_entry,
    _tool_result_requests_user_input,
)

from cyrene.agent.session import (
    _append_session_message,
    _clear_pending_question,
    _compress_old_messages,
    _compress_report_messages_for_storage,
    _expand_report_reference_history,
    _guidance_persist_context_after_ack,
    _guidance_round_context,
    _iter_report_refs,
    _load_pending_question,
    _load_round_messages,
    _load_session_messages,
    _load_session_state,
    _looks_like_report_followup,
    _normalize_pending_question,
    _pending_question_resume_context,
    _refresh_session_labels,
    _remove_last_exchange,
    _report_reference_stub,
    _report_title_from_text,
    _restore_pending_question,
    _save_session_messages,
    _schedule_memory_compression,
    _select_report_ref,
    _trim_session_messages,
    _upsert_pending_question,
    _write_session_messages_locked,
    _write_session_state,
    append_system_message,
    clear_session_id,
    get_pending_question,
    get_session_labels,
)

from cyrene.agent.round import (
    get_live_rounds,
    query_live_rounds,
)

from cyrene.agent.guidance import (
    _fan_out_guidance_to_subagents,
    _final_plain_reply_from_history,
    _final_reply_from_history,
    _final_user_reply_from_history,
    _generate_guidance_ack,
    _guidance_ack_text,
    _guidance_error_text,
    _is_affirmative_answer,
    _is_negative_answer,
    _process_main_inbox_message,
    _publish_round_guidance_update,
    _synthesize_subagent_results,
    _tool_result_fallback_text,
    _wait_for_subagent_round,
    answer_pending_question,
    format_httpx_error,
    queue_round_guidance,
)

from cyrene.agent.coordinator import (
    _run_chat_agent,
    _run_execution_agent,
    interrupt_active_run,
    run_agent,
    run_heartbeat_agent,
    run_steward_agent,
    run_task_agent,
)

from cyrene.agent.agent import (
    _run_main_agent,
)

from cyrene.agent.message import (
    _is_placeholder_reply,
)


def _register_quit_handler() -> None:
    """Register the quit tool handler with the shared handler dict.

    Called lazily or at module init — the import is deferred so that
    ``cyrene.agent`` can be imported without pulling in the full
    ``cyrene.tools`` dependency chain (PIL, pypdf, etc.).
    """
    from cyrene.tools import TOOL_HANDLERS
    TOOL_HANDLERS["quit"] = _tool_quit


# Module-level init — safe to call unconditionally because ``cyrene.tools``
# is loaded early in the application startup path.
_register_quit_handler()
_init_session_epoch()
