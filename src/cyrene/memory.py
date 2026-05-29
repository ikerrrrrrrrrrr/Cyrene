"""Memory system -- workspace initialization and memory context assembly.

Replaces the old static CLAUDE.md prompt with a living SOUL.md memory that
the Steward Agent can read, write, and evolve over time.
"""

import logging

from cyrene.config import WORKSPACE_DIR
from cyrene.conversations import ensure_conversations_dir
from cyrene.short_term import get_context as get_short_term_context
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


def get_memory_context(include_short_term: bool = True) -> str:
    """Assemble the memory context to inject into the LLM system prompt.

    Returns:
        A Markdown string containing:
          - Core SOUL.md sections (shallow memory, with expired temporaries
            filtered out).
          - Short-term cross-session memory summaries.
          - A brief note about today's conversation activity.
    """
    parts: list[str] = []

    # 1. SOUL.md shallow memory (core sections + non-expired temporaries)
    try:
        from cyrene.settings_store import is_soul_active
        if is_soul_active():
            shallow = read_shallow_memory()
            if shallow:
                parts.append(shallow)
    except Exception:
        logger.exception("Failed to read shallow memory")

    # 2. Short-term cross-session memory
    if include_short_term:
        try:
            short_term = get_short_term_context(
                max_chars=2500,
                header="[Short-term cross-session memory:]",
            )
            if short_term:
                parts.append(short_term)
        except Exception:
            logger.exception("Failed to read short-term memory")

    result = "\n\n".join(parts).strip()
    logger.debug("Memory context assembled (%d chars)", len(result))
    return result
