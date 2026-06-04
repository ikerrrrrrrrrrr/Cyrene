"""Slash-command parsing for agent entrypoints."""

from __future__ import annotations

from typing import Any

DEEP_REFLECT_COMMAND_ID = "deep-reflect"
DEEP_REFLECT_SLASHES = ("/deep-reflect", "/深度反思")


def parse_deep_reflect_command(text: str) -> dict[str, Any]:
    """Parse a deep-reflect slash command.

    Returns a small dict rather than a dataclass so call sites can pass the
    result through JSON-oriented code without extra conversion.
    """
    source = str(text or "").strip()
    for command in DEEP_REFLECT_SLASHES:
        if source == command:
            return {"matched": True, "command": DEEP_REFLECT_COMMAND_ID, "focus": "", "public_text": source}
        if source.startswith(command) and len(source) > len(command) and source[len(command)].isspace():
            return {
                "matched": True,
                "command": DEEP_REFLECT_COMMAND_ID,
                "focus": source[len(command):].strip(),
                "public_text": source,
            }
    return {"matched": False, "command": "", "focus": "", "public_text": source}


def is_deep_reflect_command(command: str, text: str = "") -> bool:
    return str(command or "").strip() == DEEP_REFLECT_COMMAND_ID or bool(parse_deep_reflect_command(text).get("matched"))


def parse_deep_reflect_focus(text: str) -> str:
    return str(parse_deep_reflect_command(text).get("focus") or "").strip()
