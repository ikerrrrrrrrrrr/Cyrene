"""Agent Inbox System -- file-system-level agent-to-agent communication.

Each agent has an inbox directory:

    data/inbox/{agent_name}/
        msg_001.json   -- message file
        msg_002.json   -- message file
        .unread        -- counter file (integer)

Message format::

    {
        "message_id": "msg_001",
        "from": "agent_a",
        "to": "agent_b",
        "type": "message" | "task_result" | "question" | "guidance",
        "content": "...",
        "timestamp": "2026-05-11T12:00:00"
    }
"""

import asyncio
import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

from cyrene.config import DATA_DIR

logger = logging.getLogger(__name__)

INBOX_DIR = DATA_DIR / "inbox"
_INBOX_LOCK = asyncio.Lock()
_MAX_CONTEXT_MESSAGE_CHARS = 4000
_MAX_CONTEXT_TOTAL_CHARS = 12000


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _inbox_path(agent_name: str, session_id: str = "") -> Path:
    if session_id:
        return INBOX_DIR / session_id / agent_name
    return INBOX_DIR / agent_name


def _unread_path(agent_name: str, session_id: str = "") -> Path:
    return _inbox_path(agent_name, session_id) / ".unread"


def _next_msg_id(agent_name: str, session_id: str = "") -> str:
    """Generate the next monotonically increasing message ID (msg_001, ...)."""
    inbox = _inbox_path(agent_name, session_id)
    existing = sorted(inbox.glob("msg_*.json"))
    if not existing:
        return "msg_001"
    nums: list[int] = []
    for p in existing:
        try:
            nums.append(int(p.stem.split("_")[1]))
        except (IndexError, ValueError):
            continue
    next_num = max(nums) + 1 if nums else 1
    return f"msg_{next_num:03d}"


def _read_unread(agent_name: str, session_id: str = "") -> int:
    """Read the current unread counter."""
    path = _unread_path(agent_name, session_id)
    try:
        if path.exists():
            return int(path.read_text(encoding="utf-8").strip())
    except Exception:
        logger.exception("Failed to read unread count for %s", agent_name)
    return 0


def _write_unread(agent_name: str, count: int, session_id: str = "") -> None:
    """Write the unread counter."""
    try:
        _unread_path(agent_name, session_id).write_text(str(count), encoding="utf-8")
    except Exception:
        logger.exception("Failed to write unread count for %s", agent_name)


def _iter_message_files(agent_name: str, session_id: str = "") -> Iterable[Path]:
    return sorted(_inbox_path(agent_name, session_id).glob("msg_*.json"))


