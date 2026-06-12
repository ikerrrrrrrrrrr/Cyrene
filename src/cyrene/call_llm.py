"""Unified LLM calling — candidates, streaming, tools, thinking, token recording.

Replaces the independent implementations previously scattered across agent.py,
search.py, scheduler.py, attachments.py, and onboarding.py.
"""
from __future__ import annotations

import asyncio
import html
import json
import logging
import os
import re
import time as _time
import uuid
from typing import Any, Callable, Awaitable

import httpx

from cyrene.config import (
    DB_PATH,
    DEFAULT_OPENAI_BASE_URL,
    _strip_wrapping_quotes,
)
from cyrene.context_trace import strip_context_metadata, summarize_context_trace
from cyrene.settings_store import get_models, get_vision_models, get_secondary_model

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Background task tracking — prevent GC from collecting fire-and-forget tasks
# ---------------------------------------------------------------------------
_pending_token_tasks: set[asyncio.Task] = set()


def _bg_token_task(task: asyncio.Task) -> None:
    _pending_token_tasks.add(task)
    task.add_done_callback(_pending_token_tasks.discard)


# ---------------------------------------------------------------------------
# Secondary model concurrency guard
# ---------------------------------------------------------------------------
_secondary_in_flight: int = 0

# ---------------------------------------------------------------------------
# Candidate failure cooldown — a dead endpoint must not slow down every call
# ---------------------------------------------------------------------------
# 连不上的候选（如下线的本地模型机器）会让每次调用都先撞一遍超时。失败后把该候选
# 冷却一段时间，期间直接跳过；全部候选都在冷却时照常尝试，避免无模型可用。
_CANDIDATE_COOLDOWN_SECONDS = 120.0
_candidate_cooldowns: dict[tuple[str, str], float] = {}

# httpx 连接超时与读超时分开：对不可达主机快速失败，而不是吃满整个调用超时。
_CONNECT_TIMEOUT_SECONDS = 5.0


def _candidate_key(candidate: dict[str, Any]) -> tuple[str, str]:
    return (str(candidate.get("model") or ""), str(candidate.get("base_url") or ""))


def _candidate_cooling(key: tuple[str, str]) -> bool:
    return _candidate_cooldowns.get(key, 0.0) > _time.monotonic()


def _set_candidate_cooldown(key: tuple[str, str]) -> None:
    _candidate_cooldowns[key] = _time.monotonic() + _CANDIDATE_COOLDOWN_SECONDS


def _clear_candidate_cooldown(key: tuple[str, str]) -> None:
    _candidate_cooldowns.pop(key, None)

# ---------------------------------------------------------------------------
# Helpers moved from agent.py / attachments.py
# ---------------------------------------------------------------------------


def _normalized_llm_endpoints(base_url: str) -> list[str]:
    normalized_base = str(base_url or DEFAULT_OPENAI_BASE_URL).strip().rstrip("/") or DEFAULT_OPENAI_BASE_URL
    endpoints = [f"{normalized_base}/chat/completions"]
    if not normalized_base.endswith("/v1"):
        endpoints.append(f"{normalized_base}/v1/chat/completions")
    return list(dict.fromkeys(endpoints))


def _normalized_candidate(raw: dict[str, Any], index: int = 0, *, active_model: str, active_base_url: str, active_api_key: str) -> dict[str, Any]:
    model = str(raw.get("model") or raw.get("name") or raw.get("id") or "").strip()
    if not model:
        model = active_model
    base_url = str(raw.get("base_url") or active_base_url or DEFAULT_OPENAI_BASE_URL).strip() or DEFAULT_OPENAI_BASE_URL
    raw_api_key = _strip_wrapping_quotes(str(raw.get("api_key") or "").strip())
    if raw_api_key:
        api_key = raw_api_key
    elif base_url.rstrip("/") == (active_base_url or DEFAULT_OPENAI_BASE_URL).rstrip("/"):
        api_key = active_api_key
    else:
        api_key = ""
    return {
        "id": str(raw.get("id") or f"candidate-{index + 1}").strip() or f"candidate-{index + 1}",
        "model": model,
        "base_url": base_url,
        "api_key": api_key,
        "endpoints": _normalized_llm_endpoints(base_url),
    }


