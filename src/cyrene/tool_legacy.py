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
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx

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
from cyrene.schedule_spec import compute_next_run
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
from cyrene.workbench_context import resolve_project_data_key_for_session

logger = logging.getLogger(__name__)
_CC_PROJECT_DIR = WORKSPACE_DIR.parent
_MAIN_ONLY_TOOLS = {
    "send_telegram",
    "send_message",
    "send_file",
    "send_wechat_file",
    "ask_user",
    "DeepReflect",
    "spawn_subagent",
    "query_round",
    # Browser tools are main-agent-only. The browser is a single shared global
    # session (one context, one page), so letting subagents drive it as well
    # would make concurrent agents fight over the same page. Reserving it for
    # the main agent sidesteps that without per-session isolation. See #52.
    "browser_navigate",
    "browser_screenshot",
    "browser_click",
    "browser_type",
    "browser_request_takeover",
}

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _resolve_workspace_path(path_str: str) -> Path:
    from cyrene.agent.state import active_workspace_dir
    workspace = active_workspace_dir()
    candidate = Path(path_str)
    path = candidate if candidate.is_absolute() else workspace / candidate
    resolved = path.resolve()
    if resolved != workspace and workspace not in resolved.parents:
        raise ValueError(
            f"⛔ 已禁止：路径超出 workspace 范围。\n"
            f"  请求路径：{path_str}\n"
            f"  完整路径：{resolved}\n"
            f"  Workspace：{workspace}\n"
            f"  Agent 没有访问此路径的权限。"
        )
    return resolved


def _workspace_permission_error() -> str:
    return "Write and delete permissions are limited to the current workspace."


def _resolve_workspace_write_target(path_str: str) -> Path:
    from cyrene.agent.state import _temporary_full_access, active_workspace_dir
    from cyrene.settings_store import get_write_permission_mode
    if get_write_permission_mode() == "full_access" or _temporary_full_access.get():
        candidate = Path(path_str)
        path = candidate if candidate.is_absolute() else active_workspace_dir() / candidate
        return path.resolve()
    try:
        return _resolve_workspace_path(path_str)
    except Exception as exc:
        raise ValueError(_workspace_permission_error()) from exc


async def _request_scope_elevation(
    *,
    tool_name: str,
    path_hint: str,
    operation: str,
    reason: str = "",
    permission_kind: str = "scope_elevation",
    options: list[str] | None = None,
    scope_hint: str = "workspace 之外的 ",
) -> str | None:
    """Resolve a permission boundary according to the active permission mode.

    Returns ``None`` when the operation is **allowed** (caller should proceed),
    or a ``str`` otherwise:

    - ``default`` mode → creates a pending question and returns the
      ``awaiting_user`` JSON; the agent loop pauses until the user answers.
    - ``auto`` mode → a review agent decides autonomously. Approve → sets
      temporary full access and returns ``None``; deny → returns a denial
      message string for the agent to see.
    - ``full_access`` mode → returns ``None`` (normally short-circuited earlier).

    Args:
        tool_name: The tool being used (e.g. "Read", "Write").
        path_hint: The target path the agent wants to access.
        operation: Human-readable description of the operation.
        reason: Why the agent needs to access this path.
        permission_kind: Meta field to identify the permission type.
        options: Custom options for the question UI.
    """
    import cyrene.agent.state as _state
    from cyrene.agent.state import (
        _current_agent_id,
        _current_client_request_id,
        _current_command,
        _current_round_id,
        _publish_runtime_event,
    )
    from cyrene.agent.session import (
        _upsert_pending_question,
        get_session_labels,
    )
    if _current_agent_id.get() != "main":
        return (
            f"⛔ 已禁止：{operation} 超出 workspace 范围。\n"
            f"Subagent 无权申请权限提升，请向主 agent 报告。"
        )
    round_id = str(_current_round_id.get() or "").strip()
    if not round_id:
        return (
            f"⛔ 已禁止：{operation} 超出 workspace 范围。\n"
            f"当前不在活动对话轮次中，无法申请权限。"
        )

    mode = _state._permission_mode.get()
    # 完全访问模式：工具层通常已用 _temporary_full_access 短路，这里保险直接放行。
    if mode == "full_access":
        return None
    # 自动模式：审核 agent 自主裁决，从不打扰用户。
    if mode == "auto":
        from cyrene.agent.auto_review import review_elevation
        approved, rationale = await review_elevation(
            tool_name=tool_name,
            operation=operation,
            path_hint=path_hint,
            reason=reason,
        )
        await _publish_runtime_event({
            "type": "auto_review",
            "approved": approved,
            "operation": operation,
            "tool_name": tool_name,
            "path_hint": path_hint,
            "rationale": rationale,
            "round_id": round_id,
        })
        if approved:
            _state._temporary_full_access.set(True)
            return None
        return (
            f"⛔ 审核 agent 拒绝了此操作（{operation}）。\n"
            f"原因：{rationale}\n"
            f"请改用更安全的方式（如限制在 workspace 内、避免破坏性命令），或向用户说明此操作的必要性。"
        )

    # 默认模式（计划模式同意后也已回退为 default）：弹出提问让用户授权。
    labels = get_session_labels(round_id)
    detail = f"\n📂 目标路径：{path_hint}" if path_hint else ""
    why = f"\n💡 原因：{reason}" if reason else ""
    effective_options = options or ["允许这次", "拒绝"]
    question = await _upsert_pending_question({
        "text": (
            f"⚠️ **Agent 尝试执行 {scope_hint}{operation}**\n\n"
            f"工具：{tool_name}{detail}{why}\n\n"
            f"请确认是否允许此操作。如果不允许，Agent 将仅能在当前 workspace 内工作。"
        ),
        "round_id": round_id,
        "round_title": labels.get("round_title", ""),
        "client_request_id": str(_current_client_request_id.get() or "").strip(),
        "options": effective_options,
        "allow_custom": True,
        "meta": {
            "kind": permission_kind,
            "tool_name": tool_name,
            "path_hint": path_hint,
            "reason": reason,
            "operation": operation,
            "command": _current_command.get() or "",
        },
    })
    return _json_result({
        "status": "awaiting_user",
        "question_id": question.get("id", ""),
        "permission": permission_kind,
        "tool": tool_name,
        "path": path_hint,
        "operation": operation,
    })


