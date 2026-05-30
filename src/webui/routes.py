"""Route handlers for the Cyrene Web UI (SPA backend)."""

import asyncio
import base64
import getpass
import json
import logging
import mimetypes
import os
import re
import shutil
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import httpx
from PIL import Image
from fastapi import APIRouter, Request, UploadFile, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, StreamingResponse

from cyrene.cc_bridge import get_cc_preview, get_cc_status
from cyrene.cc_learner import analyze_session, learn_from_session
from cyrene.cc_terminal import CCTerminalSession
from cyrene import debug
from webui.routes_map import register_map_routes
from cyrene.call_llm import _format_httpx_error as format_httpx_error
from cyrene.attachments import (
    EXPORTS_DIR as _EXPORTS_DIR,
    attachment_kind_from_meta,
    build_public_attachment_payload,
    model_supports_multimodal,
    run_vision_chat,
)
from cyrene.config import _strip_wrapping_quotes
from cyrene.agent import (
    _AWAITING_USER_SENTINEL,
    _append_session_message,
    _call_llm,
    _publish_runtime_event,
    _remove_messages_by_request_id,
    _reply_stream_writer,
    answer_pending_question,
    append_system_message,
    clear_session_id,
    get_pending_question,
    get_live_rounds,
    get_session_labels,
    interrupt_active_run,
    queue_round_guidance,
    run_agent,
)
from cyrene.config import (
    ASSISTANT_NAME,
    BASE_DIR,
    DATA_DIR,
    DEFAULT_OPENAI_BASE_URL,
    DEFAULT_OPENAI_MODEL,
    DB_PATH,
    PATTERNS_DIR,
    SEARXNG_HOST,
    SEARXNG_PORT,
    SOUL_PATH,
    STATE_FILE,
    WORKSPACE_DIR,
)
from cyrene.conversations import CONVERSATIONS_DIR, archive_exchange, search_conversations, search_conversations_structured
from cyrene.onboarding import (
    get_onboarding_status,
    reset_onboarding_state,
    save_and_test_llm_setup,
    save_personality_setup,
)
from cyrene.scheduler import reset_lottery
from cyrene.settings_store import get_all as get_web_settings
from cyrene.skills_registry import (
    build_skills as _build_skills,
    install_skill_from_path,
    skill_payload_from_record as _skill_payload_from_record,
    toggle_skill as _toggle_skill,
    uninstall_skill as _uninstall_skill,
)
from cyrene.shells import list_shells as list_live_shells
from cyrene.shells import set_cc_since
from cyrene.short_term import load_entries
from cyrene.soul import get_default_soul_content, read_soul, get_soul_path
from cyrene.version import get_version_label

logger = logging.getLogger(__name__)
_CC_PROJECT_DIR = WORKSPACE_DIR.parent

_bot: Any = None
_db_path: str = ""
_CHAT_ID = -1

_STATIC_DIR = Path(__file__).parent / "static"
_APP_DIR = _STATIC_DIR / "app"
_UPLOADS_DIR = DATA_DIR / "webui_uploads"
_SERVER_STARTED_AT = time.time()


def _ndjson_line(payload: dict[str, Any]) -> str:
    return json.dumps(payload, ensure_ascii=False) + "\n"


def _live_llm_config() -> tuple[str, str]:
    from cyrene import config as cy_config

    return cy_config.OPENAI_MODEL, cy_config.OPENAI_BASE_URL


def _get_model() -> str:
    from cyrene import config as cy_config
    return cy_config.OPENAI_MODEL


def _get_base_url() -> str:
    from cyrene import config as cy_config
    return cy_config.OPENAI_BASE_URL


def _parse_ctx_limit(ctx_str: str) -> int:
    """Parse human-readable context limit like '128K', '1M', '200K' to int."""
    ctx_str = (ctx_str or "").strip().upper()
    if not ctx_str:
        return 0
    try:
        if ctx_str.endswith("M"):
            return int(float(ctx_str[:-1]) * 1_000_000)
        if ctx_str.endswith("K"):
            return int(float(ctx_str[:-1]) * 1_000)
        return int(ctx_str)
    except (ValueError, TypeError):
        return 0


def _get_current_model_ctx_limit() -> int:
    """Look up the current model's context window limit from settings."""
    from cyrene.config_store import get_models, get_vision_models
    model_name = _get_model()
    ctx_limit = 0

    for model in get_models() or []:
        if model.get("model") == model_name or model.get("name") == model_name:
            ctx_limit = _parse_ctx_limit(model.get("ctx", ""))
            break

    if not ctx_limit:
        for model in get_vision_models() or []:
            if model.get("model") == model_name or model.get("name") == model_name:
                ctx_limit = _parse_ctx_limit(model.get("ctx", ""))
                break

    # Fallback: known model context windows when not explicitly configured
    if not ctx_limit:
        model_lower = model_name.lower()
        if any(x in model_lower for x in ("claude-opus-4", "opus-4")):
            ctx_limit = 200_000
        elif any(x in model_lower for x in ("claude-sonnet-4", "sonnet-4")):
            ctx_limit = 200_000
        elif any(x in model_lower for x in ("claude-haiku-4", "haiku-4")):
            ctx_limit = 200_000
        elif "gpt-4" in model_lower or "gpt-4o" in model_lower:
            ctx_limit = 128_000
        elif "gpt-3.5" in model_lower:
            ctx_limit = 16_000
        elif "deepseek" in model_lower:
            ctx_limit = 128_000
        elif "qwen" in model_lower:
            ctx_limit = 128_000
        elif "gemini" in model_lower:
            ctx_limit = 1_000_000

    return ctx_limit


def _remove_path(path: Path) -> None:
    if not path.exists():
        return
    if path.is_dir():
        shutil.rmtree(path, ignore_errors=True)
        return
    try:
        path.unlink()
    except FileNotFoundError:
        pass


async def _reset_app_data() -> dict[str, Any]:
    """Wipe user-modifiable runtime data and restore first-run defaults."""
    from cyrene import agent as cy_agent
    from cyrene.config import write_env_keys
    from cyrene.db import init_db
    from cyrene.inbox import clear_all_inboxes
    from cyrene.settings_store import reset_all as reset_web_settings

    await clear_session_id()

    for task in list(cy_agent._pending_compressors):
        task.cancel()
    cy_agent._pending_compressors.clear()
    await asyncio.sleep(0)

    reset_lottery()
    await clear_all_inboxes()
    reset_web_settings()
    reset_onboarding_state()

    for path in (
        STATE_FILE,
        DATA_DIR / "short_term.json",
        DATA_DIR / "lottery_state.json",
        DATA_DIR / "web_settings.json",
        DATA_DIR / "onboarding_state.json",
        DATA_DIR / ".setup_done",
    ):
        _remove_path(path)

    for path in (
        CONVERSATIONS_DIR,
        _UPLOADS_DIR,
        _EXPORTS_DIR,
        PATTERNS_DIR,
    ):
        _remove_path(path)

    db_path = Path(_db_path or str(DB_PATH))
    _remove_path(db_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    await init_db(str(db_path))

    WORKSPACE_DIR.mkdir(parents=True, exist_ok=True)
    CONVERSATIONS_DIR.mkdir(parents=True, exist_ok=True)
    soul_path = get_soul_path()
    soul_path.parent.mkdir(parents=True, exist_ok=True)
    soul_path.write_text(get_default_soul_content(), encoding="utf-8")

    write_env_keys({
        "OPENAI_API_KEY": "",
        "OPENAI_BASE_URL": DEFAULT_OPENAI_BASE_URL,
        "OPENAI_MODEL": DEFAULT_OPENAI_MODEL,
        "TELEGRAM_BOT_TOKEN": "",
    })

    return {
        "ok": True,
        "onboarding": get_onboarding_status(),
        "sessions": _build_sessions(),
    }


def _reply_stream_chunks(text: str, target_chars: int = 36) -> list[str]:
    source = str(text or "")
    if not source:
        return []

    chunks: list[str] = []
    for block in re.split(r"(\n\n+)", source):
        if not block:
            continue
        if block.startswith("\n"):
            chunks.append(block)
            continue
        remaining = block
        while remaining:
            if len(remaining) <= target_chars:
                chunks.append(remaining)
                break
            split_at = target_chars
            lower_bound = max(0, target_chars - 14)
            for index in range(target_chars - 1, lower_bound - 1, -1):
                if remaining[index] in "，。！？；：,.!?;: ":
                    split_at = index + 1
                    break
            chunks.append(remaining[:split_at])
            remaining = remaining[split_at:]
    return [chunk for chunk in chunks if chunk]


def _consume_cc_input_buffer(buffer: str, data: str) -> tuple[str, list[str]]:
    current = str(buffer or "")
    submitted: list[str] = []
    if not data:
        return current, submitted

    index = 0
    while index < len(data):
        char = data[index]
        if char == "\x1b":
            break
        if char in ("\r", "\n"):
            text = current.strip()
            if text:
                submitted.append(text)
            current = ""
        elif char in ("\x7f", "\b"):
            current = current[:-1]
        elif char == "\t":
            current += "\t"
        elif ord(char) >= 32:
            current += char
        index += 1
    return current, submitted


async def _publish_cc_learning(text: str, tmux_session: str = "") -> None:
    prompt = str(text or "").strip()
    if not prompt:
        return

    status = get_cc_status(_CC_PROJECT_DIR)
    latest_jsonl = str(status.get("latest_jsonl") or "").strip()
    await debug.publish_event(
        {
            "type": "cc_learning",
            "phase": "started",
            "tmux_session": tmux_session,
            "user_input": prompt[:200],
            "latest_jsonl": latest_jsonl,
        }
    )
    if not latest_jsonl:
        return

    try:
        result = await asyncio.to_thread(learn_from_session, Path(latest_jsonl))
    except Exception:
        logger.exception("Failed learning from Claude Code transcript %s", latest_jsonl)
        await debug.publish_event(
            {
                "type": "cc_learning",
                "phase": "error",
                "tmux_session": tmux_session,
                "user_input": prompt[:200],
                "latest_jsonl": latest_jsonl,
            }
        )
        return

    summary = result.get("summary", {})
    await debug.publish_event(
        {
            "type": "cc_learning",
            "phase": "completed",
            "tmux_session": tmux_session,
            "user_input": prompt[:200],
            "latest_jsonl": latest_jsonl,
            "highlights": summary.get("highlights", []),
            "top_tools": summary.get("top_tools", []),
            "top_tasks": summary.get("top_tasks", []),
        }
    )


async def _stream_reply_payload(response_text: str) -> StreamingResponse:
    async def event_stream():
        yield _ndjson_line({"type": "reply_start"})
        for chunk in _reply_stream_chunks(response_text):
            yield _ndjson_line({"type": "reply_delta", "delta": chunk})
            await asyncio.sleep(0)
        yield _ndjson_line({"type": "reply_done", "response": response_text})

    return StreamingResponse(
        event_stream(),
        media_type="application/x-ndjson",
        headers={"Cache-Control": "no-cache"},
    )


def _stream_agent_reply(run_coro_factory, user_message: str) -> StreamingResponse:
    async def event_stream():
        queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        saw_reply_events = False

        async def publish_reply_event(event: dict[str, Any]) -> None:
            await queue.put(dict(event))

        token = _reply_stream_writer.set(publish_reply_event)
        task = asyncio.create_task(run_coro_factory())
        _reply_stream_writer.reset(token)

        # Broadcast running status so the topbar status light updates in real-time
        await debug.publish_event({"type": "session_update", "status": "running"})

        try:
            while True:
                if task.done() and queue.empty():
                    break
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=0.1)
                except asyncio.TimeoutError:
                    continue
                if str(event.get("type") or "").startswith("reply_"):
                    saw_reply_events = True
                yield _ndjson_line(event)

            response = await task
            if response == _AWAITING_USER_SENTINEL:
                yield _ndjson_line({"type": "awaiting_user", "awaiting_user": True, "pending_question": get_pending_question()})
                return

            # Stream the response text FIRST — before any I/O (archive_exchange)
            # or SSE events, so the frontend gets reply_delta events without delay
            # and avoids the race where refreshSessions() clears pending messages
            # before the stream completes.
            if not saw_reply_events:
                yield _ndjson_line({"type": "reply_start"})
                for chunk in _reply_stream_chunks(response):
                    yield _ndjson_line({"type": "reply_delta", "delta": chunk})
                yield _ndjson_line({"type": "reply_done", "response": response})

            # Archive the exchange after streaming — file I/O must not delay
            # response delivery to the frontend.
            labels = get_session_labels()
            await archive_exchange(
                user_message,
                response,
                _CHAT_ID,
                session_title=labels.get("session_title", ""),
                round_title=labels.get("round_title", ""),
                round_id=labels.get("round_id", ""),
                archive_session_id=labels.get("archive_session_id", ""),
            )

            # Signal done last, so the SSE-triggered refreshSessions() call
            # runs after the NDJSON stream has already delivered reply_done.
            await debug.publish_event({"type": "session_update", "status": "done"})
        finally:
            if not task.done():
                task.cancel()
            # Ensure "done" is published even on cancellation/error
            await debug.publish_event({"type": "session_update", "status": "done"})

    return StreamingResponse(
        event_stream(),
        media_type="application/x-ndjson",
        headers={"Cache-Control": "no-cache"},
    )


def _safe_upload_name(filename: str) -> str:
    raw = Path(str(filename or "upload.bin")).name
    sanitized = re.sub(r"[^A-Za-z0-9._-]+", "_", raw).strip("._")
    return sanitized or "upload.bin"


def _image_dimensions(path: Path) -> tuple[int | None, int | None]:
    try:
        with Image.open(path) as image:
            return int(image.width), int(image.height)
    except Exception:
        return None, None


def _attachment_prompt_block(items: list[dict[str, Any]]) -> str:
    if not items:
        return ""
    lines = [
        "",
        "[Uploaded attachments]",
        "The user uploaded the following files into the local workspace-accessible runtime data directory.",
        "Before answering anything about these files, you MUST inspect the relevant attachment with AnalyzeAttachment.",
        "Do not answer from the filename, extension, or metadata alone.",
        "After AnalyzeAttachment returns extracted content, use that extracted content to answer the user.",
    ]
    for item in items:
        lines.append(f'- {item["name"]} ({item["content_type"]}): {item["path"]}')
    return "\n".join(lines)


async def _chat_with_uploaded_images(message: str, attachments: list[dict[str, Any]]) -> str:
    prompt = str(message or "").strip() or "Describe the uploaded image in detail and extract any visible text."
    content: list[dict[str, Any]] = [{"type": "text", "text": prompt}]
    for item in attachments:
        path = Path(str(item.get("path") or "")).resolve()
        mime = str(item.get("content_type") or mimetypes.guess_type(str(path))[0] or "image/png")
        image_b64 = base64.b64encode(path.read_bytes()).decode("ascii")
        content.append({"type": "image_url", "image_url": {"url": f"data:{mime};base64,{image_b64}"}})
    try:
        response = await _call_llm([{"role": "user", "content": content}], tools=None, max_tokens=None)
    except httpx.HTTPError as exc:
        detail = format_httpx_error(exc).lower()
        if any(token in detail for token in ("image", "vision", "multimodal", "unsupported", "invalid content")):
            result = await run_vision_chat(content, content_prompt=prompt)
            return str(result.get("vision_text") or "").strip() or "The vision fallback model returned no usable image analysis."
        raise
    response_text = str((response.get("content") if isinstance(response.get("content"), str) else "") or "").strip()
    if response_text:
        return response_text
    parts: list[str] = []
    if isinstance(response.get("content"), list):
        for item in response.get("content") or []:
            if isinstance(item, dict) and item.get("type") == "text":
                parts.append(str(item.get("text") or ""))
    merged = "".join(parts).strip()
    return merged or "The model returned no usable image analysis."


async def _persist_direct_image_chat(
    message: str,
    response: str,
    public_attachments: list[dict[str, Any]],
    client_request_id: str,
) -> None:
    round_id = f"round_{int(time.time() * 1000)}"
    user_entry: dict[str, Any] = {
        "role": "user",
        "content": str(message or ""),
        "attachments": [dict(item) for item in public_attachments],
        "round_id": round_id,
    }
    if client_request_id:
        user_entry["client_request_id"] = client_request_id
    await _append_session_message(user_entry)
    await append_system_message(
        response,
        message_meta={
            "system_initiated": False,
            "round_id": round_id,
            **({"client_request_id": client_request_id} if client_request_id else {}),
        },
        publish_event={
            "type": "chat_message",
            "round_id": round_id,
            "client_request_id": client_request_id,
        },
    )


