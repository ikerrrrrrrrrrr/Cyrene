"""Agent module state: ContextVars, locks, and LLM call wrappers.

This is the leaf module of the ``agent/`` subpackage — it must not import
from any other ``agent.*`` module, so that every other module can safely
import from it without circular-dependency risk.
"""

import asyncio
import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable

from contextvars import ContextVar

from cyrene import debug
from cyrene.config import ASSISTANT_NAME, DATA_DIR as _DATA_DIR, STATE_FILE as _STATE_FILE

# Mutable references so tests that swap STATE_FILE/DATA_DIR are visible to all
# ``agent.*`` sub-modules (which import ``state.STATE_FILE`` / ``state.DATA_DIR``).
STATE_FILE = _STATE_FILE
DATA_DIR = _DATA_DIR

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# SessionContext — per-session state container
# ---------------------------------------------------------------------------

@dataclass
class SessionContext:
    """Holds all mutable state for a single agent session.

    Each active session (including the default "" session) gets its own
    ``SessionContext`` instance.  Fields previously stored as module-level
    globals are migrated here incrementally across the multi‑session phases.
    """
    session_id: str = ""
    state_file: Path | None = None  # None → DATA_DIR / "state.json" (default session)
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    session_state_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    session_epoch: int = 0
    interrupt_event: asyncio.Event = field(default_factory=asyncio.Event)
    active_main_round_id: str = ""
    active_main_round_prompt: str = ""
    active_main_round_public_prompt: str = ""
    active_main_round_started_at: float = 0.0
    pending_compressors: set[asyncio.Task] = field(default_factory=set)
    pending_label_refreshes: set[asyncio.Task] = field(default_factory=set)
    pending_interrupt_clearers: set[asyncio.Task] = field(default_factory=set)
    pending_distill_task: asyncio.Task | None = None
    main_inbox_worker: asyncio.Task | None = None

# Per‑session identifier carried by ContextVar — set at entry to run_agent()
_current_session_id: ContextVar[str] = ContextVar("_current_session_id", default="")

# Lazily populated cache of SessionContext instances, keyed by session_id.
# The default session (id="") is created on first access and lives forever.
_sessions: dict[str, SessionContext] = {}


def _ensure_session(session_id: str = "") -> SessionContext:
    """Return the ``SessionContext`` for *session_id*, creating it if needed."""
    global _session_epoch
    if session_id not in _sessions:
        if not session_id:
            state_file: Path | None = None  # signal "use DATA_DIR / state.json"
        else:
            state_file = _DATA_DIR / "sessions" / session_id / "state.json"
            state_file.parent.mkdir(parents=True, exist_ok=True)
        ctx = SessionContext(session_id=session_id, state_file=state_file)
        # The default session MUST share the existing module-level globals
        # so that code holding a reference to ``_agent_lock`` or importing
        # ``_interrupt_event`` from this module still works correctly.
        if not session_id:
            ctx.lock = _agent_lock
            ctx.session_state_lock = _session_state_lock
            ctx.session_epoch = _session_epoch
            ctx.interrupt_event = _interrupt_event
            ctx.pending_compressors = _pending_compressors
            ctx.pending_label_refreshes = _pending_label_refreshes
            ctx.pending_interrupt_clearers = _pending_interrupt_clearers
            ctx.main_inbox_worker = _main_inbox_worker
            ctx.active_main_round_id = _active_main_round_id
            ctx.active_main_round_prompt = _active_main_round_prompt
            ctx.active_main_round_public_prompt = _active_main_round_public_prompt
            ctx.active_main_round_started_at = _active_main_round_started_at
        _sessions[session_id] = ctx
    return _sessions[session_id]


def _get_session() -> SessionContext:
    """Return the ``SessionContext`` for the currently active session."""
    return _ensure_session(_current_session_id.get())


def _session_state_file(session_id: str = "") -> Path:
    """Return the state‑file path for *session_id*.

    The default session still reads from ``DATA_DIR / "state.json"`` for full
    backward compatibility.
    """
    ctx = _ensure_session(session_id)
    if ctx.state_file is not None:
        return ctx.state_file
    return STATE_FILE

# ---------------------------------------------------------------------------
# ContextVars — per-request state
# ---------------------------------------------------------------------------

