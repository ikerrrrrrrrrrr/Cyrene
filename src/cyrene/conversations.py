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
    archive_session_id: str = "",
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
    if archive_session_id:
        meta_lines.append(f"<!-- archive_session_id: {archive_session_id} -->")
    if session_title:
        meta_lines.append(f"<!-- session_title: {session_title} -->")
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


def _parse_archive_meta(section: str, key: str) -> str:
    match = re.search(rf"<!--\s*{re.escape(key)}:\s*(.*?)\s*-->", section)
    return match.group(1).strip() if match else ""


def _split_archive_entry_blocks(content: str) -> list[str]:
    blocks: list[str] = []
    matches = list(re.finditer(r"(?m)^##\s+\S+\s+UTC\s*$", content))
    for index, match in enumerate(matches):
        start = match.start()
        end = matches[index + 1].start() if index + 1 < len(matches) else len(content)
        block = content[start:end].strip()
        block = re.sub(r"\n+---\s*\Z", "", block).strip()
        if block:
            blocks.append(block)
    return blocks


def _parse_archive_sections(content: str, date_str: str) -> list[dict[str, str]]:
    sections_out: list[dict[str, str]] = []
    file_session_title = _parse_archive_meta(content, "session_title")
    round_index = 0

    for section in _split_archive_entry_blocks(content):
        if "**User**:" not in section:
            continue
        ts_match = re.search(r"##\s*(\S+\s+UTC)", section)
        dialogue_match = re.search(r"\*\*User\*\*:\s*(.*?)\n+\*\*[^*]+\*\*:\s*(.*)\Z", section, re.DOTALL)
        if not ts_match or not dialogue_match:
            continue

        archive_session_id = _parse_archive_meta(section, "archive_session_id")
        session_title = _parse_archive_meta(section, "session_title") or file_session_title
        round_id = _parse_archive_meta(section, "round_id") or f"archive_round_{round_index}"
        round_title = _parse_archive_meta(section, "round_title")
        sections_out.append({
            "date": date_str,
            "timestamp": ts_match.group(1).strip(),
            "archive_session_id": archive_session_id,
            "session_title": session_title,
            "round_id": round_id,
            "round_title": round_title,
            "user_body": dialogue_match.group(1).strip(),
            "assistant_body": dialogue_match.group(2).strip(),
            "raw_entry": section.strip(),
        })
        round_index += 1

    return sections_out


def recall_conversations(
    query: str = "",
    session_id: str = "",
    date: str = "",
    limit: int = 5,
) -> list[dict[str, str]]:
    """Return archived conversation entries matching the given filters.

    Results are ordered from newest to oldest and are intended for agent recall,
    not for exact full-history replay.
    """
    ensure_conversations_dir()

    normalized_query = query.strip().lower()
    normalized_session_id = session_id.strip()
    if normalized_session_id.startswith("archive_"):
        _, _, normalized_session_id = normalized_session_id.partition("_")
        date_prefix, sep, archive_suffix = normalized_session_id.partition("_")
        if sep and re.fullmatch(r"\d{4}-\d{2}-\d{2}", date_prefix):
            if not date:
                date = date_prefix
            normalized_session_id = archive_suffix

    files: list[Path]
    if date:
        files = [CONVERSATIONS_DIR / f"{date}.md"]
    else:
        files = sorted(CONVERSATIONS_DIR.glob("*.md"), reverse=True)

    matches: list[dict[str, str]] = []
    for filepath in files:
        if not filepath.exists():
            continue
        date_str = filepath.stem
        try:
            content = filepath.read_text(encoding="utf-8")
        except Exception:
            logger.exception("Failed to read conversation file %s", filepath)
            continue

        sections = _parse_archive_sections(content, date_str)
        for section in reversed(sections):
            if normalized_session_id and section.get("archive_session_id", "").strip() != normalized_session_id:
                continue
            if normalized_query:
                haystack = "\n".join([
                    section.get("session_title", ""),
                    section.get("round_title", ""),
                    section.get("user_body", ""),
                    section.get("assistant_body", ""),
                    section.get("raw_entry", ""),
                ]).lower()
                if normalized_query not in haystack:
                    continue
            matches.append(section)
            if len(matches) >= max(1, limit):
                return matches

    return matches