def register_routes(app, bot: Any, db_path: str) -> None:
    global _bot, _db_path
    _bot = bot
    _db_path = db_path

    router = APIRouter()
    register_map_routes(router)

    # ---- SPA root ----

    @router.get("/", response_class=HTMLResponse)
    async def spa_root():
        return FileResponse(
            _APP_DIR / "index.html",
            headers={
                "Cache-Control": "no-store, no-cache, must-revalidate",
                "Pragma": "no-cache",
                "Expires": "0",
            },
        )

    # ---- UI bootstrap data ----

    @router.get("/api/ui-data")
    async def api_ui_data(tz: str = ""):
        return await _build_ui_data(tz)

    # ---- Chat API ----

    @router.post("/api/chat/upload")
    async def api_chat_upload(files: list[UploadFile]):
        if not files:
            return JSONResponse({"error": "no files uploaded"}, status_code=400)

        _UPLOADS_DIR.mkdir(parents=True, exist_ok=True)
        uploaded: list[dict[str, Any]] = []
        now = datetime.now().strftime("%Y%m%d_%H%M%S")

        for index, file in enumerate(files, start=1):
            safe_name = _safe_upload_name(file.filename or "")
            target = _UPLOADS_DIR / f"{now}_{index:02d}_{safe_name}"
            content = await file.read()
            target.write_bytes(content)
            content_type = str(file.content_type or mimetypes.guess_type(str(target))[0] or "application/octet-stream")
            kind = attachment_kind_from_meta(content_type, target.name)
            width, height = _image_dimensions(target) if kind == "image" else (None, None)
            uploaded.append({
                "id": target.name,
                "name": file.filename or safe_name,
                "path": str(target.resolve()),
                "content_type": content_type,
                "size": len(content),
                "kind": kind,
                "url": f"/api/chat/upload/{target.name}",
                **({"width": width} if isinstance(width, int) else {}),
                **({"height": height} if isinstance(height, int) else {}),
            })

        return {"files": uploaded}

    @router.get("/api/chat/upload/{upload_id}")
    async def api_chat_upload_file(upload_id: str):
        safe_upload_id = _safe_upload_name(upload_id)
        target = (_UPLOADS_DIR / safe_upload_id).resolve()
        uploads_root = _UPLOADS_DIR.resolve()
        if target != uploads_root and uploads_root not in target.parents:
            return JSONResponse({"error": "invalid upload path"}, status_code=400)
        if not target.exists() or not target.is_file():
            return JSONResponse({"error": "upload not found"}, status_code=404)
        return FileResponse(target)

    @router.get("/api/chat/export/{export_id}")
    async def api_chat_export_file(export_id: str):
        safe_export_id = _safe_upload_name(export_id)
        target = (_EXPORTS_DIR / safe_export_id).resolve()
        exports_root = _EXPORTS_DIR.resolve()
        if target != exports_root and exports_root not in target.parents:
            return JSONResponse({"error": "invalid export path"}, status_code=400)
        if not target.exists() or not target.is_file():
            return JSONResponse({"error": "export not found"}, status_code=404)
        return FileResponse(target)

    @router.post("/api/chat")
    async def api_chat(request: Request):
        body = await request.json()
        message = (body.get("message") or "").strip()
        attachments = body.get("attachments") if isinstance(body.get("attachments"), list) else []
        guide_round_id = str(body.get("guide_round_id") or "").strip()
        client_request_id = str(body.get("client_request_id") or "").strip()
        wants_stream = bool(body.get("stream"))
        lang = str(body.get("lang") or "").strip()
        command = str(body.get("command") or "").strip()
        mentions = body.get("mentions") if isinstance(body.get("mentions"), list) else []
        retry = bool(body.get("retry"))
        retry_request_id = str(body.get("retry_request_id") or "").strip()
        if retry and retry_request_id:
            await _remove_messages_by_request_id(retry_request_id)
        normalized_attachments = [
            {
                "id": str(item.get("id") or "").strip(),
                "name": str(item.get("name") or "file"),
                "path": str(item.get("path") or ""),
                "content_type": str(item.get("content_type") or "application/octet-stream"),
                "size": int(item.get("size") or 0),
                "kind": str(item.get("kind") or "file"),
                **({"width": int(item.get("width"))} if str(item.get("width", "")).strip().isdigit() else {}),
                **({"height": int(item.get("height"))} if str(item.get("height", "")).strip().isdigit() else {}),
            }
            for item in attachments
            if str(item.get("path") or "").strip()
        ]
        public_attachments = [build_public_attachment_payload(item) for item in normalized_attachments]
        if not message and not normalized_attachments:
            return JSONResponse({"error": "empty message"}, status_code=400)
        all_images = bool(normalized_attachments) and all(str(item.get("kind") or "") == "image" for item in normalized_attachments)
        message_with_attachments = (message or "[Attachment upload]") + _attachment_prompt_block(normalized_attachments)

        reset_lottery()
        if mentions and message:
            from cyrene.inbox import send_message
            from cyrene.subagent import _registry, reactivate, get_raw_messages, _spawn_subagent_task, _run_subagent

            valid_mentions = []
            for agent_id in mentions:
                agent_id = str(agent_id).strip()
                if not agent_id:
                    continue
                info = _registry.get(agent_id)
                if info is None:
                    continue
                valid_mentions.append(agent_id)
                status = str(info.get("status", "")).strip()
                if status in ("done", "timeout"):
                    mention_text = f"User sent you a new task. This is a round — complete it and report your result via quit.\n\n{message}"
                    await send_message("user", agent_id, "guidance", mention_text)
                    reactivated = await reactivate(agent_id)
                    if reactivated:
                        raw_msgs = await get_raw_messages(agent_id)
                        _spawn_subagent_task(
                            _run_subagent(agent_id, str(info.get("task") or ""), _bot, _CHAT_ID, _db_path, resume_messages=raw_msgs),
                            agent_id,
                        )
                else:
                    mention_text = (
                        f"[DIRECT_MESSAGE]\n"
                        f"The user has sent you guidance. This takes priority over your current approach — "
                        f"adjust your work accordingly. Use send_message_to_user ONCE to acknowledge and "
                        f"briefly say what you will change. Then continue working with the adjusted approach.\n\n"
                        f"User guidance:\n{message}"
                    )
                    await send_message("user", agent_id, "guidance", mention_text)

            if not valid_mentions:
                return JSONResponse({"error": "none of the mentioned agents exist"}, status_code=400)

            names = ", ".join(["@" + aid for aid in valid_mentions])
            response_text = f"Message sent to {names}."
            mention_prefix = " ".join(["@" + aid for aid in valid_mentions]) + " "

            user_entry = {
                "role": "user",
                "content": mention_prefix + message,
                "mentions": valid_mentions,
            }
            if normalized_attachments:
                user_entry["attachments"] = public_attachments
            if client_request_id:
                user_entry["client_request_id"] = client_request_id
            await _append_session_message(user_entry)

            if wants_stream:
                return StreamingResponse(
                    iter([_ndjson_line({"type": "reply_done", "response": response_text})]),
                    media_type="application/x-ndjson",
                    headers={"Cache-Control": "no-cache"},
                )
            return {"response": response_text}
        if guide_round_id:
            try:
                item = await queue_round_guidance(
                    guide_round_id,
                    message_with_attachments,
                    _bot,
                    _CHAT_ID,
                    _db_path,
                    client_request_id=client_request_id,
                )
            except ValueError as exc:
                return JSONResponse({"error": str(exc)}, status_code=400)
            payload = {
                "response": f"Sent to the main-agent inbox for {guide_round_id}. It will run after the current main-agent output finishes.",
                "queued": True,
                "guide_round_id": guide_round_id,
                "guide_request_id": item.get("id", ""),
            }
            if wants_stream:
                return StreamingResponse(
                    iter([_ndjson_line({"type": "queued", **payload})]),
                    media_type="application/x-ndjson",
                    headers={"Cache-Control": "no-cache"},
                )
            return payload

        try:
            if all_images:
                async def _run_direct_image_chat() -> str:
                    response_text = await _chat_with_uploaded_images(message, normalized_attachments)
                    await _persist_direct_image_chat(message, response_text, public_attachments, client_request_id)
                    labels = get_session_labels()
                    await archive_exchange(
                        message,
                        response_text,
                        _CHAT_ID,
                        session_title=labels.get("session_title", ""),
                        round_title=labels.get("round_title", ""),
                        round_id=labels.get("round_id", ""),
                        archive_session_id=labels.get("archive_session_id", ""),
                    )
                    return response_text

                if wants_stream:
                    return _stream_agent_reply(_run_direct_image_chat, message or "")
                return {"response": await _run_direct_image_chat()}
            if wants_stream:
                return _stream_agent_reply(
                    lambda: run_agent(
                        message_with_attachments,
                        _bot,
                        _CHAT_ID,
                        _db_path,
                        client_request_id=client_request_id,
                        lang=lang,
                        command=command,
                        public_user_message=message,
                        public_attachments=public_attachments,
                    ),
                    message or "",
                )
            response = await run_agent(
                message_with_attachments,
                _bot,
                _CHAT_ID,
                _db_path,
                client_request_id=client_request_id,
                lang=lang,
                command=command,
                public_user_message=message,
                public_attachments=public_attachments,
            )
            if response == _AWAITING_USER_SENTINEL:
                return {"awaiting_user": True, "pending_question": get_pending_question()}
            labels = get_session_labels()
            await archive_exchange(
                message,
                response,
                _CHAT_ID,
                session_title=labels.get("session_title", ""),
                round_title=labels.get("round_title", ""),
                round_id=labels.get("round_id", ""),
                archive_session_id=labels.get("archive_session_id", ""),
            )
            return {"response": response}
        except httpx.TimeoutException as exc:
            logger.exception(
                "Chat request timed out while calling upstream model: %s",
                format_httpx_error(exc),
            )
            return JSONResponse(
                {"error": "upstream model timed out", "detail": str(exc)},
                status_code=504,
            )
        except httpx.HTTPError as exc:
            logger.exception(
                "Chat request failed while calling upstream model: %s",
                format_httpx_error(exc),
            )
            return JSONResponse(
                {"error": "upstream model request failed", "detail": str(exc)},
                status_code=502,
            )
        except Exception as exc:
            logger.exception("Chat request crashed")
            return JSONResponse(
                {"error": "internal server error", "detail": str(exc)},
                status_code=500,
            )

    @router.post("/api/chat/answer-question")
    async def api_answer_question(request: Request):
        body = await request.json()
        question_id = str(body.get("question_id") or "").strip()
        selected_option = str(body.get("selected_option") or "").strip()
        answer_text = str(body.get("answer") or "").strip() or selected_option
        client_request_id = str(body.get("client_request_id") or "").strip()
        wants_stream = bool(body.get("stream"))
        if not question_id:
            return JSONResponse({"error": "missing question_id"}, status_code=400)
        if not answer_text:
            return JSONResponse({"error": "empty answer"}, status_code=400)

        try:
            if wants_stream:
                return _stream_agent_reply(
                    lambda: answer_pending_question(
                        question_id,
                        answer_text,
                        _bot,
                        _CHAT_ID,
                        _db_path,
                        client_request_id=client_request_id,
                    ),
                    answer_text,
                )
            response = await answer_pending_question(
                question_id,
                answer_text,
                _bot,
                _CHAT_ID,
                _db_path,
                client_request_id=client_request_id,
            )
            if response == _AWAITING_USER_SENTINEL:
                return {"awaiting_user": True, "pending_question": get_pending_question()}
            labels = get_session_labels()
            await archive_exchange(
                answer_text,
                response,
                _CHAT_ID,
                session_title=labels.get("session_title", ""),
                round_title=labels.get("round_title", ""),
                round_id=labels.get("round_id", ""),
                archive_session_id=labels.get("archive_session_id", ""),
            )
            return {"response": response}
        except ValueError as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)
        except httpx.TimeoutException as exc:
            logger.exception(
                "Question-answer request timed out while calling upstream model: %s",
                format_httpx_error(exc),
            )
            return JSONResponse(
                {"error": "upstream model timed out", "detail": str(exc)},
                status_code=504,
            )
        except httpx.HTTPError as exc:
            logger.exception(
                "Question-answer request failed while calling upstream model: %s",
                format_httpx_error(exc),
            )
            return JSONResponse(
                {"error": "upstream model request failed", "detail": str(exc)},
                status_code=502,
            )
        except Exception as exc:
            logger.exception("Question-answer request crashed")
            return JSONResponse(
                {"error": "internal server error", "detail": str(exc)},
                status_code=500,
            )

    @router.get("/api/chat/history")
    async def api_chat_history():
        return {"messages": _load_messages()}

    @router.get("/api/chat/state")
    async def api_chat_state():
        """Return raw session state (with round_id, tool_calls, etc.)."""
        from cyrene.config import STATE_FILE as _STATE_FILE
        if _STATE_FILE.exists():
            import json as _json
            try:
                data = _json.loads(_STATE_FILE.read_text(encoding="utf-8"))
                msgs = data.get("messages", [])
                return {"messages": msgs if isinstance(msgs, list) else []}
            except Exception:
                pass
        return {"messages": []}

    @router.post("/api/chat/interrupt")
    async def api_interrupt_chat():
        return {"ok": True, "interrupted": interrupt_active_run()}

    @router.post("/api/chat/clear")
    async def api_clear_session():
        await clear_session_id()
        return {"ok": True}

    @router.get("/api/subagents")
    async def api_subagents():
        from cyrene.subagent import _registry  # noqa: WPS437
        items = []
        for agent_id, info in _registry.items():
            items.append({
                "id": agent_id,
                "name": agent_id,
                "task": info.get("task", ""),
                "status": info.get("status", "running"),
                "result": info.get("result", ""),
            })
        return {"subagents": items}

    @router.get("/api/rounds/live")
    async def api_live_rounds():
        return {"rounds": get_live_rounds()}

    # ---- Group chat ----

    @router.get("/api/chat/agent-chat-messages")
    async def api_agent_chat_messages(round_id: str = ""):
        from cyrene.subagent import build_group_chat_messages

        if not round_id:
            return {"messages": [], "agents": []}
        return await build_group_chat_messages(round_id)

    @router.post("/api/chat/send-to-agents")
    async def api_send_to_agents(body: dict[str, Any]):
        from cyrene.subagent import _registry as _sub_reg
        from cyrene.inbox import send_message as _send_inbox, clear_inbox as _clear_inbox
        from cyrene import debug as _debug_comm

        round_id = str(body.get("round_id", "") or "").strip()
        text = str(body.get("text", "") or "").strip()
        mentions = body.get("mentions")
        attachments = body.get("attachments") or []

        if not round_id or not text:
            return {"ok": False, "error": "round_id and text are required"}

        # Build the full message text (append file references)
        full_text = text
        for att in attachments:
            path = str(att.get("path", "") or "").strip()
            name = str(att.get("name", "") or "").strip()
            if path:
                full_text += f"\n\n[{name}]({path})" if name else f"\n\n{path}"

        # Determine target agents
        if mentions and isinstance(mentions, list):
            targets = [str(m).strip() for m in mentions if str(m).strip()]
        else:
            # Send to all active subagents in this round
            from cyrene.subagent import _lock as _reg_lock

            async with _reg_lock:
                targets = [
                    aid for aid, info in _sub_reg.items()
                    if round_id and str(info.get("round_id", "") or "").strip() == round_id
                    and aid != "main"
                ]

        if not targets:
            return {"ok": False, "error": "No target agents found"}

        sent_to: list[str] = []
        first_msg_id = ""
        for target in targets:
            info = _sub_reg.get(target)
            is_done_timeout = info and str(info.get("status", "")).strip() in ("done", "timeout")

            if is_done_timeout:
                wrapped = f"User sent you a new task. This is a round — complete it and report your result via quit.\n\n{full_text}"
            else:
                wrapped = (
                    f"[DIRECT_MESSAGE]\n"
                    f"The user has sent you guidance. This takes priority over your current approach — "
                    f"adjust your work accordingly. Use send_message_to_user ONCE to acknowledge and "
                    f"briefly say what you will change. Then continue working with the adjusted approach.\n\n"
                    f"User guidance:\n{full_text}"
                )

            # 清空 inbox 确保 subagent 只看到这条用户消息
            await _clear_inbox(target)

            msg_id = await _send_inbox(
                from_agent="user",
                to_agent=target,
                msg_type="guidance",
                content=wrapped,
                round_id=round_id,
            )
            if msg_id:
                sent_to.append(target)
                if not first_msg_id:
                    first_msg_id = msg_id
                # Handle DONE/TIMEOUT agents: reactivate + spawn new task
                if is_done_timeout:
                    from cyrene.subagent import (
                        reactivate as _reactivate,
                        get_raw_messages as _get_raw,
                        _spawn_subagent_task,
                        _run_subagent,
                    )

                    reactivated = await _reactivate(target)
                    if reactivated:
                        raw_msgs = await _get_raw(target)
                        _spawn_subagent_task(
                            _run_subagent(target, str(info.get("task") or ""), _bot, _CHAT_ID, _db_path, resume_messages=raw_msgs),
                            target,
                        )

        # Publish SSE event for real-time group-chat update
        await _debug_comm.publish_event({
            "type": "agent_chat_user_message",
            "round_id": round_id,
            "message": {
                "id": first_msg_id or f"user_msg_{int(time.time() * 1000)}",
                "type": "user_message",
                "from": "user",
                "to": "all" if not mentions else ",".join(mentions),
                "content": text,
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "round_id": round_id,
            },
        })

        return {"ok": True, "sent_to": sent_to}

    # ---- SSE ----

    @router.get("/api/events")
    async def api_events(request: Request):
        from cyrene.debug import subscribe

        async def event_stream():
            async for event in subscribe():
                if await request.is_disconnected():
                    break
                yield f"data: {json.dumps(event, ensure_ascii=False)}\n\n"

        return StreamingResponse(
            event_stream(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",
            },
        )

    @router.get("/api/events/list")
    async def api_events_list():
        """List recent event IDs."""
        from cyrene.debug import get_recent_events
        events = get_recent_events(50)
        result = []
        for e in events:
            eid = e.get("event_id", "")
            if eid:
                result.append({"id": eid, "type": e.get("type", "?"), "caller": e.get("caller", "?")})
        return {"events": result}

    @router.get("/api/events/{event_id}")
    async def api_event_detail(event_id: str):
        from cyrene.debug import get_full_event
        event = get_full_event(event_id)
        if event is None:
            return JSONResponse({"error": "event not found"}, status_code=404)
        return event

    # ---- Claude Code terminal / learning ----

    @router.get("/api/cc/status")
    async def api_cc_status():
        return get_cc_status(_CC_PROJECT_DIR)

    @router.get("/api/status")
    async def api_status():
        return await _build_status()

    async def _build_cc_learning_snapshot() -> dict[str, Any]:
        status = get_cc_status(_CC_PROJECT_DIR)
        latest_jsonl = str(status.get("latest_jsonl") or "").strip()
        if not latest_jsonl:
            return {
                "available": False,
                "reason": "No Claude transcript found for learning.",
                "summary": {"highlights": [], "top_tools": [], "top_tasks": []},
            }
        analysis = await asyncio.to_thread(analyze_session, Path(latest_jsonl))
        return {
            "available": True,
            **analysis,
        }

    @router.get("/api/cc/learning")
    async def api_cc_learning():
        return await _build_cc_learning_snapshot()

    @router.post("/api/cc/learn")
    async def api_cc_learn():
        status = get_cc_status(_CC_PROJECT_DIR)
        latest_jsonl = str(status.get("latest_jsonl") or "").strip()
        if not latest_jsonl:
            return JSONResponse({"error": "no Claude transcript found"}, status_code=404)
        result = await asyncio.to_thread(learn_from_session, Path(latest_jsonl))
        await debug.publish_event(
            {
                "type": "cc_learning",
                "phase": "completed",
                "user_input": "",
                "latest_jsonl": latest_jsonl,
                "highlights": result.get("summary", {}).get("highlights", []),
                "top_tools": result.get("summary", {}).get("top_tools", []),
                "top_tasks": result.get("summary", {}).get("top_tasks", []),
            }
        )
        return result

    @router.websocket("/ws/cc-terminal/{tmux_session}")
    async def ws_cc_terminal(websocket: WebSocket, tmux_session: str):
        await websocket.accept()
        session = CCTerminalSession(tmux_session)
        input_buffer = ""

        try:
            await session.start()
        except Exception:
            logger.exception("Failed to attach CC terminal to tmux session %s", tmux_session)
            await websocket.send_text("\r\n[Cyrene] Failed to attach to tmux session.\r\n")
            await websocket.close(code=1011)
            return

        stream_task = asyncio.create_task(session.stream_to_ws(websocket))
        try:
            while True:
                raw = await websocket.receive_text()
                try:
                    payload = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                message_type = str(payload.get("type") or "").strip()
                if message_type == "input":
                    data = str(payload.get("data") or "")
                    await session.handle_input(data)
                    input_buffer, submitted = _consume_cc_input_buffer(input_buffer, data)
                    for prompt in submitted:
                        asyncio.create_task(_publish_cc_learning(prompt, tmux_session=tmux_session))
                elif message_type == "resize":
                    await session.handle_resize(int(payload.get("cols") or 80), int(payload.get("rows") or 24))
        except WebSocketDisconnect:
            pass
        finally:
            stream_task.cancel()
            await session.stop()

    # ---- Sessions API ----

    @router.get("/api/sessions")
    async def api_sessions():
        from cyrene import db as cy_db
        try:
            now_local = datetime.now(timezone.utc).astimezone()
            day_from = (now_local - timedelta(days=27)).strftime("%Y-%m-%d")
            day_to = now_local.strftime("%Y-%m-%d")
            model_stats = await cy_db.get_model_stats_range(_db_path, day_from, day_to)
        except Exception:
            model_stats = []
        return {"sessions": _build_sessions(), "model_stats": model_stats}

    @router.post("/api/sessions")
    async def api_create_session():
        """Start a new session by clearing current state.

        Compresses the existing conversation into short-term memory first
        (handled inside clear_session_id), then wipes state.json so the
        next message starts a fresh context window.
        """
        await clear_session_id()
        return {"ok": True, "sessions": _build_sessions()}

    @router.get("/api/sessions/archive-context")
    async def api_archive_context(cursor: str = ""):
        """Return the next archive session after *cursor*.

        Cursor is a full archive session id (``archive_YYYY-MM-DD_<id>``).
        When empty, returns the most recent archive session.
        Each message has ``isArchivedContext: true`` so the frontend can
        style it as read‑only historical context.

        Skips the current live session's own archive to avoid showing
        the same messages that are already in the live view.
        """
        # Skip the archive that belongs to the current live session
        current_skip_ids: set[str] = set()
        if STATE_FILE.exists():
            try:
                state = json.loads(STATE_FILE.read_text(encoding="utf-8"))
                caid = str(state.get("archive_session_id", "")).strip()
                cad = datetime.now().astimezone().strftime("%Y-%m-%d")
                if caid:
                    current_skip_ids.add(f"{cad}:{caid}")
            except Exception:
                pass

        archives = _build_archive_sessions(skip_archive_ids=current_skip_ids)
        if not archives:
            return {"messages": [], "hasMore": False}

        start = 0
        if cursor.strip():
            for idx, a in enumerate(archives):
                if a.get("id") == cursor.strip():
                    start = idx + 1
                    break
            else:
                return {"messages": [], "hasMore": False}

        if start >= len(archives):
            return {"messages": [], "hasMore": False}

        target = archives[start]
        raw_messages = target.get("chat", {}).get("messages", [])
        for msg in raw_messages:
            msg["isArchivedContext"] = True

        return {
            "messages": raw_messages,
            "id": target["id"],
            "archiveSessionId": target.get("archiveSessionId", ""),
            "archiveDate": target.get("archiveDate", ""),
            "title": target.get("title", ""),
            "hasMore": (start + 1) < len(archives),
        }

    @router.delete("/api/sessions/{session_id}")
    async def api_delete_session(session_id: str):
        """Delete a session.

        - run_live: same as create (clear current state).
        - archive_YYYY-MM-DD_<session_id>: deletes one archived session from that day.
        """
        if session_id == "run_live":
            await clear_session_id()
            return {"ok": True, "sessions": _build_sessions()}

        if session_id.startswith("archive_"):
            suffix = session_id[len("archive_"):]
            date_str, _, archive_session_id = suffix.partition("_")
            filepath = CONVERSATIONS_DIR / f"{date_str}.md"
            if not filepath.exists():
                return JSONResponse({"error": "session not found"}, status_code=404)
            try:
                content = filepath.read_text(encoding="utf-8")
                sections = _parse_archive_sections(content)
                kept_sections = [
                    section for section in sections
                    if str(section.get("archive_session_id", "")).strip() != archive_session_id
                ]
                if len(kept_sections) == len(sections):
                    return JSONResponse({"error": "session not found"}, status_code=404)
                _write_archive_sections(filepath, date_str, kept_sections)
            except Exception as e:
                return JSONResponse({"error": str(e)}, status_code=500)
            return {"ok": True, "sessions": _build_sessions()}

        return JSONResponse({"error": "unknown session id"}, status_code=400)

    # ---- Evolution API ----

    @router.get("/api/evolution")
    async def api_evolution():
        """Aggregated data for the Evolution page."""
        from cyrene import pattern as _pattern
        status, scripts, patterns, learned_skills, cc_learning = await asyncio.gather(
            _build_status(),
            _pattern.list_scripts("all"),
            _pattern.list_patterns("all"),
            _pattern.list_learned_skills(),
            _build_cc_learning_snapshot(),
        )
        return {
            "phase": status.get("phase", ""),
            "state": status.get("state", ""),
            "scripts": scripts,
            "patterns": patterns,
            "learned_skills": learned_skills,
            "cc_learning": cc_learning,
        }

    @router.get("/api/scripts")
    async def api_scripts(status: str = "all"):
        from cyrene import pattern as _pattern
        return {"scripts": await _pattern.list_scripts(status)}

    @router.get("/api/patterns")
    async def api_patterns(status: str = "all"):
        from cyrene import pattern as _pattern
        return {"patterns": await _pattern.list_patterns(status)}

    @router.get("/api/learned-skills")
    async def api_learned_skills():
        from cyrene import pattern as _pattern
        return {"skills": await _pattern.list_learned_skills()}

    @router.get("/api/learned-skills/{skill_id}")
    async def api_learned_skill_detail(skill_id: str):
        from cyrene import pattern as _pattern
        skill = await _pattern.get_learned_skill(skill_id)
        if skill is None:
            return JSONResponse({"error": "skill not found"}, status_code=404)
        return {"skill": skill}

    @router.get("/api/learned-skills/{skill_id}/versions")
    async def api_learned_skill_versions(skill_id: str):
        from cyrene import pattern as _pattern
        return {"versions": await _pattern.list_learned_skill_versions(skill_id)}

    @router.get("/api/learned-skills/{skill_id}/patches")
    async def api_learned_skill_patches(skill_id: str, status: str = "all"):
        from cyrene import pattern as _pattern
        return {"patches": await _pattern.list_learned_skill_patches(skill_id, status)}

    @router.get("/api/learned-skills/{skill_id}/runs")
    async def api_learned_skill_runs(skill_id: str, limit: int = 50):
        from cyrene import pattern as _pattern
        return {"runs": await _pattern.list_learned_skill_runs(skill_id, limit)}

    @router.get("/api/learned-skills/{skill_id}/replay-tests")
    async def api_learned_skill_replay_tests(skill_id: str):
        from cyrene import pattern as _pattern
        return {"tests": await _pattern.list_skill_replay_tests(skill_id)}

    @router.post("/api/learned-skills/{skill_id}/update")
    async def api_update_learned_skill(skill_id: str, request: Request):
        from cyrene import pattern as _pattern

        payload = await request.json()
        updates = payload.get("updates") if isinstance(payload, dict) else None
        reason = str((payload or {}).get("reason") or "Manual skill edit.")
        result = await _pattern.update_learned_skill(skill_id, updates if isinstance(updates, dict) else {}, reason=reason)
        if result is None:
            return JSONResponse({"error": "skill not found or invalid payload"}, status_code=404)
        return {"ok": True, "skill": result}

    @router.post("/api/learned-skills/{skill_id}/rollback")
    async def api_rollback_learned_skill(skill_id: str, request: Request):
        from cyrene import pattern as _pattern

        payload = await request.json()
        version = int((payload or {}).get("version") or 0)
        result = await _pattern.rollback_learned_skill(skill_id, version)
        if not result.get("ok"):
            return JSONResponse(result, status_code=404)
        return result

    @router.post("/api/learned-skills/{skill_id}/replay-tests/run")
    async def api_run_learned_skill_replay_tests(skill_id: str):
        from cyrene import pattern as _pattern
        result = await _pattern.run_skill_replay_tests(skill_id)
        return {"ok": True, "result": result}

    @router.post("/api/learned-skills/{skill_id}/patches/{patch_id}/apply")
    async def api_apply_learned_skill_patch(skill_id: str, patch_id: str):
        from cyrene import pattern as _pattern
        result = await _pattern.apply_skill_patch(skill_id, patch_id)
        if not result.get("ok"):
            return JSONResponse(result, status_code=400)
        return result

    @router.post("/api/learned-skills/{skill_id}/patches/{patch_id}/reject")
    async def api_reject_learned_skill_patch(skill_id: str, patch_id: str):
        from cyrene import pattern as _pattern
        ok = await _pattern.reject_skill_patch(skill_id, patch_id)
        if not ok:
            return JSONResponse({"error": "patch not found"}, status_code=404)
        return {"ok": True}

    @router.post("/api/learned-skills/{skill_id}/activate")
    async def api_activate_learned_skill(skill_id: str):
        from cyrene import pattern as _pattern
        ok = await _pattern.approve_script(skill_id)
        return {"ok": ok}

    @router.post("/api/learned-skills/{skill_id}/deprecate")
    async def api_deprecate_learned_skill(skill_id: str):
        from cyrene import pattern as _pattern
        ok = await _pattern.reject_script(skill_id)
        return {"ok": ok}

    @router.post("/api/learned-skills/{skill_id}/run")
    async def api_run_learned_skill(skill_id: str):
        from cyrene import pattern as _pattern
        result = await _pattern.run_script(skill_id)
        return {"ok": True, "result": result}

    @router.post("/api/scripts/{script_id}/approve")
    async def api_approve_script(script_id: str):
        from cyrene import pattern as _pattern
        ok = await _pattern.approve_script(script_id)
        return {"ok": ok}

    @router.post("/api/scripts/{script_id}/reject")
    async def api_reject_script(script_id: str):
        from cyrene import pattern as _pattern
        ok = await _pattern.reject_script(script_id)
        return {"ok": ok}

    @router.post("/api/scripts/{script_id}/run")
    async def api_run_script(script_id: str):
        from cyrene import pattern as _pattern
        result = await _pattern.run_script(script_id)
        return {"ok": True, "result": result}

    @router.post("/api/patterns/learn")
    async def api_patterns_learn():
        from cyrene import pattern as _pattern

        stats = await _pattern.scan_for_manual_learn()
        return {
            "ok": True,
            "stats": stats,
            "patterns": await _pattern.list_patterns("all"),
            "learned_skills": await _pattern.list_learned_skills(),
            "scripts": await _pattern.list_scripts("all"),
        }

    @router.post("/api/patterns/rebuild")
    async def api_patterns_rebuild():
        from cyrene import pattern as _pattern

        result = await _pattern.rebuild_learning_state(reprocess_all_turns=True)
        return {
            "ok": True,
            "result": result,
            "patterns": await _pattern.list_patterns("all"),
            "learned_skills": await _pattern.list_learned_skills(),
            "scripts": await _pattern.list_scripts("all"),
        }

    @router.get("/api/vocabulary")
    async def api_vocabulary():
        from cyrene import pattern as _pattern
        return await _pattern.vocabulary_snapshot()

    @router.post("/api/vocabulary/labels")
    async def api_create_vocabulary_label(request: Request):
        from cyrene import pattern as _pattern

        payload = await request.json()
        try:
            result = await _pattern.create_vocabulary_label(
                label_type=str((payload or {}).get("label_type") or ""),
                canonical_label=str((payload or {}).get("canonical_label") or ""),
                domain=str((payload or {}).get("domain") or ""),
                parent_label=str((payload or {}).get("parent_label") or ""),
                raw_description=str((payload or {}).get("raw_description") or ""),
                status=str((payload or {}).get("status") or "active"),
            )
        except Exception as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)
        return {"ok": True, "label": result}

    @router.post("/api/vocabulary/aliases")
    async def api_create_vocabulary_alias(request: Request):
        from cyrene import pattern as _pattern

        payload = await request.json()
        try:
            result = await _pattern.create_vocabulary_alias(
                label_type=str((payload or {}).get("label_type") or ""),
                canonical_label=str((payload or {}).get("canonical_label") or ""),
                alias_label=str((payload or {}).get("alias_label") or ""),
            )
        except Exception as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)
        return {"ok": True, "alias": result}

    @router.post("/api/vocabulary/unknown/{unknown_id}/promote")
    async def api_promote_unknown_label(unknown_id: str, request: Request):
        from cyrene import pattern as _pattern

        payload = await request.json()
        try:
            result = await _pattern.promote_unknown_label(
                unknown_id,
                canonical_label=str((payload or {}).get("canonical_label") or ""),
                alias_label=str((payload or {}).get("alias_label") or ""),
            )
        except Exception as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)
        return {"ok": True, "unknown": result}

    @router.post("/api/vocabulary/unknown/{unknown_id}/dismiss")
    async def api_dismiss_unknown_label(unknown_id: str):
        from cyrene import pattern as _pattern
        ok = await _pattern.dismiss_unknown_label(unknown_id)
        if not ok:
            return JSONResponse({"error": "unknown label not found"}, status_code=404)
        return {"ok": True}

    # ---- Skills install API ----

    @router.get("/api/skills/installed")
    async def api_installed_skills():
        return {"skills": _build_skills()}

    @router.post("/api/skills/install")
    async def api_install_skill(request: Request):
        body = await request.json()
        source_path = Path(str(body.get("path") or "")).expanduser()
        if not source_path.exists():
            return JSONResponse({"ok": False, "error": "invalid skill source path"}, status_code=400)
        result = install_skill_from_path(source_path)
        if not result.get("ok", False):
            return JSONResponse(result, status_code=400)
        return result

    @router.post("/api/skills/install-upload")
    async def api_install_skill_upload(request: Request):
        """Install a skill from an uploaded file (browser file picker path)."""
        import tempfile

        try:
            form = await request.form()
            file = form.get("file")
            if not file:
                return JSONResponse({"ok": False, "error": "No file provided"}, status_code=400)
            content = await file.read()
            if len(content) > 8 * 1024 * 1024:  # 8 MB (matches _MAX_SKILL_ARCHIVE_BYTES)
                return JSONResponse({"ok": False, "error": "File too large (max 8 MB)"}, status_code=400)
            suffix = Path(file.filename or "skill.tmp").suffix or ".tmp"
            with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp:
                tmp.write(content)
                tmp_path = tmp.name
            try:
                result = install_skill_from_path(Path(tmp_path))
                if not result.get("ok", False):
                    return JSONResponse(result, status_code=400)
                return result
            finally:
                Path(tmp_path).unlink(missing_ok=True)
        except Exception as e:
            return JSONResponse({"ok": False, "error": str(e)}, status_code=500)

    @router.post("/api/skills/install-picker")
    async def api_install_skill_picker():
        import platform
        import subprocess

        system = platform.system()
        if system != "Darwin":
            return JSONResponse({"ok": False, "error": f"Skill picker not supported on {system}"}, status_code=400)

        try:
            result = subprocess.run(
                ["osascript", "-e",
                 'POSIX path of (choose folder with prompt "Select skill folder containing SKILL.md")'],
                capture_output=True,
                text=True,
                timeout=120,
            )
        except subprocess.TimeoutExpired:
            return JSONResponse({"ok": False, "error": "Picker timed out — please try again"}, status_code=400)
        except Exception as e:
            return JSONResponse({"ok": False, "error": f"Picker error: {e}"}, status_code=400)

        stderr = (result.stderr or "").strip()
        if stderr and "User cancelled" not in stderr:
            return JSONResponse({"ok": False, "error": f"Picker error: {stderr}"}, status_code=400)

        selected = result.stdout.strip()
        if not selected:
            return {"ok": False, "cancelled": True}

        source_path = Path(selected).expanduser()
        if not source_path.exists():
            return JSONResponse({"ok": False, "error": "selected skill source is invalid"}, status_code=400)

        result = install_skill_from_path(source_path)
        if not result.get("ok", False):
            return JSONResponse(result, status_code=400)
        return result

    @router.post("/api/skills/{skill_id}/toggle")
    async def api_toggle_skill(skill_id: str):
        if not _toggle_skill(skill_id):
            return JSONResponse({"ok": False, "error": "skill not found"}, status_code=404)
        return {"ok": True}

    @router.post("/api/skills/{skill_id}/uninstall")
    async def api_uninstall_skill(skill_id: str):
        if not _uninstall_skill(skill_id):
            return JSONResponse({"ok": False, "error": "skill not found"}, status_code=404)
        return {"ok": True}

    # ---- Search API ----

    @router.get("/api/search/conversations")
    async def api_search_conversations(q: str = "", limit: int = 30):
        if not q.strip():
            return {"ok": False, "error": "query is required"}
        results = await search_conversations_structured(q.strip(), limit=max(1, min(limit, 100)))
        return {"ok": True, "results": results}

    # ---- Token Usage API ----

    @router.get("/api/usage/tokens")
    async def api_token_usage(days: int = 7, model: str = ""):
        from cyrene.db import get_token_usage_stats
        stats = await get_token_usage_stats(str(DB_PATH), days=max(1, min(days, 90)), model=model.strip())
        return {"ok": True, "stats": stats}

    # ---- Backup API ----

    @router.get("/api/backup/list")
    async def api_backup_list():
        from cyrene.backup import list_backups
        return {"ok": True, "backups": list_backups()}

    @router.post("/api/backup/export")
    async def api_backup_export():
        from cyrene.backup import export_backup
        result = await export_backup()
        return result

    @router.post("/api/backup/restore")
    async def api_backup_restore(request: Request):
        from cyrene.backup import restore_backup
        body = await request.json()
        path = str(body.get("path") or "").strip()
        if not path:
            return {"ok": False, "error": "path is required"}
        result = await restore_backup(path)
        return result

    @router.post("/api/backup/delete")
    async def api_backup_delete(request: Request):
        from cyrene.backup import delete_backup
        body = await request.json()
        name = str(body.get("name") or "").strip()
        if not name:
            return {"ok": False, "error": "name is required"}
        ok = await delete_backup(name)
        return {"ok": ok}

    @router.post("/api/backup/download/{backup_name}")
    async def api_backup_download(backup_name: str):
        from cyrene.backup import _BACKUP_DIR
        target = (_BACKUP_DIR / backup_name).resolve()
        backups_root = _BACKUP_DIR.resolve()
        if backups_root not in target.parents:
            return JSONResponse({"error": "invalid backup path"}, status_code=400)
        if not target.exists() or not target.is_file():
            return JSONResponse({"error": "backup not found"}, status_code=404)
        return FileResponse(target, filename=backup_name, media_type="application/zip")

    # ---- Notification API ----

    @router.post("/api/notifications/send")
    async def api_notifications_send(request: Request):
        from cyrene.notifications import notify
        body = await request.json()
        title = str(body.get("title") or "Cyrene").strip()
        text = str(body.get("text") or "").strip()
        channel = str(body.get("channel") or "auto").strip()
        if not text:
            return {"ok": False, "error": "text is required"}
        result = await notify(title, text, channel=channel)
        return result

    # ---- Browser API ----

    @router.post("/api/browser/navigate")
    async def api_browser_navigate(request: Request):
        from cyrene.browser import navigate
        body = await request.json()
        url = str(body.get("url") or "").strip()
        if not url:
            return {"ok": False, "error": "url is required"}
        result = await navigate(url)
        return result

    # ---- Memory API ----

    @router.get("/api/memory")
    async def api_memory():
        return await _build_memory()

    # ---- Skills API ----

    @router.get("/api/skills")
    async def api_skills():
        return {"skills": _build_skills()}

    # ---- Settings API ----

    @router.get("/api/onboarding")
    async def api_get_onboarding():
        return {"onboarding": get_onboarding_status()}

    @router.post("/api/onboarding/llm")
    async def api_onboarding_llm(request: Request):
        body = await request.json()
        try:
            return await save_and_test_llm_setup(
                str(body.get("api_key") or ""),
                str(body.get("base_url") or ""),
                str(body.get("model") or ""),
            )
        except ValueError as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)
        except httpx.TimeoutException as exc:
            return JSONResponse(
                {"error": "upstream model timed out", "detail": str(exc)},
                status_code=504,
            )
        except httpx.HTTPError as exc:
            return JSONResponse(
                {"error": "upstream model request failed", "detail": format_httpx_error(exc)},
                status_code=502,
            )

    @router.post("/api/onboarding/personality")
    async def api_onboarding_personality(request: Request):
        body = await request.json()
        try:
            return await save_personality_setup(
                str(body.get("mode") or ""),
                name=str(body.get("name") or ""),
                content=str(body.get("content") or ""),
            )
        except ValueError as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)
        except httpx.TimeoutException as exc:
            return JSONResponse(
                {"error": "upstream model timed out", "detail": str(exc)},
                status_code=504,
            )
        except httpx.HTTPError as exc:
            return JSONResponse(
                {"error": "upstream model request failed", "detail": format_httpx_error(exc)},
                status_code=502,
            )

    # ---- Context management (SOUL.md / workspace chips) ----

    @router.get("/api/context/state")
    async def api_context_state():
        from cyrene.settings_store import is_workspace_active, is_soul_active, get_workspace_history
        return {
            "soul_active": is_soul_active(),
            "workspace_active": is_workspace_active(),
            "workspace_dir": str(WORKSPACE_DIR),
            "workspace_history": get_workspace_history(),
        }

    @router.post("/api/context/remove-soul")
    async def api_remove_soul():
        from cyrene.settings_store import set_soul_active
        set_soul_active(False)
        return {"ok": True}

    @router.post("/api/context/add-soul")
    async def api_add_soul():
        from cyrene.settings_store import set_soul_active
        set_soul_active(True)
        return {"ok": True}

    @router.post("/api/context/remove-workspace")
    async def api_remove_workspace():
        from cyrene.settings_store import set_workspace_active
        set_workspace_active(False)
        return {"ok": True}

    @router.post("/api/context/add-workspace")
    async def api_add_workspace(request: Request):
        from cyrene.settings_store import set_workspace_active, add_workspace_to_history
        body = await request.json()
        path = str(body.get("path", "")).strip()
        set_workspace_active(True)
        if path:
            add_workspace_to_history(path)
        return {"ok": True}

    @router.post("/api/context/pick-directory")
    async def api_pick_directory():
        import platform
        import subprocess
        system = platform.system()
        if system == "Darwin":
            result = subprocess.run(
                ['osascript', '-e', 'POSIX path of (choose folder with prompt "Select workspace directory")'],
                capture_output=True, text=True, timeout=30,
            )
            path = result.stdout.strip()
            if path:
                return {"path": path}
            return {"path": "", "cancelled": True}
        return {"path": "", "error": f"Directory picker not supported on {system}"}

    @router.get("/api/settings/soul")
    async def api_get_soul():
        return {"content": _read_soul()}

    @router.put("/api/settings/soul")
    async def api_update_soul(request: Request):
        body = await request.json()
        SOUL_PATH.write_text(body.get("content", ""), encoding="utf-8")
        return {"ok": True}

    @router.get("/api/settings/keys")
    async def api_get_keys():
        from cyrene.config import get_env_keys_meta
        return {"keys": get_env_keys_meta()}

    @router.put("/api/settings/keys")
    async def api_update_keys(request: Request):
        from cyrene.config import write_env_keys, _EDITABLE_KEYS
        body = await request.json()
        updates = {}
        for key, meta in _EDITABLE_KEYS.items():
            value = body.get(key, "")
            if not value:
                continue
            # 跳过未修改的 masked 值（全为 • 或太短）
            if meta["masked"] and (value.startswith("••") or len(value) <= 8):
                continue
            updates[key] = value
        if not updates:
            return JSONResponse({"error": "no valid keys provided"}, status_code=400)
        write_env_keys(updates)
        return {"ok": True, "updated": list(updates.keys())}

    @router.get("/api/settings/models")
    async def api_get_models():
        from cyrene.settings_store import get_models, get_vision_models, get_secondary_model
        from cyrene.config import OPENAI_API_KEY, DEFAULT_OPENAI_BASE_URL, read_env_file

        def _normalize_candidates(raw_items: list[dict[str, Any]] | None, fallback_api_key: str, fallback_base_url: str) -> list[dict[str, Any]]:
            normalized_items: list[dict[str, Any]] = []
            for index, model in enumerate(raw_items or []):
                model_identifier = str(
                    model.get("model")
                    or model.get("name")
                    or model.get("id")
                    or ""
                ).strip()
                if not model_identifier:
                    continue
                normalized_items.append(
                    {
                        "id": str(model.get("id") or f"candidate-{index + 1}").strip() or f"candidate-{index + 1}",
                        "name": str(model.get("name") or model_identifier).strip() or model_identifier,
                        "model": model_identifier,
                        "desc": str(model.get("desc") or "").strip(),
                        "ctx": str(model.get("ctx") or "").strip(),
                        "price": str(model.get("price") or "").strip(),
                        "api_key": _strip_wrapping_quotes(str(model.get("api_key") or fallback_api_key).strip()),
                        "base_url": str(model.get("base_url") or fallback_base_url or DEFAULT_OPENAI_BASE_URL).strip() or DEFAULT_OPENAI_BASE_URL,
                    }
                )
            return normalized_items

        raw_models = get_models()
        raw_vision_models = get_vision_models()
        raw_secondary = get_secondary_model()
        active_model_name, base_url = _live_llm_config()
        env_keys = read_env_file()
        active_api_key = _strip_wrapping_quotes(str(env_keys.get("OPENAI_API_KEY") or OPENAI_API_KEY or "").strip())
        normalized = _normalize_candidates(raw_models, active_api_key, base_url)
        normalized_vision = _normalize_candidates(raw_vision_models, active_api_key, base_url)

        # Normalize secondary model (single item)
        sec_model = str(raw_secondary.get("model") or "").strip()
        ctx_limit = int(raw_secondary.get("ctx_limit") or 0)
        max_concurrency = int(raw_secondary.get("max_concurrency") or 0)
        if sec_model:
            normalized_secondary = {
                "id": "secondary",
                "name": str(raw_secondary.get("name") or sec_model).strip(),
                "model": sec_model,
                "desc": "",
                "ctx": "",
                "price": "",
                "api_key": _strip_wrapping_quotes(str(raw_secondary.get("api_key") or active_api_key).strip()),
                "base_url": str(raw_secondary.get("base_url") or base_url or DEFAULT_OPENAI_BASE_URL).strip() or DEFAULT_OPENAI_BASE_URL,
                "ctx_limit": ctx_limit,
                "max_concurrency": max_concurrency,
            }
        else:
            normalized_secondary = {
                "id": "secondary",
                "name": "",
                "model": "",
                "desc": "",
                "ctx": "",
                "price": "",
                "api_key": "",
                "base_url": base_url or DEFAULT_OPENAI_BASE_URL,
                "ctx_limit": 0,
                "max_concurrency": 0,
            }

        if not normalized:
            normalized = [
                {
                    "id": "candidate-1",
                    "name": active_model_name or "deepseek-v4-flash",
                    "model": active_model_name or "deepseek-v4-flash",
                    "desc": "",
                    "ctx": "",
                    "price": "",
                    "api_key": active_api_key,
                    "base_url": base_url or DEFAULT_OPENAI_BASE_URL,
                }
            ]
        if not normalized_vision:
            normalized_vision = [
                {
                    "id": "vision-candidate-1",
                    "name": normalized[0]["model"],
                    "model": normalized[0]["model"],
                    "desc": "",
                    "ctx": "",
                    "price": "",
                    "api_key": normalized[0]["api_key"],
                    "base_url": normalized[0]["base_url"],
                }
            ]

        active_model_id = next(
            (
                str(model.get("id") or "").strip()
                for model in normalized
                if str(model.get("model") or "").strip() == active_model_name
                or str(model.get("name") or "").strip() == active_model_name
                or str(model.get("id") or "").strip() == active_model_name
            ),
            str(normalized[0].get("id") or "candidate-1"),
        )
        return {
            "models": normalized,
            "primary_candidates": normalized,
            "vision_models": normalized_vision,
            "vision_candidates": normalized_vision,
            "secondary_model": normalized_secondary,
            "active": active_model_id,
            "active_model_name": active_model_name,
            "base_url": base_url,
        }

    @router.put("/api/settings/models")
    async def api_update_models(request: Request):
        from cyrene.settings_store import save_models, save_vision_models, save_secondary_model, get_secondary_model
        from cyrene.config import DEFAULT_OPENAI_BASE_URL, write_env_keys
        from cyrene.onboarding import _test_llm_connection
        body = await request.json()
        raw_models = body.get("models")
        raw_vision_models = body.get("vision_models")
        raw_secondary = body.get("secondary_model")
        if not isinstance(raw_models, list) or len(raw_models) == 0:
            return JSONResponse({"error": "models must be a non-empty list"}, status_code=400)
        if raw_vision_models is not None and (not isinstance(raw_vision_models, list) or len(raw_vision_models) == 0):
            return JSONResponse({"error": "vision_models must be a non-empty list"}, status_code=400)

        def _normalize_candidates(raw_items: list[dict[str, Any]]) -> list[dict[str, Any]]:
            normalized_items: list[dict[str, Any]] = []
            for index, model in enumerate(raw_items):
                model_identifier = str(
                    model.get("model")
                    or model.get("name")
                    or model.get("id")
                    or ""
                ).strip()
                if not model_identifier:
                    continue
                normalized_items.append(
                    {
                        "id": str(model.get("id") or f"candidate-{index + 1}").strip() or f"candidate-{index + 1}",
                        "name": model_identifier,
                        "model": model_identifier,
                        "desc": str(model.get("desc") or "").strip(),
                        "ctx": str(model.get("ctx") or "").strip(),
                        "price": str(model.get("price") or "").strip(),
                        "api_key": _strip_wrapping_quotes(str(model.get("api_key") or "").strip()),
                        "base_url": str(model.get("base_url") or DEFAULT_OPENAI_BASE_URL).strip() or DEFAULT_OPENAI_BASE_URL,
                    }
                )
            return normalized_items

        normalized = _normalize_candidates(raw_models)
        normalized_vision = _normalize_candidates(raw_vision_models if isinstance(raw_vision_models, list) else [])

        if not normalized:
            return JSONResponse({"error": "models must contain at least one valid model"}, status_code=400)
        if raw_vision_models is not None and not normalized_vision:
            return JSONResponse({"error": "vision_models must contain at least one valid model"}, status_code=400)

        primary = normalized[0]
        primary_model = str(primary.get("model") or "").strip()
        primary_base_url = str(primary.get("base_url") or DEFAULT_OPENAI_BASE_URL).strip() or DEFAULT_OPENAI_BASE_URL
        primary_api_key = _strip_wrapping_quotes(str(primary.get("api_key") or "").strip())

        try:
            await _test_llm_connection(primary_api_key, primary_base_url, primary_model)
        except httpx.TimeoutException as exc:
            return JSONResponse(
                {"error": "upstream model timed out", "detail": str(exc)},
                status_code=504,
            )
        except httpx.HTTPError as exc:
            return JSONResponse(
                {"error": "upstream model request failed", "detail": format_httpx_error(exc)},
                status_code=502,
            )
        except ValueError as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)

        save_models(normalized)
        if raw_vision_models is not None:
            save_vision_models(normalized_vision)
        if isinstance(raw_secondary, dict):
            save_secondary_model(raw_secondary)
        write_env_keys(
            {
                "OPENAI_MODEL": primary_model,
                "OPENAI_BASE_URL": primary_base_url,
                "OPENAI_API_KEY": primary_api_key,
            }
        )
        saved_secondary = get_secondary_model()
        sec_model = str(saved_secondary.get("model") or "").strip()
        ctx_limit = int(saved_secondary.get("ctx_limit") or 0)
        max_concurrency = int(saved_secondary.get("max_concurrency") or 0)
        if sec_model:
            normalized_secondary = {
                "id": "secondary",
                "name": str(saved_secondary.get("name") or sec_model).strip(),
                "model": sec_model,
                "desc": "",
                "ctx": "",
                "price": "",
                "api_key": _strip_wrapping_quotes(str(saved_secondary.get("api_key") or "").strip()),
                "base_url": str(saved_secondary.get("base_url") or DEFAULT_OPENAI_BASE_URL).strip() or DEFAULT_OPENAI_BASE_URL,
                "ctx_limit": ctx_limit,
                "max_concurrency": max_concurrency,
            }
        else:
            normalized_secondary = {
                "id": "secondary",
                "name": "",
                "model": "",
                "desc": "",
                "ctx": "",
                "price": "",
                "api_key": "",
                "base_url": DEFAULT_OPENAI_BASE_URL,
                "ctx_limit": 0,
                "max_concurrency": 0,
            }
        return {
            "ok": True,
            "models": normalized,
            "primary_candidates": normalized,
            "vision_models": normalized_vision if raw_vision_models is not None else None,
            "vision_candidates": normalized_vision if raw_vision_models is not None else None,
            "secondary_model": normalized_secondary,
            "active": str(primary.get("id") or "candidate-1"),
            "active_model_name": primary_model,
            "base_url": primary_base_url,
        }

    @router.get("/api/settings/tools")
    async def api_get_tools():
        from cyrene.settings_store import get_enabled_tools
        from cyrene.tools import TOOL_DEFS
        enabled = get_enabled_tools()
        tools = []
        for td in TOOL_DEFS:
            name = td["function"]["name"]
            tools.append({
                "name": name,
                "desc": td["function"]["description"],
                "enabled": enabled.get(name, True),
            })
        # Include MCP tools from connected servers
        try:
            from cyrene.mcp_manager import get_manager as _get_mcp_mgr
            manager = _get_mcp_mgr()
            for mcp_td in manager.get_tool_defs():
                name = mcp_td["function"]["name"]
                tools.append({
                    "name": name,
                    "desc": mcp_td["function"]["description"],
                    "enabled": enabled.get(name, True),
                    "source": "mcp",
                })
        except Exception:
            pass
        return {"tools": tools}

    @router.put("/api/settings/tools")
    async def api_update_tools(request: Request):
        from cyrene.settings_store import save_enabled_tools
        body = await request.json()
        updates = body.get("tools", {})
        if not isinstance(updates, dict) or len(updates) == 0:
            return JSONResponse({"error": "tools must be a non-empty dict"}, status_code=400)
        save_enabled_tools(updates)
        return {"ok": True, "updated": list(updates.keys())}

    @router.get("/api/settings/config")
    async def api_get_config():
        return _build_config()

    @router.put("/api/settings/config")
    async def api_update_config(request: Request):
        from cyrene.settings_store import set_ as set_setting
        body = await request.json()
        changed = []
        if "spawn_policy" in body:
            value = str(body.get("spawn_policy") or "").strip().lower()
            if value not in {"aggressive", "conservative", "off"}:
                return JSONResponse({"error": "invalid spawn_policy"}, status_code=400)
            set_setting("spawn_policy", value)
            changed.append("spawn_policy")
        if "heartbeat_interval" in body:
            value = int(body.get("heartbeat_interval") or 0)
            if value < 60:
                return JSONResponse({"error": "heartbeat_interval must be at least 60"}, status_code=400)
            set_setting("heartbeat_interval", value)
            changed.append("heartbeat_interval")
        if "wechat_notify_scheduled" in body:
            set_setting("wechat_notify_scheduled", bool(body["wechat_notify_scheduled"]))
            changed.append("wechat_notify_scheduled")
        return {"ok": True, "changed": changed}

    @router.post("/api/settings/reset-data")
    async def api_reset_data():
        return await _reset_app_data()

    @router.get("/api/settings/search")
    async def api_get_search():
        return {"search": _build_search_config()}

    @router.put("/api/settings/search")
    async def api_update_search(request: Request):
        from cyrene.settings_store import set_ as set_setting
        body = await request.json()
        changed = []
        for key in ("search_mode", "search_external_url"):
            if key in body:
                set_setting(key, body[key])
                changed.append(key)
        return {"ok": True, "changed": changed}

    # ---- MCP Servers API ----

    @router.get("/api/settings/mcp")
    async def api_get_mcp_servers():
        from cyrene.mcp_manager import get_manager as _get_mcp_mgr, get_mcp_servers as _get_servers
        manager = _get_mcp_mgr()
        return {
            "servers": manager.get_server_status(),
            "configs": _get_servers(),
        }

    @router.put("/api/settings/mcp")
    async def api_update_mcp_servers(request: Request):
        from cyrene.mcp_manager import save_mcp_servers as _save_servers
        from cyrene.mcp_manager import get_manager as _get_mcp_mgr, stop_mcp as _stop_mcp, start_mcp as _start_mcp
        body = await request.json()
        servers = body.get("servers", [])
        _save_servers(servers)
        # Restart MCP manager with new config
        _stop_mcp()
        await _start_mcp()
        return {"ok": True}

    # ---- Scheduled tasks ----

    @router.get("/api/tasks")
    async def api_list_tasks():
        from cyrene import db as cy_db
        tasks = await cy_db.get_all_tasks(_db_path)
        return {"tasks": tasks}

    @router.post("/api/tasks")
    async def api_create_task(request: Request):
        from cyrene import db as cy_db
        from croniter import croniter
        from datetime import datetime, timezone, timedelta
        body = await request.json()
        stype = body["schedule_type"]
        svalue = body["schedule_value"]

        # Compute next_run if not provided by the frontend
        next_run = body.get("next_run", "")
        if not next_run:
            now_dt = datetime.now(timezone.utc)
            try:
                if stype == "cron":
                    next_run = croniter(svalue, now_dt).get_next(datetime).isoformat()
                elif stype == "interval":
                    next_run = (now_dt + timedelta(seconds=int(svalue))).isoformat()
                elif stype == "once":
                    next_run = now_dt.isoformat()
            except Exception:
                next_run = now_dt.isoformat()

        task_id = await cy_db.create_task(
            _db_path,
            chat_id=body.get("chat_id", _CHAT_ID),
            prompt=body["prompt"],
            schedule_type=stype,
            schedule_value=svalue,
            next_run=next_run,
        )
        tasks = await cy_db.get_all_tasks(_db_path)
        return {"ok": True, "id": task_id, "tasks": tasks}

    @router.put("/api/tasks/{task_id}")
    async def api_update_task(task_id: str, request: Request):
        from cyrene import db as cy_db
        from croniter import croniter
        from datetime import datetime, timezone, timedelta
        body = await request.json()
        # Build SET clause dynamically from provided fields
        sets = []
        vals = []

        # If schedule_type or schedule_value changed, recalculate next_run
        stype = body.get("schedule_type")
        svalue = body.get("schedule_value")
        if stype and svalue and "next_run" not in body:
            now_dt = datetime.now(timezone.utc)
            try:
                if stype == "cron":
                    body["next_run"] = croniter(svalue, now_dt).get_next(datetime).isoformat()
                elif stype == "interval":
                    body["next_run"] = (now_dt + timedelta(seconds=int(svalue))).isoformat()
                elif stype == "once":
                    body["next_run"] = now_dt.isoformat()
            except Exception:
                pass

        for field in ("prompt", "schedule_type", "schedule_value", "next_run", "status"):
            if field in body:
                sets.append(f"{field} = ?")
                vals.append(body[field])
        if sets:
            import aiosqlite
            async with aiosqlite.connect(_db_path) as db:
                await db.execute(
                    f"UPDATE scheduled_tasks SET {', '.join(sets)} WHERE id = ?",
                    (*vals, task_id),
                )
                await db.commit()
        tasks = await cy_db.get_all_tasks(_db_path)
        return {"ok": True, "tasks": tasks}

    @router.delete("/api/tasks/{task_id}")
    async def api_delete_task(task_id: str):
        from cyrene import db as cy_db
        await cy_db.delete_task(_db_path, task_id)
        tasks = await cy_db.get_all_tasks(_db_path)
        return {"ok": True, "tasks": tasks}

    @router.post("/api/shutdown")
    async def api_shutdown():
        """Shutdown the daemon."""
        import os as _os
        _os._exit(0)

    # ---- Update checker ----

    @router.get("/api/update/check")
    async def api_update_check():
        """Check for updates via GitHub Releases."""
        from cyrene.updater import check_for_update, set_cached_update_info

        info = await check_for_update()
        set_cached_update_info(info)

        return {
            "update_available": info.available,
            "current_version": info.current_version,
            "latest_version": info.latest_version,
            "download_url": info.download_url,
            "release_notes": info.release_notes,
            "asset_name": info.asset_name,
            "asset_size": info.asset_size,
        }

    @router.post("/api/update/download")
    async def api_update_download():
        """下载更新包。返回下载状态。"""
        from cyrene.updater import (
            get_cached_update_info,
            download_update,
            _download_progress,
        )

        info = get_cached_update_info()
        if not info or not info.download_url:
            return {"ok": False, "error": "No update available"}

        def _progress(downloaded: int, total: int) -> None:
            _download_progress["downloaded"] = downloaded
            _download_progress["total"] = total

        _download_progress["downloaded"] = 0
        _download_progress["total"] = info.asset_size
        _download_progress["done"] = False

        dest = await download_update(info.download_url, _progress)
        _download_progress["done"] = True
        _download_progress["path"] = str(dest) if dest else ""

        if dest:
            return {
                "ok": True,
                "path": str(dest),
                "size": _download_progress["downloaded"],
            }
        return {"ok": False, "error": "Download failed"}

    @router.get("/api/update/progress")
    async def api_update_progress():
        """查询下载进度。"""
        from cyrene.updater import get_download_progress
        return get_download_progress()

    @router.post("/api/update/restart")
    async def api_update_restart():
        """写入重启脚本并退出进程（安装更新后调用）。

        无论更新文件是否存在，都通过退出码 42 通知 Electron 重启，
        避免因提前返回导致进程继续运行、关闭时误弹"崩溃"对话框。
        """
        from cyrene.updater import get_restart_script, _download_progress
        import subprocess as _sp

        dest_str = _download_progress.get("path", "")
        if dest_str:
            dest = Path(dest_str)
            if dest.exists():
                try:
                    script = get_restart_script(dest)
                    if sys.platform == "win32":
                        script_path = dest.parent / "update.bat"
                        script_path.write_text(script)
                        _sp.Popen(
                            ["cmd", "/c", str(script_path)],
                            creationflags=(
                                0x00000200 |  # CREATE_NEW_PROCESS_GROUP
                                0x00000008    # DETACHED_PROCESS
                            ),
                        )
                    else:
                        script_path = dest.parent / "update.sh"
                        script_path.write_text(script)
                        script_path.chmod(0o755)
                        _sp.Popen(
                            ["bash", str(script_path)], start_new_session=True
                        )
                except Exception:
                    logger.warning(
                        "Failed to spawn updater script", exc_info=True
                    )
            else:
                logger.warning("Update file vanished: %s", dest)
        else:
            logger.warning("Restart called but no downloaded update found")

        # 始终用退出码 42 退出，通知 Electron 释放 single-instance lock
        import os as _os
        _os._exit(42)

    app.include_router(router)