_current_agent_id: ContextVar[str] = ContextVar("_current_agent_id", default="main")
_current_round_id: ContextVar[str] = ContextVar("_current_round_id", default="")
_current_client_request_id: ContextVar[str] = ContextVar("_current_client_request_id", default="")
_caller_type: ContextVar[str] = ContextVar("_caller_type", default="main_agent")
_persist_base_messages: ContextVar[list[dict[str, Any]] | None] = ContextVar("_persist_base_messages", default=None)
_persist_merge_live_state: ContextVar[bool] = ContextVar("_persist_merge_live_state", default=False)
_persist_history_prefix_len: ContextVar[int] = ContextVar("_persist_history_prefix_len", default=0)
_persist_insert_at: ContextVar[int | None] = ContextVar("_persist_insert_at", default=None)
_pending_intermediate_user_replies: ContextVar[list[dict[str, Any]] | None] = ContextVar("_pending_intermediate_user_replies", default=None)
_reply_stream_writer: ContextVar[Callable[[dict[str, Any]], Awaitable[None]] | None] = ContextVar("_reply_stream_writer", default=None)

_ui_round_hide_initial_detail: ContextVar[bool] = ContextVar("_ui_round_hide_initial_detail", default=False)
_ui_round_assistant_meta: ContextVar[dict[str, Any] | None] = ContextVar("_ui_round_assistant_meta", default=None)
_deep_research_mode: ContextVar[bool] = ContextVar("_deep_research_mode", default=False)
_deep_research_first_round: ContextVar[bool] = ContextVar("_deep_research_first_round", default=False)
_current_command: ContextVar[str] = ContextVar("_current_command", default="")
_conversation_source: ContextVar[str] = ContextVar("_conversation_source", default="")
# Map from filename (and original name without uuid prefix) → full absolute path
# Populated by routes.py when the user sends a message with attachments.
# Allows tools to auto-resolve agent-guessed paths (e.g. /tmp/file.txt) to the
# correct webui_uploads path without requiring a permission prompt.
_attachment_paths_by_name: ContextVar[dict[str, str] | None] = ContextVar("_attachment_paths_by_name", default=None)

# ---------------------------------------------------------------------------
# Module-level shared state
# ---------------------------------------------------------------------------

_agent_lock = asyncio.Lock()
_session_state_lock = asyncio.Lock()
_session_epoch: int = 0
_interrupt_event = asyncio.Event()

_MAX_HISTORY_MESSAGES = 40
_MAX_TOOL_ROUNDS = 15  # kept for backward-compat; prefer _get_max_tool_rounds()


def _get_max_tool_rounds() -> int:
    from cyrene.settings_store import get as _get_setting
    return max(5, min(200, int(_get_setting("max_tool_rounds", 15) or 15)))

_pending_compressors: set[asyncio.Task] = set()
_pending_label_refreshes: set[asyncio.Task] = set()
_pending_interrupt_clearers: set[asyncio.Task] = set()
_main_inbox_worker: asyncio.Task | None = None

_active_main_round_id = ""
_active_main_round_prompt = ""
_active_main_round_public_prompt = ""
_active_main_round_started_at = 0.0
# 临时 full_access 标记 —— "仅这次允许" 时由 guidance 设置，round 结束时清理
# 使用 ContextVar 确保 asyncio 任务间隔离
_temporary_full_access: ContextVar[bool] = ContextVar("_temporary_full_access", default=False)

# 本轮权限模式 —— 由 /api/chat 的 mode 字段决定，round 起始设置、结束重置。
#   "default"     —— 碰到权限边界时提问让用户授权（现状）
#   "full_access" —— 默认放行所有操作（round 起始时同时置 _temporary_full_access）
#   "auto"        —— 由审核 agent 自主裁决提权请求，从不打扰用户
#   "plan"        —— 先规划再执行（同意后回退默认模式）
PERMISSION_MODES = ("default", "full_access", "auto", "plan")
_permission_mode: ContextVar[str] = ContextVar("_permission_mode", default="default")

_MAIN_INBOX_AGENT_ID = "main"
_AWAITING_USER_SENTINEL = "[[cyrene.awaiting_user]]"

_REPORT_REF_PREFIX = "[Deep research report]"
_REPORT_REF_MAX_PREVIEW = 280

# ---------------------------------------------------------------------------
# Light tool defs — Phase 1 decision toolset
# ---------------------------------------------------------------------------

