"""Discovery helpers for Claude Code tmux sessions and transcript files."""

from __future__ import annotations

import logging
import os
import re
import subprocess
import json
from datetime import datetime, timezone
from pathlib import Path
from shutil import which
from typing import Any

logger = logging.getLogger(__name__)


def _parse_timestamp(value: Any) -> datetime | None:
    """Parse an ISO timestamp string (also handles Z suffix)."""
    text = str(value or "").strip()
    if not text:
        return None
    try:
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        return datetime.fromisoformat(text)
    except ValueError:
        return None


_CLAUDE_HOME = Path.home() / ".claude"
_CLAUDE_PROJECTS_DIR = _CLAUDE_HOME / "projects"
_TMUX_SESSION_RE = re.compile(r"^[A-Za-z0-9._:-]+$")


def tmux_available() -> bool:
    """Return True when tmux can be invoked."""
    return which("tmux") is not None


def list_tmux_sessions() -> list[dict[str, Any]]:
    """Return available tmux sessions ordered by activity."""
    if not tmux_available():
        return []
    proc = _run_command(
        [
            "tmux",
            "list-sessions",
            "-F",
            "#{session_name}\t#{session_attached}\t#{session_activity}\t#{session_windows}",
        ]
    )
    if proc.returncode != 0:
        logger.debug("tmux list-sessions failed: %s", proc.stderr.strip())
        return []

    sessions: list[dict[str, Any]] = []
    for raw_line in proc.stdout.splitlines():
        parts = raw_line.split("\t")
        if len(parts) != 4:
            continue
        name, attached, activity, windows = parts
        if not name:
            continue
        try:
            activity_value = int(activity or "0")
        except ValueError:
            activity_value = 0
        try:
            window_count = int(windows or "0")
        except ValueError:
            window_count = 0
        sessions.append(
            {
                "name": name,
                "attached": attached == "1",
                "activity": activity_value,
                "window_count": window_count,
            }
        )
    sessions.sort(key=lambda item: (item["activity"], item["attached"], item["window_count"], item["name"]), reverse=True)
    return sessions


def resolve_cc_session_name(cwd: Path | None = None) -> str:
    """Resolve the fixed tmux session name for the current project."""
    cwd = (cwd or Path.cwd()).resolve()
    preferred = str(os.environ.get("CYRENE_CC_TMUX_SESSION", "")).strip()
    if preferred and _TMUX_SESSION_RE.fullmatch(preferred):
        return preferred
    return "claude-" + _session_name_from_path(cwd)


def find_cc_tmux_session(cwd: Path | None = None) -> str:
    """Return the configured Claude Code tmux session when it exists."""
    target_name = resolve_cc_session_name(cwd)
    for session in list_tmux_sessions():
        if session["name"] == target_name:
            return target_name
    return ""


def find_claude_project_dir(cwd: Path | None = None) -> Path | None:
    """Find the most likely Claude transcript directory for the current repo."""
    if not _CLAUDE_PROJECTS_DIR.exists():
        return None

    cwd = (cwd or Path.cwd()).resolve()
    explicit_candidates = _project_dir_candidates(cwd)
    known_paths = {candidate.resolve() for candidate in explicit_candidates if candidate.exists()}

    scored: list[tuple[int, float, Path]] = []
    for candidate in explicit_candidates:
        if not candidate.exists():
            continue
        latest = find_latest_jsonl(candidate)
        if latest is None:
            continue
        score = 100
        score += 20 if candidate.resolve() in known_paths else 0
        score += _score_project_dir(candidate, cwd)
        scored.append((score, latest.stat().st_mtime, candidate))

    repo_keywords = _session_keywords(cwd)
    for candidate in _CLAUDE_PROJECTS_DIR.iterdir():
        if not candidate.is_dir():
            continue
        latest = find_latest_jsonl(candidate)
        if latest is None:
            continue
        score = _score_project_dir(candidate, cwd)
        lowered_name = candidate.name.lower()
        score += sum(8 for keyword in repo_keywords if keyword and keyword in lowered_name)
        scored.append((score, latest.stat().st_mtime, candidate))

    if not scored:
        return None
    scored.sort(key=lambda item: (item[0], item[1]), reverse=True)
    return scored[0][2]