# ---------------------------------------------------------------------------
# UI data builders
# ---------------------------------------------------------------------------


def _resolve_ui_tz(tz_name: str = ""):
    name = str(tz_name or "").strip()
    if name:
        try:
            return ZoneInfo(name)
        except Exception:
            pass
    return datetime.now().astimezone().tzinfo or timezone.utc


async def _build_ui_data(tz_name: str = "") -> dict:
    """Assemble the full DATA payload the SPA expects."""
    sessions = _build_sessions()
    if not sessions:
        sessions = [_empty_session()]
    ui_tz = _resolve_ui_tz(tz_name)
    return {
        "user": _build_user(),
        "assistantName": ASSISTANT_NAME,
        "appVersion": get_version_label(),
        "dashboard": await _build_dashboard(ui_tz),
        "sessions": sessions,
        "status": await _build_status(),
        "skills": _build_skills(),
        "settings": _build_settings_meta(),
        "onboarding": get_onboarding_status(),
    }


def _build_user() -> dict:
    """User identity from environment or workspace owner."""
    name = _resolve_local_username()
    handle = re.sub(r"[^a-z0-9._-]+", "", name.lower().replace(" ", "")) or "user"
    parts = [part for part in re.split(r"[\s._-]+", name) if part]
    initials = "".join(part[0].upper() for part in parts[:2]) or name[:2].upper() or "U"
    return {"name": name, "handle": handle, "initials": initials}


