"""Conversation archiving for long-term memory."""

import logging
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

from cyrene.config import WORKSPACE_DIR

logger = logging.getLogger(__name__)

CONVERSATIONS_DIR = WORKSPACE_DIR / "conversations"


def ensure_conversations_dir() -> None:
    """Create conversations directory if it doesn't exist."""
    CONVERSATIONS_DIR.mkdir(parents=True, exist_ok=True)


def _get_today_file() -> Path:
    """Get the conversation file for today."""
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    return CONVERSATIONS_DIR / f"{today}.md"


def _upsert_session_title(content: str, date_str: str, session_title: str) -> str:
    header = f"# Conversations - {date_str}\n\n"
    if not content:
        content = header
    elif not content.startswith("# Conversations - "):
        content = header + content

    if not session_title:
        return content

    marker = f"<!-- session_title: {session_title} -->\n\n"
    pattern = re.compile(r"^(# Conversations - .*?\n\n)(?:<!-- session_title: .*? -->\n\n)?", re.DOTALL)
    if pattern.search(content):
        return pattern.sub(lambda match: match.group(1) + marker, content, count=1)
    return header + marker + content[len(header):]


async def archive_exchange(
    user_message: str,
    assistant_response: str,
    chat_id: int,
    session_title: str = "",
    round_title: str = "",
    round_id: str = "",
) -> None:
    """Archive a single user-assistant exchange to today's conversation file.

    Format:
    ## HH:MM:SS UTC

    **User**: <message>

    **Ape**: <response>

    ---
    """
    ensure_conversations_dir()

    filepath = _get_today_file()
    date_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    timestamp = datetime.now(timezone.utc).strftime("%H:%M:%S UTC")
    meta_lines = []
    if round_id:
        meta_lines.append(f"<!-- round_id: {round_id} -->")
    if round_title:
        meta_lines.append(f"<!-- round_title: {round_title} -->")
    meta_block = ("\n".join(meta_lines) + "\n\n") if meta_lines else ""

    # Build the exchange entry
    entry = f"""## {timestamp}

{meta_block}**User**: {user_message}

**Ape**: {assistant_response}

---

"""

    # Append to file (create if doesn't exist)
    try:
        if filepath.exists():
            content = filepath.read_text(encoding="utf-8")
        else:
            # Create file with header
            content = f"# Conversations - {date_str}\n\n"

        content = _upsert_session_title(content, date_str, session_title)
        content += entry
        filepath.write_text(content, encoding="utf-8")
        logger.debug(f"Archived exchange to {filepath}")
    except Exception:
        logger.exception(f"Failed to archive exchange to {filepath}")


async def get_recent_conversations(days: int = 1) -> str:
    """Return conversation records from the last *days* days.

    Each day is prefixed with ``=== YYYY-MM-DD ===`` for easy parsing.

    Returns an empty string when no conversation files are found.
    """
    ensure_conversations_dir()
    now = datetime.now(timezone.utc)
    result_parts: list[str] = []

    for i in range(days):
        date = now - timedelta(days=i)
        date_str = date.strftime("%Y-%m-%d")
        filepath = CONVERSATIONS_DIR / f"{date_str}.md"
        try:
            if filepath.exists():
                content = filepath.read_text(encoding="utf-8")
                result_parts.append(f"=== {date_str} ===\n{content}")
        except Exception:
            logger.exception("Failed to read conversation file %s", filepath)

    return "\n\n".join(result_parts).strip() if result_parts else ""


async def search_conversations(keyword: str, path: str | None = None) -> str:
    """Search conversation history for *keyword* using plain-text matching.

    This is a simple line-by-line substring search (case-insensitive) that
    does NOT use RAG or vector embeddings.  It is intentionally lightweight
    and works even when ``grep`` is unavailable on the host system.

    Args:
        keyword: The text to search for.
        path: Optional subdirectory under CONVERSATIONS_DIR to scope search.
              Defaults to the entire conversations directory.

    Returns:
        Matching lines prefixed with ``filename:line_number:``, or the string
        "No matches found."
    """
    ensure_conversations_dir()

    search_root = CONVERSATIONS_DIR
    if path:
        search_root = search_root / path

    matches: list[str] = []
    kw_lower = keyword.lower()

    try:
        # Collect all .md files sorted by name (i.e. chronologically)
        files = sorted(search_root.glob("**/*.md"))
    except Exception:
        logger.exception("Failed to list conversation files")
        return "Error searching conversations."

    for filepath in files:
        try:
            content = filepath.read_text(encoding="utf-8", errors="replace")
        except Exception:
            continue

        for line_no, line in enumerate(content.splitlines(), start=1):
            if kw_lower in line.lower():
                rel = filepath.relative_to(CONVERSATIONS_DIR)
                matches.append(f"{rel}:{line_no}:{line}")
                if len(matches) >= 200:
                    break

        if len(matches) >= 200:
            break

    return "\n".join(matches) if matches else "No matches found."
