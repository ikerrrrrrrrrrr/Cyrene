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
"""

import asyncio
import json
import logging
import random
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from croniter import croniter

from cyrene import db
from cyrene.agent import run_steward_agent, run_task_agent
from cyrene.config import BASE_DIR, DATA_DIR, OWNER_ID, SCHEDULER_INTERVAL, STEWARD_INTERVAL
from cyrene.conversations import CONVERSATIONS_DIR, get_recent_conversations
from cyrene.soul import apply_soul_update, read_soul

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

# Big-heartbeat cadence: perform proactive checks every ~30 minutes.
# Converted to the number of SCHEDULER_INTERVAL ticks.
_BIG_HEARTBEAT_INTERVAL = max(1, 1800 // SCHEDULER_INTERVAL)

# Steward cadence: run steward agent every STEWARD_INTERVAL seconds.
_STEWARD_TICK_INTERVAL = max(1, STEWARD_INTERVAL // SCHEDULER_INTERVAL)

_heartbeat_tick: int = 0
_steward_tick: int = 0


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
    logger.info(
        "Executing task %s for chat %s: %s",
        task_id, task_chat_id, prompt[:80],
    )

    wrapped_prompt = (
        "You are executing a scheduled task. "
        "You MUST use the send_message tool to notify the user in Telegram. "
        f"Task: {prompt}"
    )
    notify_state: dict[str, bool] = {"sent": False}

    start = time.monotonic()
    try:
        result = await run_task_agent(
            wrapped_prompt, bot, task_chat_id, db_path, notify_state,
        )

        # Fallback: if the model forgot to call send_message, send a plain
        # reminder so the task doesn't go completely silent.
        if not notify_state["sent"]:
            await bot.send_message(
                chat_id=task_chat_id,
                text=f"Reminder: {prompt}",
            )

        duration_ms = int((time.monotonic() - start) * 1000)
        await db.log_task_run(
            db_path, task_id, duration_ms, "success", result=result,
        )
    except Exception as e:
        duration_ms = int((time.monotonic() - start) * 1000)
        await db.log_task_run(
            db_path, task_id, duration_ms, "error", error=str(e),
        )
        result = f"Error: {e}"

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


# ---------------------------------------------------------------------------
# Proactive heartbeat  (lottery-driven)
# ---------------------------------------------------------------------------

async def _heartbeat_proactive_check(bot, db_path: str) -> None:
    """Attempt to send a proactive message to the user.

    The decision is based on a lottery draw that only happens during daytime
    (06:00-22:00 local time).  If the draw succeeds, a short prompt is sent
    through ``run_task_agent`` to generate a casual 1-2 sentence message.
    """
    if OWNER_ID is None:
        logger.debug("OWNER_ID not configured, skipping proactive check")
        return

    try:
        _load_lottery_state()

        if not _is_daytime():
            logger.debug("Nighttime, skipping proactive check")
            return

        if _lottery_draw():
            _save_lottery_state()
            logger.info("Lottery won -- sending proactive message")

            prompt = (
                "You are Cyrene, a proactive AI assistant. "
                "Send a brief, casual message to the user "
                "-- just 1-2 sentences. Do not use any tools."
            )
            try:
                response = await asyncio.wait_for(
                    run_task_agent(prompt, bot, OWNER_ID, db_path),
                    timeout=30.0,
                )
                logger.info("Proactive message sent: %s", response[:100])
            except asyncio.TimeoutError:
                logger.warning("Proactive message generation timed out")
        else:
            _save_lottery_state()
            logger.debug(
                "Lottery draw failed, probability now %.2f",
                _LOTTERY_STATE["probability"],
            )
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
        if result_stripped.upper().startswith("SKIP"):
            logger.info("Steward returned SKIP -- no changes to SOUL.md")
        elif result_stripped:
            changes = apply_soul_update(result)
            logger.info(
                "Steward applied %d change(s) to SOUL.md", len(changes),
            )
        else:
            logger.info("Steward returned empty result, no changes applied")

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
    global _heartbeat_tick, _steward_tick

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
        "(~%d min)",
        SCHEDULER_INTERVAL,
        _BIG_HEARTBEAT_INTERVAL,
        big_minutes,
    )
    return _scheduler