def _resolve_local_username() -> str:
    """Best-effort local account name for the current machine."""
    candidates = [
        os.environ.get("USER"),
        os.environ.get("USERNAME"),
        os.environ.get("LOGNAME"),
    ]
    try:
        candidates.append(getpass.getuser())
    except Exception:
        pass

    for candidate in candidates:
        if candidate and candidate.strip():
            return candidate.strip()

    return "user"


# ---------------------------------------------------------------------------
# Sessions
# ---------------------------------------------------------------------------


# Per-session CC preview cache — archived sessions keep their initial snapshot
_cc_preview_cache: dict[str, list] = {}

def _build_sessions() -> list[dict]:
    """Build session list — current state.json + parsed conversation archives."""
    sessions: list[dict] = []

    # 1. Current active session from state.json
    current = _build_current_session()
    if current:
        sessions.append(current)

    # 2. Historical sessions from conversation archives (one per day, most recent first)
    skip_archive_ids: set[str] = set()
    current_archive_session_id = str(current.get("archiveSessionId", "")).strip() if current else ""
    current_archive_date = str(current.get("archiveDate", "")).strip() if current else ""
    if current_archive_session_id and current_archive_date:
        skip_archive_ids.add(f"{current_archive_date}:{current_archive_session_id}")

    archive_sessions = _build_archive_sessions(skip_archive_ids=skip_archive_ids)
    sessions.extend(archive_sessions)

    # Per-session CC preview: live session always fresh, archives use cached snapshot
    for session in sessions:
        sid = session["id"]
        for shell in session.get("shells", []):
            if shell.get("kind") == "cc":
                if sid == "run_live":
                    _cc_preview_cache[sid] = list(shell.get("lines", []))
                elif sid in _cc_preview_cache:
                    shell["lines"] = list(_cc_preview_cache[sid])
                else:
                    _cc_preview_cache[sid] = list(shell.get("lines", []))

    return sessions


def _build_summary(raw_msgs: list[dict]) -> dict:
    usage = _usage_totals(raw_msgs)
    return {
        "tokens": _format_tokens(usage),
        "spend": _calc_spend(usage),
        "toolCalls": _count_tool_calls(raw_msgs),
        "requests": usage["requests"],
        "total_tokens": usage["total_tokens"],
    }


def _ui_pending_question(raw_pending: Any) -> dict[str, Any] | None:
    if not isinstance(raw_pending, dict):
        return None
    question_id = str(raw_pending.get("id", "")).strip()
    text = str(raw_pending.get("text", "")).strip()
    if not question_id or not text:
        return None
    options_out = []
    raw_options = raw_pending.get("options", [])
    if isinstance(raw_options, list):
        for item in raw_options:
            if isinstance(item, dict):
                option_id = str(item.get("id", "")).strip()
                label = str(item.get("label", "")).strip()
            else:
                option_id = ""
                label = str(item or "").strip()
            if not label:
                continue
            options_out.append({
                "id": option_id or f"option_{len(options_out) + 1}",
                "label": label,
            })
    return {
        "id": question_id,
        "text": text,
        "askedAt": str(raw_pending.get("asked_at", "")).strip(),
        "roundId": str(raw_pending.get("round_id", "")).strip(),
        "roundTitle": str(raw_pending.get("round_title", "")).strip(),
        "clientRequestId": str(raw_pending.get("client_request_id", "")).strip(),
        "allowCustom": bool(raw_pending.get("allow_custom", True)),
        "options": options_out,
    }


def _build_current_session() -> dict | None:
    """Build a session object from state.json + live subagents.

    Always returns a run_live entry — when state.json is missing or empty,
    returns an empty placeholder so the Chat page shows a clean "start a new
    conversation" view instead of falling back to an old archive.
    """
    state: dict[str, Any] = {}
    raw_msgs: list[dict] = []
    if STATE_FILE.exists():
        try:
            loaded = json.loads(STATE_FILE.read_text(encoding="utf-8"))
            state = loaded if isinstance(loaded, dict) else {}
            raw_msgs = state.get("messages", []) or []
        except Exception:
            raw_msgs = []
            state = {}

    pending_question = _ui_pending_question(state.get("pending_question", {}))
    messages = _convert_messages(raw_msgs) if raw_msgs else []
    current_round_id = _latest_round_id_from_messages(raw_msgs)
    current_round_title = next(
        (
            str(msg.get("round_title", "")).strip()
            for msg in reversed(raw_msgs)
            if str(msg.get("round_id", "")).strip() == current_round_id and msg.get("round_title")
        ),
        "",
    )

    from cyrene.subagent import _registry  # noqa: WPS437
    subagent_registry = _infer_subagent_entries(raw_msgs, _registry)
    subagents = []
    for agent_id, info in subagent_registry.items():
        status = info.get("status", "running")
        ui_status = {"running": "running", "waiting": "queued", "resumed": "running",
                     "done": "done", "timeout": "err"}.get(status, status)
        created_at = info.get("created_at")
        subagents.append({
            "id": agent_id,
            "name": agent_id,
            "status": ui_status,
            "task": info.get("task", ""),
            "roundId": str(info.get("round_id", "")).strip(),
            "tokens": len(info.get("messages", [])),
            "elapsed": _elapsed_since(created_at),
            "progress": _status_progress(status),
            "result": info.get("result", ""),
            "messageCount": len(info.get("messages", [])),
            "createdAt": _short_time(created_at),
            "updatedAt": _short_time(info.get("updated_at")),
        })

    subagents.sort(key=lambda item: (item.get("createdAt") == "—", item.get("createdAt"), item["name"]))
    live_rounds = get_live_rounds()

    session_start = _session_started_at(raw_msgs)
    started_at = datetime.fromtimestamp(session_start, tz=timezone.utc).strftime("%H:%M")
    duration = _format_duration(time.time() - session_start)
    last_msg = messages[-1] if messages else None

    is_empty = not messages
    if live_rounds and any(str(item.get("status", "")) == "running" for item in live_rounds):
        live_status = "running"
    elif pending_question:
        live_status = "queued"
    elif live_rounds and any(int(item.get("pendingGuidance", 0) or 0) > 0 for item in live_rounds):
        live_status = "queued"
    elif is_empty:
        live_status = "idle"  # nothing happening yet — fresh session
    else:
        # Check if the main agent is actively processing (no live_rounds exist
        # during Phase 1/2 of the main agent loop)
        recent = debug.get_recent_events(200)
        now_ts = datetime.now(timezone.utc)
        cutoff_ts = now_ts - timedelta(seconds=30)
        active_events = 0
        for e in recent:
            if e.get("type") not in ("phase_transition", "llm_call", "tool_call"):
                continue
            ts = e.get("timestamp")
            if not ts:
                continue
            try:
                if datetime.fromisoformat(ts) > cutoff_ts:
                    active_events += 1
            except (ValueError, TypeError):
                pass
        if active_events:
            live_status = "running"
        else:
            live_status = "done"

    live_summary = _build_summary(raw_msgs)
    # Save main-agent-only total_tokens BEFORE merging subagent usage
    main_agent_total_tokens = live_summary.get("total_tokens")
    subagent_usage = _merge_usage_totals(*[
        _usage_totals(info.get("messages", []))
        for info in subagent_registry.values()
    ])
    combined_live_usage = _merge_usage_totals(_usage_totals(raw_msgs), subagent_usage)
    if combined_live_usage.get("requests") is not None:
        live_summary["requests"] = combined_live_usage.get("requests")
        live_summary["tokens"] = _format_tokens(combined_live_usage)
        live_summary["spend"] = _calc_spend(combined_live_usage)
        live_summary["toolCalls"] = live_summary["toolCalls"] + sum(
            _count_tool_calls(info.get("messages", []))
            for info in subagent_registry.values()
        )
        live_summary["total_tokens"] = combined_live_usage.get("total_tokens")

    # Set timestamp filter so CC preview only shows entries from this session
    set_cc_since(started_at)

    visible_shells = [] if is_empty else list_live_shells(include_exited=False)

    return {
        "id": "run_live",
        "title": str(state.get("session_title", "")).strip() or ("new session" if is_empty else "current session"),
        "status": live_status,
        "started": started_at,
        "archiveDate": datetime.now().astimezone().strftime("%Y-%m-%d"),
        "archiveSessionId": str(state.get("archive_session_id", "")).strip(),
        "dur": duration,
        "preview": (last_msg["body"][:80] + "…") if last_msg and last_msg.get("body") else "—",
        "model": _get_model(),
        "ctx_limit": _get_current_model_ctx_limit(),
        "currentRoundId": current_round_id,
        "currentRoundTitle": current_round_title,
        "pendingQuestion": pending_question,
        "summary": live_summary,
        "main_agent_total_tokens": main_agent_total_tokens,
        "main_agent_context_tokens": _last_request_context_tokens(raw_msgs),
        "chat": {
            "contextChips": _build_context_chips(),
            "messages": messages,
        },
        "liveRounds": live_rounds,
        "shells": visible_shells,
        "subagents": subagents,
        "flow": _build_live_flow(raw_msgs, messages, subagents, subagent_registry),
    }


