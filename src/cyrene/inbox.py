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
        "type": "message" | "task_result" | "question",
        "content": "...",
        "timestamp": "2026-05-11T12:00:00"
    }
"""

import json
import logging
import threading
from datetime import datetime, timezone
from pathlib import Path

from cyrene.config import DATA_DIR

logger = logging.getLogger(__name__)

INBOX_DIR = DATA_DIR / "inbox"
_INBOX_LOCK = threading.Lock()


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _inbox_path(agent_name: str) -> Path:
    return INBOX_DIR / agent_name


def _unread_path(agent_name: str) -> Path:
    return _inbox_path(agent_name) / ".unread"


def _next_msg_id(agent_name: str) -> str:
    """Generate the next monotonically increasing message ID (msg_001, ...)."""
    inbox = _inbox_path(agent_name)
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


def _read_unread(agent_name: str) -> int:
    """Read the current unread counter."""
    path = _unread_path(agent_name)
    try:
        if path.exists():
            return int(path.read_text(encoding="utf-8").strip())
    except Exception:
        logger.exception("Failed to read unread count for %s", agent_name)
    return 0


def _write_unread(agent_name: str, count: int) -> None:
    """Write the unread counter."""
    try:
        _unread_path(agent_name).write_text(str(count), encoding="utf-8")
    except Exception:
        logger.exception("Failed to write unread count for %s", agent_name)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def ensure_inbox(agent_name: str) -> Path:
    """Make sure the inbox directory for *agent_name* exists."""
    path = _inbox_path(agent_name)
    path.mkdir(parents=True, exist_ok=True)
    return path


def send_message(
    from_agent: str,
    to_agent: str,
    msg_type: str,
    content: str,
    round_id: str = "",
) -> str:
    """Send a message to *to_agent*'s inbox.

    *msg_type* should be one of ``"message"``, ``"task_result"``, or
    ``"question"``.

    Returns the generated ``message_id``.
    """
    try:
        with _INBOX_LOCK:
            ensure_inbox(to_agent)
            msg_id = _next_msg_id(to_agent)
            message = {
                "message_id": msg_id,
                "from": from_agent,
                "to": to_agent,
                "type": msg_type,
                "content": content,
                "timestamp": datetime.now(timezone.utc).isoformat(),
            }
            if round_id:
                message["round_id"] = round_id
            msg_path = _inbox_path(to_agent) / f"{msg_id}.json"
            msg_path.write_text(
                json.dumps(message, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            current = _read_unread(to_agent)
            _write_unread(to_agent, current + 1)
        logger.info(
            "Message %s sent from %s to %s (type=%s)",
            msg_id, from_agent, to_agent, msg_type,
        )
    except Exception:
        logger.exception(
            "Failed to send message from %s to %s", from_agent, to_agent,
        )
        msg_id = ""
    return msg_id


def get_unread_count(agent_name: str) -> int:
    """Return the number of unread messages for *agent_name*."""
    return _read_unread(agent_name)


def mark_all_read(agent_name: str) -> None:
    """Reset the unread counter to zero without touching message files.

    Messages on disk are kept as a permanent log; only the unread counter
    is reset so subsequent inbox injections show 0 new messages.
    """
    _write_unread(agent_name, 0)


def read_messages(agent_name: str, mark_read: bool = True) -> list[dict]:
    """Read all messages currently in *agent_name*'s inbox.

    When *mark_read* is ``True`` (the default) the unread counter is reset
    to zero after reading.
    """
    ensure_inbox(agent_name)
    messages: list[dict] = []
    try:
        msg_files = sorted(_inbox_path(agent_name).glob("msg_*.json"))
        for f in msg_files:
            try:
                data = json.loads(f.read_text(encoding="utf-8"))
                messages.append(data)
            except Exception:
                logger.exception("Failed to read inbox message %s", f.name)
    except Exception:
        logger.exception("Failed to list inbox for %s", agent_name)

    if mark_read:
        _write_unread(agent_name, 0)

    return messages


def get_inbox_context(agent_name: str) -> str:
    """Return a formatted summary of unread messages suitable for injecting
    into an agent's system prompt.

    Returns an empty string when there are no unread messages.
    """
    count = get_unread_count(agent_name)
    if count == 0:
        return ""

    summaries: list[str] = []
    try:
        msg_files = sorted(_inbox_path(agent_name).glob("msg_*.json"))
        # Only show the latest `count` messages
        for f in msg_files[-count:]:
            try:
                data = json.loads(f.read_text(encoding="utf-8"))
                from_ = data.get("from", "unknown")
                typ = data.get("type", "message")
                content = data.get("content", "")
                summaries.append(f"[from {from_}] ({typ}) {content[:200]}")
            except Exception:
                pass
    except Exception:
        pass

    header = (
        f"You have {count} unread message{'s' if count > 1 else ''}:\n"
    )
    return header + "\n".join(summaries)


# ---------------------------------------------------------------------------
# Multi-agent communication helpers
# ---------------------------------------------------------------------------

def spawn_agent(parent_name: str, task: str) -> str:
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
        ensure_inbox(agent_name)
        send_task(agent_name, task, parent_name)
        logger.info(
            "Spawned agent '%s' from '%s' with task: %s",
            agent_name, parent_name, task[:120],
        )
    except Exception:
        logger.exception(
            "Failed to spawn agent from '%s'", parent_name,
        )
    return agent_name


def send_task(agent_name: str, task: str, parent_name: str = "system") -> str:
    """Send a task message to *agent_name*.

    Convenience wrapper around :func:`send_message` with ``msg_type="task"``.

    Returns the generated ``message_id``.
    """
    return send_message(parent_name, agent_name, "task", task)


def send_result(agent_name: str, result: str, parent_name: str = "system") -> str:
    """Send a result message to *agent_name*.

    Convenience wrapper around :func:`send_message` with ``msg_type="result"``.

    Returns the generated ``message_id``.
    """
    return send_message(parent_name, agent_name, "result", result)


def check_completed_tasks(agent_name: str) -> list[dict]:
    """Check for completed tasks from child agents.

    Reads and marks as read all messages in *agent_name*'s inbox, then
    filters for messages of type ``"result"``.

    Returns:
        A list of result message dicts, each containing at minimum
        ``from``, ``content``, and ``timestamp`` keys.
    """
    messages = read_messages(agent_name, mark_read=True)
    return [m for m in messages if m.get("type") == "result"]


def get_pending_tasks(agent_name: str) -> list[dict]:
    """Get pending task messages for *agent_name* (does NOT mark as read).

    Use this when an agent starts up to discover what it has been asked to do.

    Returns:
        A list of task message dicts of type ``"task"``.
    """
    messages = read_messages(agent_name, mark_read=False)
    return [m for m in messages if m.get("type") == "task"]
