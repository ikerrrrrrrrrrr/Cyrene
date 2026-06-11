"""Workspace-scoped conversation (对话) API for the new Workbench UI.

This module is intentionally INDEPENDENT from the legacy single-session chat
(``/api/chat`` in ``routes.py``), which the old ``--agent`` UI uses. It exposes
a parallel set of endpoints under ``/api/workbench/chats*`` so the two UIs
never share request code, while reusing the same per-session agent runtime
(``run_agent(session_id=...)``).

Data model: every Workbench project (workspace) owns two kinds of sessions —
task sessions (stored in ``workbench_projects.json``) and chat sessions
(stored here, in ``data/workbench_chats.json``, bound via ``projectId``).
Each chat keeps a public transcript (user / assistant messages with
attachments, tool trace and token usage) that survives agent-side context
compaction; the agent's own raw context lives in
``data/sessions/<chat_id>/state.json`` like any other per-session run.
"""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, StreamingResponse

from cyrene.config import DATA_DIR
from cyrene.io_utils import atomic_write_json, read_json_safe

logger = logging.getLogger(__name__)

_CHATS_STORE = DATA_DIR / "workbench_chats.json"

# Internal control tools that say nothing useful in a progress trace.
_TRACE_SKIP_TOOLS = {"use_tools", "quit"}
_USAGE_KEYS = (
    "prompt_tokens",
    "completion_tokens",
    "total_tokens",
    "prompt_cache_hit_tokens",
    "prompt_cache_miss_tokens",
)


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _short_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:10]}"


def _ndjson_line(payload: dict[str, Any]) -> str:
    return json.dumps(payload, ensure_ascii=False) + "\n"


# ---------------------------------------------------------------------------
# Store
# ---------------------------------------------------------------------------

def _read_chats_store() -> dict[str, Any]:
    data = read_json_safe(_CHATS_STORE)
    if isinstance(data, dict) and isinstance(data.get("chats"), list):
        return data
    return {"chats": []}


def _write_chats_store(payload: dict[str, Any]) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    atomic_write_json(_CHATS_STORE, payload)


def _new_chat(project_id: str, title: str = "", model: str = "") -> dict[str, Any]:
    now = _utc_now_iso()
    return {
        "id": _short_id("wbchat"),
        "projectId": str(project_id or ""),
        "kind": "chat",
        "title": str(title or "新对话").strip()[:60] or "新对话",
        "status": "idle",
        "model": model,
        "createdAt": now,
        "updatedAt": now,
        "messages": [],
    }


def _find_chat(payload: dict[str, Any], chat_id: str) -> dict[str, Any] | None:
    for chat in payload.get("chats", []):
        if str(chat.get("id") or "") == chat_id:
            return chat
    return None


def _chat_preview(chat: dict[str, Any]) -> str:
    for message in reversed(chat.get("messages") or []):
        text = str(message.get("content") or "").strip()
        if text:
            return text.replace("\n", " ")[:80]
    return ""


def _public_chat_light(chat: dict[str, Any]) -> dict[str, Any]:
    """Listing payload — transcript omitted to keep the rail cheap."""
    usage = _aggregate_usage(chat.get("messages") or [])
    return {
        "id": chat.get("id"),
        "projectId": chat.get("projectId"),
        "kind": "chat",
        "title": chat.get("title"),
        "status": chat.get("status") or "idle",
        "model": chat.get("model") or "",
        "createdAt": chat.get("createdAt"),
        "updatedAt": chat.get("updatedAt"),
        "preview": _chat_preview(chat),
        "messageCount": len(chat.get("messages") or []),
        "usage": usage,
    }


def _public_chat_full(chat: dict[str, Any]) -> dict[str, Any]:
    payload = _public_chat_light(chat)
    payload["messages"] = chat.get("messages") or []
    return payload


def _aggregate_usage(messages: list[dict[str, Any]]) -> dict[str, int]:
    totals = {key: 0 for key in _USAGE_KEYS}
    for message in messages:
        usage = message.get("usage")
        if not isinstance(usage, dict):
            continue
        for key in _USAGE_KEYS:
            try:
                totals[key] += int(usage.get(key) or 0)
            except (TypeError, ValueError):
                continue
    if not totals["total_tokens"]:
        totals["total_tokens"] = totals["prompt_tokens"] + totals["completion_tokens"]
    return totals


# ---------------------------------------------------------------------------
# Agent-state helpers (usage + tool trace for one exchange)
# ---------------------------------------------------------------------------