async def _request_write_elevation(
    *,
    tool_name: str,
    path_hint: str,
    reason: str = "",
) -> str | None:
    return await _request_scope_elevation(
        tool_name=tool_name,
        path_hint=path_hint,
        operation="写入/删除操作",
        reason=reason,
        permission_kind="write_permission_request",
        options=["仅这次允许", "始终允许", "保持仅限 workspace"],
    )


async def _request_read_elevation(
    *,
    tool_name: str,
    path_hint: str,
    reason: str = "",
) -> str | None:
    return await _request_scope_elevation(
        tool_name=tool_name,
        path_hint=path_hint,
        operation="读取操作",
        reason=reason,
        permission_kind="read_elevation",
        options=["允许这次读取", "拒绝"],
    )


def _command_is_file_deletion(command: str) -> bool:
    """Check if a shell command includes file deletion operations."""
    raw = str(command or "").strip()
    if not raw:
        return False
    # Extract the first command word (handles /bin/rm, 'rm', \rm, etc.)
    first = _extract_first_command(raw)
    if first in ("rm", "rmdir"):
        return True
    # Also detect rm$IFS and rm${IFS} (word splitting tricks)
    if re.search(r'(?:^|\s)(?:rm|rmdir)\$IFS', raw):
        return True
    if re.search(r'(?:^|\s)(?:rm|rmdir)\$\{IFS\}', raw):
        return True
    return False


async def _request_delete_confirmation(
    *,
    tool_name: str,
    command: str,
) -> str | None:
    """Request user confirmation before a destructive file operation in the workspace."""
    cmd_preview = command[:240]
    return await _request_scope_elevation(
        tool_name=tool_name,
        path_hint="",
        operation="文件删除操作",
        reason=f"Agent 尝试删除 workspace 中的文件。\n命令：{cmd_preview}",
        permission_kind="delete_confirmation",
        options=["允许删除", "拒绝"],
    )


def _extract_first_command(raw: str) -> str:
    """Extract the first command word, stripping quotes and path prefixes.

    Handles: rm, /bin/rm, 'rm', "rm", \rm, 'rm' -rf, etc.
    """
    raw = str(raw or "").strip()
    if not raw:
        return ""
    try:
        first = shlex.split(raw, posix=True)[0]
    except Exception:
        first = raw.split()[0] if raw.split() else ""
    # Strip leading path: /bin/rm → rm
    first = re.sub(r'^.*/', '', first)
    # Strip leading backslash or quotes
    first = first.lstrip("\\").lstrip("'").lstrip('"').rstrip("'").rstrip('"')
    return first.lower()


