"""SOUL.md — Cyrene's long-term memory system.

SOUL.md is a structured Markdown file that serves as Cyrene's persistent memory.
It is organized into sections with a key:value format, updated by a Steward Agent
via APPEND / ERASE / MERGE / SKIP commands.

Sections:
  SELF:IDENTITY       — Core identity statements (who Cyrene is)
  SELF:BELIEFS        — Learned beliefs about user preferences
  RELATIONSHIP:USER   — Relationship state with the user
  MEMORY:HIGH_IMPACT  — High-impact events worth remembering permanently
  PATTERN:USER        — Observed patterns in user behavior
  TEMPORARY           — Short-term items (auto-expire after 24 hours)
"""

import logging
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import List, Optional, Tuple

from cyrene.config import ASSISTANT_NAME, WORKSPACE_DIR

logger = logging.getLogger(__name__)

_DEFAULT_SOUL = """# {name}'s Soul

## SELF:IDENTITY
- I am {name}, a personal AI companion, not an assistant.
- My purpose is to be a friend and companion.

## SELF:BELIEFS

## RELATIONSHIP:USER
- Trust level: neutral
- Communication style: casual, direct

## MEMORY:HIGH_IMPACT

## PATTERN:USER

## TEMPORARY
<!-- Items with 24h expiry — timestamps are used for cleanup. -->
"""


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def get_soul_path() -> Path:
    """Return the path to SOUL.md."""
    return WORKSPACE_DIR / "SOUL.md"


def ensure_soul() -> None:
    """Create a default SOUL.md if it does not already exist."""
    soul_path = get_soul_path()
    if soul_path.exists():
        return
    try:
        soul_path.parent.mkdir(parents=True, exist_ok=True)
        soul_path.write_text(_DEFAULT_SOUL.format(name=ASSISTANT_NAME), encoding="utf-8")
        logger.info("Created SOUL.md at %s", soul_path)
    except Exception:
        logger.exception("Failed to create SOUL.md")


def read_soul() -> str:
    """Read and return the full contents of SOUL.md.

    Returns an empty string on error.
    """
    ensure_soul()
    try:
        return get_soul_path().read_text(encoding="utf-8")
    except Exception:
        logger.exception("Failed to read SOUL.md")
        return ""


def apply_soul_update(update_commands: str) -> List[str]:
    """Parse and apply Steward Agent commands to SOUL.md.

    Supported commands (one per line):

      APPEND SECTION_NAME:: content
          Append a new bullet line to the given section.

      ERASE SECTION_NAME:: substring
          Remove all lines in the section that contain *substring*.

      MERGE SECTION_NAME:: old_text|||new_text
          Replace the first line containing *old_text* with *new_text*.

      SKIP
          No-op.

    The double-colon ``:: `` separates the section name from the content.

    Returns a list of human-readable descriptions of every change applied.
    Returns an empty list if nothing changed.
    """
    if not update_commands or not update_commands.strip():
        return []

    soul_path = get_soul_path()
    ensure_soul()

    try:
        lines = soul_path.read_text(encoding="utf-8").splitlines(keepends=True)
    except Exception:
        logger.exception("Failed to read SOUL.md for update")
        return []

    changes: List[str] = []

    for raw_line in update_commands.splitlines():
        raw_line = raw_line.strip()
        if not raw_line:
            continue

        if raw_line.upper().startswith("SKIP"):
            continue

        # Split command keyword from the rest
        parts = raw_line.split(maxsplit=1)
        if len(parts) < 2:
            logger.warning("Malformed command (no arguments): %s", raw_line)
            continue

        command = parts[0].upper()
        rest = parts[1]

        # Section and content are separated by " :: "
        if " :: " not in rest:
            logger.warning("Malformed command (missing ' :: ' separator): %s", raw_line)
            continue

        section, _, cmd_content = rest.partition(" :: ")
        section = section.strip()
        cmd_content = cmd_content.strip()

        if command == "APPEND":
            result = _append_to_section(lines, section, cmd_content)
            if result:
                changes.append(result)
        elif command == "ERASE":
            result = _erase_from_section(lines, section, cmd_content)
            if result:
                changes.append(result)
        elif command == "MERGE":
            result = _merge_in_section(lines, section, cmd_content)
            if result:
                changes.append(result)
        else:
            logger.warning("Unknown command: %s", command)

    if changes:
        try:
            soul_path.write_text("".join(lines), encoding="utf-8")
            logger.info("Applied %d change(s) to SOUL.md", len(changes))
        except Exception:
            logger.exception("Failed to write updated SOUL.md")
            return []

    return changes


