"""Context provenance helpers for LLM calls.

The trace metadata is attached to internal message dictionaries under ``_ctx``.
It must never be sent to the model; ``strip_context_metadata`` removes it before
payload construction while debug logging can still summarize where context came
from.
"""

from __future__ import annotations

import hashlib
import re
from typing import Any

CTX_KEY = "_ctx"


def approx_token_count(text: Any) -> int:
    """Small CJK-aware token estimate used for debugging summaries."""
    source = str(text or "")
    if not source.strip():
        return 0
    units = re.findall(r"[一-鿿]|[A-Za-z0-9_]+|[^\s]", source)
    total = 0
    for unit in units:
        if re.fullmatch(r"[A-Za-z0-9_]+", unit):
            total += max(1, (len(unit) + 3) // 4)
        else:
            total += 1
    return total


def content_fingerprint(content: Any) -> str:
    text = str(content or "")
    return hashlib.sha256(text.encode("utf-8", errors="ignore")).hexdigest()[:16]


def context_block(
    block_id: str,
    block_type: str,
    *,
    source: str = "",
    reason: str = "",
    transforms: list[str] | None = None,
    content: Any = "",
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Create a normalized context block descriptor."""
    text = str(content or "")
    block: dict[str, Any] = {
        "id": str(block_id or "").strip() or "context.unknown",
        "type": str(block_type or "").strip() or "unknown",
        "source": str(source or "").strip(),
        "reason": str(reason or "").strip(),
        "transforms": list(transforms or []),
        "tokens_est": approx_token_count(text),
        "chars": len(text),
        "content_sha256_16": content_fingerprint(text),
    }
    if metadata:
        block["metadata"] = dict(metadata)
    return block


def attach_context(message: dict[str, Any], blocks: list[dict[str, Any]] | dict[str, Any]) -> dict[str, Any]:
    """Return a copy of *message* with trace metadata attached."""
    result = dict(message)
    block_list = [blocks] if isinstance(blocks, dict) else list(blocks or [])
    if not block_list:
        return result
    existing = result.get(CTX_KEY)
    existing_blocks: list[dict[str, Any]] = []
    if isinstance(existing, dict) and isinstance(existing.get("blocks"), list):
        existing_blocks = [dict(item) for item in existing["blocks"] if isinstance(item, dict)]
    result[CTX_KEY] = {"blocks": [*existing_blocks, *[dict(item) for item in block_list if isinstance(item, dict)]]}
    return result


def strip_context_metadata(obj: Any) -> Any:
    """Recursively remove internal context metadata from an object."""
    if isinstance(obj, dict):
        return {key: strip_context_metadata(value) for key, value in obj.items() if key != CTX_KEY}
    if isinstance(obj, list):
        return [strip_context_metadata(item) for item in obj]
    if isinstance(obj, tuple):
        return tuple(strip_context_metadata(item) for item in obj)
    return obj


def _message_content_text(message: dict[str, Any]) -> str:
    content = message.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, dict) and item.get("type") == "text":
                parts.append(str(item.get("text") or ""))
        return "\n".join(parts)
    return str(content or "")


def summarize_context_trace(messages: list[dict[str, Any]]) -> dict[str, Any]:
    """Summarize context provenance attached to a message list."""
    included: list[dict[str, Any]] = []
    message_map: list[dict[str, Any]] = []
    token_by_type: dict[str, int] = {}
    seen: set[tuple[str, int]] = set()

    for index, message in enumerate(messages):
        if not isinstance(message, dict):
            continue
        role = str(message.get("role") or "")
        message_tokens = approx_token_count(_message_content_text(message))
        ctx = message.get(CTX_KEY)
        blocks = ctx.get("blocks") if isinstance(ctx, dict) else None
        if not isinstance(blocks, list) or not blocks:
            inferred = _infer_block_for_message(index, message, message_tokens)
            blocks = [inferred]

        block_ids: list[str] = []
        for raw in blocks:
            if not isinstance(raw, dict):
                continue
            block = dict(raw)
            block_id = str(block.get("id") or "context.unknown")
            block_type = str(block.get("type") or "unknown")
            block.setdefault("tokens_est", message_tokens)
            block["message_index"] = index
            block["message_role"] = role
            block_ids.append(block_id)
            token_by_type[block_type] = token_by_type.get(block_type, 0) + int(block.get("tokens_est") or 0)
            key = (block_id, index)
            if key not in seen:
                included.append(block)
                seen.add(key)

        message_map.append({
            "message_index": index,
            "role": role,
            "tokens_est": message_tokens,
            "block_ids": block_ids,
        })

    return {
        "included": included,
        "message_map": message_map,
        "token_by_type": token_by_type,
        "total_tokens_est": sum(item["tokens_est"] for item in message_map),
    }


def _infer_block_for_message(index: int, message: dict[str, Any], message_tokens: int) -> dict[str, Any]:
    role = str(message.get("role") or "unknown")
    block_type = "history"
    block_id = f"message.{index}.{role}"
    reason = "message had no explicit context metadata"
    if message.get("compacted_block"):
        block_type = "history_compacted"
        block_id = f"history.compacted.{message.get('message_id') or index}"
    elif message.get("deep_reflection_record"):
        block_type = "history_deep_reflection"
        block_id = f"history.deep_reflection.{message.get('reflection_id') or message.get('message_id') or index}"
    elif role == "tool":
        block_type = "tool_result"
        block_id = f"tool.result.{message.get('tool_call_id') or index}"
    elif role == "system":
        block_type = "system"
        block_id = f"system.message.{index}"
    elif role == "user":
        block_type = "user"
        block_id = f"user.message.{message.get('message_id') or index}"
    elif role == "assistant":
        block_type = "assistant"
        block_id = f"assistant.message.{message.get('message_id') or index}"
    return {
        "id": block_id,
        "type": block_type,
        "source": "messages",
        "reason": reason,
        "transforms": [],
        "tokens_est": message_tokens,
        "chars": len(_message_content_text(message)),
        "content_sha256_16": content_fingerprint(_message_content_text(message)),
    }