def _shell_command_requires_write_guard(command: str) -> bool:
    raw = str(command or "").strip()
    if not raw:
        return False
    lowered = raw.lower()
    # Quick substring check first (fast path)
    if any(token in lowered for token in (
        " rm ", "rm -", "mv ", "cp ", "mkdir ", "touch ", "tee ",
        "sed -i", "truncate ", "install ", "rmdir ", "unlink ", ">",
    )):
        return True
    # Check normalized first command word
    first = _extract_first_command(raw)
    WRITE_COMMANDS = {"rm", "mv", "cp", "mkdir", "touch", "tee", "truncate", "install", "rmdir", "unlink", "dd", "sed", "ln"}
    if first in WRITE_COMMANDS:
        return True
    # Check for IFS variants: dd$IFS, rm$IFS, etc.
    if re.search(r'\b(?:rm|mv|cp|dd|tee)\$IFS\b', lowered):
        return True
    if re.search(r'\b(?:rm|mv|cp|dd|tee)\$\{IFS\}', lowered):
        return True
    # Check for ln -f / ln --force
    if " ln -f " in lowered or " ln --force " in lowered:
        return True
    return False


def _is_dangerous_subshell(command: str) -> bool:
    """Shell 命令替换 ($(...) 或反引号) 的路径无法静态预测，必须拦截并询问用户。"""
    raw = str(command or "").strip()
    if re.search(r'\$\(', raw):
        return True
    if '`' in raw:
        return True
    return False


def _check_command_substitution(command: str) -> None:
    """Raise ValueError with clear message if command contains unpredictable shell substitution."""
    if _is_dangerous_subshell(command):
        raise ValueError(
            f"⛔ 已禁止：Shell 命令包含命令替换 ($(...) 或反引号)。\n"
            f"  命令：{command[:240]}\n"
            f"  命令替换的路径无法提前验证，请使用明确的路径。"
        )


def _expand_shell_path(token: str) -> str:
    """Expand $VAR, ~, and ~user in a path token so the workspace guard sees the real path."""
    expanded = os.path.expandvars(token)
    expanded = os.path.expanduser(expanded)
    return expanded


def _extract_stderr_redirect_targets(raw: str) -> list[str]:
    """Detect stderr redirects like 2>/path, 2>>/path, &>/path."""
    targets: list[str] = []
    # 2>/path or 2>>/path
    for m in re.finditer(r'(?:^|\s)(\d*)>>?\s*(\S+)', raw):
        prefix = m.group(1)  # empty or digit
        target = m.group(2)
        # If prefix is empty or a digit like 2, and target doesn't start with & (like &1)
        if (not prefix or prefix.isdigit()) and not target.startswith("&"):
            targets.append(target)
    # &>/path (redirect both stdout and stderr)
    for m in re.finditer(r'(?:^|\s)&\s*>\s*(\S+)', raw):
        targets.append(m.group(1))
    return targets


def _guard_shell_command_workspace_write(command: str) -> None:
    raw = str(command or "").strip()
    if not raw or not _shell_command_requires_write_guard(raw):
        return
    # 命令替换无法预测展开后的路径，直接拦截
    _check_command_substitution(raw)
    try:
        tokens = shlex.split(raw, posix=True)
    except Exception:
        raise ValueError(
            f"⛔ 已禁止：Shell 命令可能包含写入操作，但无法解析。\n"
            f"  命令：{command[:200]}\n"
            f"  写入权限限定在 workspace 范围内。"
        )
    write_cmds = {"rm", "mv", "cp", "mkdir", "touch", "tee", "truncate", "install", "rmdir", "unlink", "dd", "ln"}
    cd_cmds = {"cd", "pushd"}
    separators = {"&&", "||", "|", ";"}
    path_like_tokens: list[str] = []
    active_command: str = ""  # Persists across arguments until a separator

    for token in tokens:
        stripped = token.strip()
        if not stripped:
            continue
        # Separator resets command context
        if stripped in separators:
            active_command = ""
            continue
        # Detect new write command
        if stripped in write_cmds:
            active_command = stripped
            continue
        if stripped in cd_cmds:
            active_command = stripped
            continue
        # Handle -o / --output flag (for tee, sed, etc.)
        if stripped in {"-o", "--output"}:
            path_like_tokens.append(stripped)
            continue
        # Redirect token: >path or >>path (may be attached like ">/path" or separate like "> /path")
        if stripped.startswith((">", ">>")):
            candidate = stripped.lstrip(">").strip()
            if candidate:
                path_like_tokens.append(candidate)
            active_command = ""
            continue
        if active_command in write_cmds:
            # Skip flags (start with -)
            if stripped.startswith("-"):
                continue
            # Path-like token: starts with / ./, contains /, or has file extension
            if (stripped.startswith("/") or stripped.startswith("./") or stripped.startswith("../")
                    or "/" in stripped or re.search(r"\.[A-Za-z0-9]{1,8}$", stripped)):
                # For cp/mv: only the last non-flag argument is the destination
                if active_command in ("cp", "mv"):
                    path_like_tokens.append(stripped)
                else:
                    path_like_tokens.append(stripped)
            # For cp/mv with all remaining args as dest, collect them all
            elif active_command in ("cp", "mv"):
                # This could be a dest without path chars (e.g. "cp a b" where "b" is relative dest)
                path_like_tokens.append(stripped)
        elif active_command in cd_cmds:
            # cd destination — resolve from workspace
            if not stripped.startswith("-"):
                path_like_tokens.append(stripped)

    # Detect stderr redirects (2>/path, &>/path) that the loop may have missed
    stderr_targets = _extract_stderr_redirect_targets(raw)
    path_like_tokens.extend(stderr_targets)

    # Fallback: detect > redirects not caught by the loop (e.g. 2>/path where 2 is separate)
    if ">" in raw or ">>" in raw:
        redirection_targets = re.findall(r"(?:^|[^\d])>>?\s*([^\s;&|]+)", raw)
        for target in redirection_targets:
            if target not in path_like_tokens:
                path_like_tokens.append(target)

    # For cp/mv, only keep the LAST path-like token (the destination)
    # Scan from right to find the last non-flag path token
    cp_mv_dest = ""
    for token in reversed(path_like_tokens):
        if token.startswith("-"):
            continue
        cp_mv_dest = token
        break

    blocked_paths: list[str] = []
    for token in path_like_tokens:
        if token.startswith("-"):
            continue
        try:
            # Expand $VAR and ~ before checking workspace boundary
            expanded = _expand_shell_path(token)
            if expanded != token:
                _resolve_workspace_write_target(expanded)
            else:
                _resolve_workspace_write_target(token)
        except ValueError:
            blocked_paths.append(token)
    if blocked_paths:
        raise ValueError(
            f"⛔ 已禁止：Shell 命令试图写入 workspace 之外的路径。\n"
            f"  命令：{command[:200]}\n"
            f"  被阻止的路径：{', '.join(blocked_paths[:5])}\n"
            f"  如需操作外部文件，请通过 WebUI 申请权限。"
        )


