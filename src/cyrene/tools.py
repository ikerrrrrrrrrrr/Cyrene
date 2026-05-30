"""
Tool definitions and handlers for the Cyrene agent.

All tool handler functions, tool definitions (TOOL_DEFS), tool handler registry
(TOOL_HANDLERS), tool execution dispatch (_execute_tool), and helper functions
(_resolve_workspace_path, _json_result).

NOTE: _tool_quit and the "quit" entry in TOOL_HANDLERS are kept in agent.py
to avoid circular imports. agent.py adds "quit" to TOOL_HANDLERS after import.
"""

import asyncio
import json
import logging
import os
import re
import shlex
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import quote

import httpx
from croniter import croniter

from cyrene.attachments import (
    analyze_attachment,
    build_public_attachment_payload,
    is_exported_attachment_path,
    is_uploaded_attachment_path,
    register_generated_attachment,
)
from cyrene import db
from cyrene.config import (
    DATA_DIR,
    STATE_FILE,
    WORKSPACE_DIR,
)
from cyrene.conversations import recall_conversations
from cyrene.llm import _truncate
from cyrene.search import deep_search
from cyrene.shells import close_shell as _close_shell_session
from cyrene.shells import list_shells as _list_shell_sessions
from cyrene.shells import send_shell as _send_shell_session
from cyrene.shells import start_shell as _start_shell_session
from cyrene.short_term import get_context as _get_short_term_context
from cyrene.skills_registry import (
    build_skills as _build_skills,
    install_skill_from_path as _install_skill,
    uninstall_skill as _uninstall_skill,
)
from cyrene.subagent import register as _reg_subagent, can_receive, _run_subagent, _spawn_subagent_task
from cyrene.inbox import send_message as _send_inbox
from cyrene.soul import read_shallow_memory

logger = logging.getLogger(__name__)
_CC_PROJECT_DIR = WORKSPACE_DIR.parent
_MAIN_ONLY_TOOLS = {
    "send_telegram",
    "send_message",
    "send_file",
    "send_wechat_file",
    "ask_user",
    "spawn_subagent",
    "query_round",
}

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _resolve_workspace_path(path_str: str) -> Path:
    candidate = Path(path_str)
    path = candidate if candidate.is_absolute() else WORKSPACE_DIR / candidate
    resolved = path.resolve()
    workspace = WORKSPACE_DIR.resolve()
    if resolved != workspace and workspace not in resolved.parents:
        raise ValueError(f"Path escapes workspace: {path_str}")
    return resolved


def _workspace_permission_error() -> str:
    return "Write and delete permissions are limited to the current workspace."


def _resolve_workspace_write_target(path_str: str) -> Path:
    from cyrene.settings_store import get_write_permission_mode
    if get_write_permission_mode() == "full_access":
        candidate = Path(path_str)
        path = candidate if candidate.is_absolute() else WORKSPACE_DIR / candidate
        return path.resolve()
    try:
        return _resolve_workspace_path(path_str)
    except Exception as exc:
        raise ValueError(_workspace_permission_error()) from exc


async def _request_write_elevation(
    *,
    tool_name: str,
    path_hint: str,
    reason: str = "",
) -> str:
    from cyrene.agent.state import (
        _current_agent_id,
        _current_client_request_id,
        _current_command,
        _current_round_id,
    )
    from cyrene.agent.session import (
        _upsert_pending_question,
        get_session_labels,
    )
    if _current_agent_id.get() != "main":
        return _workspace_permission_error()
    round_id = str(_current_round_id.get() or "").strip()
    if not round_id:
        return _workspace_permission_error()
    labels = get_session_labels(round_id)
    detail = f"\n目标路径：{path_hint}" if path_hint else ""
    why = f"\n原因：{reason}" if reason else ""
    question = await _upsert_pending_question({
        "text": f"这个操作需要当前 workspace 之外的写入/删除权限。是否允许提升权限？{detail}{why}",
        "round_id": round_id,
        "round_title": labels.get("round_title", ""),
        "client_request_id": str(_current_client_request_id.get() or "").strip(),
        "options": ["仅这次允许", "始终允许", "保持仅限 workspace"],
        "allow_custom": True,
        "meta": {
            "kind": "write_permission_request",
            "tool_name": tool_name,
            "path_hint": path_hint,
            "reason": reason,
            "command": _current_command.get() or "",
        },
    })
    return _json_result({
        "status": "awaiting_user",
        "question_id": question.get("id", ""),
        "permission": "write_elevation",
    })


def _shell_command_requires_write_guard(command: str) -> bool:
    lowered = str(command or "").lower()
    return any(
        token in lowered
        for token in (
            " rm ",
            "rm -",
            "mv ",
            "cp ",
            "mkdir ",
            "touch ",
            "tee ",
            "sed -i",
            "truncate ",
            "install ",
            "rmdir ",
            "unlink ",
            ">",
        )
    )


def _guard_shell_command_workspace_write(command: str) -> None:
    raw = str(command or "").strip()
    if not raw or not _shell_command_requires_write_guard(raw):
        return
    try:
        tokens = shlex.split(raw, posix=True)
    except Exception:
        raise ValueError(_workspace_permission_error())
    write_cmds = {"rm", "mv", "cp", "mkdir", "touch", "tee", "truncate", "install", "rmdir", "unlink"}
    cd_cmds = {"cd", "pushd"}
    path_like_tokens: list[str] = []
    previous = ""
    for token in tokens:
        stripped = token.strip()
        if not stripped:
            previous = stripped
            continue
        if previous in write_cmds | cd_cmds:
            path_like_tokens.append(stripped)
        elif previous in {"-o", "--output"}:
            path_like_tokens.append(stripped)
        elif stripped in write_cmds | cd_cmds:
            previous = stripped
            continue
        elif stripped.startswith((">", ">>")):
            candidate = stripped.lstrip(">").strip()
            if candidate:
                path_like_tokens.append(candidate)
        elif stripped not in {"&&", "||", "|", ";"} and (
            stripped.startswith("/")
            or stripped.startswith("./")
            or stripped.startswith("../")
            or "/" in stripped
            or re.search(r"\.[A-Za-z0-9]{1,8}$", stripped)
        ):
            if previous in write_cmds:
                path_like_tokens.append(stripped)
        previous = stripped
    if ">" in raw or ">>" in raw:
        redirection_targets = re.findall(r"(?:^|[^\d])>>?\s*([^\s;&|]+)", raw)
        path_like_tokens.extend(redirection_targets)
    for token in path_like_tokens:
        if token.startswith("-"):
            continue
        _resolve_workspace_write_target(token)


def _json_result(payload: Any) -> str:
    if isinstance(payload, str):
        return payload
    return json.dumps(payload, ensure_ascii=False)


def _resolve_tool_path(path_str: str) -> Path:
    if is_uploaded_attachment_path(path_str) or is_exported_attachment_path(path_str):
        return Path(path_str).resolve()
    return _resolve_workspace_path(path_str)


def _resolve_exportable_path(path_str: str) -> Path:
    candidate = Path(path_str)
    path = candidate if candidate.is_absolute() else WORKSPACE_DIR / candidate
    resolved = path.resolve()
    workspace = WORKSPACE_DIR.resolve()
    data_root = DATA_DIR.resolve()
    if (
        resolved == workspace
        or workspace in resolved.parents
        or resolved == data_root
        or data_root in resolved.parents
    ):
        return resolved
    raise ValueError(f"Path cannot be sent to WebUI: {path_str}")