def _build_archive_sessions(
    skip_dates: set[str] | None = None,
    skip_archive_ids: set[str] | None = None,
) -> list[dict]:
    """Build session entries from conversation archives (one per archived session)."""
    if not CONVERSATIONS_DIR.exists():
        return []

    sessions = []
    files = sorted(CONVERSATIONS_DIR.glob("*.md"), reverse=True)
    for filepath in files[:10]:  # cap at 10 most recent days
        date_str = filepath.stem
        if skip_dates and date_str in skip_dates:
            continue
        try:
            content = filepath.read_text(encoding="utf-8")
        except Exception:
            continue
        sections = _parse_archive_sections(content)
        if not sections:
            continue

        file_session_title = _parse_archive_session_title(content)
        groups: dict[str, list[dict[str, Any]]] = {}
        order: list[str] = []
        for index, section in enumerate(sections):
            archive_session_id = str(section.get("archive_session_id", "")).strip() or f"legacy_{date_str}"
            if archive_session_id not in groups:
                groups[archive_session_id] = []
                order.append(archive_session_id)
            groups[archive_session_id].append({**section, "_order": index})

        for archive_session_id in reversed(order):
            archive_key = f"{date_str}:{archive_session_id}"
            if skip_archive_ids and archive_key in skip_archive_ids:
                continue
            group_sections = groups[archive_session_id]
            messages = _messages_from_archive_sections(group_sections)
            if not messages:
                continue
            last_user = next((m for m in messages if m["role"] == "user"), None)
            group_session_title = next(
                (str(section.get("session_title", "")).strip() for section in group_sections if section.get("session_title")),
                "",
            )
            title = group_session_title or ((last_user["body"][:60] + ("…" if len(last_user["body"]) > 60 else "")) if last_user else date_str)
            preview = messages[-1].get("body", "")[:80] if messages else ""
            current_round_id = next((str(m.get("round_id", "")).strip() for m in reversed(messages) if m.get("round_id")), "")
            current_round_title = next(
                (
                    str(m.get("round_title", "")).strip()
                    for m in reversed(messages)
                    if str(m.get("round_id", "")).strip() == current_round_id and m.get("round_title")
                ),
                "",
            )

            sessions.append({
                "id": f"archive_{date_str}_{archive_session_id}",
                "title": title,
                "status": "done",
                "started": date_str,
                "dur": "—",
                "preview": preview,
                "model": _get_model(),
                "currentRoundId": current_round_id,
                "currentRoundTitle": current_round_title,
                "summary": {
                    "tokens": f"{len(messages)} msgs",
                    "spend": "—",
                    "toolCalls": 0,
                },
                "chat": {
                    "contextChips": [{"icon": "📅", "label": date_str}],
                    "messages": messages,
                },
                "liveRounds": [],
                "shells": [],
                "subagents": [],
                "flow": _build_simple_flow(messages),
            })
    return sessions


def _parse_archive_meta(section: str, key: str) -> str:
    match = re.search(rf"<!--\s*{re.escape(key)}:\s*(.*?)\s*-->", section)
    return match.group(1).strip() if match else ""


def _parse_archive_session_title(content: str) -> str:
    return _parse_archive_meta(content, "session_title")


def _split_archive_entry_blocks(content: str) -> list[str]:
    blocks: list[str] = []
    matches = list(re.finditer(r"(?m)^##\s+\S+\s+UTC\s*$", content))
    for index, match in enumerate(matches):
        start = match.start()
        end = matches[index + 1].start() if index + 1 < len(matches) else len(content)
        block = content[start:end].strip()
        block = re.sub(r"\n+---\s*\Z", "", block).strip()
        if block:
            blocks.append(block)
    return blocks


def _parse_archive_sections(content: str) -> list[dict[str, Any]]:
    """Parse a conversations/YYYY-MM-DD.md file into archive sections with metadata."""
    sections_out: list[dict[str, Any]] = []
    file_session_title = _parse_archive_session_title(content)
    round_index = 0

    for section in _split_archive_entry_blocks(content):
        if "**User**:" not in section:
            continue
        ts_match = re.search(r"##\s*(\S+\s+UTC)", section)
        dialogue_match = re.search(r"\*\*User\*\*:\s*(.*?)\n+\*\*[^*]+\*\*:\s*(.*)\Z", section, re.DOTALL)
        if not ts_match or not dialogue_match:
            continue

        ts = ts_match.group(1).strip()
        user_body = dialogue_match.group(1).strip()
        assistant_body = dialogue_match.group(2).strip()
        round_id = _parse_archive_meta(section, "round_id") or f"archive_round_{round_index}"
        round_title = _parse_archive_meta(section, "round_title")
        archive_session_id = _parse_archive_meta(section, "archive_session_id")
        session_title = _parse_archive_meta(section, "session_title")
        body_start = section.find("## ")
        raw_entry = section[body_start:].strip() if body_start >= 0 else section.strip()
        sections_out.append({
            "timestamp": ts,
            "user_body": user_body,
            "assistant_body": assistant_body,
            "round_id": round_id,
            "round_title": round_title,
            "archive_session_id": archive_session_id,
            "session_title": session_title,
            "raw_entry": raw_entry,
        })
        round_index += 1
    return sections_out


def _messages_from_archive_sections(sections: list[dict[str, Any]]) -> list[dict]:
    messages: list[dict] = []
    for index, section in enumerate(sections):
        messages.append({
            "id": f"m{index}u",
            "role": "user",
            "time": section["timestamp"],
            "body": section["user_body"],
            "round_id": section["round_id"],
            "round_title": section["round_title"],
        })
        messages.append({
            "id": f"m{index}a",
            "role": "agent",
            "time": section["timestamp"],
            "body": section["assistant_body"],
            "round_id": section["round_id"],
            "round_title": section["round_title"],
        })
    return messages


def _parse_archive_file(content: str) -> list[dict]:
    """Parse a conversations/YYYY-MM-DD.md file into UI-formatted messages."""
    return _messages_from_archive_sections(_parse_archive_sections(content))


def _write_archive_sections(filepath: Path, date_str: str, sections: list[dict[str, Any]]) -> None:
    if not sections:
        if filepath.exists():
            filepath.unlink()
        return
    first_session_title = next((str(section.get("session_title", "")).strip() for section in sections if section.get("session_title")), "")
    content = _upsert_archive_session_title(f"# Conversations - {date_str}\n\n", date_str, first_session_title)
    content += "\n---\n\n".join(section["raw_entry"] for section in sections if section.get("raw_entry")) + "\n\n---\n"
    filepath.write_text(content, encoding="utf-8")


def _upsert_archive_session_title(content: str, date_str: str, session_title: str) -> str:
    header = f"# Conversations - {date_str}\n\n"
    if not content:
        content = header
    elif not content.startswith("# Conversations - "):
        content = header + content
    if not session_title:
        return content
    marker = f"<!-- session_title: {session_title} -->\n\n"
    pattern = re.compile(r"^(# Conversations - .*?\n\n)(?:<!-- session_title: .*? -->\n\n)?", re.DOTALL)
    if pattern.search(content):
        return pattern.sub(lambda match: match.group(1) + marker, content, count=1)
    return header + marker + content[len(header):]


def _is_hidden_internal_message(message: dict[str, Any]) -> bool:
    if bool(message.get("hidden_from_ui")):
        return True
    role = str(message.get("role", "")).strip()
    content = str(message.get("content", "") or "").strip()
    if role != "user" or not content:
        return False
    return (
        content.startswith("## Research Materials\n\nBelow are the research findings gathered on this question.")
        or content.startswith("[Decision-phase correction] You attempted unavailable tool(s):")
    )


def _convert_messages(raw_msgs: list[dict]) -> list[dict]:
    """Convert state.json raw messages → UI message format."""
    out = []
    compacted_marker_emitted = False
    for i, m in enumerate(raw_msgs):
        if _is_hidden_internal_message(m):
            continue
        if isinstance(m, dict) and m.get("compacted_block"):
            if not compacted_marker_emitted:
                cid = str(m.get("message_id", "")).strip() or ("compacted" + str(i))
                out.append({"id": cid, "messageId": cid, "role": "system", "kind": "compacted", "compacted": True})
                compacted_marker_emitted = True
            continue
        role = m.get("role", "")
        if role not in ("user", "assistant"):
            continue
        content = (m.get("content") or "").strip()
        has_live_detail = bool(m.get("reasoning_content") or m.get("tool_calls"))
        has_attachments = isinstance(m.get("attachments"), list) and bool(m.get("attachments"))
        if role == "user" and not content and not m.get("attachments"):
            continue
        if role == "assistant" and not content and not has_live_detail and not has_attachments:
            continue
        ui_role = "user" if role == "user" else "agent"
        message_id = str(m.get("message_id", "")).strip() or f"m{i}"
        ui_msg = {"id": message_id, "messageId": message_id, "role": ui_role, "time": "—"}
        if content:
            ui_msg["body"] = content
        if isinstance(m.get("attachments"), list):
            ui_msg["attachments"] = [
                {
                    "id": str(item.get("id") or "").strip(),
                    "name": str(item.get("name") or "file"),
                    "content_type": str(item.get("content_type") or "application/octet-stream"),
                    "size": int(item.get("size") or 0),
                    "kind": str(item.get("kind") or "file"),
                    "url": str(item.get("url") or "").strip(),
                    **({"width": int(item.get("width"))} if str(item.get("width", "")).strip().isdigit() else {}),
                    **({"height": int(item.get("height"))} if str(item.get("height", "")).strip().isdigit() else {}),
                }
                for item in m.get("attachments")
                if isinstance(item, dict)
            ]
        if bool(m.get("intermediate_reply")):
            ui_msg["intermediateReply"] = True
        if bool(m.get("question_prompt")):
            ui_msg["questionPrompt"] = True
        question_id = str(m.get("question_id", "")).strip()
        if question_id:
            ui_msg["questionId"] = question_id
        round_id = str(m.get("round_id", "")).strip()
        if round_id:
            ui_msg["roundId"] = round_id
        client_request_id = str(m.get("client_request_id", "")).strip()
        if client_request_id:
            ui_msg["clientRequestId"] = client_request_id
        queued_guidance_id = str(m.get("queued_guidance_id", "")).strip()
        if queued_guidance_id:
            ui_msg["queuedGuidanceId"] = queued_guidance_id
        guidance_ack_for_guidance_id = str(m.get("guidance_ack_for_guidance_id", "")).strip()
        if guidance_ack_for_guidance_id:
            ui_msg["guidanceAckForGuidanceId"] = guidance_ack_for_guidance_id
        in_reply_to_guidance_id = str(m.get("in_reply_to_guidance_id", "")).strip()
        if in_reply_to_guidance_id:
            ui_msg["inReplyToGuidanceId"] = in_reply_to_guidance_id
        if m.get("reasoning_content"):
            ui_msg["thinking"] = m["reasoning_content"]
        if m.get("tool_calls"):
            tools = []
            for tc in m["tool_calls"]:
                fn = tc.get("function", {})
                args = fn.get("arguments", "")
                if isinstance(args, str) and len(args) > 80:
                    args = args[:80] + "…"
                tools.append({
                    "name": fn.get("name", "?"),
                    "arg": str(args)[:120],
                    "status": "done",
                    "out": "",
                })
            ui_msg["tools"] = tools
        out.append(ui_msg)
    return _collapse_duplicate_user_messages(
        _merge_adjacent_trace_only_messages(_dedupe_repeated_messages(out))
    )


def _is_trace_only_agent_message(msg: dict[str, Any]) -> bool:
    return (
        msg.get("role") == "agent"
        and not str(msg.get("body", "")).strip()
        and (bool(msg.get("thinking")) or bool(msg.get("tools")))
    )


def _dedupe_repeated_messages(messages: list[dict]) -> list[dict]:
    deduped: list[dict] = []
    seen_ids: set[tuple[str, str]] = set()
    for msg in messages:
        message_id = str(msg.get("messageId", "")).strip() or str(msg.get("id", "")).strip()
        if message_id:
            dedupe_key = (str(msg.get("role", "")).strip(), message_id)
            if dedupe_key in seen_ids:
                continue
            seen_ids.add(dedupe_key)
        deduped.append(msg)
    return deduped


def _merge_adjacent_trace_only_messages(messages: list[dict]) -> list[dict]:
    merged: list[dict] = []
    for msg in messages:
        if not merged:
            merged.append(msg)
            continue
        prev = merged[-1]
        prev_request_id = str(prev.get("clientRequestId", "")).strip()
        next_request_id = str(msg.get("clientRequestId", "")).strip()
        compatible_request = (
            not prev_request_id
            or not next_request_id
            or prev_request_id == next_request_id
        )
        compatible_round = str(prev.get("roundId", "")).strip() == str(msg.get("roundId", "")).strip()
        compatible_guidance = (
            not str(prev.get("queuedGuidanceId", "")).strip()
            and not str(msg.get("queuedGuidanceId", "")).strip()
            and not str(prev.get("guidanceAckForGuidanceId", "")).strip()
            and not str(msg.get("guidanceAckForGuidanceId", "")).strip()
            and not str(prev.get("inReplyToGuidanceId", "")).strip()
            and not str(msg.get("inReplyToGuidanceId", "")).strip()
        )
        if (
            _is_trace_only_agent_message(prev)
            and _is_trace_only_agent_message(msg)
            and compatible_round
            and compatible_request
            and compatible_guidance
        ):
            prev_thinking = str(prev.get("thinking", "")).strip()
            next_thinking = str(msg.get("thinking", "")).strip()
            if next_thinking:
                if prev_thinking and next_thinking != prev_thinking:
                    prev["thinking"] = prev_thinking + "\n\n" + next_thinking
                elif not prev_thinking:
                    prev["thinking"] = next_thinking
            prev_tools = list(prev.get("tools") or [])
            next_tools = list(msg.get("tools") or [])
            if next_tools:
                prev["tools"] = prev_tools + next_tools
            continue
        if (
            _is_trace_only_agent_message(prev)
            and msg.get("role") == "agent"
            and compatible_round
            and compatible_request
            and compatible_guidance
            and (
                str(msg.get("body", "")).strip()
                or str(msg.get("thinking", "")).strip()
                or bool(msg.get("tools"))
            )
        ):
            merged_msg = dict(msg)
            prev_thinking = str(prev.get("thinking", "")).strip()
            next_thinking = str(merged_msg.get("thinking", "")).strip()
            if prev_thinking:
                if next_thinking and next_thinking != prev_thinking:
                    merged_msg["thinking"] = prev_thinking + "\n\n" + next_thinking
                elif not next_thinking:
                    merged_msg["thinking"] = prev_thinking
            prev_tools = list(prev.get("tools") or [])
            next_tools = list(merged_msg.get("tools") or [])
            if prev_tools or next_tools:
                merged_msg["tools"] = prev_tools + next_tools
            if not str(merged_msg.get("clientRequestId", "")).strip() and prev_request_id:
                merged_msg["clientRequestId"] = prev_request_id
            merged[-1] = merged_msg
            continue
        merged.append(msg)
    return merged


def _collapse_duplicate_user_messages(messages: list[dict]) -> list[dict]:
    collapsed: list[dict] = []
    index = 0
    while index < len(messages):
        msg = messages[index]
        if msg.get("role") != "user":
            collapsed.append(msg)
            index += 1
            continue

        block_end = index
        while block_end < len(messages) and messages[block_end].get("role") == "user":
            block_end += 1

        block = messages[index:block_end]
        seen_bodies: set[str] = set()
        kept_reversed: list[dict] = []
        for block_msg in reversed(block):
            body = str(block_msg.get("body", "")).strip()
            if body and body in seen_bodies:
                continue
            if body:
                seen_bodies.add(body)
            kept_reversed.append(block_msg)
        collapsed.extend(reversed(kept_reversed))
        index = block_end
    return collapsed


def _count_tool_calls(raw_msgs: list[dict]) -> int:
    count = sum(len(m.get("tool_calls") or []) for m in raw_msgs)
    if count == 0:
        count = sum(1 for m in raw_msgs if m.get("role") == "tool")
    return count


def _session_started_at(raw_msgs: list[dict]) -> float:
    for m in raw_msgs:
        round_id = str(m.get("round_id", "")).strip()
        match = re.fullmatch(r"round_(\d+)", round_id)
        if match:
            return int(match.group(1)) / 1000.0
    return _SERVER_STARTED_AT


def _build_simple_flow(messages: list[dict]) -> dict:
    """Archive flow grouped by conversation round, without live tool traces."""
    rounds: list[list[dict]] = []
    current: list[dict] = []
    current_round_id = ""

    for msg in messages:
        round_id = str(msg.get("round_id", "")).strip() or current_round_id or "archive_round_0"
        if current and round_id != current_round_id:
            rounds.append(current)
            current = []
        current.append(msg)
        current_round_id = round_id
    if current:
        rounds.append(current)

    nodes: list[dict] = []
    edges: list[dict] = []
    y_offset = 0
    multiple_rounds = len(rounds) > 1

    for round_index, round_msgs in enumerate(rounds or [messages]):
        prefix = f"r{round_index}_" if multiple_rounds else ""
        last_user = next((m for m in round_msgs if m["role"] == "user"), None)
        last_agent = next((m for m in reversed(round_msgs) if m["role"] == "agent"), None)
        round_title = next((str(m.get("round_title", "")).strip() for m in round_msgs if m.get("round_title")), "") or "user request"
        user_id = f"{prefix}n_user"
        main_id = f"{prefix}n_main"
        out_id = f"{prefix}n_out"

        nodes.extend([
            {
                "id": user_id, "kind": "input", "x": 40, "y": y_offset + 80,
                "title": round_title, "status": "done",
                "detail": {
                    "role": "User",
                    "text": last_user["body"] if last_user else "",
                    "tokens": 0,
                    "time": last_user["time"] if last_user else "—",
                },
            },
            {
                "id": main_id, "kind": "main", "x": 320, "y": y_offset + 70,
                "title": f"main agent · {ASSISTANT_NAME}",
                "subtitle": "archive",
                "status": "done",
                "model": _get_model(),
                "detail": {
                    "systemPrompt": f"You are {ASSISTANT_NAME}, an AI companion. Use SOUL.md to maintain persona.",
                    "reasoning": "Loaded session from archive — no live reasoning trace.",
                    "tokensIn": 0, "tokensOut": 0,
                    "model": _get_model(), "temp": 0.2,
                },
            },
            {
                "id": out_id, "kind": "output", "x": 660, "y": y_offset + 90,
                "title": "response", "status": "done",
                "detail": {
                    "kind": "Output",
                    "content": (last_agent["body"][:600] if last_agent else "—"),
                },
            },
        ])
        edges.extend([
            {"from": user_id, "to": main_id},
            {"from": main_id, "to": out_id},
        ])
        y_offset += 180

    return {"nodes": nodes, "edges": edges}


def _build_live_flow(raw_msgs: list[dict], messages: list[dict], subagents: list[dict], registry: dict[str, dict]) -> dict:
    """Build a richer flow for the current session, stacked by conversation round."""
    rounds = _split_raw_rounds(raw_msgs)
    recent_events = debug.get_recent_events(250)
    if not rounds and raw_msgs:
        rounds = [raw_msgs]
    if not rounds:
        synthetic_round = _synthetic_live_round(registry, recent_events)
        if synthetic_round:
            rounds = [synthetic_round]
    if not rounds:
        return {"nodes": [], "edges": []}

    rounds, active_round_index = _prune_flow_rounds(rounds)
    if not rounds:
        return {"nodes": [], "edges": []}

    nodes: list[dict] = []
    edges: list[dict] = []
    next_y = 0
    multiple_rounds = len(rounds) > 1

    for round_index, round_raw in enumerate(rounds):
        is_current_round = round_index == active_round_index
        round_messages = _convert_messages(round_raw)
        round_id = _latest_round_id_from_messages(round_raw)
        round_registry = _round_registry_for_flow(round_raw, registry if is_current_round else {})
        related_agents = _related_round_agent_names(set(round_registry), round_id=round_id)
        if is_current_round and subagents:
            candidate_subagents = [
                sa for sa in subagents
                if _subagent_matches_round(sa, round_id) and (not round_registry or sa["name"] in related_agents)
            ]
            for sa in candidate_subagents:
                entry = round_registry.setdefault(sa["name"], {
                    "task": sa.get("task", ""),
                    "status": "done",
                    "result": sa.get("result", ""),
                    "messages": [],
                    "created_at": None,
                    "updated_at": None,
                    "round_id": round_id,
                })
                entry["task"] = entry.get("task") or sa.get("task", "")
                entry["status"] = _registry_status_from_ui(sa.get("status", entry.get("status", "done")))
                entry["result"] = entry.get("result") or sa.get("result", "")
        if is_current_round and not round_registry and registry:
            round_registry = {
                agent_id: dict(info)
                for agent_id, info in registry.items()
                if not round_id or info.get("round_id") in ("", round_id)
            }
        round_subagents = _subagent_cards_from_registry(round_registry)
        round_recent_events = _events_for_round(recent_events, round_id) if is_current_round else []
        prefix = f"r{round_index}_" if multiple_rounds else ""
        round_nodes, round_edges, round_bottom = _build_live_flow_round(
            prefix=prefix,
            raw_msgs=round_raw,
            messages=round_messages,
            subagents=round_subagents,
            registry=round_registry,
            recent_events=round_recent_events,
            y_offset=next_y,
            round_id=round_id,
        )
        nodes.extend(round_nodes)
        edges.extend(round_edges)
        next_y = round_bottom + 180

    return {"nodes": nodes, "edges": edges}


