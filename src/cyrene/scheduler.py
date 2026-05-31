"""Scheduler, heartbeat, and proactive-messaging lottery system.

Responsibilities
----------------
1. **Scheduled tasks** -- Check the SQLite database for due tasks and execute
   them (inherited from the original scheduler).
2. **Heartbeat** -- A lightweight periodic tick that triggers the checks above
   and, on a coarser cadence (every ~30 min), also runs the proactive lottery.
3. **Lottery** -- A probability-driven mechanism that occasionally prompts the
   assistant to send an unsolicited message to the user.  State is persisted
   to ``data/lottery_state.json`` so that it survives restarts.
4. **Smart proactive context** -- When the lottery triggers, the agent now
   receives short-term memory, recent conversation context, and relationship
   state from SOUL.md so the proactive message can reference real events
   instead of sending generic greetings.
"""

import asyncio
import json
import logging
import os
import random
import re as _re
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from croniter import croniter

from cyrene import db
from cyrene.agent import append_system_message, run_heartbeat_agent, run_steward_agent, run_task_agent
from cyrene.channels.wechat import get_current_client
from cyrene.config import BASE_DIR, DATA_DIR, OWNER_ID, SCHEDULER_INTERVAL, STATE_FILE, STEWARD_INTERVAL
from cyrene.conversations import CONVERSATIONS_DIR, get_recent_conversations
from cyrene.notifications import notify
from cyrene.short_term import clear_old_entries, get_context as get_short_term_context
from cyrene.soul import apply_soul_update, read_shallow_memory, read_soul

logger = logging.getLogger(__name__)

_scheduler: AsyncIOScheduler | None = None

# ---------------------------------------------------------------------------
# Lottery state  (persisted to disk)
# ---------------------------------------------------------------------------

_LOTTERY_STATE: dict[str, float] = {
    "probability": 0.0,       # current draw probability 0.0 .. 1.0
    "delta": 0.15,            # increment on each failed draw
    "max_probability": 0.85,  # ceiling for the accumulated probability
}
_LOTTERY_FILE = BASE_DIR / "data" / "lottery_state.json"

# ---------------------------------------------------------------------------
# Steward state  (persisted to disk)
# ---------------------------------------------------------------------------

_STEWARD_STATE_FILE = DATA_DIR / "steward_state.json"

# Big-heartbeat cadence: perform proactive checks.
# Read from web_settings.json (default 1800s = 30 min), converted to ticks.
_HEARTBEAT_INTERVAL_SECONDS: int = 0  # lazy-loaded on first use


def _get_heartbeat_interval() -> int:
    global _HEARTBEAT_INTERVAL_SECONDS
    if not _HEARTBEAT_INTERVAL_SECONDS:
        try:
            from cyrene.settings_store import get
            _HEARTBEAT_INTERVAL_SECONDS = int(get("heartbeat_interval", 1800) or 1800)
        except Exception:
            _HEARTBEAT_INTERVAL_SECONDS = 1800
    return _HEARTBEAT_INTERVAL_SECONDS


_BIG_HEARTBEAT_INTERVAL: int = 0  # set during setup_scheduler