def _normalize_schedule_datetime(raw_value: str) -> str:
    """Normalize a user-facing datetime string to UTC ISO-8601.

    If the input is naive, interpret it in the machine's local timezone so
    Web UI scheduling like "2 minutes later" behaves as the user expects.
    """
    parsed = datetime.fromisoformat(raw_value)
    if parsed.tzinfo is None:
        local_tz = datetime.now().astimezone().tzinfo or timezone.utc
        parsed = parsed.replace(tzinfo=local_tz)
    return parsed.astimezone(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# Tool implementations
# ---------------------------------------------------------------------------


async def _tool_send_message(args: dict[str, Any], bot: Any, chat_id: int, _db_path: str, notify_state: dict[str, bool] | None) -> str:
    text = str(args.get("text", ""))
    if bot is not None:
        await bot.send_message(chat_id=chat_id, text=text)
    if notify_state is not None:
        notify_state["sent"] = True
    return "Message sent."


async def _tool_send_message_to_user(args: dict[str, Any], _bot: Any, _chat_id: int, _db_path: str, _notify_state: dict[str, bool] | None) -> str:
    """Send a message directly to the user. Only available to subagents responding to @mentions."""
    text = str(args.get("text", "") or "").strip()
    if not text:
        return "Error: 'text' is required."

    from cyrene.subagent import _direct_message_mode
    if not _direct_message_mode.get():
        return (
            "Error: send_message_to_user is only available when responding to a direct "
            "user message via @mention. Use quit with your result for normal rounds."
        )

    from cyrene.agent.state import _current_agent_id, _current_round_id
    from cyrene import debug as _debug_module
    agent_id = _current_agent_id.get() or "subagent"
    round_id = str(_current_round_id.get() or "").strip()
    await _debug_module.publish_event({
        "type": "agent_comm",
        "from": agent_id,
        "to": "user",
        "content": text,
        "summary": text[:100].replace("\n", " ").strip() + ("..." if len(text) > 100 else ""),
        "msg_type": "reply",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "round_id": round_id,
        "message_id": f"reply_{agent_id}_{int(time.time() * 1000)}",
    })
    if _notify_state is not None:
        _notify_state["sent"] = True
    _direct_message_mode.set(False)
    return "Message sent. Now act on the user's guidance — adjust your approach and continue working with your other tools."


async def _tool_send_user_message(args: dict[str, Any], _bot: Any, _chat_id: int, _db_path: str, _notify_state: dict[str, bool] | None) -> str:
    text = str(args.get("text", "") or "").strip()
    if not text:
        return "Error: 'text' is required."
    from cyrene.agent.state import _current_agent_id, _current_client_request_id, _current_round_id
    from cyrene.agent.session import append_system_message
    from cyrene.agent.message import _insert_intermediate_user_reply

    if _current_agent_id.get() != "main":
        return "Only the main agent can send a user-visible WebUI message. Subagents must report via quit or send_agent_message."

    round_id = str(_current_round_id.get() or "").strip()
    if not round_id:
        await append_system_message(text)
        if _notify_state is not None:
            _notify_state["sent"] = True
        return "System message sent to the user."

    client_request_id = str(_current_client_request_id.get() or "").strip()
    await _insert_intermediate_user_reply(
        text,
        round_id=round_id,
        client_request_id=client_request_id,
    )
    if _notify_state is not None:
        _notify_state["sent"] = True
    return "Mid-run message sent to the user."


async def _tool_send_file(args: dict[str, Any], _bot: Any, _chat_id: int, _db_path: str, _notify_state: dict[str, bool] | None) -> str:
    path_arg = str(args.get("path", "") or "").strip()
    if not path_arg:
        return "Error: 'path' is required."

    from cyrene.agent.state import _current_agent_id, _current_client_request_id, _current_round_id
    from cyrene.agent.session import append_system_message
    from cyrene.agent.message import _insert_intermediate_user_reply

    if _current_agent_id.get() != "main":
        return "Only the main agent can send a file to the WebUI."

    path = _resolve_exportable_path(path_arg)
    if not path.exists() or not path.is_file():
        return f"Error: file not found: {path}"

    text = str(args.get("text", "") or "").strip()
    attachment = build_public_attachment_payload(
        register_generated_attachment(str(path), display_name=str(args.get("name", "") or "").strip() or None)
    )

    round_id = str(_current_round_id.get() or "").strip()
    client_request_id = str(_current_client_request_id.get() or "").strip()
    if round_id:
        await _insert_intermediate_user_reply(
            text,
            round_id=round_id,
            client_request_id=client_request_id,
            attachments=[attachment],
        )
    else:
        await append_system_message(
            text,
            message_meta={"attachments": [attachment]},
        )
    if _notify_state is not None:
        _notify_state["sent"] = True
    return _json_result({
        "status": "sent",
        "attachment": attachment,
    })


async def _tool_send_wechat_file(args: dict[str, Any], bot: Any, chat_id: int, _db_path: str, notify_state: dict[str, bool] | None) -> str:
    """Send a file to the user via WeChat CDN.

    Requires ``bot`` to be a ``WeChatClient`` (i.e. the agent is running
    on the WeChat channel).
    """
    path_arg = str(args.get("path", "") or "").strip()
    if not path_arg:
        return "Error: 'path' is required."

    from cyrene.agent.state import _current_agent_id, _current_round_id, _current_client_request_id
    from cyrene.agent.message import _insert_intermediate_user_reply
    from cyrene.agent.session import append_system_message

    if _current_agent_id.get() != "main":
        return "Only the main agent can send files via WeChat."

    path = _resolve_exportable_path(path_arg)
    if not path.exists() or not path.is_file():
        return f"Error: file not found: {path}"

    name = str(args.get("name", "") or "").strip() or path.name
    text = str(args.get("text", "") or "").strip()
    dedupe_key = f"{path.resolve()}|{name}|{text}"

    if notify_state is not None:
        sent_wechat_files = notify_state.setdefault("sent_wechat_files", set())
        if dedupe_key in sent_wechat_files:
            return f"Skipped duplicate WeChat file send: {name}"

    # Send via WeChat if the bot supports it
    send_file_fn = getattr(bot, "send_file", None)
    if send_file_fn is not None:
        try:
            ok = await send_file_fn(chat_id=str(chat_id), filepath=str(path), filename=name)
            if not ok:
                return "File too large or upload failed — a text notice was sent to WeChat instead."
        except Exception as e:
            logger.exception("send_wechat_file failed")
            return f"Error sending file via WeChat: {e}"
    else:
        return "Error: current channel does not support WeChat file sending. Use send_file for WebUI attachments."

    # Notify WebUI — best-effort, swallow errors so a failed notification
    # never triggers an LLM retry of the WeChat send.
    desc = f"[WeChat sent: {name}]"
    if text:
        desc += f" — {text}"
    try:
        round_id = str(_current_round_id.get() or "").strip()
        if round_id:
            client_request_id = str(_current_client_request_id.get() or "").strip()
            await _insert_intermediate_user_reply(desc, round_id=round_id, client_request_id=client_request_id)
        else:
            await append_system_message(desc, message_meta={})
    except Exception:
        logger.exception("Failed to write WebUI notification for WeChat file send")

    if notify_state is not None:
        sent_wechat_files = notify_state.setdefault("sent_wechat_files", set())
        sent_wechat_files.add(dedupe_key)
        notify_state["sent"] = True
    return f"File sent via WeChat: {name}"


async def _tool_ask_user(args: dict[str, Any], _bot: Any, _chat_id: int, _db_path: str, _notify_state: dict[str, bool] | None) -> str:
    text = str(args.get("text", "") or "").strip()
    if not text:
        return "Error: 'text' is required."

    from cyrene.agent.state import _current_agent_id, _current_client_request_id, _current_command, _current_round_id
    from cyrene.agent.session import _upsert_pending_question

    if _current_agent_id.get() != "main":
        return "Only the main agent can ask the user a clarification question."

    round_id = str(_current_round_id.get() or "").strip()
    if not round_id:
        return "Cannot ask the user a question outside an active chat round."

    raw_options = args.get("options", [])
    options: list[str] = []
    if isinstance(raw_options, list):
        for item in raw_options:
            label = str(item or "").strip()
            if label:
                options.append(label)

    from cyrene.agent.session import get_session_labels

    labels = get_session_labels(round_id)
    question = await _upsert_pending_question({
        "text": text,
        "round_id": round_id,
        "round_title": labels.get("round_title", ""),
        "client_request_id": str(_current_client_request_id.get() or "").strip(),
        "options": options[:6],
        "allow_custom": True,
        "meta": {"command": _current_command.get() or ""},
    })
    return _json_result({
        "status": "awaiting_user",
        "question_id": question.get("id", ""),
        "option_count": len(question.get("options", []) or []),
    })


async def _tool_prompt_claude_code(args: dict[str, Any], _bot: Any, _chat_id: int, _db_path: str, _notify_state: dict[str, bool] | None) -> str:
    task = str(args.get("task", "") or "").strip()
    if not task:
        return "Error: 'task' is required."

    from cyrene.agent.state import _current_agent_id, _current_client_request_id, _current_round_id
    from cyrene.agent.session import _upsert_pending_question, get_session_labels
    from cyrene.agent.prompts import build_claude_code_question_payload, optimize_claude_code_prompt
    from cyrene.cc_bridge import get_cc_status

    if _current_agent_id.get() != "main":
        return "Only the main agent can prepare a Claude Code prompt for user confirmation."

    round_id = str(_current_round_id.get() or "").strip()
    if not round_id:
        return "Cannot prepare a Claude Code prompt outside an active chat round."

    status = get_cc_status(_CC_PROJECT_DIR)
    if not bool(status.get("available")):
        reason = str(status.get("reason") or "Claude Code is not running.").strip()
        return _json_result({
            "status": "error",
            "reason": reason,
            "can_launch": bool(status.get("can_launch")),
        })

    optimized_prompt = await optimize_claude_code_prompt(task)
    payload = build_claude_code_question_payload(
        task,
        optimized_prompt,
        tmux_session=str(status.get("tmux_session") or "").strip(),
    )
    labels = get_session_labels(round_id)
    question = await _upsert_pending_question({
        "text": payload["text"],
        "round_id": round_id,
        "round_title": labels.get("round_title", ""),
        "client_request_id": str(_current_client_request_id.get() or "").strip(),
        "options": payload["options"],
        "allow_custom": bool(payload.get("allow_custom", True)),
        "meta": payload.get("meta", {}),
    })
    return _json_result({
        "status": "awaiting_user",
        "question_id": question.get("id", ""),
        "prompt": optimized_prompt,
        "tmux_session": str(status.get("tmux_session") or "").strip(),
    })


async def _tool_schedule_task(args: dict[str, Any], _bot: Any, chat_id: int, db_path: str, _notify_state: dict[str, bool] | None) -> str:
    stype = str(args["schedule_type"])
    svalue = str(args["schedule_value"])
    now = datetime.now(timezone.utc)

    if stype == "cron":
        next_run = croniter(svalue, now).get_next(datetime).isoformat()
    elif stype == "interval":
        next_run = (now + timedelta(milliseconds=int(svalue))).isoformat()
    elif stype == "once":
        next_run = _normalize_schedule_datetime(svalue)
        svalue = next_run
    else:
        raise ValueError(f"Unknown schedule_type: {stype}")

    task_id = await db.create_task(db_path, chat_id, str(args["prompt"]), stype, svalue, next_run)
    return f"Task {task_id} scheduled. Next run: {next_run}"


async def _tool_list_tasks(_args: dict[str, Any], _bot: Any, _chat_id: int, db_path: str, _notify_state: dict[str, bool] | None) -> str:
    tasks = await db.get_all_tasks(db_path)
    if not tasks:
        return "No scheduled tasks."
    lines = [f"- [{t['id']}] {t['status']} | {t['schedule_type']}({t['schedule_value']}) | {t['prompt'][:60]}" for t in tasks]
    return "\n".join(lines)


async def _tool_pause_task(args: dict[str, Any], _bot: Any, _chat_id: int, db_path: str, _notify_state: dict[str, bool] | None) -> str:
    task_id = str(args["task_id"])
    ok = await db.update_task_status(db_path, task_id, "paused")
    return f"Task {task_id} paused." if ok else f"Task {task_id} not found."


async def _tool_resume_task(args: dict[str, Any], _bot: Any, _chat_id: int, db_path: str, _notify_state: dict[str, bool] | None) -> str:
    task_id = str(args["task_id"])
    ok = await db.update_task_status(db_path, task_id, "active")
    return f"Task {task_id} resumed." if ok else f"Task {task_id} not found."


async def _tool_cancel_task(args: dict[str, Any], _bot: Any, _chat_id: int, db_path: str, _notify_state: dict[str, bool] | None) -> str:
    task_id = str(args["task_id"])
    ok = await db.delete_task(db_path, task_id)
    return f"Task {task_id} cancelled." if ok else f"Task {task_id} not found."


async def _tool_read(args: dict[str, Any], _bot: Any, _chat_id: int, _db_path: str, _notify_state: dict[str, bool] | None) -> str:
    from cyrene.settings_store import is_workspace_active
    if not is_workspace_active():
        return "Workspace access is disabled. Ask the user to add workspace via '+ add context' in the chat input, or set a workspace directory in Settings."
    path = _resolve_tool_path(str(args["path"]))
    return _truncate(path.read_text(encoding="utf-8"))


async def _tool_write(args: dict[str, Any], _bot: Any, _chat_id: int, _db_path: str, _notify_state: dict[str, bool] | None) -> str:
    from cyrene.settings_store import is_workspace_active
    if not is_workspace_active():
        return "Workspace access is disabled. Ask the user to add workspace via '+ add context' in the chat input, or set a workspace directory in Settings."
    try:
        path = _resolve_workspace_write_target(str(args["path"]))
    except ValueError:
        return await _request_write_elevation(tool_name="Write", path_hint=str(args.get("path", "")))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(str(args.get("content", "")), encoding="utf-8")
    return f"Wrote {path}"


async def _tool_edit(args: dict[str, Any], _bot: Any, _chat_id: int, _db_path: str, _notify_state: dict[str, bool] | None) -> str:
    from cyrene.settings_store import is_workspace_active
    if not is_workspace_active():
        return "Workspace access is disabled. Ask the user to add workspace via '+ add context' in the chat input, or set a workspace directory in Settings."
    try:
        path = _resolve_workspace_write_target(str(args["path"]))
    except ValueError:
        return await _request_write_elevation(tool_name="Edit", path_hint=str(args.get("path", "")))
    old_string = str(args["old_string"])
    new_string = str(args["new_string"])
    replace_all = bool(args.get("replace_all", False))

    content = path.read_text(encoding="utf-8")
    occurrences = content.count(old_string)
    if occurrences == 0:
        raise ValueError("old_string not found")
    if occurrences > 1 and not replace_all:
        raise ValueError("old_string matched multiple times; set replace_all=true")

    updated = content.replace(old_string, new_string) if replace_all else content.replace(old_string, new_string, 1)
    path.write_text(updated, encoding="utf-8")
    replaced = occurrences if replace_all else 1
    return f"Edited {path}. Replacements: {replaced}"


async def _tool_analyze_attachment(args: dict[str, Any], _bot: Any, _chat_id: int, _db_path: str, _notify_state: dict[str, bool] | None) -> str:
    path = _resolve_tool_path(str(args["path"]))
    prompt = str(args.get("prompt", "") or "")
    force_refresh = bool(args.get("force_refresh", False))
    result = await analyze_attachment(str(path), prompt=prompt, force_refresh=force_refresh)
    return _json_result(result)


async def _tool_glob(args: dict[str, Any], _bot: Any, _chat_id: int, _db_path: str, _notify_state: dict[str, bool] | None) -> str:
    from cyrene.settings_store import is_workspace_active
    if not is_workspace_active():
        return "Workspace access is disabled. Ask the user to add workspace via '+ add context' in the chat input, or set a workspace directory in Settings."
    pattern = str(args["pattern"])
    matches = sorted(str(path.relative_to(WORKSPACE_DIR)) for path in WORKSPACE_DIR.glob(pattern))
    return "\n".join(matches[:200]) if matches else "No matches."


async def _tool_grep(args: dict[str, Any], _bot: Any, _chat_id: int, _db_path: str, _notify_state: dict[str, bool] | None) -> str:
    from cyrene.settings_store import is_workspace_active
    if not is_workspace_active():
        return "Workspace access is disabled. Ask the user to add workspace via '+ add context' in the chat input, or set a workspace directory in Settings."
    pattern = re.compile(str(args["pattern"]))
    search_root = _resolve_workspace_path(str(args.get("path", ".")))
    glob_pattern = str(args.get("glob", "**/*"))
    lines: list[str] = []

    for path in search_root.glob(glob_pattern):
        if not path.is_file():
            continue
        try:
            content = path.read_text(encoding="utf-8")
        except Exception:
            continue
        for index, line in enumerate(content.splitlines(), start=1):
            if pattern.search(line):
                rel = path.relative_to(WORKSPACE_DIR)
                lines.append(f"{rel}:{index}:{line}")
                if len(lines) >= 200:
                    return "\n".join(lines)
    return "\n".join(lines) if lines else "No matches."


async def _tool_bash(args: dict[str, Any], _bot: Any, _chat_id: int, _db_path: str, _notify_state: dict[str, bool] | None) -> str:
    command = str(args["command"])
    try:
        _guard_shell_command_workspace_write(command)
    except ValueError:
        return await _request_write_elevation(tool_name="Bash", path_hint="", reason=command[:240])
    timeout_ms = int(args.get("timeout_ms", 120000))
    timeout_sec = timeout_ms / 1000
    shell = os.environ.get("SHELL") or "/bin/sh"
    proc = await asyncio.create_subprocess_exec(
        shell,
        "-lc",
        command,
        cwd=str(WORKSPACE_DIR),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )

    from cyrene.agent.state import _interrupt_event

    stdout_chunks: list[bytes] = []
    stderr_chunks: list[bytes] = []

    async def _read(stream: asyncio.StreamReader | None, chunks: list[bytes]) -> None:
        if stream is None:
            return
        while True:
            chunk = await stream.read(8192)
            if not chunk:
                break
            chunks.append(chunk)

    reads = asyncio.gather(_read(proc.stdout, stdout_chunks), _read(proc.stderr, stderr_chunks))
    import time as _time
    deadline = _time.monotonic() + timeout_sec

    try:
        while True:
            if reads.done():
                break
            remaining = deadline - _time.monotonic()
            if remaining <= 0:
                proc.kill()
                reads.cancel()
                try:
                    await reads
                except (asyncio.CancelledError, Exception):
                    pass
                raise ValueError(f"Command timed out after {timeout_ms} ms")
            if _interrupt_event.is_set():
                proc.kill()
                reads.cancel()
                try:
                    await reads
                except (asyncio.CancelledError, Exception):
                    pass
                payload = {
                    "exit_code": -1,
                    "stdout": _truncate(b"".join(stdout_chunks).decode("utf-8", errors="replace")),
                    "stderr": "Command interrupted by new user message.",
                }
                return _json_result(payload)
            try:
                await asyncio.wait_for(asyncio.shield(reads), timeout=min(1, remaining))
            except asyncio.TimeoutError:
                pass

        await proc.wait()
    except ValueError:
        raise
    except Exception:
        proc.kill()
        raise

    payload = {
        "exit_code": proc.returncode,
        "stdout": _truncate(b"".join(stdout_chunks).decode("utf-8", errors="replace")),
        "stderr": _truncate(b"".join(stderr_chunks).decode("utf-8", errors="replace")),
    }
    return _json_result(payload)


async def _tool_webfetch(args: dict[str, Any], _bot: Any, _chat_id: int, _db_path: str, _notify_state: dict[str, bool] | None) -> str:
    url = str(args["url"])
    async with httpx.AsyncClient(follow_redirects=True, timeout=30.0) as client:
        response = await client.get(url)
        response.raise_for_status()
    return _truncate(response.text)


async def _tool_websearch(args: dict[str, Any], _bot: Any, _chat_id: int, _db_path: str, _notify_state: dict[str, bool] | None) -> str:
    query = str(args.get("query", ""))
    if not query:
        return "No query provided."

    # 超过 15 个字符的复杂查询走深度搜索，简单的直接搜索
    if len(query) > 15:
        result = await deep_search(query)
        return result

    # 短查询走 DuckDuckGo 搜索，失败时 fallback 到 Bing
    url = f"https://html.duckduckgo.com/html/?q={quote(query)}"
    try:
        async with httpx.AsyncClient(follow_redirects=True, timeout=30.0) as client:
            response = await client.get(url, headers={"User-Agent": "Mozilla/5.0"})
            response.raise_for_status()
        html = response.text
        matches = re.findall(r'<a[^>]*class="result__a"[^>]*href="(.*?)"[^>]*>(.*?)</a>', html, re.S)
        if matches:
            results = []
            for href, title in matches[:10]:
                clean_title = re.sub(r"<.*?>", "", title).strip()
                results.append(f"- {clean_title}\n  {href}")
            return "\n".join(results)
    except Exception:
        pass

    # DuckDuckGo 失败，尝试 Bing
    try:
        bing_url = f"https://www.bing.com/search?q={quote(query)}&setmkt=en-US"
        bing_headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Edg/131.0.0.0",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        }
        async with httpx.AsyncClient(follow_redirects=True, timeout=30.0) as client:
            bresp = await client.get(bing_url, headers=bing_headers)
            bresp.raise_for_status()
        bhtml = bresp.text
        blocks = re.findall(r'<li\s+class="b_algo"[^>]*>([\s\S]*?)</li>', bhtml, re.DOTALL)
        bresults = []
        for block in blocks[:10]:
            hm = re.search(r'<h2[^>]*>\s*<a[^>]+href="([^"]+)"[^>]*>([\s\S]*?)</a>', block, re.DOTALL)
            if hm:
                bt = re.sub(r'<[^>]+>', '', hm.group(2)).strip()
                bu = hm.group(1)
                if bt and not bu.startswith('/'):
                    bresults.append(f"- {bt}\n  {bu}")
        if bresults:
            return "\n".join(bresults)
    except Exception:
        pass

    return "No results."


async def _tool_send_agent_message(args: dict[str, Any], _bot: Any, _chat_id: int, _db_path: str, _notify_state: dict[str, bool] | None) -> str:
    """Send a message to another sub-agent via inbox."""
    target = str(args.get("to", ""))
    content = str(args.get("content", ""))
    if not target or not content:
        return "Error: both 'to' and 'content' are required."
    from cyrene.agent.state import _current_agent_id, _current_round_id
    current_round_id = _current_round_id.get()
    if not await can_receive(target, round_id=current_round_id):
        if target.lower() in {"main", "main_agent", "cyrene", "danny", "host", "coordinator", "parent"}:
            return "The main-agent inbox is reserved for user guidance. Put your final conclusion in your next quit response; the parent agent will collect it automatically."
        if current_round_id:
            return f"Cannot deliver: agent '{target}' is not available in the current round ({current_round_id})."
        return f"Cannot deliver: agent '{target}' is not available (finished or timed out)."
    from_agent = _current_agent_id.get()
    await _send_inbox(from_agent, target, "chat", content, round_id=current_round_id)
    # Publish SSE event for real-time flow diagram updates
    from cyrene import debug as _debug_comm
    await _debug_comm.publish_event({
        "type": "agent_comm",
        "from": from_agent,
        "to": target,
        "content": content,  # full content for group chat
        "summary": content[:100].replace("\n", " ").strip() + ("..." if len(content) > 100 else ""),
        "msg_type": "chat",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "round_id": current_round_id,
    })
    return f"Message sent to {target}."


async def _tool_broadcast_agent_message(args: dict[str, Any], _bot: Any, _chat_id: int, _db_path: str, _notify_state: dict[str, bool] | None) -> str:
    """Broadcast a message to all peer sub-agents in the current round."""
    content = str(args.get("content", ""))
    if not content:
        return "Error: 'content' is required."
    from cyrene.agent.state import _current_agent_id, _current_round_id
    from cyrene.subagent import _registry as _sub_registry, _lock as _reg_lock
    current_round_id = _current_round_id.get()
    from_agent = _current_agent_id.get()

    # Collect all peer agent IDs in the current round
    async with _reg_lock:
        peers = [
            aid for aid, info in _sub_registry.items()
            if aid != from_agent
            and (not current_round_id or str(info.get("round_id", "")) == current_round_id)
        ]

    if not peers:
        return "No peer sub-agents are available to receive the broadcast."

    sent_count = 0
    errors: list[str] = []
    for peer_id in peers:
        if await can_receive(peer_id, round_id=current_round_id):
            msg_id = await _send_inbox(from_agent, peer_id, "progress", content, round_id=current_round_id)
            if msg_id:
                sent_count += 1
            else:
                errors.append(f"{peer_id}: failed to deliver")
        else:
            errors.append(f"{peer_id}: not available")

    result = f"Broadcast sent to {sent_count}/{len(peers)} peers."
    if errors:
        result += f" Skipped: {', '.join(errors)}"

    # Publish SSE event for real-time flow diagram updates
    from cyrene import debug as _debug_comm
    await _debug_comm.publish_event({
        "type": "agent_comm",
        "from": from_agent,
        "to": "all",
        "content": content,  # full content for group chat
        "summary": content[:100].replace("\n", " ").strip() + ("..." if len(content) > 100 else ""),
        "msg_type": "progress",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "round_id": current_round_id,
        "broadcast": True,
    })
    return result


async def _tool_spawn_subagent(args: dict[str, Any], bot: Any, chat_id: int, db_path: str, _notify_state: dict[str, bool] | None) -> str:
    """Spawn a sub-agent to handle a specific task."""
    agent_id = str(args.get("agent_id", ""))
    task = str(args.get("task", ""))
    use_secondary = bool(args.get("use_secondary", False))
    role = str(args.get("role", ""))
    if role and role not in ("moderator", "participant"):
        role = ""
    if not agent_id or not task:
        return "Error: agent_id and task are required."
    from cyrene.agent.state import _current_agent_id, _current_round_id
    if _current_agent_id.get() != "main":
        return "Only the main agent can spawn subagents."
    await _reg_subagent(agent_id, task, round_id=_current_round_id.get(), role=role)
    _spawn_subagent_task(_run_subagent(agent_id, task, bot, chat_id, db_path, use_secondary=use_secondary, role=role), agent_id)
    suffix = " (secondary model)" if use_secondary else ""
    role_suffix = f" [role={role}]" if role else ""
    return f"Sub-agent '{agent_id}' spawned{suffix}{role_suffix}. Task: {task[:80]}"


async def _tool_query_round(args: dict[str, Any], _bot: Any, _chat_id: int, _db_path: str, _notify_state: dict[str, bool] | None) -> str:
    """Query live round status for the main agent."""
    from cyrene.agent.state import _current_agent_id

    if _current_agent_id.get() != "main":
        return "Only the main agent can inspect live round status."
    from cyrene.agent.round import query_live_rounds

    return query_live_rounds(round_id=str(args.get("round_id", "")).strip())


async def _tool_recall_memory(args: dict[str, Any], _bot: Any, _chat_id: int, _db_path: str, _notify_state: dict[str, bool] | None) -> str:
    """Recall archived session history plus persisted memory."""
    query = str(args.get("query", "") or "").strip()
    session_id = str(args.get("session_id", "") or "").strip()
    date = str(args.get("date", "") or "").strip()
    limit = max(1, min(int(args.get("limit", 5) or 5), 10))
    include_soul = bool(args.get("include_soul", True))
    include_short_term = bool(args.get("include_short_term", True))

    matches = recall_conversations(
        query=query,
        session_id=session_id,
        date=date,
        limit=limit,
    )
    payload: dict[str, Any] = {
        "query": query,
        "session_id": session_id,
        "date": date,
        "matches": [
            {
                "date": item.get("date", ""),
                "timestamp": item.get("timestamp", ""),
                "archive_session_id": item.get("archive_session_id", ""),
                "session_title": item.get("session_title", ""),
                "round_id": item.get("round_id", ""),
                "round_title": item.get("round_title", ""),
                "user": item.get("user_body", ""),
                "assistant": item.get("assistant_body", ""),
            }
            for item in matches
        ],
    }
    if include_short_term:
        payload["short_term_memory"] = _get_short_term_context(
            max_chars=1800,
            header="[Short-term cross-session memory:]",
        )
    if include_soul:
        payload["soul_memory"] = _truncate(read_shallow_memory(), 3000)
    if not payload["matches"]:
        payload["note"] = "No archived session matches found for the given filters."
    return _json_result(payload)


async def _tool_start_shell(args: dict[str, Any], _bot: Any, _chat_id: int, _db_path: str, _notify_state: dict[str, bool] | None) -> str:
    from cyrene.agent.state import _current_round_id

    cwd = str(_resolve_workspace_path(str(args.get("cwd", ".") or ".")))
    command = str(args.get("command", "") or "")
    if command:
        try:
            _guard_shell_command_workspace_write(command)
        except ValueError:
            return await _request_write_elevation(tool_name="StartShell", path_hint=cwd, reason=command[:240])
    snap = await _start_shell_session(
        command=command,
        cwd=cwd,
        title=str(args.get("title", "") or ""),
        round_id=_current_round_id.get(),
    )
    return _json_result({
        "shell_id": snap.get("id", ""),
        "status": snap.get("status", ""),
        "cwd": snap.get("cwd", "."),
        "title": snap.get("title", "independent shell"),
    })


async def _tool_send_shell(args: dict[str, Any], _bot: Any, _chat_id: int, _db_path: str, _notify_state: dict[str, bool] | None) -> str:
    command = str(args.get("command", ""))
    try:
        _guard_shell_command_workspace_write(command)
    except ValueError:
        return await _request_write_elevation(tool_name="SendShell", path_hint="", reason=command[:240])
    snap = await _send_shell_session(
        str(args.get("shell_id", "")),
        command,
        wait_ms=int(args.get("wait_ms", 700) or 700),
    )
    return _json_result({
        "shell_id": snap.get("id", ""),
        "status": snap.get("status", ""),
        "elapsed": snap.get("elapsed", "—"),
        "lines": snap.get("lines", [])[-20:],
    })


async def _tool_list_shells(_args: dict[str, Any], _bot: Any, _chat_id: int, _db_path: str, _notify_state: dict[str, bool] | None) -> str:
    shells = _list_shell_sessions(include_exited=False)
    if not shells:
        return "No independent shells are currently running."
    return _json_result([
        {
            "shell_id": item.get("id", ""),
            "title": item.get("title", "independent shell"),
            "cwd": item.get("cwd", "."),
            "status": item.get("status", ""),
            "elapsed": item.get("elapsed", "—"),
        }
        for item in shells
    ])


async def _tool_close_shell(args: dict[str, Any], _bot: Any, _chat_id: int, _db_path: str, _notify_state: dict[str, bool] | None) -> str:
    snap = await _close_shell_session(str(args.get("shell_id", "")))
    return _json_result({
        "shell_id": snap.get("id", ""),
        "status": snap.get("status", ""),
        "elapsed": snap.get("elapsed", "—"),
    })


async def _tool_cc_status(_args: dict[str, Any], _bot: Any, _chat_id: int, _db_path: str, _notify_state: dict[str, bool] | None) -> str:
    from cyrene.cc_bridge import get_cc_status
    return json.dumps(get_cc_status(_CC_PROJECT_DIR), ensure_ascii=False)


async def _tool_cc_launch(args: dict[str, Any], _bot: Any, _chat_id: int, _db_path: str, _notify_state: dict[str, bool] | None) -> str:
    from cyrene.cc_bridge import launch_cc_tmux
    session_name = str(args.get("session_name", "") or "").strip()
    return json.dumps(launch_cc_tmux(cwd=_CC_PROJECT_DIR, session_name=session_name), ensure_ascii=False)


# ---------------------------------------------------------------------------
# Skill management tools
# ---------------------------------------------------------------------------


async def _tool_install_skill(args: dict[str, Any], _bot: Any, _chat_id: int, _db_path: str, _notify_state: dict[str, bool] | None) -> str:
    path_str = str(args.get("path", "")).strip()
    if not path_str:
        return json.dumps({"ok": False, "error": "path is required"}, ensure_ascii=False)
    source = Path(path_str)
    if not source.is_absolute():
        source = WORKSPACE_DIR / source
    source = source.resolve()
    if not source.exists():
        return json.dumps({"ok": False, "error": f"path does not exist: {source}"}, ensure_ascii=False)
    result = _install_skill(source)
    if result.get("ok"):
        skill = result.get("skill", {})
        summary = {
            "ok": True,
            "skill": {
                "id": skill.get("id"),
                "name": skill.get("name"),
                "desc": skill.get("desc"),
                "enabled": skill.get("enabled", True),
                "files": len(skill.get("files", [])),
            },
        }
        if result.get("already_installed"):
            summary["already_installed"] = True
        return json.dumps(summary, ensure_ascii=False)
    return json.dumps({"ok": False, "error": result.get("error", "unknown error")}, ensure_ascii=False)


async def _tool_uninstall_skill(args: dict[str, Any], _bot: Any, _chat_id: int, _db_path: str, _notify_state: dict[str, bool] | None) -> str:
    skill_id = str(args.get("skill_id", "")).strip()
    if not skill_id:
        return json.dumps({"ok": False, "error": "skill_id is required"}, ensure_ascii=False)
    skills = _build_skills()
    match = None
    for s in skills:
        if s.get("id") == skill_id or s.get("name", "").lower() == skill_id.lower():
            match = s
            break
    if not match:
        return json.dumps({"ok": False, "error": f"skill not found: {skill_id}"}, ensure_ascii=False)
    removed = _uninstall_skill(match["id"])
    return json.dumps({"ok": removed, "skill_id": match["id"], "name": match.get("name")}, ensure_ascii=False)


async def _tool_list_skills(_args: dict[str, Any], _bot: Any, _chat_id: int, _db_path: str, _notify_state: dict[str, bool] | None) -> str:
    skills = [
        {
            "id": s.get("id"),
            "name": s.get("name"),
            "desc": s.get("desc", "")[:120],
            "enabled": s.get("enabled", True),
            "files": len(s.get("files", [])),
        }
        for s in _build_skills()
    ]
    return json.dumps({"ok": True, "skills": skills}, ensure_ascii=False)


# ---------------------------------------------------------------------------
# Tool definitions and dispatch
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# New tool handlers
# ---------------------------------------------------------------------------


async def _tool_browser_navigate(args: dict[str, Any], _bot: Any, _chat_id: int, _db_path: str, _notify_state: dict[str, bool] | None) -> str:
    from cyrene.browser import navigate
    url = str(args.get("url") or "").strip()
    if not url:
        return "No URL provided."
    result = await navigate(url, extract_text=True)
    parts = [f"Title: {result.get('title', '—')}", f"URL: {result.get('url', url)}"]
    if result.get("text"):
        parts.append(result["text"])
    if result.get("error"):
        parts.append(f"Error: {result['error']}")
    return "\n\n".join(parts)


async def _tool_browser_screenshot(args: dict[str, Any], _bot: Any, _chat_id: int, _db_path: str, _notify_state: dict[str, bool] | None) -> str:
    from cyrene.browser import screenshot
    url = str(args.get("url") or "").strip()
    if not url:
        return "No URL provided."
    result = await screenshot(url)
    if result.get("ok"):
        return f"Screenshot saved to {result['path']}.\nTitle: {result.get('title', '—')}"
    return f"Screenshot failed: {result.get('error', 'unknown error')}"


async def _tool_browser_click(args: dict[str, Any], _bot: Any, _chat_id: int, _db_path: str, _notify_state: dict[str, bool] | None) -> str:
    from cyrene.browser import click
    selector = str(args.get("selector") or "").strip()
    if not selector:
        return "No CSS selector provided."
    result = await click(selector)
    if result.get("ok"):
        return f"Clicked {selector}.\nURL: {result.get('url', '—')}\nTitle: {result.get('title', '—')}"
    return f"Click failed: {result.get('error', 'unknown error')}"


async def _tool_browser_type(args: dict[str, Any], _bot: Any, _chat_id: int, _db_path: str, _notify_state: dict[str, bool] | None) -> str:
    from cyrene.browser import type_text
    selector = str(args.get("selector") or "").strip()
    text = str(args.get("text") or "").strip()
    submit = bool(args.get("submit", False))
    if not selector:
        return "No CSS selector provided."
    result = await type_text(selector, text, submit=submit)
    if result.get("ok"):
        return f"Typed into {selector}.\nURL: {result.get('url', '—')}\nTitle: {result.get('title', '—')}"
    return f"Type failed: {result.get('error', 'unknown error')}"


async def _tool_send_notification(args: dict[str, Any], _bot: Any, _chat_id: int, _db_path: str, _notify_state: dict[str, bool] | None) -> str:
    from cyrene.notifications import notify
    from cyrene.agent.state import _conversation_source

    title = str(args.get("title") or "Cyrene").strip()
    text = str(args.get("text") or "").strip()
    channel = str(args.get("channel") or "auto").strip()
    if not text:
        return "No notification text provided."

    source = _conversation_source.get()

    # When the conversation started from WebUI (default), skip Telegram and WeChat
    # so that WebUI interactions don't leak to external messaging channels.
    # The settings toggle (notify_telegram / notify_wechat) still controls
    # scheduled/background notifications through the scheduler.
    if source == "webui":
        if channel in ("telegram", "wechat"):
            return f"{channel.capitalize()} notifications are not available from WebUI."
        if channel == "auto":
            # Only try sse — desktop/webhook are local and OK too, but "auto"
            # from WebUI should not attempt Telegram/WeChat
            result = await notify(title, text, channel="sse")
        else:
            result = await notify(title, text, channel=channel)
    else:
        result = await notify(title, text, channel=channel)

    if result.get("ok"):
        channels = list(result.get("channels", {}).keys())
        return f"Notification sent via: {', '.join(channels)}"
    errors = [f"{k}: {v.get('error', '?')}" for k, v in result.get("channels", {}).items() if not v.get("ok")]
    return f"Notification failed: {'; '.join(errors)}"


# ---------------------------------------------------------------------------
# TOOL_DEFS
# ---------------------------------------------------------------------------

TOOL_DEFS = [
    {
        "type": "function",
        "function": {
            "name": "send_telegram",
            "description": "Send a Telegram message to the user. NOT for agent-to-agent communication — use send_agent_message instead.",
            "parameters": {"type": "object", "properties": {"text": {"type": "string"}}, "required": ["text"]},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "send_message",
            "description": "Main agent only. Send a brief user-visible mid-run reply in the current chat. Never use this for subagent coordination or subagent final delivery.",
            "parameters": {"type": "object", "properties": {"text": {"type": "string"}}, "required": ["text"]},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "send_message_to_user",
            "description": "Reply directly to the user. Only available when the user has @mentioned you directly. Use this to respond to the user's direct message. Not for normal rounds — use quit for those.",
            "parameters": {"type": "object", "properties": {"text": {"type": "string"}}, "required": ["text"]},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "send_file",
            "description": "Main agent only. Send a file you have ACTUALLY CREATED to the WebUI as a downloadable attachment. Only call this for files that exist in the workspace — never fabricate or guess paths. The path must point to a real file you wrote via Write/Bash. Do NOT merely print a filename or path in chat.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Workspace-relative or absolute path to a file you created that actually exists."},
                    "name": {"type": "string", "description": "Optional display filename shown in the WebUI."},
                    "text": {"type": "string", "description": "Brief description of the file contents. Keep it factual and short."},
                },
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "ask_user",
            "description": "Ask the user a clarification question and pause until they answer. Use this liberally — asking is better than assuming. Trigger when: the request is ambiguous, details are missing, multiple reasonable approaches exist, or you need sign-off before a risky action. If you need to ask the user anything, use this tool instead of putting a question in assistant text. Use freeform text for open questions, or add a short options array for structured choices. The UI always allows custom answers even with options.",
            "parameters": {
                "type": "object",
                "properties": {
                    "text": {"type": "string", "description": "The clarification question to show the user."},
                    "options": {
                        "type": "array",
                        "description": "Optional short option labels when structured choices would help.",
                        "items": {"type": "string"},
                    },
                },
                "required": ["text"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "PromptClaudeCode",
            "description": "Prepare a stronger Claude Code prompt from the user's task, then show it to the user for confirmation. Use this when the user wants Claude Code to execute a task and you want Cyrene to optimize the prompt first. Requires Claude Code to already be running; check with CheckClaudeCode and launch with StartClaudeCode if needed.",
            "parameters": {
                "type": "object",
                "properties": {
                    "task": {"type": "string", "description": "The task that should be turned into a better Claude Code prompt."},
                },
                "required": ["task"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "schedule_task",
            "description": "Schedule a task. schedule_type must be cron, interval, or once.",
            "parameters": {
                "type": "object",
                "properties": {
                    "prompt": {"type": "string"},
                    "schedule_type": {"type": "string"},
                    "schedule_value": {"type": "string"},
                },
                "required": ["prompt", "schedule_type", "schedule_value"],
            },
        },
    },
    {
        "type": "function",
        "function": {"name": "list_tasks", "description": "List all scheduled tasks.", "parameters": {"type": "object", "properties": {}}},
    },
    {
        "type": "function",
        "function": {
            "name": "pause_task",
            "description": "Pause a scheduled task.",
            "parameters": {"type": "object", "properties": {"task_id": {"type": "string"}}, "required": ["task_id"]},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "resume_task",
            "description": "Resume a paused scheduled task.",
            "parameters": {"type": "object", "properties": {"task_id": {"type": "string"}}, "required": ["task_id"]},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "cancel_task",
            "description": "Cancel and delete a scheduled task.",
            "parameters": {"type": "object", "properties": {"task_id": {"type": "string"}}, "required": ["task_id"]},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "Read",
            "description": "Read a UTF-8 text file from the workspace.",
            "parameters": {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "Write",
            "description": "Write a UTF-8 text file in the workspace.",
            "parameters": {
                "type": "object",
                "properties": {"path": {"type": "string"}, "content": {"type": "string"}},
                "required": ["path", "content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "Edit",
            "description": "Replace an exact string in a text file.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "old_string": {"type": "string"},
                    "new_string": {"type": "string"},
                    "replace_all": {"type": "boolean"},
                },
                "required": ["path", "old_string", "new_string"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "AnalyzeAttachment",
            "description": "Analyze an uploaded attachment or workspace file. PDFs are parsed to text locally. Images return metadata and, when the current model appears multimodal, a vision-based description/OCR. Use this whenever the user uploaded a PDF or image and you need its contents.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Absolute path to the uploaded file or a workspace-relative path."},
                    "prompt": {"type": "string", "description": "Optional custom instruction for image analysis."},
                    "force_refresh": {"type": "boolean", "description": "Recompute analysis instead of using cached sidecar output."},
                },
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "Glob",
            "description": "Find files in the workspace using a glob pattern.",
            "parameters": {"type": "object", "properties": {"pattern": {"type": "string"}}, "required": ["pattern"]},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "Grep",
            "description": "Search file contents by regex pattern inside the workspace.",
            "parameters": {
                "type": "object",
                "properties": {
                    "pattern": {"type": "string"},
                    "path": {"type": "string"},
                    "glob": {"type": "string"},
                },
                "required": ["pattern"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "Bash",
            "description": "Run a shell command in the workspace.",
            "parameters": {
                "type": "object",
                "properties": {"command": {"type": "string"}, "timeout_ms": {"type": "integer"}},
                "required": ["command"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "RecallMemory",
            "description": "Recall relevant memory from other archived sessions. Searches conversation archives by keyword, session ID, or date, and can include short-term memory plus SOUL.md memory in the result.",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Keyword or phrase to search for in archived conversations."},
                    "session_id": {"type": "string", "description": "Optional archive session id, such as session_abcd1234 or archive_2026-05-19_session_abcd1234."},
                    "date": {"type": "string", "description": "Optional date filter in YYYY-MM-DD format."},
                    "limit": {"type": "integer", "description": "Maximum number of archived conversation matches to return (1-10)."},
                    "include_soul": {"type": "boolean", "description": "Whether to include SOUL.md shallow memory in the result."},
                    "include_short_term": {"type": "boolean", "description": "Whether to include short-term cross-session memory in the result."},
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "StartShell",
            "description": "Start an independent persistent shell session for long-running work. Use this when you need a shell that stays alive and should keep appearing in the UI shell list.",
            "parameters": {
                "type": "object",
                "properties": {
                    "cwd": {"type": "string"},
                    "title": {"type": "string"},
                    "command": {"type": "string", "description": "Optional initial command to run immediately after the shell starts"},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "SendShell",
            "description": "Send a command to an existing persistent shell session and wait briefly for new output.",
            "parameters": {
                "type": "object",
                "properties": {
                    "shell_id": {"type": "string"},
                    "command": {"type": "string"},
                    "wait_ms": {"type": "integer"},
                },
                "required": ["shell_id", "command"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "ListShells",
            "description": "List currently running independent persistent shell sessions.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "CloseShell",
            "description": "Terminate an independent persistent shell session.",
            "parameters": {
                "type": "object",
                "properties": {
                    "shell_id": {"type": "string"},
                },
                "required": ["shell_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "WebFetch",
            "description": "Fetch a URL and return the response text.",
            "parameters": {"type": "object", "properties": {"url": {"type": "string"}}, "required": ["url"]},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "WebSearch",
            "description": "Search the web and return the top result links.",
            "parameters": {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "quit",
            "description": "Call this when the task is complete and the interaction should end.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "send_agent_message",
            "description": "Send a message to another sub-agent via inbox. Use this to communicate with other sub-agents.",
            "parameters": {
                "type": "object",
                "properties": {
                    "to": {"type": "string", "description": "Target agent ID"},
                    "content": {"type": "string", "description": "Message content"},
                },
                "required": ["to", "content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "broadcast_agent_message",
            "description": "CAUTION: Broadcast a message to ALL peer sub-agents simultaneously — use SPARINGLY. Every broadcast interrupts every peer. Only broadcast information that EVERY peer genuinely needs (e.g. a shared source URL, a critical deadline). For targeted coordination, use send_agent_message instead.",
            "parameters": {
                "type": "object",
                "properties": {
                    "content": {"type": "string", "description": "Message content to broadcast to all peers"},
                },
                "required": ["content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "spawn_subagent",
            "description": "Main agent only. Spawn a sub-agent. Subagents must not spawn more subagents; they should coordinate with peers via send_agent_message and finish via quit.",
            "parameters": {
                "type": "object",
                "properties": {
                    "agent_id": {"type": "string", "description": "Unique ID for the sub-agent"},
                    "task": {"type": "string", "description": "The task for the sub-agent to complete"},
                    "use_secondary": {"type": "boolean", "description": "Route this sub-agent to the secondary (local small) model for simple tasks that don't need the main model's full reasoning."},
                    "role": {"type": "string", "enum": ["moderator", "participant"], "description": "Optional role for multi-agent discussions. 'moderator' speaks first and drives the discussion; 'participant' waits for the moderator then contributes substantively."},
                },
                "required": ["agent_id", "task"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "query_round",
            "description": "Inspect currently live rounds and their progress. Use this when the user asks how a background round is going or wants the status of a still-running discussion.",
            "parameters": {
                "type": "object",
                "properties": {
                    "round_id": {"type": "string", "description": "Optional specific live round id to inspect"},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "CheckClaudeCode",
            "description": "Check if Claude Code is currently running in a tmux session. Use this when the user asks about Claude Code status, or before StartClaudeCode to see if it's already running. Returns whether CC is running, the session name, and whether it can be launched.",
            "parameters": {
                "type": "object",
                "properties": {},
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "StartClaudeCode",
            "description": "Start Claude Code in a new tmux session. Use this when the user asks you to start, open, launch, or run Claude Code. Creates a detached tmux session named after the project, then registers it so it appears in the WebUI active shells list. Do NOT use Bash to start Claude Code — use this tool instead.",
            "parameters": {
                "type": "object",
                "properties": {
                    "session_name": {
                        "type": "string",
                        "description": "Optional custom tmux session name. If omitted a name is derived from the project directory.",
                    },
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "InstallSkill",
            "description": "Install an external skill from a local path. Supports .md / .txt / .prompt / .json / .yaml / .yml files, directories containing SKILL.md, and .zip archives. The skill is added to the agent's system prompt on the next conversation turn.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "Absolute or workspace-relative path to the skill file, directory, or zip archive.",
                    },
                },
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "UninstallSkill",
            "description": "Uninstall an external skill by its ID or name. Removes the skill files and disables it.",
            "parameters": {
                "type": "object",
                "properties": {
                    "skill_id": {
                        "type": "string",
                        "description": "The ID or name of the skill to uninstall.",
                    },
                },
                "required": ["skill_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "ListSkills",
            "description": "List all installed external skills with their ID, name, description, and enabled status.",
            "parameters": {
                "type": "object",
                "properties": {},
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "send_wechat_file",
            "description": "Send a file you have CREATED to the user via WeChat. Only works when the current conversation is on the WeChat channel — files are encrypted with AES-128-ECB and uploaded to CDN. A delivery notice appears in the WebUI chat history.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Workspace-relative or absolute path to a file you created that actually exists."},
                    "name": {"type": "string", "description": "Optional display filename shown in WeChat and WebUI."},
                    "text": {"type": "string", "description": "Brief description shown alongside the file in WebUI."},
                },
                "required": ["path"],
            },
        },
    },
    # ---- Browser tools ----
    {
        "type": "function",
        "function": {
            "name": "browser_navigate",
            "description": "Fetch a web page and return its readable text content. Use for browsing documentation, news, or any public web page. Returns title, URL, and extracted text.",
            "parameters": {
                "type": "object",
                "properties": {
                    "url": {"type": "string", "description": "The full URL to navigate to (e.g. https://example.com/page)"},
                },
                "required": ["url"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "browser_screenshot",
            "description": "Take a screenshot of a web page. Requires Playwright to be installed.",
            "parameters": {
                "type": "object",
                "properties": {
                    "url": {"type": "string", "description": "The full URL to screenshot."},
                },
                "required": ["url"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "browser_click",
            "description": "Click an element on the current page by CSS selector. Requires Playwright. Call browser_navigate first.",
            "parameters": {
                "type": "object",
                "properties": {
                    "selector": {"type": "string", "description": "CSS selector for the element to click (e.g. 'button.submit', '#login-btn', 'a[href=\"/page\"]')"},
                },
                "required": ["selector"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "browser_type",
            "description": "Type text into an input element on the current page. Requires Playwright. Call browser_navigate first.",
            "parameters": {
                "type": "object",
                "properties": {
                    "selector": {"type": "string", "description": "CSS selector for the input element."},
                    "text": {"type": "string", "description": "The text to type."},
                    "submit": {"type": "boolean", "description": "Press Enter after typing to submit the form."},
                },
                "required": ["selector", "text"],
            },
        },
    },
    # ---- Notification tool ----
    {
        "type": "function",
        "function": {
            "name": "send_notification",
            "description": "Send a desktop or webhook notification. Use for alerts, reminders, or when you need the user's attention outside the chat. Supports Telegram and WeChat if configured.",
            "parameters": {
                "type": "object",
                "properties": {
                    "title": {"type": "string", "description": "Short notification title."},
                    "text": {"type": "string", "description": "Notification body text."},
                    "channel": {"type": "string", "description": "Delivery channel: 'auto' (try all available), 'desktop', 'webhook', 'telegram', 'wechat', or 'sse'."},
                },
                "required": ["text"],
            },
        },
    },
]


# TOOL_HANDLERS without "quit" — agent.py adds it after import to avoid circular import.
TOOL_HANDLERS: dict[str, Any] = {
    "send_telegram": _tool_send_message,
    "send_wechat_file": _tool_send_wechat_file,
    "send_message": _tool_send_user_message,
    "send_message_to_user": _tool_send_message_to_user,
    "send_file": _tool_send_file,
    "ask_user": _tool_ask_user,
    "PromptClaudeCode": _tool_prompt_claude_code,
    "send_agent_message": _tool_send_agent_message,
    "broadcast_agent_message": _tool_broadcast_agent_message,
    "spawn_subagent": _tool_spawn_subagent,
    "query_round": _tool_query_round,
    "schedule_task": _tool_schedule_task,
    "list_tasks": _tool_list_tasks,
    "pause_task": _tool_pause_task,
    "resume_task": _tool_resume_task,
    "cancel_task": _tool_cancel_task,
    "Read": _tool_read,
    "Write": _tool_write,
    "Edit": _tool_edit,
    "AnalyzeAttachment": _tool_analyze_attachment,
    "Glob": _tool_glob,
    "Grep": _tool_grep,
    "Bash": _tool_bash,
    "RecallMemory": _tool_recall_memory,
    "StartShell": _tool_start_shell,
    "SendShell": _tool_send_shell,
    "ListShells": _tool_list_shells,
    "CloseShell": _tool_close_shell,
    "WebFetch": _tool_webfetch,
    "WebSearch": _tool_websearch,
    "CheckClaudeCode": _tool_cc_status,
    "StartClaudeCode": _tool_cc_launch,
    "InstallSkill": _tool_install_skill,
    "UninstallSkill": _tool_uninstall_skill,
    "ListSkills": _tool_list_skills,
    # New tools
    "browser_navigate": _tool_browser_navigate,
    "browser_screenshot": _tool_browser_screenshot,
    "browser_click": _tool_browser_click,
    "browser_type": _tool_browser_type,
    "send_notification": _tool_send_notification,
}

# Register map pin tool (deferred import to avoid circular dependency).
def _register_map_tool() -> None:
    from cyrene.map_pin_tool import register_to
    register_to(TOOL_DEFS, TOOL_HANDLERS)

_register_map_tool()


def get_active_tool_defs() -> list[dict]:
    """Return TOOL_DEFS filtered by enabled tools, plus MCP tools from connected servers.

    Protected tools (quit) are always included.
    """
    return get_active_tool_defs_for_actor()


def _tool_blocklist_for_actor(actor: str) -> set[str]:
    return set(_MAIN_ONLY_TOOLS) if actor == "subagent" else set()


def is_tool_allowed_for_actor(name: str, actor: str = "main") -> bool:
    return str(name or "") not in _tool_blocklist_for_actor(actor)


def get_active_tool_defs_for_actor(actor: str = "main") -> list[dict]:
    """Return enabled tool defs filtered for the requested actor type."""
    from cyrene.settings_store import is_tool_enabled

    blocked = _tool_blocklist_for_actor(actor)
    defs = [
        td for td in TOOL_DEFS
        if is_tool_enabled(td["function"]["name"]) and td["function"]["name"] not in blocked
    ]

    # Append MCP tools from connected servers
    try:
        from cyrene.mcp_manager import get_manager as _get_mcp_mgr

        manager = _get_mcp_mgr()
        for mcp_td in manager.get_tool_defs():
            name = mcp_td["function"]["name"]
            if is_tool_enabled(name) and name not in blocked:
                defs.append(mcp_td)
    except Exception:
        logger.warning("Failed to fetch MCP tool defs", exc_info=True)

    return defs


async def _execute_tool(name: str, arguments: dict[str, Any], bot: Any, chat_id: int, db_path: str, notify_state: dict[str, bool] | None) -> str:
    if name == "spawn_subagent":
        from cyrene.settings_store import get_spawn_policy
        if get_spawn_policy() == "off":
            return "Subagent spawning is disabled by the current spawn policy (`off`). Stay in single-agent mode unless the user explicitly changes this setting."
    handler = TOOL_HANDLERS.get(name)
    if handler is None:
        # Fallback: try MCP tool
        from cyrene import debug as _debug
        from cyrene.agent.state import _caller_type, _current_round_id
        from cyrene.mcp_manager import get_manager as _get_mcp_mgr

        _t0 = time.monotonic()
        try:
            manager = _get_mcp_mgr()
            result = await manager.execute_tool(name, arguments)
            if _debug.VERBOSE:
                _debug.log_tool_call(_caller_type.get(), name, arguments, result, (time.monotonic() - _t0) * 1000)
            await _debug.publish_event({
                "type": "tool_call", "caller": _caller_type.get(), "tool": name, "args": arguments,
                "result": str(result),
                "round_id": _current_round_id.get(),
            })
            from cyrene.pattern import record_action
            await record_action(name, arguments, _caller_type.get(), _current_round_id.get(),
                          (time.monotonic() - _t0) * 1000,
                          result=result, success=True, error="")
            return result
        except ValueError:
            raise ValueError(f"Unknown tool: {name}")
        except Exception as e:
            from cyrene.pattern import record_action
            await record_action(
                name,
                arguments,
                _caller_type.get(),
                _current_round_id.get(),
                (time.monotonic() - _t0) * 1000,
                result=f"Tool {name} failed: {e}",
                success=False,
                error=str(e),
            )
            return f"Tool {name} failed: {e}"

    _t0 = time.monotonic()
    try:
        result = await handler(arguments, bot, chat_id, db_path, notify_state)
    except Exception as e:
        from cyrene import debug
        from cyrene.agent.state import _caller_type, _current_round_id
        await debug.publish_event({
            "type": "tool_call", "caller": _caller_type.get(), "tool": name, "args": arguments,
            "result": f"Tool failed: {e}",
            "round_id": _current_round_id.get(),
        })
        from cyrene.pattern import record_action
        await record_action(
            name,
            arguments,
            _caller_type.get(),
            _current_round_id.get(),
            (time.monotonic() - _t0) * 1000,
            result=f"Tool failed: {e}",
            success=False,
            error=str(e),
        )
        raise
    from cyrene import debug
    if debug.VERBOSE:
        from cyrene.agent.state import _caller_type
        debug.log_tool_call(_caller_type.get(), name, arguments, result, (time.monotonic() - _t0) * 1000)
    from cyrene.agent.state import _caller_type, _current_round_id
    await debug.publish_event({
        "type": "tool_call", "caller": _caller_type.get(), "tool": name, "args": arguments,
        "result": str(result),
        "round_id": _current_round_id.get(),
    })
    from cyrene.pattern import record_action
    tool_success = not str(result).lower().startswith("tool failed:")
    await record_action(
        name,
        arguments,
        _caller_type.get(),
        _current_round_id.get(),
        (time.monotonic() - _t0) * 1000,
        result=result,
        success=tool_success,
        error="" if tool_success else str(result),
    )
    return result