def _session_state_messages(session_id: str) -> list[dict[str, Any]]:
    from cyrene.agent.state import _session_state_file
    data = read_json_safe(_session_state_file(session_id))
    if isinstance(data, dict) and isinstance(data.get("messages"), list):
        return data["messages"]
    return []


def _tool_args_preview(raw_arguments: str) -> str:
    try:
        args = json.loads(raw_arguments or "{}")
    except Exception:
        return ""
    if not isinstance(args, dict):
        return ""
    parts = [str(value) for value in args.values() if value not in (None, "", [], {})]
    preview = ", ".join(parts)
    return preview[:80]


def _extract_exchange_meta(
    state_messages: list[dict[str, Any]], start_index: int
) -> tuple[list[dict[str, Any]], dict[str, int], list[dict[str, Any]]]:
    """Collect tool trace + token usage + agent-produced files from this exchange."""
    trace: list[dict[str, Any]] = []
    usage = {key: 0 for key in _USAGE_KEYS}
    files: list[dict[str, Any]] = []
    seen_file_urls: set[str] = set()
    for message in state_messages[start_index:]:
        if str(message.get("role") or "") != "assistant":
            continue
        raw_usage = message.get("usage")
        if isinstance(raw_usage, dict):
            for key in _USAGE_KEYS:
                try:
                    usage[key] += int(raw_usage.get(key) or 0)
                except (TypeError, ValueError):
                    continue
        for tool_call in message.get("tool_calls") or []:
            fn = tool_call.get("function") if isinstance(tool_call, dict) else None
            name = str((fn or {}).get("name") or "").strip()
            if not name or name in _TRACE_SKIP_TOOLS:
                continue
            trace.append({
                "tool": name,
                "preview": _tool_args_preview(str((fn or {}).get("arguments") or "")),
            })
        # Files the agent attached to its replies (report exports, send_file…)
        for item in message.get("attachments") or []:
            if not isinstance(item, dict):
                continue
            url = str(item.get("url") or "").strip()
            key = url or str(item.get("name") or "")
            if not key or key in seen_file_urls:
                continue
            seen_file_urls.add(key)
            files.append({
                "id": str(item.get("id") or "").strip(),
                "name": str(item.get("name") or "file"),
                "content_type": str(item.get("content_type") or "application/octet-stream"),
                "size": int(item.get("size") or 0),
                "kind": str(item.get("kind") or "file"),
                "url": url,
            })
    if not usage["total_tokens"]:
        usage["total_tokens"] = usage["prompt_tokens"] + usage["completion_tokens"]
    return trace[:40], usage, files[:20]


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

