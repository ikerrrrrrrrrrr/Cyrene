"""Runtime-only context identity helpers.

Context identities are generated for active agent requests even when verbose
logging is off. They are never sent to model providers; verbose mode only
controls whether full request graphs are written to debug JSONL.
"""

from __future__ import annotations

import hashlib
import re
from contextvars import ContextVar, Token
from datetime import datetime, timezone
from typing import Any

_current_request_id: ContextVar[str] = ContextVar("context_identity_request_id", default="")
_current_request_label: ContextVar[str] = ContextVar("context_identity_request_label", default="")


def _slug(value: Any, *, max_len: int = 54) -> str:
    text = str(value or "").strip().lower()
    text = re.sub(r"\s+", "-", text)
    text = re.sub(r"[^0-9a-zA-Z_\-\.\u4e00-\u9fff]+", "-", text)
    text = re.sub(r"-{2,}", "-", text).strip("-._")
    if not text:
        return "empty"
    return text[:max_len].strip("-._") or "empty"


def fingerprint(value: Any, *, length: int = 12) -> str:
    text = str(value or "")
    return hashlib.sha256(text.encode("utf-8", errors="ignore")).hexdigest()[:length]


def begin_request(kind: str, label: str, *, round_id: str = "", client_request_id: str = "") -> tuple[Token[str], Token[str], str]:
    """Start an identity scope and return ContextVar tokens plus request id."""
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    label_slug = _slug(label, max_len=72)
    parts = [f"req.{_slug(kind, max_len=24)}", ts]
    if round_id:
        parts.append(_slug(round_id, max_len=40))
    if client_request_id:
        parts.append(_slug(client_request_id, max_len=40))
    parts.append(label_slug)
    parts.append(f"sha256-{fingerprint(label)}")
    request_id = ".".join(parts)
    token_id = _current_request_id.set(request_id)
    token_label = _current_request_label.set(str(label or ""))
    return token_id, token_label, request_id


def reset_request(tokens: tuple[Token[str], Token[str], str] | None) -> None:
    if not tokens:
        return
    token_id, token_label, _request_id = tokens
    _current_request_label.reset(token_label)
    _current_request_id.reset(token_id)


def current_request_id() -> str:
    return _current_request_id.get()


def current_request_label() -> str:
    return _current_request_label.get()


def enabled() -> bool:
    return bool(current_request_id())


def make_cid(kind: str, label: str, *, content: Any = "", extra: str = "") -> str:
    """Create a human-readable cid with a content hash suffix."""
    parts = ["cid", _slug(kind, max_len=30), _slug(label, max_len=86)]
    if extra:
        parts.append(_slug(extra, max_len=46))
    parts.append(f"sha256-{fingerprint(content if content != '' else label)}")
    return ".".join(parts)


def source_node_id(cid: str, kind: str = "") -> str:
    source_kind = _slug(kind or "source", max_len=30)
    return f"node.source.{source_kind}.{_slug(cid, max_len=96)}"


def event_node_id(event_type: str, event_id: str, *, caller: str = "", name: str = "", phase: str = "") -> str:
    label = ".".join(part for part in (_slug(caller, max_len=36), _slug(name or phase, max_len=36), _slug(event_id, max_len=36)) if part)
    return f"node.{_slug(event_type, max_len=24)}.{label or _slug(event_id, max_len=36)}"


def tool_schema_cid(tools: list[dict] | None) -> str:
    defs = tools or []
    names = []
    for tool in defs:
        if isinstance(tool, dict):
            names.append(str((tool.get("function") or {}).get("name") or "").strip())
    names = [name for name in names if name]
    label = f"tools.available.{len(defs)}tools" + (("." + ".".join(names[:8])) if names else "")
    return make_cid("tool_schema", label, content=repr(defs))


def tool_schema_source(tools: list[dict] | None) -> dict[str, Any] | None:
    if not enabled() or not tools:
        return None
    cid = tool_schema_cid(tools)
    names = [
        str((tool.get("function") or {}).get("name") or "").strip()
        for tool in (tools or [])
        if isinstance(tool, dict)
    ]
    return {
        "cid": cid,
        "node_id": source_node_id(cid, "tool_schema"),
        "type": "tool_schema",
        "label": f"Tool schema ({len(tools)} tools)",
        "tool_names": [name for name in names if name],
    }


def tool_result_cid(tool_name: str, tool_call_id: str, args: Any, result: Any) -> str:
    label = f"{tool_name or 'tool'}.{tool_call_id or 'call'}"
    return make_cid("tool_result", label, content=result, extra=str(args)[:180])


def block_identity(block_id: str, block_type: str, *, content: Any = "", source: str = "") -> dict[str, str]:
    cid = make_cid(block_type or "context", block_id or "unknown", content=content, extra=source)
    return {
        "cid": cid,
        "source_node_id": source_node_id(cid, block_type or "context"),
    }
