"""Memory system -- workspace initialization and memory context assembly.

Replaces the old static CLAUDE.md prompt with a living SOUL.md memory that
the Steward Agent can read, write, and evolve over time.
"""

import logging
from datetime import datetime, timezone

from cyrene.config import WORKSPACE_DIR
from cyrene.conversations import CONVERSATIONS_DIR, ensure_conversations_dir
from cyrene.soul import ensure_soul, read_shallow_memory

logger = logging.getLogger(__name__)


def ensure_workspace() -> None:
    """Initialize workspace: create directories, SOUL.md, and conversations/.

    Signature is kept unchanged so that ``__main__.py`` and ``local_cli.py``
    continue to work without modification.
    """
    try:
        WORKSPACE_DIR.mkdir(parents=True, exist_ok=True)
        ensure_soul()
        ensure_conversations_dir()
        logger.info("Workspace initialized at %s", WORKSPACE_DIR)
    except Exception:
        logger.exception("Failed to initialize workspace")


def get_memory_context() -> str:
    """Assemble the memory context to inject into the LLM system prompt.

    Returns:
        A Markdown string containing:
          - Core SOUL.md sections (shallow memory, with expired temporaries
            filtered out).
          - A brief note about today's conversation activity.
    """
    parts: list[str] = []

    # 1. SOUL.md shallow memory (core sections + non-expired temporaries)
    try:
        shallow = read_shallow_memory()
        if shallow:
            parts.append(shallow)
    except Exception:
        logger.exception("Failed to read shallow memory")

    # 2. Today's conversation summary
    try:
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        today_file = CONVERSATIONS_DIR / f"{today}.md"
        if today_file.exists():
            raw = today_file.read_text(encoding="utf-8")
            exchange_count = raw.count("## ") - 1  # subtract the file-level H1
            if exchange_count > 0:
                parts.append(f"---\nToday's conversation has {exchange_count} exchange(s) so far.")
    except Exception:
        logger.debug("Could not read today's conversation file", exc_info=True)

    result = "\n\n".join(parts).strip()
    logger.debug("Memory context assembled (%d chars)", len(result))
    return result
