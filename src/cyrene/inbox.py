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
from datetime import datetime, timezone
from pathlib import Path

from cyrene.config import DATA_DIR

logger = logging.getLogger(__name__)

INBOX_DIR = DATA_DIR / "inbox"


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
) -> str:
    """Send a message to *to_agent*'s inbox.

    *msg_type* should be one of ``"message"``, ``"task_result"``, or
    ``"question"``.

    Returns the generated ``message_id``.
    """
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
    msg_path = _inbox_path(to_agent) / f"{msg_id}.json"
    try:
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
    return msg_id


def get_unread_count(agent_name: str) -> int:
    """Return the number of unread messages for *agent_name*."""
    return _read_unread(agent_name)


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