_LIGHT_TOOL_DEFS = [
    {"type": "function", "function": {"name": "use_tools", "description": "MANDATORY gateway to full tool access. Call this for ANY request that involves doing things — file ops, search, web, code, shell, scheduling, sub-agents, data, browser automation, notifications, etc. This is the ONLY way to reach real tools. Skip ONLY for pure conversation (opinions, greetings, conceptual explanations). IMPORTANT: set task to the user's EXACT original message, do not rewrite it.", "parameters": {"type": "object", "properties": {"task": {"type": "string"}}, "required": ["task"]}}},
    {"type": "function", "function": {"name": "ask_user", "description": "Ask the user a clarification question. Use this proactively whenever: the request is ambiguous, a critical detail is missing, multiple approaches exist and the choice matters, or you need confirmation before a destructive/irreversible action. Guessing is worse than asking. If you need to ask the user anything, use this tool instead of writing a question in assistant text. Use freeform text, or add a short options array when structured choices help. Do not combine with other tools in the same turn.", "parameters": {"type": "object", "properties": {"text": {"type": "string"}, "options": {"type": "array", "items": {"type": "string"}}}, "required": ["text"]}}},
    {"type": "function", "function": {"name": "quit", "description": "Call this when the interaction is done.", "parameters": {"type": "object", "properties": {}}}},
]

_DEEP_RESEARCH_LIGHT_TOOL_DEFS = [
    {"type": "function", "function": {"name": "ask_user", "description": "Ask the user a clarification question. Use this to ask about the desired report length before starting research. Use freeform text, or add a short options array when structured choices help. Do not combine with other tools in the same turn.", "parameters": {"type": "object", "properties": {"text": {"type": "string"}, "options": {"type": "array", "items": {"type": "string"}}}, "required": ["text"]}}},
    {"type": "function", "function": {"name": "quit", "description": "Call this if the user does not want deep research.", "parameters": {"type": "object", "properties": {}}}},
]


# ---------------------------------------------------------------------------
# Session epoch (survives server restarts)
# ---------------------------------------------------------------------------

def _init_session_epoch() -> None:
    global _session_epoch
    try:
        if STATE_FILE.exists():
            data = json.loads(STATE_FILE.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                epoch = data.get("_session_epoch", 0)
                _session_epoch = epoch
                _ensure_session("").session_epoch = epoch
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Runtime event helpers
# ---------------------------------------------------------------------------

async def _publish_runtime_event(event: dict[str, Any]) -> None:
    round_id = _current_round_id.get()
    if round_id and not str(event.get("round_id", "")).strip():
        event = {**event, "round_id": round_id}
    session_id = _current_session_id.get()
    if session_id:
        event = {**event, "session_id": session_id}
    await debug.publish_event(event)


async def _emit_reply_stream_event(event: dict[str, Any]) -> None:
    writer = _reply_stream_writer.get()
    if writer is None:
        return
    await writer(dict(event))


def _streaming_reply_requested() -> bool:
    return _reply_stream_writer.get() is not None


# ---------------------------------------------------------------------------
# LLM call wrappers
# ---------------------------------------------------------------------------

def _llm_phase_name(tools: list | None) -> str:
    return "phase1" if tools is _LIGHT_TOOL_DEFS else ("phase2" if tools else "no_tools")


async def _call_llm(
    messages: list[dict],
    tools: list | None = None,
    max_tokens: int | None = 32000,
    *,
    secondary: bool = False,
    thinking: str = "auto",
) -> dict:
    from cyrene.call_llm import call_llm as _unified_call_llm

    return await _unified_call_llm(
        messages,
        tools=tools,
        max_tokens=max_tokens,
        model_type="secondary" if secondary else "primary",
        thinking=thinking,
        caller=_caller_type.get(),
        phase=_llm_phase_name(tools),
        round_id=_current_round_id.get(),
    )


async def _call_llm_stream(messages: list[dict], max_tokens: int | None = 32000, *, secondary: bool = False) -> dict[str, Any]:
    from cyrene.call_llm import call_llm as _unified_call_llm

    return await _unified_call_llm(
        messages,
        max_tokens=max_tokens,
        model_type="secondary" if secondary else "primary",
        stream=True,
        stream_callback=_reply_stream_writer.get(),
        caller=_caller_type.get(),
        phase=_llm_phase_name(None),
        round_id=_current_round_id.get(),
    )


# ---------------------------------------------------------------------------
# Quit tool handler (registered in __init__.py to avoid circular imports)
# ---------------------------------------------------------------------------

async def _tool_quit(args: dict[str, Any], _bot: Any, _chat_id: int, _db_path: str, _notify_state: dict[str, bool] | None) -> str:
    return "Interaction ended."