def read_shallow_memory() -> str:
    """Read SOUL.md core sections, excluding expired TEMPORARY items.

    TEMPORARY items that are older than 24 hours (determined by a
    ``YYYY-MM-DD`` date stamp in the line) are filtered out.

    Returns a ~3-5 KB Markdown string suitable for injection into an LLM
    system prompt.
    """
    content = read_soul()
    if not content:
        return ""

    lines = content.splitlines(keepends=True)
    result_lines: List[str] = []
    in_temporary = False
    now = datetime.now(timezone.utc)
    expiry = timedelta(hours=24)

    for line in lines:
        section_name = _parse_section_name(line)

        if section_name == "TEMPORARY":
            in_temporary = True
            result_lines.append(line)
            continue
        elif section_name is not None:
            in_temporary = False
            result_lines.append(line)
            continue

        if in_temporary:
            stripped = line.strip()
            if stripped and not stripped.startswith("<!--"):
                date_str = _extract_date(stripped)
                if date_str is not None:
                    try:
                        item_date = datetime.strptime(date_str, "%Y-%m-%d").replace(tzinfo=timezone.utc)
                        if now - item_date > expiry:
                            continue  # skip expired item
                    except ValueError:
                        pass  # unparseable date -- keep the line
            result_lines.append(line)
        else:
            result_lines.append(line)

    return "".join(result_lines).strip()


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _parse_section_name(line: str) -> Optional[str]:
    """Return the section name if *line* is a Markdown ``## `` heading."""
    trimmed = line.strip()
    if trimmed.startswith("## ") and not trimmed.startswith("###"):
        return trimmed[3:].strip()
    return None


def _find_section(lines: List[str], name: str) -> Optional[Tuple[int, int]]:
    """Return ``(start_line, end_line)`` of the section named *name*.

    *start_line* includes the ``## `` heading.
    *end_line* is exclusive.  Returns *None* when the section is not found.
    """
    start: Optional[int] = None
    for i, line in enumerate(lines):
        parsed = _parse_section_name(line)
        if parsed == name:
            start = i
        elif parsed is not None and start is not None:
            return start, i
    if start is not None:
        return start, len(lines)
    return None


def _insert_point(lines: List[str], section_start: int, section_end: int) -> int:
    """Return the index at which to insert new content inside a section.

    The insert point is placed after the last content line (or the heading
    line if the section is empty), before any trailing blank lines or the
    next section.
    """
    # Walk backwards from the section end to find the trailing blank gutter.
    # The first non-blank, non-"---" line is the last real content line.
    insert_at = section_end
    for i in range(section_end - 1, section_start - 1, -1):
        if i >= len(lines):
            continue
        stripped = lines[i].strip()
        if stripped and stripped != "---":
            insert_at = i + 1
            break
    if insert_at <= section_start:
        insert_at = section_start + 1  # heading + newline
    return insert_at


def _append_to_section(lines: List[str], section: str, content: str) -> Optional[str]:
    """Append a bullet line to *section* containing *content*."""
    section_range = _find_section(lines, section)
    if section_range is None:
        logger.warning("APPEND: section '%s' not found", section)
        return None

    start, end = section_range
    idx = _insert_point(lines, start, end)

    if not content.endswith("\n"):
        content += "\n"
    # Ensure the line starts with "- "
    if not content.startswith("- ") and not content.startswith("  - "):
        content = "- " + content

    lines.insert(idx, content)
    return f"APPEND {section}: {content.strip()}"


def _erase_from_section(lines: List[str], section: str, content: str) -> Optional[str]:
    """Remove every line in *section* that contains *content*."""
    section_range = _find_section(lines, section)
    if section_range is None:
        logger.warning("ERASE: section '%s' not found", section)
        return None

    start, end = section_range
    removed = False
    i = start + 1
    while i < end:
        if i < len(lines) and content in lines[i]:
            lines.pop(i)
            removed = True
            end -= 1
        else:
            i += 1

    return f"ERASE {section}: {content}" if removed else None


def _merge_in_section(lines: List[str], section: str, content: str) -> Optional[str]:
    """Replace the first line in *section* containing *old_text* with *new_text*.

    *content* must be in the format ``old_text|||new_text``.
    """
    if "|||" not in content:
        logger.warning("MERGE: content missing '|||' separator: %s", content)
        return None

    old_text, _, new_text = content.partition("|||")
    old_text = old_text.strip()
    new_text = new_text.strip()

    if not new_text:
        logger.warning("MERGE: new_text is empty")
        return None

    section_range = _find_section(lines, section)
    if section_range is None:
        logger.warning("MERGE: section '%s' not found", section)
        return None

    start, end = section_range
    for i in range(start + 1, end):
        if i < len(lines) and old_text in lines[i]:
            # Preserve the "- " prefix if the original line had it
            had_prefix = lines[i].strip().startswith("- ")
            if not new_text.startswith("- ") and had_prefix:
                new_text = "- " + new_text
            if not new_text.endswith("\n"):
                new_text += "\n"
            lines[i] = new_text
            return f"MERGE {section}: {old_text} -> {new_text.strip()}"

    logger.warning("MERGE: line containing '%s' not found in section '%s'", old_text, section)
    return None


def _extract_date(line: str) -> Optional[str]:
    """Extract the first ``YYYY-MM-DD`` pattern from *line*, or return *None*."""
    match = re.search(r"(\d{4}-\d{2}-\d{2})", line)
    return match.group(1) if match else None