def _json_result(payload: Any) -> str:
    if isinstance(payload, str):
        return payload
    return json.dumps(payload, ensure_ascii=False)


def _resolve_tool_path(path_str: str) -> Path:
    if is_uploaded_attachment_path(path_str) or is_exported_attachment_path(path_str):
        return Path(path_str).resolve()
    # Auto-resolve filename to the correct upload path when the agent guesses wrong paths.
    from cyrene.agent.state import _attachment_paths_by_name, _temporary_full_access, active_workspace_dir
    from cyrene.settings_store import get_write_permission_mode
    att_map = _attachment_paths_by_name.get()
    if att_map:
        basename = Path(path_str).name
        if basename in att_map:
            return Path(att_map[basename]).resolve()
    # Honour temporary full-access grants (write-once, read-always) and permanent mode.
    if _temporary_full_access.get() or get_write_permission_mode() == "full_access":
        candidate = Path(path_str)
        path = candidate if candidate.is_absolute() else active_workspace_dir() / candidate
        return path.resolve()
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

    sender = str(_current_agent_id.get() or "").strip()
    if sender not in {"main", "scheduler"}:
        return "Only the main agent can send a user-visible WebUI message. Subagents must report via quit or send_agent_message."

    if sender == "scheduler":
        await append_system_message(
            text,
            message_meta={"scheduled": True},
            publish_event={"scheduled": True},
        )
        if _notify_state is not None:
            _notify_state["sent"] = True
        return "Scheduled message sent to the user."

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
    registered = register_generated_attachment(str(path), display_name=str(args.get("name", "") or "").strip() or None)
    attachment = build_public_attachment_payload(registered)

    # Register in knowledge base
    try:
        from cyrene.config import get_knowledge_db_path
        from cyrene.knowledge import store, ingest
        import mimetypes
        doc_path = registered.get("path", "")
        if doc_path:
            from pathlib import Path
            import mimetypes
            doc_file = Path(doc_path)
            content_type = mimetypes.guess_type(str(doc_file))[0] or "application/octet-stream"
            from cyrene.attachments import attachment_kind_from_meta
            kind = attachment_kind_from_meta(content_type, doc_file.name)
            content_hash = store.content_hash_file(doc_file)
            _kb_db_path = str(get_knowledge_db_path())
            doc = await store.upsert_document_by_path(
                _kb_db_path,
                path=str(doc_file.resolve()),
                source="generated",
                name=registered.get("name", doc_file.name),
                content_type=content_type,
                kind=kind,
                size=doc_file.stat().st_size if doc_file.exists() else 0,
                metadata={"sent_to_chat": True},
                content_hash=content_hash,
            )
            if doc.get("status") in {"pending", "error"}:
                asyncio.create_task(ingest.index_document(_kb_db_path, doc["id"]))
    except Exception as e:
        logger.debug(f"Failed to register generated file in knowledge base: {e}")

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