def find_latest_jsonl(project_dir: Path | None) -> Path | None:
    """Return the most recent Claude transcript JSONL for a project directory."""
    if project_dir is None or not project_dir.exists():
        return None

    paths: list[Path] = []
    paths.extend(project_dir.glob("*.jsonl"))
    for child in project_dir.iterdir():
        if child.is_dir():
            paths.extend(child.glob("*.jsonl"))
    existing = [path for path in paths if path.is_file()]
    if not existing:
        return None
    existing.sort(key=lambda path: path.stat().st_mtime, reverse=True)
    return existing[0]


def get_cc_status(cwd: Path | None = None) -> dict[str, Any]:
    """Return a frontend-friendly status summary for the CC terminal integration."""
    sync_cc_shell_status()
    cwd = (cwd or Path.cwd()).resolve()
    expected_session = resolve_cc_session_name(cwd)
    project_dir = find_claude_project_dir(cwd)
    latest_jsonl = find_latest_jsonl(project_dir)
    tmux_sessions = list_tmux_sessions()
    tmux_session = expected_session if any(session["name"] == expected_session for session in tmux_sessions) else ""

    available = bool(tmux_session)
    can_launch = False
    if available:
        _register_cc_shell(tmux_session, cwd)
        reason = ""
    elif not tmux_available():
        reason = "tmux is not installed or not on PATH."
    elif not tmux_sessions:
        reason = f"Claude Code session '{expected_session}' is not running."
        can_launch = True
    else:
        reason = f"Claude Code session '{expected_session}' is not running."
        can_launch = True

    latest_updated_at = ""
    if latest_jsonl is not None:
        latest_updated_at = datetime.fromtimestamp(latest_jsonl.stat().st_mtime, tz=timezone.utc).isoformat()

    return {
        "available": available,
        "can_launch": can_launch,
        "tmux_available": tmux_available(),
        "expected_session": expected_session,
        "tmux_session": tmux_session,
        "reason": reason,
        "project_dir": str(project_dir) if project_dir else "",
        "latest_jsonl": str(latest_jsonl) if latest_jsonl else "",
        "latest_updated_at": latest_updated_at,
        "session_count": len(tmux_sessions),
        "sessions": [session["name"] for session in tmux_sessions[:5]],
    }


def launch_cc_tmux(cwd: Path | None = None, session_name: str = "") -> dict[str, Any]:
    """Create a new tmux session running Claude Code in the project directory.

    Called by the CCLaunch agent tool.  The session name is chosen so that
    :func:`find_cc_tmux_session` picks it up on the next status poll.

    Returns ``{"ok": True, "session": "..."}`` on success.
    """
    if not tmux_available():
        return {"ok": False, "reason": "tmux is not installed or not on PATH."}

    cwd = (cwd or Path.cwd()).resolve()

    # 优先使用调用方指定的名字，否则根据项目目录自动生成
    if session_name and _TMUX_SESSION_RE.fullmatch(session_name):
        name = session_name
    else:
        name = resolve_cc_session_name(cwd)

    # 检查同名 session 是否已存在
    for session in list_tmux_sessions():
        if session["name"] == name:
            _register_cc_shell(name, cwd)
            return {"ok": True, "session": name, "detail": "Session already exists."}

    _run_command(["tmux", "set-option", "-g", "default-terminal", "tmux-256color"])

    # 找到 claude 可执行文件
    cc_bin = _find_claude_bin()

    # tmux new-session -d: 后台创建, 不 attach
    result = _run_command([
        "tmux", "new-session", "-d", "-s", name,
        "-e", "TERM=tmux-256color",
        "-e", "COLORTERM=truecolor",
        "-c", str(cwd),
        cc_bin,
    ])
    if result.returncode != 0:
        error = result.stderr.strip() or "tmux new-session failed"
        logger.warning("Failed to create tmux session '%s': %s", name, error)
        return {"ok": False, "reason": error}

    # 注册为 external shell，使 CC 出现在 WebUI 的活动 shell 列表中
    _register_cc_shell(name, cwd)

    logger.info("Created tmux session '%s' running %s in %s", name, cc_bin, cwd)
    return {"ok": True, "session": name, "detail": f"Launched {cc_bin} in tmux session '{name}'."}