def _synthetic_live_round(registry: dict[str, dict], recent_events: list[dict]) -> list[dict]:
    if not registry:
        return []
    round_id = next((str(info.get("round_id", "")).strip() for info in registry.values() if info.get("round_id")), "")
    latest_phase = next((e for e in reversed(recent_events) if e.get("type") == "phase_transition"), None)
    latest_llm = next((e for e in reversed(recent_events) if e.get("type") == "llm_call" and e.get("caller") == "main_agent"), None)
    prompt = (
        latest_phase.get("detail")
        if latest_phase and latest_phase.get("detail")
        else latest_llm.get("response")
        if latest_llm and latest_llm.get("response")
        else "Live round in progress"
    )
    entry: dict[str, Any] = {"role": "user", "content": prompt}
    if round_id:
        entry["round_id"] = round_id
    return [entry]


def _split_raw_rounds(raw_msgs: list[dict]) -> list[list[dict]]:
    rounds: list[list[dict]] = []
    current: list[dict] = []
    current_key = ""
    anonymous_round_index = 0
    for msg in raw_msgs:
        round_id = str(msg.get("round_id", "")).strip()
        if round_id:
            next_key = f"round:{round_id}"
        elif msg.get("role") == "user":
            anonymous_round_index += 1
            next_key = f"anon:{anonymous_round_index}"
        else:
            next_key = current_key or f"anon:{max(anonymous_round_index, 1)}"

        if current and next_key != current_key:
            rounds.append(current)
            current = []

        if not current:
            current_key = next_key
            current = [msg]
            continue

        current.append(msg)
    if current:
        rounds.append(current)
    return rounds


def _round_has_activity(raw_msgs: list[dict]) -> bool:
    return any(str(msg.get("role", "")) != "user" for msg in raw_msgs)


def _prune_flow_rounds(rounds: list[list[dict]]) -> tuple[list[list[dict]], int]:
    """Keep substantive rounds plus the latest pending user-only round.

    This prevents interrupted trailing user messages from stretching the flow
    into multiple empty rounds while still preserving the latest pending input.
    """
    if not rounds:
        return [], -1

    substantive_indices = [i for i, round_raw in enumerate(rounds) if _round_has_activity(round_raw)]
    if not substantive_indices:
        return [rounds[-1]], 0

    keep_indices = set(substantive_indices)
    latest_substantive = substantive_indices[-1]
    tail_pending = [
        i for i in range(latest_substantive + 1, len(rounds))
        if not _round_has_activity(rounds[i])
    ]
    if tail_pending:
        keep_indices.add(tail_pending[-1])

    pruned: list[list[dict]] = []
    index_map: dict[int, int] = {}
    for original_index, round_raw in enumerate(rounds):
        if original_index not in keep_indices:
            continue
        index_map[original_index] = len(pruned)
        pruned.append(round_raw)

    return pruned, index_map[latest_substantive]


def _round_registry_for_flow(raw_msgs: list[dict], live_registry: dict[str, dict]) -> dict[str, dict]:
    round_id = _latest_round_id_from_messages(raw_msgs)
    entries: dict[str, dict] = _snapshot_entries_from_messages(raw_msgs, round_id=round_id)
    for msg in raw_msgs:
        for tc in msg.get("tool_calls") or []:
            fn = tc.get("function", {})
            if fn.get("name") != "spawn_subagent":
                continue
            args = _safe_json_loads(fn.get("arguments") or "{}")
            if not isinstance(args, dict):
                continue
            agent_id = str(args.get("agent_id") or "").strip()
            if not agent_id:
                continue
            live = dict(live_registry.get(agent_id, {}))
            if round_id and live.get("round_id") and live.get("round_id") != round_id:
                live = {}
            task = str(args.get("task") or live.get("task") or "")
            _merge_subagent_record(entries, agent_id, {
                "task": task,
                "status": live.get("status", entries.get(agent_id, {}).get("status", "done")),
                "result": live.get("result", entries.get(agent_id, {}).get("result", "")),
                "messages": list(live.get("messages", [])) or list(entries.get(agent_id, {}).get("messages", [])),
                "created_at": live.get("created_at", entries.get(agent_id, {}).get("created_at")),
                "updated_at": live.get("updated_at", entries.get(agent_id, {}).get("updated_at")),
                "round_id": round_id or live.get("round_id", entries.get(agent_id, {}).get("round_id", "")),
            })
    for agent_id, live in live_registry.items():
        live_round_id = str(live.get("round_id", "")).strip()
        if round_id and live_round_id and live_round_id != round_id:
            continue
        _merge_subagent_record(entries, agent_id, {
            "task": live.get("task", ""),
            "status": live.get("status", "done"),
            "result": live.get("result", ""),
            "messages": list(live.get("messages", [])),
            "created_at": live.get("created_at"),
            "updated_at": live.get("updated_at"),
            "round_id": round_id or live_round_id,
        })
    return entries


def _related_round_agent_names(seed_ids: set[str], round_id: str = "") -> set[str]:
    if not seed_ids:
        return set()
    related = set(seed_ids)
    inbox_root = DATA_DIR / "inbox"
    if not inbox_root.exists():
        return related

    changed = True
    while changed:
        changed = False
        for msg_file in inbox_root.glob("*/*.json"):
            try:
                payload = json.loads(msg_file.read_text(encoding="utf-8"))
            except Exception:
                continue
            if round_id and str(payload.get("round_id", "")) != round_id:
                continue
            from_agent = str(payload.get("from", ""))
            to_agent = str(payload.get("to", ""))
            if from_agent in related or to_agent in related:
                size_before = len(related)
                if from_agent:
                    related.add(from_agent)
                if to_agent:
                    related.add(to_agent)
                changed = changed or len(related) != size_before
    return related


def _round_id_from_messages(raw_msgs: list[dict]) -> str:
    for msg in raw_msgs:
        round_id = str(msg.get("round_id", "")).strip()
        if round_id:
            return round_id
    return ""


def _latest_round_id_from_messages(raw_msgs: list[dict]) -> str:
    for msg in reversed(raw_msgs):
        round_id = str(msg.get("round_id", "")).strip()
        if round_id:
            return round_id
    return ""


def _events_for_round(recent_events: list[dict], round_id: str) -> list[dict]:
    if not round_id:
        return list(recent_events)
    return [
        event for event in recent_events
        if str(event.get("round_id", "")).strip() == round_id
    ]


def _subagent_matches_round(subagent: dict[str, Any], round_id: str) -> bool:
    if not round_id:
        return True
    subagent_round_id = str(subagent.get("roundId") or subagent.get("round_id") or "").strip()
    return not subagent_round_id or subagent_round_id == round_id


def _registry_status_from_ui(status: str) -> str:
    return {
        "running": "running",
        "queued": "waiting",
        "done": "done",
        "err": "timeout",
    }.get(status, status)


def _is_summary_agent_id(agent_id: str) -> bool:
    return str(agent_id or "").startswith("agent_summary_")


def _iter_flow_snapshots(raw_msgs: list[dict], round_id: str = "") -> list[dict[str, Any]]:
    snapshots: list[dict[str, Any]] = []
    for msg in raw_msgs:
        snapshot = msg.get("subagent_flow_snapshot")
        if not isinstance(snapshot, dict):
            continue
        snapshot_round_id = str(snapshot.get("round_id", "")).strip() or str(msg.get("round_id", "")).strip()
        if round_id and snapshot_round_id and snapshot_round_id != round_id:
            continue
        snapshots.append(snapshot)
    return snapshots


def _merge_subagent_record(entries: dict[str, dict[str, Any]], agent_id: str, meta: dict[str, Any]) -> None:
    incoming = dict(meta)
    incoming_round_id = str(incoming.get("round_id", "")).strip()
    existing = entries.get(agent_id)
    if existing is None:
        entries[agent_id] = incoming
        return

    existing_round_id = str(existing.get("round_id", "")).strip()
    if incoming_round_id and existing_round_id and incoming_round_id != existing_round_id:
        entries[agent_id] = incoming
        return

    merged = dict(existing)
    for key, value in incoming.items():
        if key == "messages":
            if value:
                merged["messages"] = value
            else:
                merged.setdefault("messages", [])
            continue
        if value not in (None, "", []):
            merged[key] = value
        else:
            merged.setdefault(key, value)
    entries[agent_id] = merged


def _snapshot_entries_from_messages(raw_msgs: list[dict], round_id: str = "") -> dict[str, dict[str, Any]]:
    entries: dict[str, dict[str, Any]] = {}
    for snapshot in _iter_flow_snapshots(raw_msgs, round_id=round_id):
        agents = snapshot.get("agents") or {}
        if not isinstance(agents, dict):
            continue
        snapshot_round_id = str(snapshot.get("round_id", "")).strip()
        for agent_id, info in agents.items():
            if not isinstance(info, dict):
                continue
            meta = dict(info)
            meta.setdefault("round_id", snapshot_round_id)
            meta.setdefault("messages", [])
            _merge_subagent_record(entries, str(agent_id), meta)
    return entries


def _snapshot_comm_messages_from_messages(raw_msgs: list[dict], round_id: str = "") -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str, str]] = set()
    for snapshot in _iter_flow_snapshots(raw_msgs, round_id=round_id):
        comm_messages = snapshot.get("comm_messages") or []
        if not isinstance(comm_messages, list):
            continue
        for item in comm_messages:
            if not isinstance(item, dict):
                continue
            from_agent = str(item.get("from", "")).strip()
            to_agent = str(item.get("to", "")).strip()
            body = str(item.get("content", ""))
            message_id = str(item.get("message_id") or "").strip()
            dedupe_key = (message_id, from_agent, to_agent, body)
            if dedupe_key in seen:
                continue
            seen.add(dedupe_key)
            items.append(dict(item))
    items.sort(key=lambda item: str(item.get("timestamp") or ""))
    return items


def _subagent_cards_from_registry(round_registry: dict[str, dict]) -> list[dict]:
    cards: list[dict] = []
    for agent_id, info in round_registry.items():
        status = info.get("status", "done")
        ui_status = {"running": "running", "waiting": "queued", "resumed": "running",
                     "done": "done", "timeout": "err"}.get(status, status)
        created_at = info.get("created_at")
        cards.append({
            "id": agent_id,
            "name": agent_id,
            "status": ui_status,
            "task": info.get("task", ""),
            "tokens": len(info.get("messages", [])),
            "elapsed": _elapsed_since(created_at),
            "progress": _status_progress(status),
            "result": info.get("result", ""),
            "messageCount": len(info.get("messages", [])),
            "createdAt": _short_time(created_at),
            "updatedAt": _short_time(info.get("updated_at")),
        })
    return cards


def _build_live_flow_round(
    prefix: str,
    raw_msgs: list[dict],
    messages: list[dict],
    subagents: list[dict],
    registry: dict[str, dict],
    recent_events: list[dict],
    y_offset: int,
    round_id: str,
) -> tuple[list[dict], list[dict], int]:
    main_x = 320
    main_y = y_offset + 70
    main_tool_x = 600
    subagent_x = 900
    subagent_tool_x = 1220
    output_x = 1540
    subagent_base_y = y_offset + 40
    subagent_gap_y = 220

    last_user = next((m for m in messages if m["role"] == "user"), None)
    latest_main_llm = next((e for e in reversed(recent_events) if e.get("type") == "llm_call" and e.get("caller") == "main_agent"), None)
    latest_phase = next((e for e in reversed(recent_events) if e.get("type") == "phase_transition"), None)
    latest_agent = next((m for m in reversed(messages) if m["role"] == "agent"), None)
    latest_assistant_raw = next((m for m in reversed(raw_msgs) if m.get("role") == "assistant"), None)
    round_title = next((str(m.get("round_title", "")).strip() for m in raw_msgs if m.get("round_title")), "") or "user request"
    system_initiated = any(bool(m.get("system_initiated")) for m in raw_msgs if isinstance(m, dict))
    if system_initiated and round_title == "user request":
        round_title = "proactive check-in"
    main_usage = _usage_totals(raw_msgs)
    main_tool_base_y = main_y + 150

    main_id = f"{prefix}n_main"
    user_id = f"{prefix}n_user"
    output_id = f"{prefix}n_out"
    main_completed = bool(latest_agent)

    tool_nodes, tool_edges = _build_tool_nodes_for_owner(
        owner_node_id=main_id,
        owner_title=f"main agent · {ASSISTANT_NAME}",
        owner_x=main_x,
        owner_y=main_y,
        raw_messages=raw_msgs,
        recent_events=recent_events,
        caller_prefix="main_agent",
        x=main_tool_x,
        base_y=main_tool_base_y,
        owner_completed=main_completed,
    )
    main_status = (
        "running"
        if any(sa["status"] == "running" for sa in subagents) or any(node["status"] == "running" for node in tool_nodes)
        else ("done" if main_completed else "queued")
    )

    nodes = [
        {
            "id": main_id, "kind": "main", "x": main_x, "y": main_y,
            "title": f"main agent · {ASSISTANT_NAME}",
            "subtitle": latest_phase["to"] if latest_phase and latest_phase.get("to") else "orchestrator",
            "status": main_status,
            "model": _get_model(),
            "detail": {
                "systemPrompt": (
                    f"You are {ASSISTANT_NAME}. Two-phase loop: lightweight tool decision, "
                    "then full tool loop with subagent spawn. Chat filter applies SOUL.md voice."
                ),
                "reasoning": (
                    latest_assistant_raw.get("reasoning_content")
                    if latest_assistant_raw and latest_assistant_raw.get("reasoning_content")
                    else latest_main_llm.get("response")
                    if latest_main_llm and latest_main_llm.get("response")
                    else latest_phase.get("detail")
                    if latest_phase and latest_phase.get("detail")
                    else "Session step completed."
                ),
                "tokensIn": main_usage.get("prompt_tokens") if main_usage.get("prompt_tokens") is not None else "—",
                "tokensOut": main_usage.get("completion_tokens") if main_usage.get("completion_tokens") is not None else "—",
                "model": _get_model(), "temp": 0.2,
            },
        },
    ]
    edges: list[dict[str, Any]] = []
    if last_user and not system_initiated:
        user_text = str(last_user.get("body") or "").strip() or (
            "[Uploaded attachment]"
            if last_user.get("attachments")
            else "—"
        )
        nodes.insert(0, {
            "id": user_id, "kind": "input", "x": 40, "y": y_offset + 80,
            "title": round_title, "status": "done",
            "detail": {
                "role": "User",
                "text": user_text,
                "tokens": 0,
                "time": last_user["time"] if last_user else "—",
            },
        })
        edges.append({"from": user_id, "to": main_id, "kind": "active" if main_status == "running" else None})
    nodes.extend(tool_nodes)
    edges.extend(tool_edges)

    agent_node_ids: dict[str, str] = {}
    subagent_bottoms: list[int] = []
    subagent_y = subagent_base_y
    for i, sa in enumerate(subagents):
        nid = f"{prefix}n_sa_{i}"
        agent_node_ids[sa["name"]] = nid
        is_summary_agent = _is_summary_agent_id(sa["name"])
        info = registry.get(sa["name"], {})
        agent_messages = info.get("messages", [])
        latest_subassistant = next((m for m in reversed(agent_messages) if m.get("role") == "assistant"), None)
        sub_usage = _usage_totals(agent_messages)
        sub_tool_count = _count_tool_nodes_for_owner(
            raw_messages=agent_messages,
            recent_events=recent_events,
            caller_prefix=f"subagent_{sa['name']}",
        )
        nodes.append({
            "id": nid, "kind": "subagent",
            "x": subagent_x, "y": subagent_y,
            "title": f"{'summary subagent' if is_summary_agent else 'subagent'} · {sa['name']}",
            "subtitle": ("synthesizer" if is_summary_agent else sa["task"][:30]),
            "status": sa["status"],
            "detail": {
                "name": sa["name"],
                "task": sa["task"],
                "parent": "main agent",
                "role": "summary" if is_summary_agent else "worker",
                "spawnedAt": sa.get("createdAt", "—"),
                "tokensIn": sub_usage.get("prompt_tokens") if sub_usage.get("prompt_tokens") is not None else "—",
                "tokensOut": sub_usage.get("completion_tokens") if sub_usage.get("completion_tokens") is not None else "—",
                "model": _get_model(),
                "reasoning": latest_subassistant.get("reasoning_content") if latest_subassistant else "",
                "result": sa.get("result", ""),
            },
        })
        edges.append({
            "from": main_id,
            "to": nid,
            "kind": "dashed" if is_summary_agent else ("active" if sa["status"] == "running" else None),
        })

        sub_nodes, sub_edges = _build_tool_nodes_for_owner(
            owner_node_id=nid,
            owner_title=f"subagent · {sa['name']}",
            owner_x=subagent_x,
            owner_y=subagent_y,
            raw_messages=agent_messages,
            recent_events=recent_events,
            caller_prefix=f"subagent_{sa['name']}",
            x=subagent_tool_x,
            base_y=subagent_y,
            owner_completed=sa["status"] in {"done", "err"},
        )
        nodes.extend(sub_nodes)
        edges.extend(sub_edges)
        lane_height = _agent_lane_height(sub_tool_count)
        subagent_bottoms.append(subagent_y + lane_height)
        subagent_y += lane_height + subagent_gap_y

    summary_agent_name = next((name for name in agent_node_ids if _is_summary_agent_id(name)), "")
    if summary_agent_name:
        summary_node_id = agent_node_ids[summary_agent_name]
        for agent_name, node_id in agent_node_ids.items():
            if agent_name == summary_agent_name:
                continue
            edges.append({"from": node_id, "to": summary_node_id, "kind": "dashed"})

    edges.extend(_build_comm_edges(
        agent_node_ids,
        agent_entries=registry,
        round_id=round_id,
        persisted_messages=_snapshot_comm_messages_from_messages(raw_msgs, round_id=round_id),
    ))

    output_content = str(latest_agent.get("body") or "") if latest_agent else ""
    output_status = "done" if output_content else ("running" if subagents else "queued")
    if output_content or subagents:
        flow_bottom = max(subagent_bottoms) if subagent_bottoms else (main_tool_base_y + _agent_lane_height(max(1, len(tool_nodes))))
        output_y = y_offset + 90 if not subagents else max(y_offset + 90, int((main_y + flow_bottom) / 2) - 43)
        nodes.append({
            "id": output_id, "kind": "output", "x": output_x, "y": output_y,
            "title": "response", "status": output_status,
            "detail": {
                "kind": "Output",
                "content": output_content or "Waiting for subagent synthesis…",
            },
        })
        edges.append({
            "from": main_id,
            "to": output_id,
            "kind": "active" if output_status == "running" else None,
        })
        if summary_agent_name:
            edges.append({
                "from": agent_node_ids[summary_agent_name],
                "to": output_id,
                "kind": "dashed",
            })

    bottom = max((node["y"] + 86) for node in nodes) if nodes else y_offset
    return nodes, edges, bottom


def _empty_session() -> dict:
    """Placeholder when no real session exists yet."""
    return {
        "id": "run_empty",
        "title": "no active session",
        "status": "queued",
        "started": "—",
        "dur": "—",
        "preview": "Send a message to start a session.",
        "model": _get_model(),
        "summary": {"tokens": "0", "spend": "$0.00", "toolCalls": 0},
        "chat": {
            "contextChips": _build_context_chips(),
            "messages": [],
        },
        "liveRounds": [],
        "shells": [],
        "subagents": [],
        "flow": {
            "nodes": [
                {
                    "id": "n_main", "kind": "main", "x": 200, "y": 80,
                    "title": f"main agent · {ASSISTANT_NAME}",
                    "subtitle": "idle", "status": "queued",
                    "model": _get_model(),
                    "detail": {
                        "systemPrompt": f"You are {ASSISTANT_NAME}.",
                        "reasoning": "Waiting for user input.",
                        "tokensIn": 0, "tokensOut": 0,
                        "model": _get_model(), "temp": 0.2,
                    },
                }
            ],
            "edges": [],
        },
    }


# ---------------------------------------------------------------------------
# Status
# ---------------------------------------------------------------------------


async def _build_status() -> dict:
    """Status data for the Status / Dashboard page."""
    return {
        "phase": "evolve",
        "state": "进化",
        "metrics": [],
        "sparkData": [],
        "workers": [],
        "logs": [],
        "services": [],
        "model": _get_model(),
        "base_url": _get_base_url(),
        "short_term_entries": 0,
        "session_messages": 0,
        "scheduled_tasks": 0,
        "soul_exists": SOUL_PATH.exists(),
    }