async def _tool_deep_reflect(args: dict[str, Any], _bot: Any, _chat_id: int, _db_path: str, _notify_state: dict[str, bool] | None) -> str:
    return (
        "DeepReflect is handled inside the main chat loop so it can access the live visible transcript. "
        "If you see this fallback, continue without changing persisted history."
    )


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
    from cyrene.agent.state import _current_session_id

    stype = str(args["schedule_type"])
    svalue = str(args["schedule_value"])
    now = datetime.now(timezone.utc)
    permission_mode = str(args.get("permission_mode", "workspace_only") or "workspace_only").strip().lower()
    if permission_mode not in ("workspace_only", "full_access"):
        permission_mode = "workspace_only"

    next_run = compute_next_run(stype, svalue, now=now)
    if stype == "once":
        # Persist the normalized UTC time as the stored value too, so a re-read
        # of the task shows exactly when it will fire.
        svalue = next_run

    # 如果任务需要 full_access 权限，先向用户申请
    if permission_mode == "full_access":
        prompt_preview = str(args.get("prompt", ""))[:120]
        elevation_result = await _request_scope_elevation(
            tool_name="schedule_task",
            path_hint="",
            operation="定时任务的外部文件访问权限",
            reason=f"此定时任务可能在执行时需要读写 workspace 之外的文件。\n任务内容：{prompt_preview}",
            permission_kind="task_permission_request",
            options=["仅此任务允许 full_access", "拒绝，保持 workspace_only"],
        )
        status = json.loads(elevation_result)
        if str(status.get("status", "")).strip() == "awaiting_user":
            return elevation_result

    project_id = resolve_project_data_key_for_session(_current_session_id.get())
    task_id = await db.create_task(
        db_path,
        chat_id,
        str(args["prompt"]),
        stype,
        svalue,
        next_run,
        permission_mode=permission_mode,
        project_id=project_id,
    )
    return f"Task {task_id} scheduled. Next run: {next_run} 权限模式：{permission_mode}"


async def _tool_list_tasks(_args: dict[str, Any], _bot: Any, _chat_id: int, db_path: str, _notify_state: dict[str, bool] | None) -> str:
    tasks = await db.get_all_tasks(db_path)
    if not tasks:
        return "No scheduled tasks."
    lines = []
    for t in tasks:
        perm = str(t.get("permission_mode") or "workspace_only")
        tag = " 🔓" if perm == "full_access" else ""
        lines.append(f"- [{t['id']}]{tag} {t['status']} | {t['schedule_type']}({t['schedule_value']}) | {t['prompt'][:60]}")
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
    try:
        path = _resolve_tool_path(str(args["path"]))
    except ValueError:
        return await _request_read_elevation(
            tool_name="Read",
            path_hint=str(args.get("path", "")),
            reason=f"Agent 想要读取此文件。",
        )
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
    try:
        path = _resolve_tool_path(str(args["path"]))
    except ValueError:
        return await _request_read_elevation(
            tool_name="AnalyzeAttachment",
            path_hint=str(args.get("path", "")),
            reason="Agent 想要分析此文件内容。",
        )
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
    # 命令替换无法提前验证路径，先拦截并询问用户
    if _is_dangerous_subshell(command):
        return await _request_scope_elevation(
            tool_name="Bash",
            path_hint="",
            operation="包含命令替换的 Shell 操作",
            reason=f"命令包含 $() 或反引号，其展开路径无法静态验证。\n命令：{command[:240]}",
            permission_kind="subshell_elevation",
            options=["允许执行", "拒绝"],
            scope_hint="",
        )
    try:
        _guard_shell_command_workspace_write(command)
    except ValueError:
        return await _request_write_elevation(tool_name="Bash", path_hint="", reason=command[:240])
    # 即使是 workspace 内的文件删除操作，也需要用户确认
    if _command_is_file_deletion(command):
        delete_result = await _request_delete_confirmation(tool_name="Bash", command=command)
        status = json.loads(delete_result)
        if str(status.get("status", "")).strip() == "awaiting_user":
            return delete_result
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
    return await deep_search(query)


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
    from cyrene.agent.state import _current_agent_id, _current_round_id, _current_session_id
    if _current_agent_id.get() != "main":
        return "Only the main agent can spawn subagents."
    session_id = _current_session_id.get()
    await _reg_subagent(agent_id, task, round_id=_current_round_id.get(), role=role, session_id=session_id)
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