def _session_name_from_path(cwd: Path) -> str:
    name = cwd.name or "project"
    clean = re.sub(r"[^A-Za-z0-9._-]+", "-", name).strip("-").lower()
    return clean or "project"


def _find_claude_bin() -> str:
    for candidate in ("claude", "cc"):
        if which(candidate):
            return candidate
    return "claude"


def _register_cc_shell(name: str, cwd: Path) -> None:
    """Register a Claude Code tmux session as an external shell in the WebUI."""
    try:
        from cyrene.shells import register_external_shell, _external_shells

        shell_id = f"cc-{name}"
        existing = _external_shells.get(shell_id)
        if existing is not None:
            existing["title"] = "Claude Code"
            existing["cwd"] = str(cwd)
            existing["kind"] = "cc"
            existing["tmuxSession"] = name
            return

        register_external_shell(
            shell_id=shell_id,
            title="Claude Code",
            cwd=str(cwd),
            extra={
                "kind": "cc",
                "tmuxSession": name,
            },
        )
        logger.debug("Registered CC external shell: %s", shell_id)
    except Exception:
        logger.exception("Failed to register CC shell for session %s", name)


def sync_cc_shell_status() -> None:
    """Sync external shell entries with actual tmux sessions.

    - Removes shells whose tmux session no longer exists.
    - Updates status for shells whose session is still running.
    """
    try:
        from cyrene.shells import _external_shells, unregister_external_shell, set_external_shell_status

        active_names = {session["name"] for session in list_tmux_sessions()}

        # 收集需要清理的 key（避免在遍历时修改）
        stale: list[str] = []
        for shell_id, shell in _external_shells.items():
            if shell.get("kind") != "cc":
                continue
            tmux_name = shell.get("tmuxSession", "")
            if not tmux_name:
                continue
            if tmux_name in active_names:
                set_external_shell_status(shell_id, "running")
            else:
                stale.append(shell_id)

        for shell_id in stale:
            unregister_external_shell(shell_id)
            logger.info("Unregistered stale CC shell: %s", shell_id)
    except Exception:
        logger.exception("Failed to sync CC shell status")


def _run_command(args: list[str], cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        args,
        cwd=str(cwd) if cwd else None,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )


def _project_dir_candidates(cwd: Path) -> list[Path]:
    candidates: list[Path] = []
    seen: set[str] = set()
    for path in _candidate_source_paths(cwd):
        candidate = _CLAUDE_PROJECTS_DIR / _sanitize_project_path(path)
        key = str(candidate)
        if key in seen:
            continue
        seen.add(key)
        candidates.append(candidate)
    return candidates


def _candidate_source_paths(cwd: Path) -> list[Path]:
    paths: list[Path] = [cwd]
    git_root = _git_path(["git", "rev-parse", "--show-toplevel"], cwd)
    if git_root is not None:
        paths.append(git_root)
    git_common_dir = _git_path(["git", "rev-parse", "--git-common-dir"], cwd)
    if git_common_dir is not None:
        if git_common_dir.name == ".git":
            paths.append(git_common_dir.parent)
        elif git_common_dir.parent.name == "worktrees":
            paths.append(git_common_dir.parent.parent)

    unique: list[Path] = []
    seen: set[str] = set()
    for path in paths:
        try:
            resolved = path.resolve()
        except Exception:
            continue
        key = str(resolved)
        if key in seen:
            continue
        seen.add(key)
        unique.append(resolved)
    return unique