async def _build_memory() -> dict:
    """Assemble full memory state for the Memory page."""
    import re
    from datetime import datetime, timezone

    # --- SOUL.md ---
    soul_content = read_soul()
    soul_exists = bool(soul_content)
    sections: list[dict] = []
    current_section: dict | None = None
    temporary_count = 0
    temporary_expired = 0
    now = datetime.now(timezone.utc)

    for line in soul_content.splitlines() if soul_content else []:
        trimmed = line.strip()
        if trimmed.startswith("## ") and not trimmed.startswith("### "):
            if current_section:
                sections.append(current_section)
            name = trimmed[3:].strip()
            current_section = {"name": name, "entries": [], "entry_count": 0}
        elif current_section is not None:
            if trimmed and not trimmed.startswith("<!--"):
                current_section["entries"].append(trimmed)
                current_section["entry_count"] += 1
                if current_section["name"] == "TEMPORARY":
                    temporary_count += 1
                    date_match = re.search(r"(\d{4}-\d{2}-\d{2})", trimmed)
                    if date_match:
                        try:
                            item_date = datetime.strptime(date_match.group(1), "%Y-%m-%d").replace(tzinfo=timezone.utc)
                            if (now - item_date).days >= 1:
                                temporary_expired += 1
                        except ValueError:
                            pass
    if current_section:
        sections.append(current_section)

    # --- Short-term memory ---
    st_entries = load_entries()
    short_term = {
        "entries": sorted(st_entries, key=lambda e: e.get("last_mentioned", ""), reverse=True),
        "total": len(st_entries),
    }

    # --- Context window ---
    session_msgs: list = []
    if STATE_FILE.exists():
        try:
            session_msgs = json.loads(STATE_FILE.read_text(encoding="utf-8")).get("messages", [])
        except Exception:
            session_msgs = []
    from cyrene.config_store import get_current_ctx_limit
    from cyrene.call_llm import _message_token_estimate
    _ctx_limit = get_current_ctx_limit()
    context_window = {
        "messages": len(session_msgs),
        "max": 40,
        "tokens": sum(_message_token_estimate(m) for m in session_msgs) if session_msgs else 0,
        "ctx_limit": _ctx_limit,
        "trigger_tokens": int(_ctx_limit * 0.6) if _ctx_limit else 0,
        "compacted_blocks": sum(1 for m in session_msgs if isinstance(m, dict) and m.get("compacted_block")),
    }

    # --- Conversation archive ---
    archive_days = 0
    today_exchanges = 0
    if CONVERSATIONS_DIR.exists():
        archive_files = sorted(CONVERSATIONS_DIR.glob("*.md"))
        archive_days = len(archive_files)
        today_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        today_file = CONVERSATIONS_DIR / f"{today_str}.md"
        if today_file.exists():
            try:
                raw = today_file.read_text(encoding="utf-8")
                today_exchanges = raw.count("## ") - 1
            except Exception:
                pass

    return {
        "soul": {
            "exists": soul_exists,
            "path": str(get_soul_path()),
            "sections": sections,
            "temporary_count": temporary_count,
            "temporary_expired": temporary_expired,
        },
        "short_term": short_term,
        "context_window": context_window,
        "archive": {
            "days": archive_days,
            "today_exchanges": max(0, today_exchanges),
        },
    }


async def _build_dashboard(ui_tz=None) -> dict:
    """Aggregate homepage data from memory, soul, archive, and scheduler state."""
    from cyrene import db as cy_db
    from cyrene.subagent import _registry  # noqa: WPS437

    ui_tz = ui_tz or (datetime.now().astimezone().tzinfo or timezone.utc)
    now_local = datetime.now(ui_tz)

    st_entries = load_entries()
    try:
        tasks = await cy_db.get_all_tasks(_db_path)
    except Exception:
        tasks = []

    today = now_local.strftime("%Y-%m-%d")
    soul_content = read_soul()
    soul_path = get_soul_path()
    soul_stat = soul_path.stat() if soul_path.exists() else None
    soul_lines = [line.strip() for line in soul_content.splitlines() if line.strip().startswith("- ")]
    recent_soul_items = soul_lines[-3:]
    recent_memories = sorted(
        st_entries,
        key=lambda entry: (str(entry.get("last_mentioned", "")), int(entry.get("mention_count", 0))),
        reverse=True,
    )[:6]

    today_entries = [
        entry for entry in st_entries
        if str(entry.get("last_mentioned", "")).strip() == today
    ]
    learned_today = sorted(
        today_entries,
        key=lambda entry: (int(entry.get("mention_count", 0)), abs(int(entry.get("emotional_valence", 0)))),
        reverse=True,
    )[:4]

    session_msgs: list[dict[str, Any]] = []
    if STATE_FILE.exists():
        try:
            session_state = json.loads(STATE_FILE.read_text(encoding="utf-8"))
            session_msgs = session_state.get("messages", []) if isinstance(session_state, dict) else []
        except Exception:
            session_msgs = []
    session_usage = _usage_totals(session_msgs)
    subagent_usage = _merge_usage_totals(*[
        _usage_totals(info.get("messages", []))
        for info in _registry.values()
    ])
    combined_usage = _merge_usage_totals(session_usage, subagent_usage)

    reminder_items = []
    for task in sorted(tasks, key=lambda item: str(item.get("next_run") or "")):
        next_run = str(task.get("next_run") or "").strip()
        status = str(task.get("status") or "").strip()
        if not next_run or status not in {"active", "paused"}:
            continue
        reminder_items.append({
            "id": str(task.get("id") or ""),
            "prompt": str(task.get("prompt") or "").strip(),
            "next_run": next_run,
            "schedule_type": str(task.get("schedule_type") or "").strip(),
            "status": status,
        })
    reminder_items = reminder_items[:6]

    archive_snippets: list[dict[str, Any]] = []
    for filepath in sorted(CONVERSATIONS_DIR.glob("*.md"), reverse=True)[:7]:
        date_str = filepath.stem
        try:
            sections = _parse_archive_sections(filepath.read_text(encoding="utf-8"))
        except Exception:
            continue
        for section in reversed(sections):
            user_body = str(section.get("user_body", "")).strip()
            assistant_body = str(section.get("assistant_body", "")).strip()
            if user_body or assistant_body:
                archive_snippets.append({
                    "date": date_str,
                    "title": str(section.get("round_title") or section.get("session_title") or "").strip(),
                    "user": user_body,
                    "assistant": assistant_body,
                })
    archive_snippets = archive_snippets[:6]

    hist_days = 27
    day_from = (now_local - timedelta(days=hist_days)).strftime("%Y-%m-%d")
    day_to = today
    stats_rows = await cy_db.get_daily_stats_range(_db_path, day_from, day_to)
    stats_by_day = {
        str(row.get("day") or ""): row
        for row in stats_rows
        if str(row.get("day") or "").strip()
    }
    model_stats_rows = await cy_db.get_model_stats_range(_db_path, day_from, day_to)
    topic_rows = await cy_db.get_topic_counts_range(_db_path, day_from, day_to, limit=18)
    archive_day_count = await cy_db.count_stat_days(_db_path)

    # 从 daily_stats 汇总全量历史数据（与 timeline 同源）
    historical_prompt = sum((r.get("prompt_tokens") or 0) for r in stats_by_day.values())
    historical_completion = sum((r.get("completion_tokens") or 0) for r in stats_by_day.values())
    historical_total = sum((r.get("total_tokens") or 0) for r in stats_by_day.values())
    historical_cache_hit = sum((r.get("cache_hit_tokens") or 0) for r in stats_by_day.values())
    historical_cache_miss = sum((r.get("cache_miss_tokens") or 0) for r in stats_by_day.values())
    historical_requests = sum((r.get("llm_requests") or 0) for r in stats_by_day.values())

    # 按模型计算总花费（不同模型定价不同）
    total_spend = 0.0
    for row in model_stats_rows:
        mdl = str(row.get("model") or "").strip().lower()
        pt = int(row.get("prompt_tokens") or 0)
        ct = int(row.get("completion_tokens") or 0)
        if "opus-4" in mdl:
            total_spend += (pt / 1_000_000) * 15.0 + (ct / 1_000_000) * 75.0
        elif "sonnet-4" in mdl:
            total_spend += (pt / 1_000_000) * 3.0 + (ct / 1_000_000) * 15.0
        elif "haiku-4" in mdl:
            total_spend += (pt / 1_000_000) * 0.25 + (ct / 1_000_000) * 1.25
        elif "deepseek-v4-flash" in mdl:
            total_spend += (pt / 1_000_000) * 0.14 + (ct / 1_000_000) * 0.28
        elif "deepseek-reasoner" in mdl:
            total_spend += (pt / 1_000_000) * 0.55 + (ct / 1_000_000) * 2.19
        elif "deepseek" in mdl or "deepseek-chat" in mdl:
            total_spend += (pt / 1_000_000) * 0.14 + (ct / 1_000_000) * 0.28
        else:
            total_spend += (pt / 1_000_000) * 1.0 + (ct / 1_000_000) * 2.0
    spend_str = "<$0.01" if total_spend < 0.01 else f"${total_spend:.2f}"

    # 情感数据从 short_term 条目按 last_mentioned 日期聚合，不依赖数据库
    emotion_by_day: dict[str, list[float]] = {}
    for entry in st_entries:
        day = str(entry.get("last_mentioned", "")).strip()
        if day:
            valence = int(entry.get("emotional_valence", 0) or 0)
            emotion_by_day.setdefault(day, []).append(valence)

    emotion_series = []
    for offset in range(hist_days, -1, -1):
        day = (now_local - timedelta(days=offset)).strftime("%Y-%m-%d")
        vals = emotion_by_day.get(day, [])
        avg = round(sum(vals) / len(vals), 2) if vals else 0.0
        emotion_series.append({
            "date": day,
            "value": avg,
            "count": len(vals),
        })

    token_timeline: dict[str, dict[str, int]] = {}
    for offset in range(hist_days, -1, -1):
        day = (now_local - timedelta(days=offset)).strftime("%Y-%m-%d")
        row = stats_by_day.get(day) or {}
        token_timeline[day] = {
            "prompt": int(row.get("prompt_tokens") or 0),
            "completion": int(row.get("completion_tokens") or 0),
            "requests": int(row.get("llm_requests") or 0),
        }

    heatmap_days = [
        (now_local - timedelta(days=offset)).strftime("%Y-%m-%d")
        for offset in range(hist_days, -1, -1)
    ]
    heatmap_row_defs = [
        ("00:00", 0, 4),
        ("04:00", 4, 8),
        ("08:00", 8, 12),
        ("12:00", 12, 16),
        ("16:00", 16, 20),
        ("20:00", 20, 24),
    ]
    heatmap_column_map = {
        "00:00": "activity_00_04",
        "04:00": "activity_04_08",
        "08:00": "activity_08_12",
        "12:00": "activity_12_16",
        "16:00": "activity_16_20",
        "20:00": "activity_20_24",
    }
    heatmap_buckets: dict[str, list[int]] = {}
    for label, _, _ in heatmap_row_defs:
        column = heatmap_column_map[label]
        heatmap_buckets[label] = [
            int((stats_by_day.get(day) or {}).get(column) or 0)
            for day in heatmap_days
        ]

    activity_heatmap = {
        "days": heatmap_days,
        "rows": [
            {"label": label, "values": heatmap_buckets[label]}
            for label, _, _ in heatmap_row_defs
        ],
    }

    return {
        "today": {
            "learned": learned_today,
            "learned_count": len(today_entries),
            "memory_count": len(st_entries),
            "archive_days": archive_day_count,
        },
        "soul": {
            "path": str(soul_path),
            "updated_at": datetime.fromtimestamp(soul_stat.st_mtime, tz=timezone.utc).isoformat() if soul_stat else "",
            "recent_items": recent_soul_items,
            "section_count": soul_content.count("\n## ") + (1 if soul_content.strip().startswith("# ") else 0),
        },
        "topic_cloud": topic_rows,
        "emotion": emotion_series,
        "usage": {
            "requests": historical_requests,
            "tokens": _format_tokens({
                "prompt_tokens": historical_prompt,
                "completion_tokens": historical_completion,
                "total_tokens": historical_total,
            }),
            "spend": spend_str,
            "prompt_tokens": historical_prompt,
            "completion_tokens": historical_completion,
            "total_tokens": historical_total,
            "cache_hit_tokens": historical_cache_hit,
            "cache_miss_tokens": historical_cache_miss,
            "total_messages": (session_usage.get("requests") or 0) + (subagent_usage.get("requests") or 0),
            "active_days": sum(1 for row in stats_by_day.values() if int(row.get("llm_requests") or 0) > 0),
            "current_streak": _calc_current_streak(stats_by_day, today),
            "longest_streak": _calc_longest_streak(stats_by_day),
            "peak_hour": _calc_peak_hour(stats_by_day),
            "timeline": [
                {
                    "date": day,
                    "prompt": values["prompt"],
                    "completion": values["completion"],
                    "requests": values["requests"],
                }
                for day, values in token_timeline.items()
            ],
        },
        "reminders": reminder_items,
        "recent_memories": recent_memories,
        "recent_archive": archive_snippets,
        "activity_heatmap": activity_heatmap,
        "model_stats": model_stats_rows,
    }


def _extract_topic_terms(text: str, limit: int = 12) -> list[str]:
    """Extract simple high-signal topic terms from mixed Chinese/English text."""
    source = (text or "").lower()
    english_stop = {
        "the", "and", "for", "that", "this", "with", "from", "have", "about",
        "what", "when", "your", "just", "into", "then", "they", "them", "their",
        "would", "could", "should", "there", "here", "been", "were", "will",
        "some", "more", "than", "after", "before", "need", "want", "like",
        "today", "yesterday", "tomorrow", "really", "also", "maybe", "because",
        "http", "https", "assistant", "cyrene", "user",
    }
    chinese_stop = {
        "今天", "最近", "这个", "那个", "一下", "已经", "我们", "你们", "然后",
        "需要", "可以", "还是", "就是", "一个", "没有", "什么", "怎么", "如果",
        "现在", "自己", "因为", "所以", "以及", "但是", "进行", "相关", "问题",
        "工作", "页面", "功能", "内容",
    }
    tokens = re.findall(r"[\u4e00-\u9fff]{2,}|[a-z][a-z0-9_-]{2,}", source)
    results: list[str] = []
    for token in tokens:
        if token in english_stop or token in chinese_stop:
            continue
        if token.isascii() and len(token) < 4:
            continue
        results.append(token)
        if len(results) >= limit:
            break
    return results


def _read_recent_logs() -> list[dict]:
    """Read the most recent debug log file and convert to status log rows."""
    from cyrene.config import DATA_DIR
    if not DATA_DIR.exists():
        return _placeholder_logs()
    log_files = sorted(DATA_DIR.glob("debug_*.jsonl"), reverse=True)
    if not log_files:
        return _placeholder_logs()
    latest = log_files[0]
    rows: list[dict] = []
    try:
        with open(latest, "r", encoding="utf-8") as fh:
            lines = fh.readlines()
    except Exception:
        return _placeholder_logs()
    for line in lines[-40:]:
        try:
            entry = json.loads(line)
        except Exception:
            continue
        kind = entry.get("type", "info")
        ts = entry.get("timestamp", "")[11:19]
        if kind == "llm_call":
            caller = entry.get("caller", "?")
            phase = entry.get("phase", "?")
            duration = entry.get("duration_ms", 0)
            rows.append({"t": ts, "lvl": "info", "msg": f"{caller} · {phase} · {duration}ms"})
        elif kind == "tool_call":
            caller = entry.get("caller", "?")
            tool = entry.get("tool", "?")
            rows.append({"t": ts, "lvl": "ok", "msg": f"{caller} → {tool}"})
        elif kind == "session_start":
            rows.append({"t": ts, "lvl": "info", "msg": "session started"})
    return list(reversed(rows[-20:]))


def _placeholder_logs() -> list[dict]:
    now = datetime.now(timezone.utc).strftime("%H:%M:%S")
    return [{"t": now, "lvl": "info", "msg": "no debug logs yet — verbose mode is enabled, logs appear after agent runs"}]


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------


def _build_settings_meta() -> dict:
    return {
        "sections": [
            {"id": "general", "label": "General"},
            {"id": "channels", "label": "Channels"},
            {"id": "models", "label": "Models"},
            {"id": "agents", "label": "Agents"},
            {"id": "appearance", "label": "Appearance"},
            {"id": "capabilities", "label": "Capabilities"},
            {"id": "data", "label": "Data"},
            {"id": "about", "label": "About"},
        ],
    }


def _build_config() -> dict:
    settings = get_web_settings()
    live_model, live_base_url = _live_llm_config()
    return {
        "model": live_model,
        "base_url": live_base_url,
        "assistant_name": ASSISTANT_NAME,
        "base_dir": str(BASE_DIR),
        "data_dir": str(DATA_DIR),
        "soul_path": str(SOUL_PATH),
        "workspace_dir": str(WORKSPACE_DIR),
        "soul_content": _read_soul(),
        "search_mode": settings.get("search_mode", "builtin"),
        "search_external_url": settings.get("search_external_url", ""),
        "spawn_policy": settings.get("spawn_policy", "conservative"),
        "heartbeat_interval": settings.get("heartbeat_interval", 1800),
        "wechat_notify_scheduled": settings.get("wechat_notify_scheduled", True),
        "search_port": str(SEARXNG_PORT),
        "search_host": SEARXNG_HOST,
    }


def _build_context_chips() -> list[dict]:
    """Build context chips reflecting current SOUL.md and workspace state."""
    from cyrene.settings_store import is_workspace_active, is_soul_active
    chips = []
    if is_soul_active():
        chips.append({"icon": "🧠", "label": "SOUL.md", "key": "soul"})
    if is_workspace_active():
        chips.append({"icon": "📁", "label": "workspace", "key": "workspace"})
    return chips


def _build_search_config() -> dict:
    settings = get_web_settings()
    return {
        "search_mode": settings.get("search_mode", "builtin"),
        "search_external_url": settings.get("search_external_url", ""),
        "auto_start_enabled": os.getenv("SEARXNG_AUTO_START", "1") not in ("0", "false", "no"),
        "env_searxng_url": os.getenv("SEARXNG_URL", ""),
    }


# ---------------------------------------------------------------------------
# Misc helpers
# ---------------------------------------------------------------------------


def _load_messages() -> list[dict]:
    msgs = _load_state_messages()
    if msgs:
        result = []
        for m in msgs:
            role = m.get("role", "")
            if role not in ("user", "assistant"):
                continue
            content = m.get("content", "")
            if not content or not content.strip():
                continue
            result.append({"role": role, "content": content})
        if result:
            return result

    archive_msgs = _parse_conversation_archive()
    if archive_msgs:
        return archive_msgs

    return []


def _load_state_messages() -> list[dict]:
    if not STATE_FILE.exists():
        return []
    try:
        data = json.loads(STATE_FILE.read_text(encoding="utf-8"))
        return data.get("messages", []) or []
    except Exception:
        return []


def _infer_subagent_entries(raw_msgs: list[dict], registry: dict[str, dict]) -> dict[str, dict]:
    entries: dict[str, dict] = _snapshot_entries_from_messages(raw_msgs)
    for agent_id, info in registry.items():
        _merge_subagent_record(entries, agent_id, dict(info))
    for entry in entries.values():
        entry.setdefault("messages", [])

    spawned: dict[str, dict[str, str]] = {}
    for msg in raw_msgs:
        for tc in msg.get("tool_calls") or []:
            fn = tc.get("function", {})
            if fn.get("name") != "spawn_subagent":
                continue
            args = _safe_json_loads(fn.get("arguments") or "{}")
            if not isinstance(args, dict):
                continue
            agent_id = str(args.get("agent_id") or "").strip()
            if not agent_id:
                continue
            spawned[agent_id] = {
                "task": str(args.get("task") or ""),
                "round_id": str(msg.get("round_id", "")).strip(),
            }

    for agent_id, meta in spawned.items():
        entry = entries.setdefault(agent_id, {})
        meta_round_id = str(meta.get("round_id", "")).strip()
        existing_round_id = str(entry.get("round_id", "")).strip()
        if meta_round_id and existing_round_id and meta_round_id != existing_round_id:
            # Treat a reused agent ID in a later round as a fresh live subagent.
            entry["task"] = meta["task"] or entry.get("task", "")
            entry["round_id"] = meta_round_id
            entry["status"] = "running"
            entry["result"] = ""
            entry["messages"] = []
            entry["created_at"] = None
            entry["updated_at"] = None
            continue
        entry.setdefault("task", meta["task"])
        entry.setdefault("round_id", meta_round_id)
        entry.setdefault("status", "done")
        entry.setdefault("result", "")
        entry.setdefault("messages", [])
        entry.setdefault("created_at", None)
        entry.setdefault("updated_at", None)

    inbox_meta = _scan_inbox_agents()
    for agent_id, meta in inbox_meta.items():
        entry = entries.setdefault(agent_id, {})
        entry.setdefault("task", spawned.get(agent_id, {}).get("task", "Discuss with other subagents"))
        entry.setdefault("status", "done")
        entry.setdefault("result", "")
        if not entry.get("messages"):
            entry["messages"] = [{}] * int(meta.get("message_count") or 0)
        if meta.get("created_at") and not entry.get("created_at"):
            entry["created_at"] = meta["created_at"]
        if meta.get("updated_at") and not entry.get("updated_at"):
            entry["updated_at"] = meta["updated_at"]
        if meta.get("round_id") and not entry.get("round_id"):
            entry["round_id"] = meta["round_id"]

    return entries


def _parse_conversation_archive() -> list[dict]:
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    filepath = CONVERSATIONS_DIR / f"{today}.md"
    if not filepath.exists():
        return []
    content = filepath.read_text(encoding="utf-8")
    messages = []
    current_user = None
    current_lines: list[str] = []
    in_assistant = False
    for line in content.split("\n"):
        if line.startswith("**User**: "):
            if current_user and current_lines:
                messages.append({"role": "user", "content": current_user})
                messages.append({"role": "assistant", "content": "\n".join(current_lines).strip()})
            current_user = line[len("**User**: "):].strip()
            current_lines = []
            in_assistant = False
        elif line.startswith("**") and "**: " in line and not line.startswith("**User**"):
            in_assistant = True
            idx = line.index("**: ")
            current_lines = [line[idx + len("**: "):]]
        elif in_assistant:
            if line.strip() == "---":
                if current_user and current_lines:
                    messages.append({"role": "user", "content": current_user})
                    messages.append({"role": "assistant", "content": "\n".join(current_lines).strip()})
                current_user = None
                current_lines = []
                in_assistant = False
            else:
                current_lines.append(line)
    if current_user and current_lines:
        messages.append({"role": "user", "content": current_user})
        messages.append({"role": "assistant", "content": "\n".join(current_lines).strip()})
    return messages


def _read_soul() -> str:
    try:
        if SOUL_PATH.exists():
            return SOUL_PATH.read_text(encoding="utf-8")
    except Exception:
        pass
    return ""


def _format_duration(seconds: float) -> str:
    seconds = int(seconds)
    m, s = divmod(seconds, 60)
    h, m = divmod(m, 60)
    if h:
        return f"{h}h {m:02d}m"
    return f"{m:02d}:{s:02d}"


def _status_progress(status: str) -> float:
    return {
        "running": 0.45,
        "resumed": 0.65,
        "waiting": 0.82,
        "done": 1.0,
        "timeout": 1.0,
    }.get(status, 0.5)


