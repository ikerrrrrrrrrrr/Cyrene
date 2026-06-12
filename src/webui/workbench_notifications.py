"""Persistent notification center store for the Workbench UI."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any

from cyrene.config import DATA_DIR
from cyrene.io_utils import atomic_write_json, read_json_safe

_NOTIFICATIONS_STORE = DATA_DIR / "workbench_notifications.json"
_MAX_ITEMS = 400
_VALID_TABS = {"all", "mention", "comment", "system"}


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _short_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:10]}"


def _resolve_project_ref(project_ref: str | None) -> dict[str, str]:
    raw = str(project_ref or "").strip()
    out = {"projectId": "", "projectKey": "", "projectName": "", "workspacePath": ""}
    if not raw:
        return out
    try:
        from webui import routes as R

        payload = R._read_workbench_store()
        for project in payload.get("projects", []):
            pid = str(project.get("id") or "")
            pkey = str(R._workbench_project_data_key(project) or "")
            if raw in (pid, pkey):
                out["projectId"] = pid
                out["projectKey"] = pkey
                out["projectName"] = str(project.get("name") or "")
                out["workspacePath"] = str(project.get("workspacePath") or "")
                return out
    except Exception:
        pass
    out["projectId"] = raw
    out["projectKey"] = raw
    return out


def _read_store() -> dict[str, Any]:
    data = read_json_safe(_NOTIFICATIONS_STORE)
    if isinstance(data, dict) and isinstance(data.get("items"), list):
        return data
    return {"items": []}


def _write_store(payload: dict[str, Any]) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    atomic_write_json(_NOTIFICATIONS_STORE, payload)


def append_notification(
    *,
    title: str,
    body: str = "",
    tab: str = "system",
    project_ref: str | None = None,
    source: str = "",
    source_label: str = "",
    link_label: str = "",
    meta: dict[str, Any] | None = None,
    created_at: str | None = None,
) -> dict[str, Any]:
    title = str(title or "").strip()
    if not title:
        raise ValueError("title is required")
    tab = str(tab or "system").strip().lower()
    if tab not in _VALID_TABS or tab == "all":
        tab = "system"
    project = _resolve_project_ref(project_ref)
    item = {
        "id": _short_id("notif"),
        "title": title[:120],
        "body": str(body or "").strip()[:400],
        "tab": tab,
        "projectId": project["projectId"],
        "projectKey": project["projectKey"],
        "projectName": project["projectName"],
        "workspacePath": project["workspacePath"],
        "source": str(source or "").strip()[:80],
        "sourceLabel": str(source_label or "").strip()[:80],
        "linkLabel": str(link_label or "").strip()[:80],
        "createdAt": str(created_at or _utc_now_iso()),
        "read": False,
        "meta": meta if isinstance(meta, dict) else {},
    }
    payload = _read_store()
    items = payload.setdefault("items", [])
    items.insert(0, item)
    del items[_MAX_ITEMS:]
    _write_store(payload)
    return item


def list_notifications(*, tab: str = "all", limit: int = 80) -> dict[str, Any]:
    tab = str(tab or "all").strip().lower()
    if tab not in _VALID_TABS:
        tab = "all"
    limit = max(1, min(int(limit or 80), 200))
    payload = _read_store()
    items = payload.get("items", [])
    filtered = [item for item in items if tab == "all" or str(item.get("tab") or "") == tab]
    unread_total = 0
    unread_by_tab = {"mention": 0, "comment": 0, "system": 0}
    for item in items:
        if item.get("read"):
            continue
        unread_total += 1
        key = str(item.get("tab") or "")
        if key in unread_by_tab:
            unread_by_tab[key] += 1
    return {
        "items": filtered[:limit],
        "unreadCount": unread_total,
        "counts": {
            "all": len(items),
            "mention": sum(1 for item in items if str(item.get("tab") or "") == "mention"),
            "comment": sum(1 for item in items if str(item.get("tab") or "") == "comment"),
            "system": sum(1 for item in items if str(item.get("tab") or "") == "system"),
        },
        "unreadByTab": {"all": unread_total, **unread_by_tab},
    }


def mark_notifications_read(ids: list[str] | None = None, *, mark_all: bool = False) -> dict[str, Any]:
    payload = _read_store()
    items = payload.get("items", [])
    wanted = {str(item).strip() for item in (ids or []) if str(item).strip()}
    changed = 0
    for item in items:
        if item.get("read"):
            continue
        if mark_all or str(item.get("id") or "") in wanted:
            item["read"] = True
            changed += 1
    if changed:
        _write_store(payload)
    return {"ok": True, "changed": changed}
