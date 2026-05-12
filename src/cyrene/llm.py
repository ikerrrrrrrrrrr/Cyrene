"""
LLM helper utilities: text extraction, truncation, and constants.

These are pure functions with no dependencies on agent.py or tools.py.
"""

import logging
from typing import Any

logger = logging.getLogger(__name__)

_MAX_TOOL_OUTPUT_CHARS = 12000


def _truncate(text: str, limit: int = _MAX_TOOL_OUTPUT_CHARS) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n...[truncated {len(text) - limit} chars]"


def _assistant_text(message: dict[str, Any]) -> str:
    """Extract text content from an assistant message."""
    content = message.get("content")
    if isinstance(content, str):
        if content.strip():
            return content
    elif isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, dict) and item.get("type") == "text":
                parts.append(str(item.get("text", "")))
        text = "".join(parts)
        if text.strip():
            return text
    # Fallback: use reasoning_content if content is empty (Qwen-style models)
    reasoning = message.get("reasoning_content")
    if reasoning and isinstance(reasoning, str):
        return reasoning.strip()
    return ""