def _base_root(url: str) -> str:
    """Normalize a base URL for equality checks ("…/v1" 与不带 /v1 视为同端点)."""
    normalized = str(url or "").strip().rstrip("/")
    if normalized.endswith("/v1"):
        normalized = normalized[: -len("/v1")].rstrip("/")
    return normalized.lower()


def _inherit_sibling_keys(candidates: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Keyless candidates inherit the key of the first same-endpoint candidate
    that has one ("…/v1" 与不带 /v1 视为同端点)。跨提供商不继承；本地端点可以
    始终无 key（请求时不会带 Authorization 头）。"""
    keyed_roots: dict[str, str] = {}
    for candidate in candidates:
        root = _base_root(candidate.get("base_url") or "")
        if candidate.get("api_key") and root not in keyed_roots:
            keyed_roots[root] = candidate["api_key"]
    for candidate in candidates:
        if not candidate.get("api_key"):
            candidate["api_key"] = keyed_roots.get(_base_root(candidate.get("base_url") or ""), "")
    return candidates


def _resolve_llm_candidates() -> list[dict[str, Any]]:
    """模型列表是唯一事实来源：每个候选自带「标识符 + API Key + Base URL」，
    按列表顺序逐个尝试、失败回退下一条。env 里的 OPENAI_* 只是「保存模型设置时
    镜像 models[0]」的派生值，仅用于补全某条目缺失的 base_url/key，本身不作为
    独立候选参与调用。列表为空（从未配置过任何模型）时返回空——调用方会抛出一个
    明确的「未配置模型」错误，而不是用空 key 撞一个必然 401 的默认端点。"""
    active_model = str(os.environ.get("OPENAI_MODEL", "deepseek-chat") or "").strip() or "deepseek-chat"
    active_base_url = str(os.environ.get("OPENAI_BASE_URL", DEFAULT_OPENAI_BASE_URL) or "").strip() or DEFAULT_OPENAI_BASE_URL
    active_api_key = _strip_wrapping_quotes(str(os.environ.get("OPENAI_API_KEY", "") or "").strip())

    candidates: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str]] = set()
    for index, raw in enumerate(get_models() or []):
        candidate = _normalized_candidate(raw, index, active_model=active_model, active_base_url=active_base_url, active_api_key=active_api_key)
        key = (candidate["model"], candidate["base_url"], candidate["api_key"])
        if key in seen:
            continue
        seen.add(key)
        candidates.append(candidate)

    return _inherit_sibling_keys(candidates)


def _resolve_secondary_candidates() -> list[dict[str, Any]]:
    secondary = get_secondary_model()
    model = str(secondary.get("model") or "").strip()
    if not model:
        return []
    base_url = str(secondary.get("base_url") or "").strip()
    if not base_url:
        base_url = str(os.environ.get("OPENAI_BASE_URL", DEFAULT_OPENAI_BASE_URL) or "").strip() or DEFAULT_OPENAI_BASE_URL
    api_key = _strip_wrapping_quotes(str(secondary.get("api_key") or "").strip())
    if not api_key:
        primary_base = (os.environ.get("OPENAI_BASE_URL", DEFAULT_OPENAI_BASE_URL) or "").strip().rstrip("/") or DEFAULT_OPENAI_BASE_URL.rstrip("/")
        if base_url.rstrip("/") == primary_base:
            api_key = _strip_wrapping_quotes(str(os.environ.get("OPENAI_API_KEY", "") or "").strip())
    ctx_limit = int(secondary.get("ctx_limit") or 0)
    max_concurrency = int(secondary.get("max_concurrency") or 0)
    return [{
        "id": "secondary",
        "model": model,
        "base_url": base_url,
        "api_key": api_key,
        "endpoints": _normalized_llm_endpoints(base_url),
        "ctx_limit": ctx_limit,
        "max_concurrency": max_concurrency,
    }]


def _resolve_vision_candidates() -> list[dict[str, Any]]:
    """Primary chain first (the primary model may be vision-capable), then the
    dedicated vision entries — same per-entry key semantics as the primary list."""
    active_model = str(os.environ.get("OPENAI_MODEL", "deepseek-chat") or "").strip() or "deepseek-chat"
    active_base_url = str(os.environ.get("OPENAI_BASE_URL", DEFAULT_OPENAI_BASE_URL) or "").strip() or DEFAULT_OPENAI_BASE_URL
    active_api_key = _strip_wrapping_quotes(str(os.environ.get("OPENAI_API_KEY", "") or "").strip())

    candidates = _resolve_llm_candidates()
    seen: set[tuple[str, str, str]] = {
        (candidate["model"], candidate["base_url"], candidate["api_key"]) for candidate in candidates
    }

    for raw in get_vision_models() or []:
        candidate = _normalized_candidate(raw, 0, active_model=active_model, active_base_url=active_base_url, active_api_key=active_api_key)
        key = (candidate["model"], candidate["base_url"], candidate["api_key"])
        if key not in seen:
            seen.add(key)
            candidates.append(candidate)

    return _inherit_sibling_keys(candidates)


def _resolve_candidates(model_type: str) -> list[dict[str, Any]]:
    """Return ordered candidate list for the given model_type.

    * ``"primary"``   -> ``_resolve_llm_candidates()``
    * ``"secondary"`` -> secondary first, primary fallback appended
    * ``"vision"``    -> ``_resolve_vision_candidates()``
    """
    if model_type == "primary":
        return _resolve_llm_candidates()
    if model_type == "secondary":
        secondary = _resolve_secondary_candidates()
        primary = _resolve_llm_candidates()
        if secondary:
            return secondary + primary
        return primary
    if model_type == "vision":
        return _resolve_vision_candidates()
    return _resolve_llm_candidates()


# ---------------------------------------------------------------------------
# Message sanitisation
# ---------------------------------------------------------------------------


_INTERNAL_MSG_KEYS = frozenset({
    "message_id", "round_id", "round_title", "client_request_id",
    "hidden_from_ui", "system_initiated", "usage", "attachments",
    "compacted_block", "llm_compacted", "report_expanded_for_turn",
    "report_ref", "report_archive_session_id", "report_round_id",
    "report_title", "deep_reflection_record", "reflection_id",
    "subagent_flow_snapshot", "proactive",
})


def _strip_internal_fields(message: dict) -> dict:
    """Remove Cyrene-internal fields that must not be sent to the LLM."""
    return {k: v for k, v in message.items() if k not in _INTERNAL_MSG_KEYS}


def _sanitize_messages_for_llm(messages: list[dict]) -> list[dict]:
    """Ensure valid tool_calls/tool message pairing with unique tool_call_ids."""
    import uuid as _uuid

    messages = [_strip_internal_fields(m) for m in strip_context_metadata(messages)]
    seen_ids: set[str] = set()
    result: list[dict[str, Any]] = []
    i = 0
    while i < len(messages):
        msg = messages[i]
        role = str(msg.get("role", ""))

        if role == "assistant" and msg.get("tool_calls"):
            tc_list = msg["tool_calls"]
            all_valid = True
            for j, tc in enumerate(tc_list):
                idx = i + 1 + j
                if idx >= len(messages):
                    all_valid = False
                    break
                tm = messages[idx]
                if tm.get("role") != "tool" or tm.get("tool_call_id") != tc.get("id", ""):
                    all_valid = False
                    break

            if all_valid:
                old_ids = [tc.get("id", "") for tc in tc_list]
                has_dupes = any(oid in seen_ids for oid in old_ids)

                if has_dupes:
                    new_msg = dict(msg)
                    new_tc_list = []
                    new_ids = []
                    for tc in tc_list:
                        new_tc = dict(tc)
                        new_id = f"call_{_uuid.uuid4().hex[:12]}"
                        new_tc["id"] = new_id
                        new_tc_list.append(new_tc)
                        new_ids.append(new_id)
                        seen_ids.add(new_id)
                    new_msg["tool_calls"] = new_tc_list
                    result.append(new_msg)
                    for j, new_id in enumerate(new_ids):
                        tool_msg = dict(messages[i + 1 + j])
                        tool_msg["tool_call_id"] = new_id
                        result.append(tool_msg)
                else:
                    for oid in old_ids:
                        seen_ids.add(oid)
                    result.append(msg)
                    for j in range(len(tc_list)):
                        result.append(messages[i + 1 + j])

                i += 1 + len(tc_list)
            else:
                i += 1
        elif role == "tool":
            i += 1
        else:
            result.append(msg)
            i += 1

    return result


# ---------------------------------------------------------------------------
# Payload building
# ---------------------------------------------------------------------------

def _approx_token_count(text: str) -> int:
    """Estimate token count with CJK-aware heuristic.

    CJK characters average ~1 token each; runs of ASCII word chars
    average ~0.25 tokens/char (4 chars per token); punctuation/other
    are counted individually.
    """
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


def _message_token_estimate(message: dict[str, Any]) -> int:
    total = 4
    content = message.get("content")
    if isinstance(content, str):
        total += _approx_token_count(content)
    elif isinstance(content, list):
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                total += _approx_token_count(block.get("text") or "")
    else:
        total += _approx_token_count(content or "")
    total += _approx_token_count(message.get("role") or "")
    total += _approx_token_count(message.get("reasoning_content") or "")
    for tc in message.get("tool_calls") or []:
        total += _approx_token_count(tc.get("function", {}).get("name") or "")
        total += _approx_token_count(tc.get("function", {}).get("arguments") or "")
    total += _approx_token_count(message.get("tool_call_id") or "")
    return total


def _build_payload(
    messages: list[dict],
    tools: list | None,
    max_tokens: int | None,
    stream: bool,
    model: str,
    thinking: str,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "model": model,
        "messages": _sanitize_messages_for_llm(messages),
    }
    if max_tokens is not None:
        payload["max_tokens"] = max_tokens
    if tools:
        payload["tools"] = tools
        payload["tool_choice"] = "auto"
    if stream:
        payload["stream"] = True
        payload["stream_options"] = {"include_usage": True}

    if thinking == "auto":
        if "deepseek" in model.lower():
            payload["thinking"] = {"type": "enabled"}
    elif thinking == "enabled":
        payload["thinking"] = {"type": "enabled"}
    elif thinking == "disabled":
        payload.pop("thinking", None)
    return payload


# ---------------------------------------------------------------------------
# Response processing
# ---------------------------------------------------------------------------


def _message_from_upstream_payload(data: dict[str, Any]) -> dict[str, Any]:
    choices = data.get("choices")
    if isinstance(choices, list) and choices:
        first = choices[0] or {}
        message = first.get("message")
        if isinstance(message, dict):
            return message
    if isinstance(data.get("message"), dict):
        return dict(data["message"])
    output = data.get("output")
    if isinstance(output, dict):
        if isinstance(output.get("message"), dict):
            return dict(output["message"])
        if isinstance(output.get("text"), str):
            return {"role": "assistant", "content": output["text"]}
    if isinstance(data.get("response"), dict):
        return dict(data["response"])
    error_text = (
        data.get("error")
        or data.get("message")
        or data.get("detail")
        or data.get("msg")
        or json.dumps(data, ensure_ascii=False)[:400]
    )
    raise ValueError(f"Upstream response missing choices/message payload: {error_text}")


_DSML_MARKER = r"(?:｜｜|\|\|)DSML(?:｜｜|\|\|)"
_DSML_TOOL_BLOCK_RE = re.compile(
    rf"<{_DSML_MARKER}tool_calls>(?P<body>.*?)</{_DSML_MARKER}tool_calls>",
    re.DOTALL,
)
_DSML_INVOKE_RE = re.compile(
    rf"<{_DSML_MARKER}invoke\s+name=(?P<quote>[\"'])(?P<name>.*?)(?P=quote)\s*(?:/>|>(?P<body>.*?)</{_DSML_MARKER}invoke>)",
    re.DOTALL,
)
_DSML_PARAMETER_RE = re.compile(
    rf"<{_DSML_MARKER}parameter\s+name=(?P<quote>[\"'])(?P<name>.*?)(?P=quote)(?P<attrs>[^>]*)>(?P<value>.*?)</{_DSML_MARKER}parameter>",
    re.DOTALL,
)


def _dsml_parameter_value(value: str, attrs: str) -> Any:
    text = html.unescape(str(value or "").strip())
    string_match = re.search(r"""\bstring\s*=\s*["'](?P<value>true|false)["']""", str(attrs or ""), re.IGNORECASE)
    if string_match and string_match.group("value").lower() == "true":
        return text
    try:
        return json.loads(text)
    except Exception:
        return text


def _normalize_dsml_tool_calls(message: dict[str, Any], tools: list | None) -> dict[str, Any]:
    """Convert DeepSeek's textual DSML fallback into OpenAI-style tool calls."""
    if not tools or message.get("tool_calls"):
        return message
    content = message.get("content")
    if not isinstance(content, str) or "DSML" not in content:
        return message

    allowed_names = {
        str(tool.get("function", {}).get("name") or "").strip()
        for tool in tools
        if isinstance(tool, dict)
    }
    blocks = list(_DSML_TOOL_BLOCK_RE.finditer(content))
    if not blocks:
        return message

    tool_calls: list[dict[str, Any]] = []
    for block in blocks:
        invocations = list(_DSML_INVOKE_RE.finditer(block.group("body")))
        if not invocations:
            return message
        for invocation in invocations:
            name = html.unescape(invocation.group("name")).strip()
            if not name or name not in allowed_names:
                return message
            arguments: dict[str, Any] = {}
            for parameter in _DSML_PARAMETER_RE.finditer(invocation.group("body") or ""):
                parameter_name = html.unescape(parameter.group("name")).strip()
                if parameter_name:
                    arguments[parameter_name] = _dsml_parameter_value(parameter.group("value"), parameter.group("attrs"))
            tool_calls.append({
                "index": len(tool_calls),
                "id": f"call_dsml_{uuid.uuid4().hex[:16]}",
                "type": "function",
                "function": {
                    "name": name,
                    "arguments": json.dumps(arguments, ensure_ascii=False),
                },
            })

    normalized = dict(message)
    normalized["content"] = _DSML_TOOL_BLOCK_RE.sub("", content).strip()
    normalized["tool_calls"] = tool_calls
    return normalized


def _normalized_usage(usage: Any, messages: list[dict[str, Any]], response_message: dict[str, Any]) -> dict[str, int]:
    if isinstance(usage, dict) and any(isinstance(usage.get(key), int) for key in ("prompt_tokens", "completion_tokens", "total_tokens")):
        prompt = int(usage.get("prompt_tokens") or 0)
        completion = int(usage.get("completion_tokens") or 0)
        total = int(usage.get("total_tokens") or (prompt + completion))
        normalized: dict[str, int] = {
            "prompt_tokens": prompt,
            "completion_tokens": completion,
            "total_tokens": total,
        }
        for key in ("prompt_cache_hit_tokens", "prompt_cache_miss_tokens"):
            if isinstance(usage.get(key), int):
                normalized[key] = int(usage.get(key))
        return normalized
    prompt = sum(_message_token_estimate(message) for message in messages) + 8
    completion = _message_token_estimate(response_message) + 8
    return {
        "prompt_tokens": prompt,
        "completion_tokens": completion,
        "total_tokens": prompt + completion,
    }


def _extract_stream_delta_text(delta: dict[str, Any]) -> str:
    content = delta.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, dict) and item.get("type") == "text":
                text = item.get("text")
                if isinstance(text, str):
                    parts.append(text)
        return "".join(parts)
    return ""


def _format_httpx_error(exc: Exception) -> str:
    parts: list[str] = [type(exc).__name__]
    detail = str(exc or "").strip()
    if detail:
        parts.append(detail)

    request = getattr(exc, "request", None)
    if request is not None:
        method = str(getattr(request, "method", "") or "").strip()
        url = str(getattr(request, "url", "") or "").strip()
        request_part = "request="
        if method:
            request_part += method
        if url:
            request_part += f" {url}" if method else url
        parts.append(request_part)

    response = getattr(exc, "response", None)
    if response is not None:
        parts.append(f"status={response.status_code}")
        try:
            body = str(response.text or "").strip()
        except Exception:
            body = ""
        if body:
            body_preview = re.sub(r"\s+", " ", body)[:500]
            parts.append(f"body={body_preview}")

    cause = getattr(exc, "__cause__", None)
    if cause is not None:
        cause_text = str(cause or "").strip()
        if cause_text:
            parts.append(f"cause={type(cause).__name__}: {cause_text}")
        else:
            parts.append(f"cause={type(cause).__name__}")

    return " | ".join(parts)


def _looks_like_vision_capability_error(exc: Exception) -> bool:
    detail = str(exc).lower()
    return any(token in detail for token in ("image", "vision", "multimodal", "unsupported", "invalid content", "input_image"))


# ---------------------------------------------------------------------------
# Token recording
# ---------------------------------------------------------------------------


def _record_token_usage_faf(
    model: str,
    usage: dict,
    duration_ms: int,
    caller: str,
    *,
    round_id: str = "",
) -> None:
    """Fire-and-forget token usage recording."""
    from cyrene.db import record_token_usage

    _bg_token_task(asyncio.create_task(record_token_usage(
        str(DB_PATH),
        model=model,
        prompt_tokens=int(usage.get("prompt_tokens") or 0),
        completion_tokens=int(usage.get("completion_tokens") or 0),
        total_tokens=int(usage.get("total_tokens") or 0),
        cache_hit_tokens=int(usage.get("prompt_cache_hit_tokens") or 0),
        cache_miss_tokens=int(usage.get("prompt_cache_miss_tokens") or 0),
        duration_ms=duration_ms,
        round_id=round_id,
        caller=caller,
    )))


# ---------------------------------------------------------------------------
# SSE event publishing
# ---------------------------------------------------------------------------


async def _publish_llm_event(
    caller: str,
    phase: str,
    messages: list[dict],
    tools: list | None,
    response: dict,
    model: str,
    duration_ms: int,
    session_id: str = "",
    round_id: str = "",
) -> None:
    from cyrene import debug

    event = {
        "type": "llm_call",
        "caller": caller,
        "phase": phase,
        "model": model,
        "tools": [t.get("function", {}).get("name") for t in (tools or [])],
        "messages": _sanitize_messages_for_llm(messages),
        "context_trace": summarize_context_trace(messages),
        "response": response,
        "usage": response.get("usage") or {},
        "duration_ms": duration_ms,
    }
    if round_id:
        event["round_id"] = round_id
    await debug.publish_event(event, session_id=session_id)


# ---------------------------------------------------------------------------
# The unified call_llm function
# ---------------------------------------------------------------------------


async def call_llm(
    messages: list[dict],
    *,
    tools: list | None = None,
    model_type: str = "primary",
    candidates: list[dict] | None = None,
    max_tokens: int | None = None,
    timeout: float = 120.0,
    stream: bool = False,
    stream_callback: Callable[[dict[str, Any]], Awaitable[None]] | None = None,
    thinking: str = "auto",
    caller: str = "unknown",
    phase: str = "unknown",
    return_text: bool = False,
    publish_events: bool = True,
    record_usage: bool = True,
    round_id: str = "",
    session_id: str = "",
) -> dict | str:
    """Unified LLM calling entry point.

    Args:
        messages: The conversation history.
        tools: Optional tool definitions (triggers ``tool_choice="auto"``).
        model_type: ``"primary"``, ``"secondary"``, or ``"vision"``.
        candidates: Explicit candidate list (overrides ``model_type``).
        max_tokens: If ``None``, omit from payload (let the model decide).
        timeout: HTTP client timeout in seconds.
        stream: If ``True``, emit ``reply_start`` / ``reply_delta`` / ``reply_done``
            events via ``stream_callback`` and return the accumulated text.
        stream_callback: Called with events when *stream* is ``True``.
        thinking: ``"auto"`` (enable for DeepSeek models), ``"enabled"``, ``"disabled"``.
        caller: Identifier used in SSE events and token recording.
        phase: Execution phase tag for SSE events.
        return_text: Return plain ``str`` instead of a message ``dict``.
        publish_events: Whether to publish ``llm_call`` SSE events.
        record_usage: Whether to record token usage to the database.

    Returns:
        Message ``dict`` with keys ``role``, ``content``, ``usage``, ``model``
        (and optionally ``tool_calls``, ``reasoning_content``).
        If ``return_text=True``, returns the content as ``str`` instead.

    Raises:
        httpx.HTTPError: When all candidates and endpoints fail.
    """
    global _secondary_in_flight

    _t0 = _time.monotonic()

    resolved = candidates if candidates is not None else _resolve_candidates(model_type)
    if not resolved:
        resolved = _resolve_llm_candidates()
    if not resolved:
        # No model configured at all (fresh install, never onboarded / saved a
        # model). Keep the historical "return empty string" contract so callers
        # degrade exactly as before, but log it — previously a phantom env
        # candidate masked this by 401'ing on a default endpoint instead.
        logger.error(
            "call_llm has no model candidates [caller=%s phase=%s]; configure one in Settings → Models.",
            caller, phase,
        )
        return ""

    # ctx_limit check for secondary model: if messages exceed the limit,
    # skip secondary and fall through to primary candidates
    if resolved and resolved[0].get("id") == "secondary":
        ctx_limit = int(resolved[0].get("ctx_limit") or 0)
        if ctx_limit > 0:
            total_tokens = sum(_message_token_estimate(m) for m in messages)
            if total_tokens > ctx_limit:
                resolved = resolved[1:] if len(resolved) > 1 else _resolve_llm_candidates()

    # Skip candidates that recently failed (dead endpoint / bad key). If that
    # would leave nothing, ignore cooldowns and try the full list anyway.
    available = [c for c in resolved if not _candidate_cooling(_candidate_key(c))]
    skipped_cooling = [c for c in resolved if _candidate_cooling(_candidate_key(c))]
    if not available:
        available = resolved
        skipped_cooling = []
    failed_this_call: list[str] = []

    def _candidate_label(c: dict[str, Any]) -> str:
        return f"{c.get('id')}({c.get('model')}@{c.get('base_url')})"

    transport = httpx.AsyncHTTPTransport(retries=1)
    client_timeout = httpx.Timeout(timeout, connect=min(_CONNECT_TIMEOUT_SECONDS, timeout))
    async with httpx.AsyncClient(transport=transport, timeout=client_timeout) as client:
        last_error: Exception | None = None

        for candidate in available:
            is_secondary = candidate.get("id") == "secondary"
            max_conc = int(candidate.get("max_concurrency") or 0)

            # Concurrency guard for secondary model
            if is_secondary and max_conc > 0 and _secondary_in_flight >= max_conc:
                continue

            try:
                if is_secondary and max_conc > 0:
                    _secondary_in_flight += 1
                model = str(candidate.get("model") or "").strip()
                payload = _build_payload(messages, tools, max_tokens, stream, model, thinking)

                headers = {"Content-Type": "application/json"}
                api_key = str(candidate.get("api_key") or "").strip()
                if api_key and api_key.lower() not in ("lmstudio", "dummy", ""):
                    headers["Authorization"] = f"Bearer {api_key}"

                endpoints = list(candidate.get("endpoints") or [])
                candidate_error: Exception | None = None

                for endpoint in endpoints:
                    try:
                        if stream:
                            msg = await _handle_stream(client, endpoint, payload, headers, stream_callback)
                        else:
                            resp = await client.post(endpoint, json=payload, headers=headers)
                            if resp.status_code != 200:
                                resp.raise_for_status()
                            data = resp.json()
                            msg = _message_from_upstream_payload(data)
                            msg["usage"] = _normalized_usage(data.get("usage"), messages, msg)

                        msg = _normalize_dsml_tool_calls(msg, tools)
                        msg.setdefault("role", "assistant")
                        msg.setdefault("content", "")
                        if msg.get("usage"):
                            msg["usage"]["model"] = model

                        duration_ms = round((_time.monotonic() - _t0) * 1000)

                        _clear_candidate_cooldown(_candidate_key(candidate))
                        if failed_this_call or skipped_cooling:
                            logger.warning(
                                "call_llm succeeded on %s after %d failed and %d cooled-down candidate(s)%s "
                                "— check the model settings for stale entries",
                                _candidate_label(candidate), len(failed_this_call), len(skipped_cooling),
                                (": " + "; ".join(failed_this_call)) if failed_this_call else "",
                            )

                        # Success — publish events, record usage, return
                        from cyrene import debug as cy_debug

                        if cy_debug.VERBOSE:
                            cy_debug.log_llm_call(caller, phase, messages, tools, msg, duration_ms)

                        if publish_events:
                            await _publish_llm_event(
                                caller, phase, messages, tools, msg, model, duration_ms,
                                session_id=session_id, round_id=round_id,
                            )

                        if record_usage:
                            _record_token_usage_faf(
                                model, msg.get("usage") or {}, duration_ms, caller,
                                round_id=round_id,
                            )

                        if return_text:
                            return msg.get("content", "")
                        msg["model"] = model
                        return msg

                    except httpx.HTTPError as exc:
                        candidate_error = exc
                        last_error = exc
                        if endpoint != endpoints[-1]:
                            continue
                        logger.warning(
                            "call_llm candidate failed [caller=%s phase=%s model=%s endpoint=%s candidate=%s]: %s",
                            caller, phase, model, endpoint, candidate.get("id"), _format_httpx_error(exc),
                        )

                if candidate_error:
                    # All endpoints for this candidate failed — cool it down and
                    # try the next one. The error is preserved in last_error and
                    # re-raised only after all candidates are exhausted.
                    _set_candidate_cooldown(_candidate_key(candidate))
                    failed_this_call.append(
                        f"{_candidate_label(candidate)}: {_format_httpx_error(candidate_error)}"
                    )
                    continue

            except Exception as exc:
                last_error = exc
                _set_candidate_cooldown(_candidate_key(candidate))
                failed_this_call.append(f"{_candidate_label(candidate)}: {exc.__class__.__name__}: {exc}")
                if model_type == "vision" and _looks_like_vision_capability_error(exc):
                    continue
                continue
            finally:
                if is_secondary and max_conc > 0:
                    _secondary_in_flight -= 1

        if last_error:
            logger.exception(
                "call_llm all candidates exhausted [caller=%s phase=%s]: %s",
                caller, phase, _format_httpx_error(last_error),
            )
            raise last_error
    return ""


# ---------------------------------------------------------------------------
# Streaming handler
# ---------------------------------------------------------------------------


async def _handle_stream(
    client: httpx.AsyncClient,
    endpoint: str,
    payload: dict[str, Any],
    headers: dict[str, str],
    stream_callback: Callable[[dict[str, Any]], Awaitable[None]] | None,
) -> dict[str, Any]:
    accumulated: list[str] = []
    reasoning_parts: list[str] = []
    usage: dict[str, Any] = {}
    started = False

    async with client.stream("POST", endpoint, json=payload, headers=headers) as resp:
        if resp.status_code != 200:
            resp.raise_for_status()
        async for raw_line in resp.aiter_lines():
            line = str(raw_line or "").strip()
            if not line:
                continue
            if line.startswith("data:"):
                line = line[5:].strip()
            if not line:
                continue
            if line == "[DONE]":
                break
            try:
                data = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(data.get("usage"), dict):
                usage = data["usage"]
            for choice in data.get("choices") or []:
                delta = choice.get("delta") or {}
                text = _extract_stream_delta_text(delta)
                rc = delta.get("reasoning_content")
                if isinstance(rc, str) and rc.strip():
                    reasoning_parts.append(rc)
                if not text:
                    continue
                if not started and stream_callback:
                    await stream_callback({"type": "reply_start"})
                    started = True
                accumulated.append(text)
                if stream_callback:
                    await stream_callback({"type": "reply_delta", "delta": text})

    full_text = "".join(accumulated)
    if not started and stream_callback:
        await stream_callback({"type": "reply_start"})
    if stream_callback:
        await stream_callback({"type": "reply_done", "response": full_text})

    msg: dict[str, Any] = {"role": "assistant", "content": full_text}
    if reasoning_parts:
        msg["reasoning_content"] = "".join(reasoning_parts)
    msg["usage"] = _normalized_usage(usage, payload.get("messages", []), msg)
    return msg