def _short_time(value: str | None) -> str:
    if not value:
        return "—"
    try:
        return datetime.fromisoformat(value).astimezone().strftime("%H:%M:%S")
    except Exception:
        return "—"


def _elapsed_since(value: str | None) -> str:
    if not value:
        return "—"
    try:
        dt = datetime.fromisoformat(value)
        return _format_duration((datetime.now(timezone.utc) - dt.astimezone(timezone.utc)).total_seconds())
    except Exception:
        return "—"


def _safe_json_loads(value: str) -> dict[str, Any] | list[Any] | None:
    try:
        return json.loads(value)
    except Exception:
        return None


def _summarize_text(value: str, limit: int = 96) -> str:
    text = re.sub(r"\s+", " ", (value or "").strip())
    if len(text) <= limit:
        return text
    return text[:limit] + "…"


def _tool_output_map(raw_messages: list[dict]) -> dict[str, str]:
    outputs: dict[str, str] = {}
    for msg in raw_messages:
        if msg.get("role") == "tool" and msg.get("tool_call_id"):
            outputs[str(msg["tool_call_id"])] = str(msg.get("content") or "")
    return outputs


def _tool_output_ids(raw_messages: list[dict]) -> set[str]:
    return {
        str(msg["tool_call_id"])
        for msg in raw_messages
        if msg.get("role") == "tool" and msg.get("tool_call_id")
    }


def _tool_args_signature(value: Any) -> str:
    parsed = _safe_json_loads(value) if isinstance(value, str) else value
    normalized = parsed if parsed is not None else value
    try:
        return json.dumps(normalized, ensure_ascii=False, sort_keys=True)
    except Exception:
        return json.dumps(str(normalized), ensure_ascii=False)


def _usage_totals(raw_messages: list[dict]) -> dict[str, int | None]:
    totals: dict[str, int | None] = {
        "prompt_tokens": 0,
        "completion_tokens": 0,
        "total_tokens": 0,
        "prompt_cache_hit_tokens": 0,
        "prompt_cache_miss_tokens": 0,
        "requests": 0,
    }
    found = False
    for msg in raw_messages:
        usage = msg.get("usage")
        if not isinstance(usage, dict):
            continue
        totals["requests"] = int(totals["requests"] or 0) + 1
        for key in ("prompt_tokens", "completion_tokens", "total_tokens", "prompt_cache_hit_tokens", "prompt_cache_miss_tokens"):
            value = usage.get(key)
            if isinstance(value, int):
                totals[key] = int(totals[key] or 0) + value
                found = True
    if not found and not totals["requests"]:
        return {key: None for key in totals}
    if not totals["total_tokens"] and (totals["prompt_tokens"] or totals["completion_tokens"]):
        totals["total_tokens"] = int(totals["prompt_tokens"] or 0) + int(totals["completion_tokens"] or 0)
    return totals


def _last_request_context_tokens(raw_msgs: list[dict]) -> int | None:
    """Tokens of the most recent LLM request — approximates current context occupancy.

    Unlike _usage_totals (which sums every request in the session), this returns the
    last request's own token count, so it reflects how full the context window is now.
    """
    for msg in reversed(raw_msgs):
        usage = msg.get("usage")
        if not isinstance(usage, dict):
            continue
        total = usage.get("total_tokens")
        if isinstance(total, int) and total > 0:
            return total
        prompt = usage.get("prompt_tokens")
        if isinstance(prompt, int) and prompt > 0:
            completion = usage.get("completion_tokens")
            return prompt + (completion if isinstance(completion, int) else 0)
    return None


def _merge_usage_totals(*usage_items: dict[str, int | None]) -> dict[str, int | None]:
    merged = {
        "prompt_tokens": 0,
        "completion_tokens": 0,
        "total_tokens": 0,
        "prompt_cache_hit_tokens": 0,
        "prompt_cache_miss_tokens": 0,
        "requests": 0,
    }
    found = False
    for usage in usage_items:
        if not isinstance(usage, dict):
            continue
        for key in merged:
            value = usage.get(key)
            if isinstance(value, int):
                merged[key] += value
                found = True
    if not found:
        return {key: None for key in merged}
    if not merged["total_tokens"] and (merged["prompt_tokens"] or merged["completion_tokens"]):
        merged["total_tokens"] = merged["prompt_tokens"] + merged["completion_tokens"]
    return merged


def _format_tokens(usage: dict[str, int | None] | None) -> str:
    if not isinstance(usage, dict):
        return "—"
    prompt_tokens = usage.get("prompt_tokens")
    completion_tokens = usage.get("completion_tokens")
    total_tokens = usage.get("total_tokens")
    parts: list[str] = []
    if prompt_tokens is not None:
        parts.append(f"{_fmt_tok(prompt_tokens)} in")
    if completion_tokens is not None:
        parts.append(f"{_fmt_tok(completion_tokens)} out")
    if total_tokens is not None:
        parts.append(f"{_fmt_tok(total_tokens)} total")
    return " / ".join(parts) if parts else "—"


def _fmt_tok(n: int) -> str:
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if n >= 1_000:
        return f"{n / 1_000:.1f}k"
    return str(n)


def _model_pricing() -> dict[str, float] | None:
    """Return token pricing metadata for known models, or None."""
    model_lower = _get_model().lower()
    if "opus-4" in model_lower or "claude-opus-4" in model_lower:
        return {"input": 15.0, "output": 75.0}
    if "sonnet-4" in model_lower or "claude-sonnet-4" in model_lower:
        return {"input": 3.0, "output": 15.0}
    if "haiku-4" in model_lower or "claude-haiku-4" in model_lower:
        return {"input": 0.25, "output": 1.25}
    if "deepseek-v4-flash" in model_lower:
        return {"input": 0.14, "output": 0.28, "cache_hit": 0.0}
    if "deepseek-v4" in model_lower or "deepseek-chat" in model_lower:
        return {"input": 0.14, "output": 0.28, "cache_hit": 0.05}
    if "deepseek-reasoner" in model_lower:
        return {"input": 0.55, "output": 2.19, "cache_hit": 0.14}
    return None


def _calc_spend(usage: dict[str, int | None] | None) -> str:
    if not isinstance(usage, dict):
        return "—"
    pricing = _model_pricing()
    if pricing is None:
        return "—"
    prompt_tokens = usage.get("prompt_tokens")
    completion_tokens = usage.get("completion_tokens")
    cache_hit_tokens = usage.get("prompt_cache_hit_tokens")
    cache_miss_tokens = usage.get("prompt_cache_miss_tokens")
    input_price = pricing["input"]
    output_price = pricing["output"]
    cache_hit_price = pricing.get("cache_hit", input_price)
    cost = 0.0
    if isinstance(cache_hit_tokens, int) and isinstance(cache_miss_tokens, int) and (cache_hit_tokens or cache_miss_tokens):
        cost += (cache_hit_tokens / 1_000_000) * cache_hit_price
        cost += (cache_miss_tokens / 1_000_000) * input_price
    elif prompt_tokens is not None:
        cost += (prompt_tokens / 1_000_000) * input_price
    if completion_tokens is not None:
        cost += (completion_tokens / 1_000_000) * output_price
    if cost < 0.01:
        return "<$0.01"
    return f"${cost:.2f}"


def _calc_current_streak(stats_by_day: dict[str, dict], today: str) -> int:
    streak = 0
    for offset in range(366):
        day = (datetime.strptime(today, "%Y-%m-%d") - timedelta(days=offset)).strftime("%Y-%m-%d")
        row = stats_by_day.get(day)
        if row and int(row.get("llm_requests") or 0) > 0:
            streak += 1
        else:
            break
    return streak


def _calc_longest_streak(stats_by_day: dict[str, dict]) -> int:
    longest = 0
    current = 0
    for offset in range(365):
        day = (datetime.now() - timedelta(days=offset)).strftime("%Y-%m-%d")
        row = stats_by_day.get(day)
        if row and int(row.get("llm_requests") or 0) > 0:
            current += 1
            longest = max(longest, current)
        else:
            current = 0
    return longest


_ACTIVITY_COLUMNS = [
    ("activity_00_04", "00:00-04:00"),
    ("activity_04_08", "04:00-08:00"),
    ("activity_08_12", "08:00-12:00"),
    ("activity_12_16", "12:00-16:00"),
    ("activity_16_20", "16:00-20:00"),
    ("activity_20_24", "20:00-24:00"),
]


def _calc_peak_hour(stats_by_day: dict[str, dict]) -> str:
    totals: dict[str, int] = {}
    for col, _label in _ACTIVITY_COLUMNS:
        totals[col] = sum(int(row.get(col) or 0) for row in stats_by_day.values())
    best_col = max(totals, key=totals.get) if any(totals.values()) else ""
    for col, label in _ACTIVITY_COLUMNS:
        if col == best_col:
            return label
    return "—"


def _build_shells_from_messages(raw_msgs: list[dict]) -> list[dict]:
    """Extract bash/shell tool calls from raw messages and build shell entries."""
    shells: list[dict] = []
    tool_results: dict[str, str] = {}
    for msg in raw_msgs:
        if msg.get("role") == "tool" and msg.get("tool_call_id"):
            tool_results[str(msg["tool_call_id"])] = str(msg.get("content") or "")

    shell_index = 0
    for msg in raw_msgs:
        if msg.get("role") != "assistant":
            continue
        for tc in msg.get("tool_calls") or []:
            fn = tc.get("function", {})
            name = fn.get("name", "")
            if name.lower() not in ("bash", "shell", "cmd", "terminal"):
                continue
            args_str = fn.get("arguments", "{}")
            try:
                args = json.loads(args_str) if isinstance(args_str, str) else args_str
            except Exception:
                args = {}
            if not isinstance(args, dict):
                args = {}
            cmd = args.get("command") or args.get("cmd") or json.dumps(args)
            cwd = args.get("cwd") or args.get("workdir") or "workspace/"
            result = tool_results.get(str(tc.get("id")), "")
            lines: list[dict] = [
                {"kind": "shell-prompt", "text": f"$ {cmd}"},
            ]
            if result:
                for line in result.strip().split("\n")[:30]:
                    lines.append({"kind": "shell-out", "text": line})
            else:
                lines.append({"kind": "shell-out", "text": "(running…)"})

            shells.append({
                "id": f"shell_{shell_index}",
                "cwd": cwd,
                "pid": "—",
                "lines": lines,
            })
            shell_index += 1

    return shells


def _build_tool_nodes_for_owner(
    owner_node_id: str,
    owner_title: str,
    owner_x: int,
    owner_y: int,
    raw_messages: list[dict],
    recent_events: list[dict],
    caller_prefix: str,
    x: int,
    base_y: int,
    owner_completed: bool = False,
) -> tuple[list[dict], list[dict]]:
    nodes: list[dict] = []
    edges: list[dict] = []
    tool_outputs = _tool_output_map(raw_messages)
    tool_output_ids = _tool_output_ids(raw_messages)
    tool_index = 0

    for msg_index, msg in enumerate(raw_messages):
        tool_calls = msg.get("tool_calls") or []
        for call_index, tc in enumerate(tool_calls):
            fn = tc.get("function", {})
            raw_args = fn.get("arguments") or "{}"
            parsed_args = _safe_json_loads(raw_args) if isinstance(raw_args, str) else raw_args
            tool_call_id = str(tc.get("id") or "")
            output = tool_outputs.get(tool_call_id, "")
            has_output = tool_call_id in tool_output_ids
            has_followup = any(
                later.get("role") in {"assistant", "tool", "user"}
                for later in raw_messages[msg_index + 1:]
            )
            status = "done" if has_output or has_followup or owner_completed else "running"
            if has_output:
                output_detail = output or "Completed with no captured output."
            elif status == "done":
                output_detail = "Completed after follow-up activity; no tool output was captured."
            else:
                output_detail = "Running…"
            nid = f"{owner_node_id}_tool_{msg_index}_{call_index}"
            nodes.append({
                "id": nid,
                "kind": "tool",
                "x": x,
                "y": base_y + tool_index * 112,
                "title": fn.get("name", "tool"),
                "subtitle": _summarize_text(str(raw_args), 36) if raw_args else "",
                "status": status,
                "detail": {
                    "name": fn.get("name", "tool"),
                    "owner": owner_title,
                    "input": parsed_args if parsed_args is not None else raw_args,
                    "output": output_detail,
                    "duration": "—",
                },
            })
            edges.append({
                "from": owner_node_id,
                "to": nid,
                "kind": "active" if status == "running" else None,
            })
            tool_index += 1

    overlay_events = [
        event for event in recent_events
        if event.get("type") == "tool_call" and str(event.get("caller", "")).startswith(caller_prefix)
    ][-6:]
    for event_index, event in enumerate(overlay_events):
        event_signature = _tool_args_signature(event.get("args", {}))
        if any(
            node["detail"].get("name") == event.get("tool")
            and _tool_args_signature(node["detail"].get("input", {})) == event_signature
            for node in nodes
        ):
            continue
        nid = f"{owner_node_id}_live_tool_{event_index}"
        nodes.append({
            "id": nid,
            "kind": "tool",
            "x": x,
            "y": base_y + tool_index * 112,
            "title": event.get("tool", "tool"),
            "subtitle": _summarize_text(json.dumps(event.get("args", {}), ensure_ascii=False), 36),
            "status": "done",
            "detail": {
                "name": event.get("tool", "tool"),
                "owner": owner_title,
                "input": event.get("args", {}),
                "output": event.get("result_preview", "Completed."),
                "duration": "recent",
                "eventKey": f"{event.get('tool')}::{event_signature}",
            },
        })
        edges.append({"from": owner_node_id, "to": nid})
        tool_index += 1

    return nodes, edges


def _count_tool_nodes_for_owner(
    raw_messages: list[dict],
    recent_events: list[dict],
    caller_prefix: str,
) -> int:
    count = sum(len(msg.get("tool_calls") or []) for msg in raw_messages)
    message_keys = {
        (
            tc.get("function", {}).get("name", "tool"),
            json.dumps(
                _safe_json_loads(tc.get("function", {}).get("arguments") or "{}")
                if isinstance(tc.get("function", {}).get("arguments"), str)
                else (tc.get("function", {}).get("arguments") or {}),
                ensure_ascii=False,
                sort_keys=True,
            ),
        )
        for msg in raw_messages
        for tc in (msg.get("tool_calls") or [])
    }
    overlay_events = [
        event for event in recent_events
        if event.get("type") == "tool_call" and str(event.get("caller", "")).startswith(caller_prefix)
    ][-6:]
    overlay_count = 0
    for event in overlay_events:
        event_key = (
            event.get("tool", "tool"),
            json.dumps(event.get("args", {}), ensure_ascii=False, sort_keys=True),
        )
        if event_key in message_keys:
            continue
        overlay_count += 1
    return count + overlay_count


def _agent_lane_height(tool_count: int) -> int:
    base_height = 86
    if tool_count <= 0:
        return base_height
    return max(base_height, base_height + (tool_count - 1) * 112)


def _build_comm_edges(
    agent_node_ids: dict[str, str],
    agent_entries: dict[str, dict[str, Any]] | None = None,
    round_id: str = "",
    persisted_messages: list[dict[str, Any]] | None = None,
) -> list[dict]:
    edges: list[dict] = []
    if not agent_node_ids:
        return edges

    # Track per-pair messages for threading and weight
    pair_messages: dict[tuple[str, str], list[dict]] = {}
    pair_index: dict[tuple[str, str, str, str], int] = {}
    # Map to deduplicate: (from_agent, to_agent, content[:80]) -> edge_index
    content_index: dict[tuple[str, str, str], int] = {}

    def _add_message_to_pair(
        from_agent: str,
        to_agent: str,
        body: str,
        *,
        label: str = "chat",
        timestamp: str = "",
        source: str = "",
        summary: str = "",
        priority: str = "normal",
        raw_timestamp: str = "",
    ) -> None:
        if from_agent not in agent_node_ids or to_agent not in agent_node_ids:
            return
        if not body.strip():
            return

        pair_key = (from_agent, to_agent)
        content_key = (from_agent, to_agent, body[:80])

        if content_key in content_index:
            # Update existing edge with richer metadata
            idx = content_index[content_key]
            existing_msg = edges[idx].setdefault("message", {})
            if (not existing_msg.get("time") or existing_msg.get("time") == "—") and timestamp:
                existing_msg["time"] = _short_time(timestamp)
            if summary and not existing_msg.get("summary"):
                existing_msg["summary"] = summary
            if priority == "high":
                existing_msg["priority"] = "high"
            # Increment weight even for duplicates (counts total messages)
            edges[idx]["weight"] = edges[idx].get("weight", 1) + 1
            pair_messages.setdefault(pair_key, []).append({
                "from": from_agent,
                "to": to_agent,
                "body": body,
                "label": label,
                "time": _short_time(timestamp) if timestamp else "—",
                "summary": summary,
                "priority": priority,
                "source": source,
            })
            return

        edge_summary = summary if summary else _summarize_text(body, 90)
        edge_label = label
        if priority == "high":
            edge_label = label + " !"

        edge_entry = {
            "from": agent_node_ids[from_agent],
            "to": agent_node_ids[to_agent],
            "kind": "comm",
            "label": edge_label,
            "weight": 1,
            "message": {
                "time": _short_time(timestamp) if timestamp else "—",
                "raw_timestamp": raw_timestamp or timestamp or "",
                "summary": edge_summary,
                "body": body,
                "source": source or "tool_call",
                "msg_type": label,
                "priority": priority,
            },
        }
        edges.append(edge_entry)
        content_index[content_key] = len(edges) - 1
        pair_messages.setdefault(pair_key, []).append({
            "from": from_agent,
            "to": to_agent,
            "body": body,
            "label": label,
            "time": _short_time(timestamp) if timestamp else "—",
            "raw_timestamp": raw_timestamp or timestamp or "",
            "summary": edge_summary,
            "priority": priority,
            "source": source,
        })

    for agent_name, info in (agent_entries or {}).items():
        if agent_name not in agent_node_ids:
            continue
        messages = info.get("messages", []) or []
        tool_outputs = {
            str(msg.get("tool_call_id") or ""): str(msg.get("content") or "")
            for msg in messages
            if isinstance(msg, dict) and msg.get("role") == "tool" and msg.get("tool_call_id")
        }
        for msg in messages:
            if not isinstance(msg, dict) or msg.get("role") != "assistant":
                continue
            for tc in msg.get("tool_calls") or []:
                fn = tc.get("function", {}) if isinstance(tc, dict) else {}
                tool_name = str(fn.get("name") or "").strip()
                if tool_name not in ("send_agent_message", "broadcast_agent_message"):
                    continue
                args = _safe_json_loads(fn.get("arguments") or "{}")
                if not isinstance(args, dict):
                    continue
                output = tool_outputs.get(str(tc.get("id") or ""), "")
                output_lower = output.lower()
                if output and "message sent to" not in output_lower and "broadcast sent to" not in output_lower:
                    continue
                body = str(args.get("content") or "")
                if tool_name == "broadcast_agent_message":
                    # Broadcast edges go to each peer
                    peer_ids = [aid for aid in agent_node_ids if aid != agent_name]
                    for peer_id in peer_ids:
                        _add_message_to_pair(agent_name, peer_id, body, label="progress", source="tool_call")
                else:
                    to_agent = str(args.get("to") or "").strip()
                    _add_message_to_pair(agent_name, to_agent, body, source="tool_call")

    for payload in persisted_messages or []:
        if not isinstance(payload, dict):
            continue
        if round_id and str(payload.get("round_id", "")).strip() != round_id:
            continue
        _add_message_to_pair(
            str(payload.get("from", "")).strip(),
            str(payload.get("to", "")).strip(),
            str(payload.get("content", "")),
            label=str(payload.get("type", "chat") or "chat"),
            timestamp=str(payload.get("timestamp", "") or ""),
            source="snapshot_log",
            summary=str(payload.get("summary", "") or ""),
            priority=str(payload.get("priority", "normal") or "normal"),
        )

    for agent_name in agent_node_ids:
        inbox_dir = DATA_DIR / "inbox" / agent_name
        if not inbox_dir.exists():
            continue
        for msg_file in sorted(inbox_dir.glob("msg_*.json")):
            try:
                payload = json.loads(msg_file.read_text(encoding="utf-8"))
            except Exception:
                continue
            from_agent = str(payload.get("from", ""))
            to_agent = str(payload.get("to", ""))
            if round_id and str(payload.get("round_id", "")) != round_id:
                continue
            _add_message_to_pair(
                from_agent,
                to_agent,
                str(payload.get("content", "")),
                label=str(payload.get("type", "chat") or "chat"),
                timestamp=str(payload.get("timestamp", "") or ""),
                source="inbox_log",
                summary=str(payload.get("summary", "") or ""),
                priority=str(payload.get("priority", "normal") or "normal"),
            )

    # Attach all messages for each pair to the edge
    for i, edge in enumerate(edges):
        pair = None
        for (f, t), msgs in pair_messages.items():
            if edge["from"] == agent_node_ids.get(f) and edge["to"] == agent_node_ids.get(t):
                pair = (f, t)
                edge["messages"] = msgs
                break
        if pair:
            edge["weight"] = len(pair_messages.get(pair, []))

    return edges


def _scan_inbox_agents() -> dict[str, dict[str, Any]]:
    agents: dict[str, dict[str, Any]] = {}
    inbox_root = DATA_DIR / "inbox"
    if not inbox_root.exists():
        return agents

    for inbox_dir in sorted(path for path in inbox_root.iterdir() if path.is_dir()):
        agent_id = inbox_dir.name
        timestamps: list[str] = []
        round_ids: list[str] = []
        msg_count = 0
        for msg_file in sorted(inbox_dir.glob("msg_*.json")):
            try:
                payload = json.loads(msg_file.read_text(encoding="utf-8"))
            except Exception:
                continue
            msg_count += 1
            timestamp = payload.get("timestamp")
            if isinstance(timestamp, str) and timestamp:
                timestamps.append(timestamp)
            round_id = str(payload.get("round_id", "")).strip()
            if round_id:
                round_ids.append(round_id)

        if msg_count == 0:
            continue

        timestamps.sort()
        agents[agent_id] = {
            "message_count": msg_count,
            "created_at": timestamps[0] if timestamps else None,
            "updated_at": timestamps[-1] if timestamps else None,
            "round_id": round_ids[-1] if round_ids else "",
        }

    return agents