def _load_messages_from_files(msg_files: Iterable[Path]) -> list[dict]:
    messages: list[dict] = []
    for msg_file in msg_files:
        try:
            data = json.loads(msg_file.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                messages.append(data)
        except Exception:
            logger.exception("Failed to read inbox message %s", msg_file.name)
    return messages


def _read_unread_messages(agent_name: str, session_id: str = "") -> list[dict]:
    unread_count = max(0, _read_unread(agent_name, session_id))
    if unread_count == 0:
        return []
    msg_files = list(_iter_message_files(agent_name, session_id))
    if not msg_files:
        return []
    return _load_messages_from_files(msg_files[-unread_count:])


def _truncate_for_context(text: str, limit: int = _MAX_CONTEXT_MESSAGE_CHARS) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + "\n...[truncated]..."


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def ensure_inbox(agent_name: str, session_id: str = "") -> Path:
    """Make sure the inbox directory for *agent_name* exists."""
    path = _inbox_path(agent_name, session_id)
    path.mkdir(parents=True, exist_ok=True)
    return path


async def send_message(
    from_agent: str,
    to_agent: str,
    msg_type: str,
    content: str,
    round_id: str = "",
    priority: str = "normal",
    in_reply_to: str = "",
    session_id: str = "",
) -> str:
    """Send a message to *to_agent*'s inbox.

    *msg_type* should be one of ``"message"``, ``"task_result"``,
    ``"question"``, ``"progress"``, ``"finding"``, or ``"ack"``.

    *priority* can be ``"normal"`` or ``"high"``.

    *in_reply_to* is the message_id of the message being replied to (for threading).

    Returns the generated ``message_id``.
    """
    try:
        async with _INBOX_LOCK:
            ensure_inbox(to_agent, session_id)
            msg_id = _next_msg_id(to_agent, session_id)
            # Auto-generate a one-line summary for display in flow diagrams
            summary = content[:120].replace("\n", " ").strip()
            if len(content) > 120:
                summary += "..."
            message = {
                "message_id": msg_id,
                "from": from_agent,
                "to": to_agent,
                "type": msg_type,
                "content": content,
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "summary": summary,
                "priority": priority,
            }
            if round_id:
                message["round_id"] = round_id
            if in_reply_to:
                message["in_reply_to"] = in_reply_to
            msg_path = _inbox_path(to_agent, session_id) / f"{msg_id}.json"
            msg_path.write_text(
                json.dumps(message, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            current = _read_unread(to_agent, session_id)
            _write_unread(to_agent, current + 1, session_id)
        logger.info(
            "Message %s sent from %s to %s (type=%s priority=%s)",
            msg_id, from_agent, to_agent, msg_type, priority,
        )
    except Exception:
        logger.exception(
            "Failed to send message from %s to %s", from_agent, to_agent,
        )
        msg_id = ""
    return msg_id


def get_unread_count(agent_name: str, session_id: str = "") -> int:
    """Return the number of unread messages for *agent_name*."""
    return _read_unread(agent_name, session_id)


async def mark_all_read(agent_name: str, session_id: str = "") -> None:
    """Reset the unread counter to zero without touching message files.

    Messages on disk are kept as a permanent log; only the unread counter
    is reset so subsequent inbox injections show 0 new messages.
    """
    async with _INBOX_LOCK:
        _write_unread(agent_name, 0, session_id)


async def mark_read_count(agent_name: str, count: int = 1, session_id: str = "") -> None:
    """Acknowledge the oldest unread inbox messages for *agent_name*.

    Unread messages are always interpreted as the oldest entries inside the
    trailing unread tail. Decrementing the unread counter therefore advances
    the read cursor without mutating message log files on disk.
    """
    if count <= 0:
        return
    async with _INBOX_LOCK:
        current = _read_unread(agent_name, session_id)
        _write_unread(agent_name, max(0, current - count), session_id)


async def clear_inbox(agent_name: str, session_id: str = "") -> None:
    """Delete all message files and reset unread state for one inbox."""
    async with _INBOX_LOCK:
        ensure_inbox(agent_name, session_id)
        for msg_file in _iter_message_files(agent_name, session_id):
            try:
                msg_file.unlink()
            except FileNotFoundError:
                continue
        unread_path = _unread_path(agent_name, session_id)
        if unread_path.exists():
            try:
                unread_path.unlink()
            except FileNotFoundError:
                pass


async def clear_all_inboxes(session_id: str = "") -> None:
    """Delete every inbox directory and unread counter under the inbox root.

    When *session_id* is provided, only that session's inbox tree is cleared.
    Otherwise the entire ``inbox/`` directory tree is wiped.
    """
    async with _INBOX_LOCK:
        if session_id:
            session_dir = INBOX_DIR / session_id
            if session_dir.exists():
                for path in session_dir.glob("*"):
                    try:
                        path.unlink()
                    except FileNotFoundError:
                        continue
                try:
                    session_dir.rmdir()
                except FileNotFoundError:
                    pass
            return
        # Clear all inboxes (legacy behavior)
        if INBOX_DIR.exists():
            for inbox_dir in sorted(path for path in INBOX_DIR.iterdir() if path.is_dir()):
                for path in inbox_dir.glob("*"):
                    try:
                        path.unlink()
                    except FileNotFoundError:
                        continue
                try:
                    inbox_dir.rmdir()
                except FileNotFoundError:
                    continue
        INBOX_DIR.mkdir(parents=True, exist_ok=True)


async def read_messages(agent_name: str, mark_read: bool = True, session_id: str = "") -> list[dict]:
    """Read all messages currently in *agent_name*'s inbox.

    When *mark_read* is ``True`` (the default) the unread counter is reset
    to zero after reading.
    """
    async with _INBOX_LOCK:
        ensure_inbox(agent_name, session_id)
        try:
            messages = _load_messages_from_files(_iter_message_files(agent_name, session_id))
        except Exception:
            logger.exception("Failed to list inbox for %s", agent_name)
            messages = []

        if mark_read:
            _write_unread(agent_name, 0, session_id)

    return messages


async def read_unread_messages(agent_name: str, session_id: str = "") -> list[dict]:
    """Read unread messages in FIFO order without acknowledging them."""
    async with _INBOX_LOCK:
        ensure_inbox(agent_name, session_id)
        return _read_unread_messages(agent_name, session_id)


def get_unread_messages(agent_name: str, session_id: str = "") -> list[dict]:
    """Return unread messages in FIFO order without mutating inbox state."""
    try:
        ensure_inbox(agent_name, session_id)
        return _read_unread_messages(agent_name, session_id)
    except Exception:
        logger.exception("Failed to read unread inbox messages for %s", agent_name)
        return []


def get_inbox_context(agent_name: str, session_id: str = "") -> str:
    """Return a formatted summary of unread messages suitable for injecting
    into an agent's system prompt.

    Returns an empty string when there are no unread messages.
    """
    unread_messages = get_unread_messages(agent_name)
    count = len(unread_messages)
    if count == 0:
        return ""

    summaries: list[str] = []
    total_chars = 0
    try:
        for data in unread_messages:
            from_ = data.get("from", "unknown")
            typ = data.get("type", "message")
            content = _truncate_for_context(str(data.get("content", "")))
            rendered = f"[from {from_}] ({typ}) {content}"
            if total_chars + len(rendered) > _MAX_CONTEXT_TOTAL_CHARS:
                remaining = _MAX_CONTEXT_TOTAL_CHARS - total_chars
                if remaining <= 0:
                    summaries.append("[older unread content omitted]")
                    break
                rendered = _truncate_for_context(rendered, remaining)
                summaries.append(rendered)
                summaries.append("[older unread content omitted]")
                break
            summaries.append(rendered)
            total_chars += len(rendered)
    except Exception:
        pass

    header = (
        f"You have {count} unread message{'s' if count > 1 else ''}:\n"
    )
    return header + "\n".join(summaries)


# ---------------------------------------------------------------------------
# Multi-agent communication helpers
# ---------------------------------------------------------------------------

async def spawn_agent(parent_name: str, task: str, session_id: str = "") -> str:
    """Spawn a child agent to execute a task, and send it the task message.

    1. Generates a unique child agent name (``agent_<timestamp>``).
    2. Creates the child's inbox directory.
    3. Sends the task message to the child's inbox.

    Args:
        parent_name: Name of the spawning agent.
        task: Description of the task for the child agent.

    Returns:
        The generated child agent name.
    """
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S_%f")
    agent_name = f"agent_{timestamp}"
    try:
        ensure_inbox(agent_name, session_id)
        await send_task(agent_name, task, parent_name, session_id=session_id)
        logger.info(
            "Spawned agent '%s' from '%s' with task: %s",
            agent_name, parent_name, task[:120],
        )
    except Exception:
        logger.exception(
            "Failed to spawn agent from '%s'", parent_name,
        )
    return agent_name


async def send_task(agent_name: str, task: str, parent_name: str = "system", session_id: str = "") -> str:
    """Send a task message to *agent_name*.

    Convenience wrapper around :func:`send_message` with ``msg_type="task"``.

    Returns the generated ``message_id``.
    """
    return await send_message(parent_name, agent_name, "task", task, session_id=session_id)


async def send_result(agent_name: str, result: str, parent_name: str = "system", session_id: str = "") -> str:
    """Send a result message to *agent_name*.

    Convenience wrapper around :func:`send_message` with ``msg_type="result"``.

    Returns the generated ``message_id``.
    """
    return await send_message(parent_name, agent_name, "result", result, session_id=session_id)


async def check_completed_tasks(agent_name: str, session_id: str = "") -> list[dict]:
    """Check for completed tasks from child agents.

    Reads and marks as read all messages in *agent_name*'s inbox, then
    filters for messages of type ``"result"``.

    Returns:
        A list of result message dicts, each containing at minimum
        ``from``, ``content``, and ``timestamp`` keys.
    """
    messages = await read_messages(agent_name, mark_read=True, session_id=session_id)
    return [m for m in messages if m.get("type") == "result"]


async def get_pending_tasks(agent_name: str, session_id: str = "") -> list[dict]:
    """Get pending task messages for *agent_name* (does NOT mark as read).

    Use this when an agent starts up to discover what it has been asked to do.

    Returns:
        A list of task message dicts of type ``"task"``.
    """
    messages = await read_messages(agent_name, mark_read=False, session_id=session_id)
    return [m for m in messages if m.get("type") == "task"]