def _git_path(args: list[str], cwd: Path) -> Path | None:
    proc = _run_command(args, cwd)
    if proc.returncode != 0:
        return None
    raw = proc.stdout.strip()
    if not raw:
        return None
    path = Path(raw)
    if not path.is_absolute():
        path = (cwd / path).resolve()
    return path


def _sanitize_project_path(path: Path) -> str:
    return str(path.resolve()).replace(os.sep, "-")


def _session_keywords(cwd: Path) -> list[str]:
    keywords: list[str] = []
    for path in _candidate_source_paths(cwd):
        for value in (path.name, path.stem):
            token = str(value or "").strip().lower()
            if token and token not in keywords:
                keywords.append(token)
    return keywords


def _score_tmux_session(name: str, session: dict[str, Any], keywords: list[str]) -> int:
    lowered = name.lower()
    score = 0
    if "claude" in lowered:
        score += 40
    if "cyrene" in lowered:
        score += 18
    if lowered.startswith("cc") or "-cc" in lowered or "_cc" in lowered:
        score += 10
    score += sum(12 for keyword in keywords if keyword and keyword in lowered)
    score += 5 if session.get("attached") else 0
    score += min(int(session.get("window_count") or 0), 6)
    return score


def _score_project_dir(project_dir: Path, cwd: Path) -> int:
    lowered_name = project_dir.name.lower()
    score = 0
    sanitized_cwd = _sanitize_project_path(cwd).lower()
    if lowered_name == sanitized_cwd:
        score += 80
    for keyword in _session_keywords(cwd):
        if keyword and keyword in lowered_name:
            score += 12
    return score


def get_cc_preview(cwd: Path | None = None, limit: int = 8, min_updated_at: str = "", since: str = "") -> dict[str, Any]:
    """Return a fixed-rule transcript preview for the side shell card.

    Args:
        since: ISO timestamp — only include entries after this time.
    """
    cwd = (cwd or Path.cwd()).resolve()
    project_dir = find_claude_project_dir(cwd)
    latest_jsonl = find_latest_jsonl(project_dir)
    if latest_jsonl is None:
        return {"lines": [], "updated_at": ""}

    updated_at = datetime.fromtimestamp(latest_jsonl.stat().st_mtime, tz=timezone.utc).isoformat()
    if min_updated_at:
        try:
            latest_dt = datetime.fromisoformat(updated_at)
            min_dt = datetime.fromisoformat(str(min_updated_at).strip())
            if latest_dt < min_dt:
                return {
                    "lines": [],
                    "updated_at": updated_at,
                    "latest_jsonl": str(latest_jsonl),
                }
        except ValueError:
            pass

    preview_lines = _build_preview_lines(latest_jsonl, limit=max(2, limit), since=since)
    return {
        "lines": preview_lines,
        "updated_at": updated_at,
        "latest_jsonl": str(latest_jsonl),
    }


def send_prompt_to_cc(prompt: str, cwd: Path | None = None) -> dict[str, Any]:
    """Send a prompt to the active Claude Code tmux session and press Enter."""
    text = str(prompt or "").rstrip()
    if not text:
        return {"ok": False, "reason": "Prompt is empty."}
    if not tmux_available():
        return {"ok": False, "reason": "tmux is not installed or not on PATH."}

    cwd = (cwd or Path.cwd()).resolve()
    session_name = find_cc_tmux_session(cwd)
    if not session_name:
        return {"ok": False, "reason": "Claude Code is not running for this project."}

    pane_target = session_name
    proc = _run_command(["tmux", "list-panes", "-t", session_name, "-F", "#{pane_id}"])
    if proc.returncode == 0 and proc.stdout.strip():
        pane_target = proc.stdout.splitlines()[0].strip() or session_name

    lines = text.splitlines() or [text]
    for line in lines:
        literal = line.rstrip("\r")
        if literal:
            result = _run_command(["tmux", "send-keys", "-t", pane_target, "-l", literal])
            if result.returncode != 0:
                return {"ok": False, "reason": result.stderr.strip() or "tmux send-keys failed"}
        result = _run_command(["tmux", "send-keys", "-t", pane_target, "Enter"])
        if result.returncode != 0:
            return {"ok": False, "reason": result.stderr.strip() or "tmux send-keys failed"}

    return {
        "ok": True,
        "session": session_name,
        "pane_target": pane_target,
        "line_count": len(lines),
    }