def register_workbench_chat_routes(router: APIRouter, bot: Any, db_path: str) -> None:
    # Heavyweight helpers (store access, attachments, agent entrypoints) live in
    # webui.routes; import lazily at call time to avoid a circular import.

    def _routes():
        from webui import routes as legacy_routes
        return legacy_routes

    @router.get("/api/workbench/chats")
    async def api_workbench_list_chats(project: str = ""):
        payload = _read_chats_store()
        chats = [
            _public_chat_light(chat)
            for chat in payload.get("chats", [])
            if not project or str(chat.get("projectId") or "") == project
        ]
        chats.sort(key=lambda item: str(item.get("updatedAt") or ""), reverse=True)
        return {"chats": chats}

    @router.post("/api/workbench/chats")
    async def api_workbench_create_chat(request: Request):
        body = await request.json()
        project_id = str(body.get("project") or body.get("projectId") or "").strip()
        if not project_id:
            return JSONResponse({"error": "project is required"}, status_code=400)
        R = _routes()
        store = R._read_workbench_store()
        if not R._workbench_find_project(store, project_id):
            return JSONResponse({"error": "project not found"}, status_code=404)
        payload = _read_chats_store()
        chat = _new_chat(project_id, str(body.get("title") or ""), R._get_model())
        payload.setdefault("chats", []).insert(0, chat)
        _write_chats_store(payload)
        return {"ok": True, "chat": _public_chat_full(chat)}

    @router.get("/api/workbench/chats/{chat_id}")
    async def api_workbench_get_chat(chat_id: str):
        payload = _read_chats_store()
        chat = _find_chat(payload, chat_id)
        if not chat:
            return JSONResponse({"error": "chat not found"}, status_code=404)
        return {"chat": _public_chat_full(chat)}

    @router.patch("/api/workbench/chats/{chat_id}")
    async def api_workbench_update_chat(chat_id: str, request: Request):
        body = await request.json()
        payload = _read_chats_store()
        chat = _find_chat(payload, chat_id)
        if not chat:
            return JSONResponse({"error": "chat not found"}, status_code=404)
        if "title" in body:
            chat["title"] = str(body.get("title") or "").strip()[:60] or chat.get("title")
        chat["updatedAt"] = _utc_now_iso()
        _write_chats_store(payload)
        return {"ok": True, "chat": _public_chat_full(chat)}

    @router.delete("/api/workbench/chats/{chat_id}")
    async def api_workbench_delete_chat(chat_id: str):
        from cyrene.agent import clear_session_id, interrupt_active_run
        payload = _read_chats_store()
        chats = payload.get("chats", [])
        next_chats = [chat for chat in chats if str(chat.get("id") or "") != chat_id]
        if len(next_chats) == len(chats):
            return JSONResponse({"error": "chat not found"}, status_code=404)
        payload["chats"] = next_chats
        _write_chats_store(payload)
        try:
            interrupt_active_run(session_id=chat_id)
            await clear_session_id(session_id=chat_id)
        except Exception:
            logger.exception("Failed to clear agent state for chat %s", chat_id)
        return {"ok": True}

    @router.post("/api/workbench/chats/{chat_id}/messages")
    async def api_workbench_chat_send(chat_id: str, request: Request):
        from cyrene.agent import run_agent
        from cyrene.agent.state import PERMISSION_MODES, _attachment_paths_by_name, _reply_stream_writer

        body = await request.json()
        message = str(body.get("message") or "").strip()
        attachments = body.get("attachments") if isinstance(body.get("attachments"), list) else []
        command = str(body.get("command") or "").strip()
        wants_stream = bool(body.get("stream"))
        mode = str(body.get("mode") or "auto").strip().lower()
        if mode not in PERMISSION_MODES:
            mode = "auto"

        R = _routes()
        normalized = R._workbench_normalize_attachments(attachments)
        public_attachments = [R.build_public_attachment_payload(item) for item in normalized]
        if not message and not normalized:
            return JSONResponse({"error": "message is required"}, status_code=400)

        payload = _read_chats_store()
        chat = _find_chat(payload, chat_id)
        if not chat:
            return JSONResponse({"error": "chat not found"}, status_code=404)
        project_id = str(chat.get("projectId") or "")

        now = _utc_now_iso()
        user_entry: dict[str, Any] = {
            "id": _short_id("msg"),
            "role": "user",
            "content": message,
            "createdAt": now,
        }
        if public_attachments:
            user_entry["attachments"] = public_attachments
        messages = chat.setdefault("messages", [])
        is_first_message = not any(m.get("role") == "user" for m in messages)
        messages.append(user_entry)
        if is_first_message and chat.get("title") in ("", "新对话", None) and message:
            chat["title"] = message.replace("\n", " ")[:24]
        chat["status"] = "running"
        chat["model"] = R._get_model()
        chat["updatedAt"] = now
        _write_chats_store(payload)

        agent_message = message
        if normalized:
            agent_message = (message or "[Attachment upload]") + R._attachment_prompt_block(normalized)
            # Auto-allow uploaded files for tool read guards (same as /api/chat).
            att_map: dict[str, str] = {}
            for item in normalized:
                full_path = str(item.get("path") or "").strip()
                if not full_path:
                    continue
                from pathlib import Path as _Path
                uuid_name = _Path(full_path).name
                att_map[uuid_name] = full_path
                parts = uuid_name.split("_", 1)
                if len(parts) == 2:
                    att_map[parts[1]] = full_path
            _attachment_paths_by_name.set(att_map)

        state_len_before = len(_session_state_messages(chat_id))

        async def _run() -> str:
            return await run_agent(
                user_message=agent_message,
                bot=bot,
                chat_id=R._CHAT_ID,
                db_path=db_path,
                session_id=chat_id,
                permission_mode=mode,
                command=command,
                public_user_message=message or None,
                public_attachments=public_attachments or None,
            )

        def _finalize(reply_text: str) -> dict[str, Any]:
            """Persist the assistant message (trace + usage + files) and settle status."""
            trace, usage, files = _extract_exchange_meta(_session_state_messages(chat_id), state_len_before)
            fresh = _read_chats_store()
            fresh_chat = _find_chat(fresh, chat_id)
            if not fresh_chat:
                return {}
            assistant_entry: dict[str, Any] = {
                "id": _short_id("msg"),
                "role": "assistant",
                "content": str(reply_text or ""),
                "createdAt": _utc_now_iso(),
                "model": fresh_chat.get("model") or "",
            }
            if trace:
                assistant_entry["trace"] = trace
            if any(usage.values()):
                assistant_entry["usage"] = usage
            if files:
                assistant_entry["attachments"] = files
            fresh_chat.setdefault("messages", []).append(assistant_entry)
            fresh_chat["status"] = "idle"
            fresh_chat["updatedAt"] = assistant_entry["createdAt"]
            _write_chats_store(fresh)
            if not command:
                R.schedule_capture(project_id, message, str(reply_text or ""))
            return assistant_entry

        def _settle_status() -> None:
            fresh = _read_chats_store()
            fresh_chat = _find_chat(fresh, chat_id)
            if fresh_chat and fresh_chat.get("status") == "running":
                fresh_chat["status"] = "idle"
                _write_chats_store(fresh)

        if not wants_stream:
            try:
                reply = await _run()
            except Exception as exc:
                logger.exception("Workbench chat run failed for %s", chat_id)
                _settle_status()
                return JSONResponse({"error": "agent run failed", "detail": str(exc)}, status_code=502)
            assistant_entry = _finalize(reply)
            return {"ok": True, "userMessage": user_entry, "assistantMessage": assistant_entry}

        async def event_stream():
            queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
            saw_reply_events = False

            async def publish(event: dict[str, Any]) -> None:
                await queue.put(dict(event))

            token = _reply_stream_writer.set(publish)
            task = asyncio.create_task(_run())
            _reply_stream_writer.reset(token)

            yield _ndjson_line({"type": "ack", "userMessage": user_entry, "chatId": chat_id})
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
                try:
                    reply = await task
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    logger.exception("Workbench chat streaming run failed for %s", chat_id)
                    _settle_status()
                    yield _ndjson_line({
                        "type": "error",
                        "error": "model_call_failed",
                        "message": str(exc).strip() or exc.__class__.__name__,
                    })
                    return
                if not saw_reply_events:
                    yield _ndjson_line({"type": "reply_start"})
                    for chunk in R._reply_stream_chunks(reply):
                        yield _ndjson_line({"type": "reply_delta", "delta": chunk})
                    yield _ndjson_line({"type": "reply_done", "response": reply})
                assistant_entry = _finalize(reply)
                yield _ndjson_line({"type": "saved", "assistantMessage": assistant_entry})
            finally:
                if not task.done():
                    task.cancel()
                _settle_status()

        return StreamingResponse(
            event_stream(),
            media_type="application/x-ndjson",
            headers={"Cache-Control": "no-cache"},
        )

    @router.post("/api/workbench/chats/{chat_id}/to-task")
    async def api_workbench_chat_to_task(chat_id: str, request: Request):
        """Promote a conversation into a task session of its project (开始执行)."""
        body = await request.json()
        payload = _read_chats_store()
        chat = _find_chat(payload, chat_id)
        if not chat:
            return JSONResponse({"error": "chat not found"}, status_code=404)
        R = _routes()
        store = R._read_workbench_store()
        project = R._workbench_find_project(store, str(chat.get("projectId") or ""))
        if not project:
            return JSONResponse({"error": "project not found"}, status_code=404)
        last_user = ""
        for message in reversed(chat.get("messages") or []):
            if message.get("role") == "user" and str(message.get("content") or "").strip():
                last_user = str(message["content"]).strip()
                break
        title = str(body.get("title") or chat.get("title") or "新任务").strip()[:80] or "新任务"
        goal = str(body.get("goal") or last_user or title).strip()
        session = R._workbench_new_session(project.get("id"), title, goal)
        session["events"] = [{
            "id": _short_id("event"),
            "type": "CreatedFromChat",
            "createdAt": _utc_now_iso(),
            "body": f"由对话「{chat.get('title')}」创建。",
            "chatId": chat_id,
        }]
        project.setdefault("sessions", []).insert(0, session)
        project["updatedAt"] = session["createdAt"]
        store["activeProjectId"] = project.get("id")
        store["activeSessionId"] = session["id"]
        R._write_workbench_store(store)
        return {"ok": True, "session": session, **store}


async def remove_project_chats(project_id: str) -> int:
    """Bulk-remove all chats of a project (called when the project is deleted)."""
    from cyrene.agent import clear_session_id
    project_id = str(project_id or "").strip()
    if not project_id:
        return 0
    payload = _read_chats_store()
    doomed = [chat for chat in payload.get("chats", []) if str(chat.get("projectId") or "") == project_id]
    if doomed:
        payload["chats"] = [chat for chat in payload.get("chats", []) if str(chat.get("projectId") or "") != project_id]
        _write_chats_store(payload)
    for chat in doomed:
        try:
            await clear_session_id(session_id=str(chat.get("id") or ""))
        except Exception:
            logger.exception("Failed to clear agent state for chat %s", chat.get("id"))
    return len(doomed)