async def _tool_search_knowledge(args: dict[str, Any], _bot: Any, _chat_id: int, _db_path: str, _notify_state: dict[str, bool] | None) -> str:
    """Search the user's knowledge base for relevant passages."""
    query = str(args.get("query", "") or "").strip()
    if not query:
        return "Error: query is required."

    k = max(1, int(args.get("k", 6) or 6))

    try:
        from cyrene.config import get_knowledge_db_path
        from cyrene.knowledge import retrieve

        results = await retrieve.search_knowledge(str(get_knowledge_db_path()), query, k=k)
        if not results:
            return "No matching documents found in the knowledge base."

        output_lines = [f"Found {len(results)} matching passage(s) from your knowledge base:\n"]
        for i, result in enumerate(results, start=1):
            doc_name = result.get("document_name", "Unknown")
            content = result.get("content", "")[:400]
            score = result.get("score", 0)
            output_lines.append(f"[{i}. {doc_name}] (score: {score:.2f})\n{content}\n")
        return "\n".join(output_lines)
    except Exception as e:
        logger.debug(f"Knowledge base search failed: {e}")
        return f"Error searching knowledge base: {str(e)}"


async def _tool_start_shell(args: dict[str, Any], _bot: Any, _chat_id: int, _db_path: str, _notify_state: dict[str, bool] | None) -> str:
    from cyrene.agent.state import _current_round_id

    cwd = str(_resolve_workspace_path(str(args.get("cwd", ".") or ".")))
    command = str(args.get("command", "") or "")
    if command:
        if _is_dangerous_subshell(command):
            return await _request_scope_elevation(
                tool_name="StartShell",
                path_hint="",
                operation="包含命令替换的 Shell 操作",
                reason=f"命令包含 $() 或反引号，其展开路径无法静态验证。\n命令：{command[:240]}",
                permission_kind="subshell_elevation",
                options=["允许执行", "拒绝"],
                scope_hint="",
            )
        try:
            _guard_shell_command_workspace_write(command)
        except ValueError:
            return await _request_write_elevation(tool_name="StartShell", path_hint=cwd, reason=command[:240])
        if _command_is_file_deletion(command):
            delete_result = await _request_delete_confirmation(tool_name="StartShell", command=command)
            status = json.loads(delete_result)
            if str(status.get("status", "")).strip() == "awaiting_user":
                return delete_result
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
    if _is_dangerous_subshell(command):
        return await _request_scope_elevation(
            tool_name="SendShell",
            path_hint="",
            operation="包含命令替换的 Shell 操作",
            reason=f"命令包含 $() 或反引号，其展开路径无法静态验证。\n命令：{command[:240]}",
            permission_kind="subshell_elevation",
            options=["允许执行", "拒绝"],
            scope_hint="",
        )
    try:
        _guard_shell_command_workspace_write(command)
    except ValueError:
        return await _request_write_elevation(tool_name="SendShell", path_hint="", reason=command[:240])
    if _command_is_file_deletion(command):
        delete_result = await _request_delete_confirmation(tool_name="SendShell", command=command)
        status = json.loads(delete_result)
        if str(status.get("status", "")).strip() == "awaiting_user":
            return delete_result
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
    try:
        source = _resolve_tool_path(path_str)
    except ValueError:
        return json.dumps({"ok": False, "error": "skill source must be within workspace"}, ensure_ascii=False)
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
        try:
            os.unlink(result["path"])
        except OSError:
            pass
        return f"Screenshot taken.\nTitle: {result.get('title', '—')}"
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


async def _tool_browser_request_takeover(args: dict[str, Any], _bot: Any, _chat_id: int, _db_path: str, _notify_state: dict[str, bool] | None) -> str:
    from cyrene import debug
    from cyrene.browser import get_session
    from cyrene.agent.state import _current_agent_id, _current_client_request_id, _current_round_id
    from cyrene.agent.session import _clear_pending_question, _upsert_pending_question, get_session_labels

    if _current_agent_id.get() != "main":
        return "Only the main agent can request a browser takeover."
    round_id = str(_current_round_id.get() or "").strip()
    if not round_id:
        return "Cannot request a browser takeover outside an active chat round."

    reason = str(args.get("reason") or "").strip() or "请在浏览器窗口完成登录，然后点「我已完成登录」。"

    try:
        session = await get_session()
    except Exception as exc:
        return f"Browser takeover unavailable (Playwright/Chromium not ready): {exc}"
    current_url = await session.current_url()

    # Ask in the app FIRST (the standard question popup), then open the real
    # browser window. The confirmation lives in the app's question UI — the
    # browser panel only shows a passive "waiting for login" placeholder.
    await debug.publish_event({
        "type": "browser_takeover_request",
        "round_id": round_id,
        "url": current_url,
        "reason": reason,
    })
    labels = get_session_labels(round_id)
    question = await _upsert_pending_question({
        "text": reason,
        "round_id": round_id,
        "round_title": labels.get("round_title", ""),
        "client_request_id": str(_current_client_request_id.get() or "").strip(),
        "options": ["我已完成登录"],
        "allow_custom": False,
        "meta": {"kind": "browser_takeover", "url": current_url},
    })
    try:
        await session.switch_to_headed(current_url)
    except Exception as exc:
        # Couldn't open the window — undo the pending question and clear the panel.
        try:
            await _clear_pending_question(str(question.get("id", "")))
        except Exception:
            pass
        await debug.publish_event({"type": "browser_takeover_cancelled", "round_id": round_id})
        return f"Failed to open the browser window for takeover: {exc}"
    return _json_result({
        "status": "awaiting_user",
        "question_id": question.get("id", ""),
        "takeover": True,
    })


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