# Steward cadence: run steward agent every STEWARD_INTERVAL seconds.
_STEWARD_TICK_INTERVAL = max(1, STEWARD_INTERVAL // SCHEDULER_INTERVAL)

_heartbeat_tick: int = 0
_steward_tick: int = 0
_cleanup_tick: int = 0
_CLEANUP_TICK_INTERVAL = max(1, 86400 // SCHEDULER_INTERVAL)  # once a day


def _load_lottery_state() -> None:
    """Restore lottery state from ``_LOTTERY_FILE``."""
    global _LOTTERY_STATE
    try:
        if _LOTTERY_FILE.exists():
            data = json.loads(_LOTTERY_FILE.read_text(encoding="utf-8"))
            _LOTTERY_STATE["probability"] = float(data.get("probability", 0.0))
            _LOTTERY_STATE["delta"] = float(data.get("delta", 0.15))
            _LOTTERY_STATE["max_probability"] = float(
                data.get("max_probability", 0.85)
            )
            logger.debug(
                "Loaded lottery state: probability=%.2f",
                _LOTTERY_STATE["probability"],
            )
    except Exception:
        logger.exception("Failed to load lottery state, using defaults")


def _save_lottery_state() -> None:
    """Persist current lottery state to ``_LOTTERY_FILE``."""
    try:
        _LOTTERY_FILE.parent.mkdir(parents=True, exist_ok=True)
        _LOTTERY_FILE.write_text(
            json.dumps(_LOTTERY_STATE, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    except Exception:
        logger.exception("Failed to save lottery state")


def reset_lottery() -> None:
    """Reset the lottery probability to zero.

    Called when the user sends a message -- user initiative resets the
    proactive impulse.
    """
    _LOTTERY_STATE["probability"] = 0.0
    _save_lottery_state()
    logger.debug("Lottery probability reset by user activity")


def _is_daytime() -> bool:
    """``True`` between 06:00 and 22:00 in local time."""
    hour = datetime.now().hour
    return 6 <= hour < 22


def _lottery_draw() -> bool:
    """Perform a probabilistic draw.

    * On **win** (random value < current probability): probability is reset
      to zero and ``True`` is returned.
    * On **loss**: probability is increased by *delta* (capped at
      *max_probability*) and ``False`` is returned.
    """
    prob = _LOTTERY_STATE["probability"]
    if random.random() < prob:
        _LOTTERY_STATE["probability"] = 0.0
        return True
    _LOTTERY_STATE["probability"] = min(
        _LOTTERY_STATE["probability"] + _LOTTERY_STATE["delta"],
        _LOTTERY_STATE["max_probability"],
    )
    return False


# ---------------------------------------------------------------------------
# Silence detection — infer how long since the user last spoke
# ---------------------------------------------------------------------------


def _last_user_message_time() -> datetime | None:
    """Infer the timestamp of the user's most recent message.

    Tries ``state.json`` first (using the file modification time as a proxy),
    then falls back to scanning today's conversation archive for the last
    ``## HH:MM:SS UTC`` heading that precedes a ``**User**:`` entry.

    Returns ``None`` when no user message can be found.
    """
    # 1. state.json: use file mtime as a rough proxy (messages carry no
    #    per-message timestamp field).
    try:
        if STATE_FILE.exists():
            mtime = STATE_FILE.stat().st_mtime
            data = json.loads(STATE_FILE.read_text(encoding="utf-8"))
            messages = data.get("messages", []) if isinstance(data, dict) else []
            # Only trust mtime when there actually IS a user message
            for msg in reversed(messages):
                if msg.get("role") == "user" and str(msg.get("content", "")).strip():
                    return datetime.fromtimestamp(mtime, tz=timezone.utc)
    except Exception:
        logger.debug("Could not read state.json for silence detection", exc_info=True)

    # 2. Fallback: scan conversation archives for the most recent
    #    ``**User**:`` entry with an explicit timestamp heading.
    try:
        if CONVERSATIONS_DIR.exists():
            files = sorted(CONVERSATIONS_DIR.glob("*.md"), reverse=True)
            for filepath in files:
                content = filepath.read_text(encoding="utf-8")
                # Each exchange starts with "## HH:MM:SS UTC", then optional
                # metadata comments, then "**User**: ..." — match lazily.
                matches = _re.findall(
                    r"## (\d{2}:\d{2}:\d{2} UTC)\n.*?\*\*User\*\*:",
                    content,
                    _re.DOTALL,
                )
                if matches:
                    latest_ts = matches[-1]
                    date_str = filepath.stem  # YYYY-MM-DD
                    clean_ts = latest_ts.replace(" UTC", "")
                    dt_str = f"{date_str} {clean_ts}"
                    try:
                        return datetime.strptime(dt_str, "%Y-%m-%d %H:%M:%S").replace(
                            tzinfo=timezone.utc,
                        )
                    except ValueError:
                        logger.debug(
                            "Unparseable timestamp in %s: %s",
                            filepath.name,
                            latest_ts,
                            exc_info=True,
                        )
                        continue
    except Exception:
        logger.debug(
            "Could not scan conversation archives for silence detection",
            exc_info=True,
        )

    return None


def _silence_hours() -> float | None:
    """Return hours since the user's last message, or *None* if unknown."""
    last = _last_user_message_time()
    if last is None:
        return None
    delta = datetime.now(timezone.utc) - last.astimezone(timezone.utc)
    return delta.total_seconds() / 3600


# ---------------------------------------------------------------------------
# Proactive context assembly — memory + conversations + personality
# ---------------------------------------------------------------------------


async def _assemble_proactive_context(db_path: str = "") -> str:
    """Gather memory, conversation, and personality context for a proactive
    message so the agent can reference real events.

    Returns a Markdown string assembled from three sources:

    * SOUL.md — RELATIONSHIP:USER and PATTERN:USER sections.
    * Short-term memory — recent facts, preferences, emotional patterns.
    * Today's conversation archive — what the user just talked about.

    Every source is best-effort; failures are logged and skipped.
    """
    parts: list[str] = []

    # 1. SOUL.md shallow memory — relationship + observed patterns
    try:
        soul = read_shallow_memory()
        if soul:
            relevant_lines: list[str] = []
            capture = False
            for line in soul.splitlines():
                if line.startswith("## RELATIONSHIP:USER") or line.startswith(
                    "## PATTERN:USER",
                ):
                    capture = True
                    relevant_lines.append(line)
                elif line.startswith("## ") and capture:
                    capture = False
                elif capture:
                    relevant_lines.append(line)
            if relevant_lines:
                parts.append(
                    "## Your relationship with the user\n"
                    + "\n".join(relevant_lines),
                )
    except Exception:
        logger.debug(
            "Could not read SOUL.md for proactive context",
            exc_info=True,
        )

    # 2. Short-term memory — compressed facts / preferences / emotions
    try:
        st = get_short_term_context(
            max_chars=1500,
            header="## Recent memories about the user",
        )
        if st and st != "## Recent memories about the user":
            parts.append(st)
    except Exception:
        logger.debug(
            "Could not read short-term memory for proactive context",
            exc_info=True,
        )

    # 3. Today's conversation — what the user just talked about
    try:
        conversations = await get_recent_conversations(days=1)
        if conversations:
            if len(conversations) > 3000:
                # Keep the tail (most recent exchanges)
                conversations = conversations[-3000:]
                # Splice back onto a section boundary so we don't start
                # mid-exchange.
                boundary = conversations.find("\n=== ")
                if boundary > 100:
                    conversations = conversations[boundary + 1:]
            parts.append("## Recent conversation\n" + conversations)
    except Exception:
        logger.debug(
            "Could not read conversations for proactive context",
            exc_info=True,
        )

    # 4. Active entities — due soon, stale, open decisions
    if db_path:
        try:
            from datetime import timedelta
            from cyrene.entities import list_entities, query_entities

            now_dt = datetime.now(timezone.utc)
            due_cutoff = (now_dt + timedelta(hours=24)).isoformat()
            stale_cutoff = (now_dt - timedelta(days=7)).isoformat()

            due_soon = await query_entities(db_path, due_before=due_cutoff, status="active")
            all_active = await list_entities(db_path, status="active", limit=200)
            stale = [e for e in all_active if e.get("last_referenced_at", "") < stale_cutoff]
            open_dec = [
                e for e in all_active
                if e["type"] == "decision" and not (
                    e["metadata"].get("outcome") if isinstance(e["metadata"], dict) else False
                )
            ]

            entity_lines: list[str] = []
            if due_soon:
                titles = "、".join(e["title"] for e in due_soon[:3])
                entity_lines.append(f"- 即将到期（24h内）：{titles}")
            if stale:
                entity_lines.append(f"- 长时间未提及：{stale[0]['title']}")
            if open_dec:
                entity_lines.append(f"- 待跟进的决策：{open_dec[0]['title']}")

            if entity_lines:
                parts.append("## 需要关注的事务\n" + "\n".join(entity_lines))
        except Exception:
            logger.debug("Could not load entity context for proactive message", exc_info=True)

    return "\n\n".join(parts).strip()


def _build_proactive_system_prompt() -> str:
    """System prompt for the proactive message generation LLM call."""
    return (
        "You are Cyrene, a personal AI companion. "
        "Generate a brief proactive check-in message (1–3 sentences) "
        "based on the provided context. "
        "Reference specific recent events or memories when available. "
        "Match the communication style described in the relationship section. "
        "Write in the user's language (Chinese if they write in Chinese, "
        "English if in English). "
        "Return ONLY the message text — no explanations, no prefixes, no quotes."
    )


def _build_proactive_user_prompt(context: str, silence_hours: float | None) -> str:
    """Build the user prompt with memory context and current situation."""
    now = datetime.now().strftime("%H:%M")
    today = datetime.now().strftime("%Y-%m-%d")

    silence_note = ""
    if silence_hours is not None:
        if silence_hours < 2:
            silence_note = ""
        elif silence_hours < 12:
            silence_note = (
                "The user has been away for a few hours. "
                "A warm reconnection is appropriate."
            )
        elif silence_hours < 48:
            silence_note = (
                "The user hasn't checked in for a while. "
                "Show you notice their absence with warmth, not pressure."
            )
        else:
            silence_note = (
                "The user has been away for quite some time. "
                "Be gentle — show you care, but don't overwhelm. "
                "Keep it short."
            )

    silence_line = (
        f"Hours since user's last message: {silence_hours:.0f}"
        if silence_hours is not None
        else "Unable to determine when the user last messaged"
    )

    return f"""## Memory context
{context if context else "No recent context available."}

## Guidelines
- Reference something SPECIFIC from the memory context above — a recent topic, a plan the user mentioned, a concern they shared.
- If the user mentioned plans, events, or concerns recently — follow up on them naturally.
- If the user's recent emotional patterns suggest stress or tiredness, be warm and supportive.
- If there are open topics from the recent conversation, follow up.
- If there's truly nothing specific to reference, do a gentle check-in — but avoid generic "how are you".
{silence_note}

## Current situation
- Date: {today}
- Current time: {now}
- {silence_line}"""


# ---------------------------------------------------------------------------
# Scheduled-task execution  (preserved from the original scheduler)
# ---------------------------------------------------------------------------

async def _check_and_execute_tasks(bot, db_path: str) -> None:
    """Query all due tasks from the database and execute each one."""
    try:
        tasks = await db.get_due_tasks(db_path)
    except Exception:
        logger.exception("Failed to query due tasks")
        return

    for task in tasks:
        try:
            await _execute_task(task, bot, db_path)
        except Exception:
            logger.exception("Failed to execute task %s", task["id"])


async def _execute_task(task: dict, bot, db_path: str) -> None:
    """Run a single scheduled task and update its next-run time."""
    task_id = task["id"]
    task_chat_id = task["chat_id"]
    prompt = task["prompt"]
    permission_mode = str(task.get("permission_mode") or "workspace_only").strip().lower()
    logger.info(
        "Executing task %s for chat %s (permission: %s): %s",
        task_id, task_chat_id, permission_mode, prompt[:80],
    )

    # Apply stored permission_mode: temporarily elevate write permissions via ContextVar
    from cyrene.agent.state import _temporary_full_access as _tmp_wpm
    if permission_mode == "full_access":
        _tmp_wpm.set(True)
        logger.info("Temporarily elevated write permissions to full_access for task %s", task_id)

    wrapped_prompt = (
        "You are executing a scheduled task. "
        "You MUST use the send_message tool to notify the user in Telegram. "
        f"Task: {prompt}"
    )
    notify_state: dict[str, bool] = {"sent": False}

    start = time.monotonic()
    had_error = False
    try:
        result = await run_task_agent(
            wrapped_prompt, bot, task_chat_id, db_path, notify_state,
        )

        # Fallback: if the model forgot to call send_message, send a plain
        # reminder through the Web UI persistence path so the task doesn't go
        # completely silent in web-only mode.
        if not notify_state["sent"]:
            await append_system_message(
                f"Reminder: {prompt}",
                message_meta={"scheduled": True},
                publish_event={"scheduled": True},
            )

        duration_ms = int((time.monotonic() - start) * 1000)
        await db.log_task_run(
            db_path, task_id, duration_ms, "success", result=result,
        )
    except Exception as e:
        had_error = True
        duration_ms = int((time.monotonic() - start) * 1000)
        await db.log_task_run(
            db_path, task_id, duration_ms, "error", error=str(e),
        )
        result = f"Error: {e}"
    finally:
        # Restore original permission mode after task execution
        if permission_mode == "full_access":
            _tmp_wpm.set(False)
            logger.info("Restored write permissions after task %s", task_id)

    # Calculate next_run based on schedule type
    stype = task["schedule_type"]
    svalue = task["schedule_value"]
    now = datetime.now(timezone.utc)

    try:
        if stype == "cron":
            next_run = croniter(svalue, now).get_next(datetime).isoformat()
            await db.update_task_after_run(
                db_path, task_id, result, next_run, "active",
            )
        elif stype == "interval":
            next_run = (now + timedelta(milliseconds=int(svalue))).isoformat()
            await db.update_task_after_run(
                db_path, task_id, result, next_run, "active",
            )
        elif stype == "once":
            await db.update_task_after_run(
                db_path, task_id, result, None, "completed",
            )
        else:
            logger.warning(
                "Unknown schedule_type %s for task %s", stype, task_id,
            )
    except Exception:
        logger.exception(
            "Failed to update task %s after execution", task_id,
        )

    # ── Multi-channel notifications after task execution ─────────────────
    try:
        summary = prompt[:120] + ("…" if len(prompt) > 120 else "")
        status_label = "error" if had_error else "completed"

        # macOS desktop notification
        await notify(
            title=f"Scheduled task {status_label}",
            body=summary,
            channel="desktop",
        )

        # SSE event for frontend browser notifications
        await notify(
            title=f"Scheduled task {status_label}",
            body=summary,
            channel="sse",
        )

        # WeChat notification — controlled by notify_wechat setting
        await notify(
            title=f"Scheduled task {status_label}",
            body=summary,
            channel="wechat",
        )
    except Exception:
        logger.exception("Failed to send task execution notifications")


# ---------------------------------------------------------------------------
# Proactive message delivery — bot + session state + SSE event
# ---------------------------------------------------------------------------


async def _deliver_proactive_message(text: str, bot, chat_id: int) -> None:
    """Deliver a proactive message so it appears in both the bot and the Web UI.

    1. Sends the text through the bot (Telegram or WebBot).
    2. Appends an assistant entry to ``state.json`` so the message is visible
       in the Web UI chat history on the next page load.
    3. Publishes a ``chat_message`` SSE event so connected frontends update
       in real time without a refresh.

    The state.json write is best-effort — failures are logged and swallowed
    so a corrupt or missing state file never blocks proactive delivery.
    """
    # 1. Bot delivery (Telegram push or WebBot memory queue)
    if bot is not None:
        await bot.send_message(chat_id=chat_id, text=text)

    # 2. Write to session state for Web UI chat history
    try:
        from uuid import uuid4

        from cyrene import debug

        if STATE_FILE.exists():
            state = json.loads(STATE_FILE.read_text(encoding="utf-8"))
        else:
            state = {}
        if not isinstance(state, dict):
            state = {}

        messages = state.get("messages", [])
        if not isinstance(messages, list):
            messages = []

        entry: dict = {
            "role": "assistant",
            "content": text,
            "message_id": f"msg_{uuid4().hex}",
            "proactive": True,
        }
        messages.append(entry)

        # Keep within the context-window limit (same as agent.py)
        if len(messages) > 40:
            messages = messages[-40:]

        state["messages"] = messages
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        STATE_FILE.write_text(
            json.dumps(state, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

        # 3. Push SSE event so connected frontends update in real time
        await debug.publish_event({
            "type": "chat_message",
            "proactive": True,
        })
    except Exception:
        logger.exception(
            "Failed to write proactive message to session state"
        )


# ---------------------------------------------------------------------------
# Proactive heartbeat  (lottery-driven)
# ---------------------------------------------------------------------------

async def _heartbeat_proactive_check(bot, db_path: str) -> None:
    """Attempt to send a context-aware proactive message to the user.

    The decision to send is based on the lottery draw, but the trigger is
    also influenced by how long the user has been silent:

    * Normal: lottery draw with accumulating probability (delta 0.15, max 0.85).
    * Silent > 72 h: always trigger regardless of lottery state.

    When triggered, a minimal LLM call (no tools, no thinking mode, no SSE
    events from the agent loop) generates a personalised message.  The text
    is then delivered to the bot AND written to session state so it appears
    in the Web UI chat history.
    """
    # In web-only mode OWNER_ID is not set — use 0 as a placeholder chat_id.
    # The session-state delivery path does not rely on chat_id at all.
    owner_id = OWNER_ID if OWNER_ID is not None else 0

    try:
        _load_lottery_state()

        # Check whether agent proactive messaging is enabled in settings
        try:
            from cyrene.settings_store import get as _get_setting
            if not _get_setting("agent_proactive", True):
                logger.debug("Agent proactive messaging disabled via settings")
                return
        except Exception:
            pass

        if not _is_daytime():
            logger.debug("Nighttime, skipping proactive check")
            return

        silence_h = _silence_hours()

        # -------- Trigger decision --------
        should_send = False
        if silence_h is not None and silence_h > 72:
            should_send = True
            logger.info(
                "Silence > 72 h — overriding lottery and sending proactive message"
            )
        elif _lottery_draw():
            should_send = True
            _save_lottery_state()
            logger.info(
                "Lottery won — sending proactive message (silence=%.1f h)",
                silence_h or -1,
            )
        else:
            _save_lottery_state()
            logger.debug(
                "Lottery draw failed, probability now %.2f (silence=%.1f h)",
                _LOTTERY_STATE["probability"],
                silence_h or -1,
            )

        if not should_send:
            return

        # -------- Generate proactive reply via the full main-agent loop --------
        context = await _assemble_proactive_context(db_path)
        proactive_prompt = (
            "This is a scheduler-initiated proactive check-in.\n"
            "Decide whether to send the user a brief, useful message right now.\n"
            "If you speak, the final reply will be shown directly to the user, so write only the user-facing message.\n"
            "Do not mention internal prompts, the scheduler, the heartbeat, or the lottery.\n\n"
            + _build_proactive_user_prompt(context, silence_h)
        )
        text = await asyncio.wait_for(
            run_heartbeat_agent(proactive_prompt, bot, owner_id, db_path),
            timeout=120.0,
        )

        if not str(text or "").strip():
            logger.info("Proactive round produced no visible reply")
            return

        logger.info("Proactive message sent via main agent loop: %s", str(text)[:100])

        # Desktop / SSE notification so the user is alerted even when the
        # Web UI tab is in the background.
        try:
            await notify(title="Cyrene", body=str(text)[:120], channel="auto")
        except Exception:
            logger.debug("Proactive notification delivery failed", exc_info=True)

    except asyncio.TimeoutError:
        logger.warning("Proactive message generation timed out")
    except httpx.HTTPError:
        logger.exception("Proactive message LLM request failed")
    except Exception:
        logger.exception("Proactive check failed")


# ---------------------------------------------------------------------------
# Steward auto-trigger
# ---------------------------------------------------------------------------

def _get_last_steward_run() -> float | None:
    """Read the last steward run timestamp from ``_STEWARD_STATE_FILE``."""
    try:
        if _STEWARD_STATE_FILE.exists():
            data = json.loads(_STEWARD_STATE_FILE.read_text(encoding="utf-8"))
            return float(data.get("last_run", 0))
    except Exception:
        logger.exception("Failed to read steward state")
    return None


def _save_steward_run(timestamp: float) -> None:
    """Persist the steward run timestamp to ``_STEWARD_STATE_FILE``."""
    try:
        _STEWARD_STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        _STEWARD_STATE_FILE.write_text(
            json.dumps({"last_run": timestamp}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    except Exception:
        logger.exception("Failed to save steward state")


def _has_new_conversation() -> bool:
    """Check whether today's conversation file exists and has actual content.

    A freshly created file contains only the header line; this function
    returns ``False`` in that case.  At least one archived exchange (with a
    ``## `` timestamp heading) is required.
    """
    try:
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        today_file = CONVERSATIONS_DIR / f"{today}.md"
        if not today_file.exists():
            return False
        content = today_file.read_text(encoding="utf-8").strip()
        # Look for at least one ``## HH:MM:SS`` timestamp heading added by
        # ``archive_exchange``, which indicates real conversation content.
        return bool(content) and "##" in content
    except Exception:
        logger.exception("Failed to check for new conversations")
        return False


async def _run_steward_if_needed(bot, db_path: str) -> None:
    """Check conditions and run the steward agent when appropriate.

    Triggers when:
    1. At least ``STEWARD_INTERVAL`` seconds have elapsed since the last run.
    2. Today's conversation file exists and contains archived exchanges.
    """
    try:
        last_run = _get_last_steward_run()
        now = time.time()

        if last_run is not None and (now - last_run) < STEWARD_INTERVAL:
            logger.debug(
                "Steward not due yet (last run %.0f s ago)", now - last_run,
            )
            return

        if not _has_new_conversation():
            logger.debug("No new conversations today, skipping steward")
            return

        if OWNER_ID is None:
            logger.debug("OWNER_ID not configured, skipping steward")
            return

        logger.info("Steward conditions met -- running steward agent")

        conversation_text = await get_recent_conversations(days=1)
        soulmd_content = read_soul()

        if not conversation_text:
            logger.debug("No conversation text available, skipping steward")
            return

        result = await run_steward_agent(
            conversation_text, soulmd_content, bot, OWNER_ID, db_path,
        )

        result_stripped = (result or "").strip()
        if result_stripped.upper().startswith("SKIP") and "ENTITY" not in result_stripped:
            logger.info("Steward returned SKIP -- no changes to SOUL.md")
        elif result_stripped:
            changes = apply_soul_update(result)
            logger.info(
                "Steward applied %d change(s) to SOUL.md", len(changes),
            )
        else:
            logger.info("Steward returned empty result, no changes applied")

        # 解析 Steward 提取的实体（ENTITY 行）
        try:
            from cyrene.entities import add_candidate, has_similar_entity
            for line in result_stripped.splitlines():
                line = line.strip()
                if not line.upper().startswith("ENTITY "):
                    continue
                # Parse: ENTITY type="task" title="..." confidence="0.85" content="..."
                import re as _re2
                e_type = _re2.search(r'type="([^"]*)"', line)
                e_title = _re2.search(r'title="([^"]*)"', line)
                e_conf = _re2.search(r'confidence="([^"]*)"', line)
                e_content = _re2.search(r'content="([^"]*)"', line)
                if e_type and e_title and e_conf:
                    entity_type = e_type.group(1)
                    entity_title = e_title.group(1)
                    # 去重检查：同类型+相似标题的实体或候选已存在时跳过
                    if await has_similar_entity(db_path, entity_type, entity_title):
                        logger.debug("Skipping duplicate entity: %s / %s", entity_type, entity_title)
                        continue
                    candidate_id = await add_candidate(
                        db_path,
                        type=entity_type,
                        title=entity_title,
                        content=e_content.group(1) if e_content else "",
                        confidence=float(e_conf.group(1)),
                        raw_text=line,
                    )
                    logger.info("Steward extracted entity candidate %s: %s", candidate_id[:8], entity_title)
        except Exception:
            logger.exception("Failed to parse steward entity extractions")

        # 处理置信度 >= 0.8 的候选事务，自动提升为正式事务
        try:
            from cyrene.entities import process_candidates
            promoted = await process_candidates(db_path)
            if promoted:
                logger.info("Steward promoted %d candidate entity/entities", len(promoted))
        except Exception:
            logger.exception("process_candidates failed during steward run")

        _save_steward_run(now)

    except Exception:
        logger.exception("Steward auto-trigger failed")


# ---------------------------------------------------------------------------
# Main heartbeat
# ---------------------------------------------------------------------------

async def _heartbeat(bot, db_path: str) -> None:
    """Periodic heartbeat invoked by APScheduler.

    1. Check for due scheduled tasks and execute them.
    2. On every Nth tick (N such that ``N * SCHEDULER_INTERVAL ~ 30 min``)
       run the proactive lottery check.
    3. On every Mth tick (M such that ``M * SCHEDULER_INTERVAL ~ STEWARD_INTERVAL``)
       run the steward agent auto-trigger.
    """
    global _heartbeat_tick, _steward_tick, _cleanup_tick

    try:
        await _check_and_execute_tasks(bot, db_path)

        # -- Lottery proactive check --
        _heartbeat_tick += 1
        if _heartbeat_tick >= _BIG_HEARTBEAT_INTERVAL:
            _heartbeat_tick = 0
            await _heartbeat_proactive_check(bot, db_path)

        # -- Steward auto-trigger --
        _steward_tick += 1
        if _steward_tick >= _STEWARD_TICK_INTERVAL:
            _steward_tick = 0
            await _run_steward_if_needed(bot, db_path)

        # -- Pattern detection --
        from cyrene.pattern import tick as _pattern_tick
        await _pattern_tick(bot, db_path)

        # -- Short-term memory cleanup (daily) --
        _cleanup_tick += 1
        if _cleanup_tick >= _CLEANUP_TICK_INTERVAL:
            _cleanup_tick = 0
            clear_old_entries(days=7)
    except Exception:
        logger.exception("Heartbeat error")


# ---------------------------------------------------------------------------
# Public entry point  (signature preserved for bot.py compatibility)
# ---------------------------------------------------------------------------

def setup_scheduler(bot, db_path: str) -> AsyncIOScheduler:
    """Create and return an :class:`AsyncIOScheduler` with the heartbeat job.

    The signature is kept stable so that ``bot._post_init`` continues to
    work without modification.
    """
    global _scheduler
    global _BIG_HEARTBEAT_INTERVAL
    _load_lottery_state()
    hb_seconds = _get_heartbeat_interval()
    _BIG_HEARTBEAT_INTERVAL = max(1, hb_seconds // SCHEDULER_INTERVAL)
    _scheduler = AsyncIOScheduler()
    _scheduler.add_job(
        _heartbeat,
        "interval",
        seconds=SCHEDULER_INTERVAL,
        args=[bot, db_path],
        id="heartbeat",
        replace_existing=True,
    )
    big_minutes = (_BIG_HEARTBEAT_INTERVAL * SCHEDULER_INTERVAL) // 60
    logger.info(
        "Scheduler configured: interval=%ds, big_heartbeat every %d ticks "
        "(~%d min, configured=%ds)",
        SCHEDULER_INTERVAL,
        _BIG_HEARTBEAT_INTERVAL,
        big_minutes,
        hb_seconds,
    )
    return _scheduler
