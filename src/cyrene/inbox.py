"""Inbox management for Cyrene."""

from cyrene.config import INBOX_DIR


def ensure_inbox(name: str) -> None:
    """Ensure the inbox directory exists for the given name."""
    inbox = INBOX_DIR / name
    inbox.mkdir(parents=True, exist_ok=True)