async def _tool_track_entity(args, bot, chat_id, db_path, notify_state):
    from cyrene.entities import create_entity
    entity = await create_entity(
        db_path,
        type=args.get("type", "task"),
        title=args["title"],
        content=args.get("content", ""),
        priority=args.get("priority", "medium"),
        due_date=args.get("due_date"),
        people=args.get("people", []),
        tags=args.get("tags", []),
        source=args.get("source", "extracted"),
        confidence=args.get("confidence", 1.0),
        source_round_id=args.get("source_round_id"),
    )
    return f"已记录事务：{entity['title']}（ID: {entity['id'][:8]}）"


async def _tool_update_entity(args, bot, chat_id, db_path, notify_state):
    from cyrene.entities import update_entity
    field = args["field"]
    value = args["value"]
    entity = await update_entity(db_path, args["id"], **{field: value})
    if entity is None:
        return f"未找到事务 {args['id']}"
    return f"已更新事务 {entity['title']} 的 {field}"


async def _tool_list_entities(args, bot, chat_id, db_path, notify_state):
    from cyrene.entities import list_entities
    entities = await list_entities(
        db_path,
        type=args.get("type"),
        status=args.get("status", "active"),
        limit=args.get("limit", 50),
    )
    if not entities:
        return "没有找到符合条件的事务。"
    lines = [f"- [{e['type']}] {e['title']}（{e['status']}）{' 截止：'+e['due_date'] if e.get('due_date') else ''}" for e in entities]
    return f"找到 {len(entities)} 条事务：\n" + "\n".join(lines)


async def _tool_query_entities(args, bot, chat_id, db_path, notify_state):
    from cyrene.entities import query_entities
    entities = await query_entities(
        db_path,
        q=args.get("q", ""),
        type=args.get("type"),
        due_before=args.get("due_before"),
    )
    if not entities:
        return "没有找到匹配的事务。"
    lines = [f"- [{e['type']}] {e['title']}" for e in entities]
    return f"找到 {len(entities)} 条事务：\n" + "\n".join(lines)