def _build_preview_lines(jsonl_path: Path, limit: int, since: str = "") -> list[dict[str, str]]:
    lines: list[dict[str, str]] = []
    since_dt: datetime | None = None
    if since:
        try:
            since_dt = datetime.fromisoformat(str(since).strip().replace("Z", "+00:00"))
        except ValueError:
            pass

    try:
        with open(jsonl_path, "r", encoding="utf-8", errors="replace") as handle:
            for raw_line in handle:
                raw_line = raw_line.strip()
                if not raw_line:
                    continue
                try:
                    entry = json.loads(raw_line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(entry, dict):
                    continue

                # Skip entries before the since timestamp
                if since_dt is not None:
                    entry_ts = _parse_timestamp(entry.get("timestamp"))
                    if entry_ts is not None and entry_ts < since_dt:
                        continue

                msg_type = str(entry.get("type") or "")
                if msg_type == "user":
                    text = _preview_user_text(entry)
                    if text:
                        lines.extend(_format_preview_block("shell-prompt", "> ", text))
                elif msg_type == "assistant":
                    text = _preview_assistant_text(entry)
                    if text:
                        lines.extend(_format_preview_block("shell-out", "Claude: ", text))
    except Exception:
        logger.exception("Failed building Claude Code preview from %s", jsonl_path)
        return []

    return lines[-limit:]


def _preview_user_text(entry: dict[str, Any]) -> str:
    message = entry.get("message", {})
    if not isinstance(message, dict) or message.get("role") != "user":
        return ""
    text = _extract_text_blocks(message.get("content"))
    if text == "[Request interrupted by user]":
        return ""
    return text


def _preview_assistant_text(entry: dict[str, Any]) -> str:
    message = entry.get("message", {})
    if not isinstance(message, dict) or message.get("role") != "assistant":
        return ""
    return _extract_assistant_text(message.get("content"))


def _extract_text_blocks(content: Any) -> str:
    if isinstance(content, str):
        return content.strip()
    if not isinstance(content, list):
        return ""

    texts: list[str] = []
    for block in content:
        if not isinstance(block, dict):
            continue
        if block.get("type") != "text":
            continue
        text = str(block.get("text") or "").strip()
        if text:
            texts.append(text)
    return "\n".join(texts).strip()


def _extract_assistant_text(content: Any) -> str:
    if isinstance(content, str):
        return content.strip()
    if not isinstance(content, list):
        return ""

    texts: list[str] = []
    for block in content:
        if not isinstance(block, dict):
            continue
        if block.get("type") != "text":
            continue
        text = str(block.get("text") or "").strip()
        if text:
            texts.append(text)
    return "\n\n".join(texts).strip()


def _format_preview_block(kind: str, prefix: str, text: str) -> list[dict[str, str]]:
    normalized = re.sub(r"\s+\n", "\n", str(text or "").strip())
    if not normalized:
        return []

    raw_lines = [line.strip() for line in normalized.splitlines() if line.strip()]
    if not raw_lines:
        return []

    clipped: list[str] = []
    for line in raw_lines[:3]:
        compact = re.sub(r"\s+", " ", line).strip()
        if compact:
            clipped.append(compact[:220] + ("…" if len(compact) > 220 else ""))
    if len(raw_lines) > 3:
        clipped.append("…")

    formatted: list[dict[str, str]] = []
    for index, line in enumerate(clipped):
        formatted.append({
            "kind": kind,
            "text": (prefix if index == 0 else "  ") + line,
        })
    return formatted
