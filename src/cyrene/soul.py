"""Soul / identity management for Cyrene."""

from cyrene.config import ASSISTANT_NAME, SOUL_PATH


def ensure_soul() -> None:
    """Ensure the SOUL.md file exists."""
    SOUL_PATH.parent.mkdir(parents=True, exist_ok=True)
    if not SOUL_PATH.exists():
        SOUL_PATH.write_text(f"# {ASSISTANT_NAME}'s Soul\n\nIdentity file for {ASSISTANT_NAME}.\n")