async def _tool_delete_entity(args, bot, chat_id, db_path, notify_state):
    from cyrene.entities import delete_entity
    success = await delete_entity(db_path, args["id"], permanent=args.get("permanent", False))
    return "已删除事务。" if success else f"未找到事务 {args['id']}"


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
            "name": "enter_plan_mode",
            "description": "Main agent only. Enter PLAN MODE: decompose the user's request into ordered steps, each broken into concrete tasks, show the plan in the right sidebar's 计划 tab, and ask the user to approve / reject / revise before doing any real work. Use this proactively for complex, multi-step, or risky tasks where the user would benefit from reviewing the approach first. Do NOT combine with other tools in the same turn; calling this pauses the round for the user's decision.",
            "parameters": {
                "type": "object",
                "properties": {
                    "focus": {"type": "string", "description": "Optional note on what the plan should emphasize or any constraints to respect."},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "DeepReflect",
            "description": "Main agent only. Reframe the next working context when the current approach is not satisfying the user's goal, repeated work is not converging, or user guidance shows the direction is wrong. Do not use this merely because one tool failed. The visible transcript is preserved; future LLM context uses a compressed reflection packet.",
            "parameters": {
                "type": "object",
                "properties": {
                    "goal_gap": {"type": "string", "description": "What user goal or requirement is not being satisfied by the current approach."},
                    "user_requirement": {"type": "string", "description": "Optional exact user requirement or correction that should guide the reframing."},
                    "scope": {"type": "string", "enum": ["current_round", "session_tail"], "description": "Which visible transcript span to compress. Defaults to current_round."},
                    "focus": {"type": "string", "description": "Optional next-direction focus for the reflection worker."},
                },
                "required": ["goal_gap"],
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
            "description": "Schedule a task. schedule_type must be cron, interval, or once. Use permission_mode=\"full_access\" only when the task MUST read/write files outside the workspace (the user will be asked to confirm at creation time).",
            "parameters": {
                "type": "object",
                "properties": {
                    "prompt": {"type": "string"},
                    "schedule_type": {"type": "string", "enum": ["cron", "interval", "once"]},
                    "schedule_value": {
                        "type": "string",
                        "description": "For 'cron': a crontab expression (e.g. '0 9 * * *'). For 'interval': the number of SECONDS between runs (e.g. '3600' = hourly). For 'once': an ISO-8601 datetime, or empty to run as soon as possible.",
                    },
                    "permission_mode": {
                        "type": "string",
                        "enum": ["workspace_only", "full_access"],
                        "description": "Permission scope. 'workspace_only' (default) restricts all file access to the workspace. 'full_access' allows reading/writing anywhere — the user must confirm before the task is created.",
                    },
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
            "name": "SearchKnowledge",
            "description": "Search the user's knowledge base (uploaded/imported/generated documents) for relevant passages via hybrid keyword+vector retrieval. Use this whenever the user references their documents, files, notes, or materials.",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Keyword or phrase to search for in documents."},
                    "k": {"type": "integer", "description": "Maximum number of matching chunks to return (default: 6)."},
                },
                "required": ["query"],
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
            "description": "Main agent only. Spawn a sub-agent. If the user explicitly asks for N subagents, named peer agents, or one subagent per item/person/city/option, call this tool once for EACH requested agent in the same assistant turn before expecting peer coordination. Subagents must not spawn more subagents; they should coordinate with peers via send_agent_message and finish via quit.",
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
    {
        "type": "function",
        "function": {
            "name": "browser_request_takeover",
            "description": "Hand the browser to the user to log in. Call this AS SOON AS you hit a login wall, CAPTCHA, or 2FA — before doing any deeper work on the page. A real browser window opens for the user to authenticate; you pause until they confirm, then resume in the same (now logged-in) session. Requires Playwright.",
            "parameters": {
                "type": "object",
                "properties": {
                    "reason": {"type": "string", "description": "Short message telling the user what to log into (e.g. 'Please log in to your Gmail account')."},
                },
                "required": ["reason"],
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
    # ---- Entity / 事务 tools ----
    {
        "type": "function",
        "function": {
            "name": "track_entity",
            "description": "Track an entity (task, project, decision, knowledge, relationship, event, resource, idea, problem, habit). Used for explicit recording or implicit extraction.",
            "parameters": {
                "type": "object",
                "properties": {
                    "type": {"type": "string", "enum": ["task","project","decision","knowledge","relationship","event","resource","idea","problem","habit"], "description": "Entity type"},
                    "title": {"type": "string", "description": "Brief title"},
                    "content": {"type": "string", "description": "Detailed description"},
                    "priority": {"type": "string", "enum": ["high","medium","low"], "description": "Priority level"},
                    "due_date": {"type": "string", "description": "Due date in ISO 8601 format"},
                    "people": {"type": "array", "items": {"type": "string"}, "description": "Related people"},
                    "tags": {"type": "array", "items": {"type": "string"}, "description": "Tags"},
                    "source": {"type": "string", "enum": ["explicit","extracted"], "description": "Source type"},
                    "confidence": {"type": "number", "description": "Confidence 0-1"},
                    "source_round_id": {"type": "string", "description": "Source round ID"},
                },
                "required": ["type", "title"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "update_entity",
            "description": "Update an entity field.",
            "parameters": {
                "type": "object",
                "properties": {
                    "id": {"type": "string", "description": "Entity ID"},
                    "field": {"type": "string", "enum": ["status","priority","due_date","content","tags","people","title","effort","metadata"], "description": "Field to update"},
                    "value": {"description": "New value"},
                },
                "required": ["id", "field", "value"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_entities",
            "description": "List entities with optional filtering by type and status.",
            "parameters": {
                "type": "object",
                "properties": {
                    "type": {"type": "string", "description": "Filter by type"},
                    "status": {"type": "string", "enum": ["active","paused","done","archived","abandoned"], "description": "Filter by status"},
                    "limit": {"type": "integer", "description": "Max results, default 50"},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "query_entities",
            "description": "Search entities by keyword and filter by due date.",
            "parameters": {
                "type": "object",
                "properties": {
                    "q": {"type": "string", "description": "Search keyword"},
                    "type": {"type": "string", "description": "Filter by type"},
                    "due_before": {"type": "string", "description": "Due before this date (ISO 8601)"},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "delete_entity",
            "description": "Delete or archive an entity. Default is soft delete (archived).",
            "parameters": {
                "type": "object",
                "properties": {
                    "id": {"type": "string", "description": "Entity ID"},
                    "permanent": {"type": "boolean", "description": "true=permanent delete, false=archive"},
                },
                "required": ["id"],
            },
        },
    },
]


# Legacy implementation module kept for shared helpers, schemas, and backward-compatible
# function imports. The live mutable registry is owned by cyrene.registry_tools.
TOOL_HANDLERS: dict[str, Any] = {}
