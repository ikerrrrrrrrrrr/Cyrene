"""Helpers for mapping an agent session to a Workbench project scope."""

from __future__ import annotations

import re
from typing import Any

from cyrene.config import DATA_DIR
from cyrene.io_utils import read_json_safe

_WORKBENCH_STORE = DATA_DIR / "workbench_projects.json"
_WORKBENCH_CHATS_STORE = DATA_DIR / "workbench_chats.json"
_LEGACY_DATA_KEY = "default"


def _safe_workbench_data_key(raw: str | None) -> str:
    text = str(raw or "").strip()
    if not text:
        return _LEGACY_DATA_KEY
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "_", text).strip("._")
    return cleaned or _LEGACY_DATA_KEY


def _read_projects() -> list[dict[str, Any]]:
    payload = read_json_safe(_WORKBENCH_STORE)
    projects = payload.get("projects") if isinstance(payload, dict) else None
    return projects if isinstance(projects, list) else []


def resolve_project_data_key_for_session(session_id: str | None) -> str:
    """Resolve a Workbench chat/task session to the stored schedule project_id."""
    sid = str(session_id or "").strip()
    if not sid:
        return _LEGACY_DATA_KEY

    projects = _read_projects()
    project_id = ""

    chats_payload = read_json_safe(_WORKBENCH_CHATS_STORE)
    chats = chats_payload.get("chats") if isinstance(chats_payload, dict) else None
    if isinstance(chats, list):
        for chat in chats:
            if str(chat.get("id") or "") == sid:
                project_id = str(chat.get("projectId") or "").strip()
                break

    if not project_id:
        for project in projects:
            for session in project.get("sessions") or []:
                if str(session.get("id") or "") == sid:
                    project_id = str(project.get("id") or "").strip()
                    break
            if project_id:
                break

    if not project_id:
        return _LEGACY_DATA_KEY

    for project in projects:
        if str(project.get("id") or "") == project_id:
            return _safe_workbench_data_key(project.get("dataKey") or project_id)

    return _safe_workbench_data_key(project_id)


__all__ = ["resolve_project_data_key_for_session"]
