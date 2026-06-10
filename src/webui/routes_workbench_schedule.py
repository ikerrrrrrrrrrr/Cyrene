"""Schedule / calendar API for the new Workbench UI.

This module is intentionally INDEPENDENT from the legacy scheduled-tasks
endpoints (``/api/tasks`` in ``routes.py``) that the old ``--agent`` UI uses.
It exposes a parallel set of endpoints under ``/api/workbench/schedule/*`` so the
two UIs never share request code.

The only thing shared is the pure data layer — that *is* the backend interface
we reuse:

* ``cyrene.db``            — scheduled-task CRUD + run logs (agent 定时任务)
* ``cyrene.schedule_spec`` — the single source of truth for ``next_run`` / cron
* ``cyrene.entities``      — entities that carry a ``due_date`` (任务截止)

The headline endpoint is ``GET /occurrences``: it expands every active/paused
scheduled task (cron / interval / once) **and** every entity deadline into
concrete, dated calendar events inside a ``[start, end]`` window. Cron expansion
runs server-side through croniter so the calendar shows exactly what the
scheduler will fire — there is no second cron implementation in the browser.

Timezone note: ``scheduled_tasks.next_run`` and cron expressions are evaluated
in **UTC** (see ``schedule_spec.compute_next_run``). Occurrences are therefore
returned as UTC ISO-8601; the frontend renders them in the viewer's local time.
The Workbench create form mirrors this by building cron fields from a chosen
local time's UTC components, so "what you see is when it fires".
"""

from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone

import aiosqlite
from croniter import croniter
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

# Max concrete occurrences expanded per recurring task per window. A calendar is
# not meant to render a sub-minute cron across a month; we cap and move on.
_MAX_OCC_PER_TASK = 200

# Default visual block length for an (instantaneous) task trigger, in minutes.
_DEFAULT_EVENT_MINUTES = 30

# Mirrors ``routes.py``'s ``_CHAT_ID`` — the web/local chat the task belongs to.
_DEFAULT_CHAT_ID = -1


# ── helpers ──────────────────────────────────────────────────────────────


def _parse_iso_utc(raw: str | None) -> datetime | None:
    """Parse an ISO-8601 string into a tz-aware UTC datetime (None on failure).

    A naive value is interpreted as UTC (that's how ``next_run`` is stored).
    """
    if not raw:
        return None
    try:
        dt = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except (ValueError, TypeError):
        # Tolerate date-only strings like "2026-06-10".
        try:
            dt = datetime.fromisoformat(str(raw)[:10])
        except (ValueError, TypeError):
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _category_for_task(schedule_type: str) -> str:
    return "task_once" if schedule_type == "once" else "task_recurring"


def _recurrence_label(schedule_type: str, schedule_value: str) -> str:
    """Human (Chinese) description of a task's cadence for the detail panel."""
    stype = (schedule_type or "").strip()
    sval = (schedule_value or "").strip()
    if stype == "once":
        return "单次"
    if stype == "interval":
        try:
            secs = int(sval)
        except (TypeError, ValueError):
            return "固定间隔"
        if secs % 86400 == 0:
            return f"每 {secs // 86400} 天"
        if secs % 3600 == 0:
            return f"每 {secs // 3600} 小时"
        if secs % 60 == 0:
            return f"每 {secs // 60} 分钟"
        return f"每 {secs} 秒"
    if stype == "cron":
        parts = sval.split()
        if len(parts) == 5:
            minute, hour, dom, month, dow = parts
            hhmm = ""
            if minute.isdigit() and hour.isdigit():
                hhmm = f" {int(hour):02d}:{int(minute):02d}(UTC)"
            if dom == "*" and month == "*" and dow == "*":
                return f"每天{hhmm}"
            if dom == "*" and month == "*" and dow != "*":
                names = ["日", "一", "二", "三", "四", "五", "六"]
                try:
                    return f"每周{names[int(dow) % 7]}{hhmm}"
                except (ValueError, IndexError):
                    pass
            if dom != "*" and month == "*" and dow == "*":
                return f"每月 {dom} 号{hhmm}"
        return f"Cron: {sval}"
    return stype or "—"


def _expand_task(task: dict, start: datetime, end: datetime) -> list[datetime]:
    """Return the concrete fire times of one task within ``[start, end]`` (UTC)."""
    stype = (task.get("schedule_type") or "").strip()
    sval = (task.get("schedule_value") or "").strip()
    next_run = _parse_iso_utc(task.get("next_run"))
    occ: list[datetime] = []

    if stype == "once":
        anchor = next_run or _parse_iso_utc(sval)
        if anchor and start <= anchor <= end:
            occ.append(anchor)
        return occ

    if stype == "interval":
        try:
            step = int(sval)
        except (TypeError, ValueError):
            return occ
        if step <= 0:
            return occ
        anchor = next_run or start
        # Slide the anchor to the first occurrence >= start via arithmetic so a
        # tiny interval over a wide window doesn't spin in a Python loop.
        if anchor > start:
            k = math.ceil((anchor - start).total_seconds() / step)
            first = anchor - timedelta(seconds=k * step)
            if first < start:
                first += timedelta(seconds=step)
        else:
            k = math.floor((start - anchor).total_seconds() / step)
            first = anchor + timedelta(seconds=k * step)
            if first < start:
                first += timedelta(seconds=step)
        t = first
        while t <= end and len(occ) < _MAX_OCC_PER_TASK:
            if t >= start:
                occ.append(t)
            t += timedelta(seconds=step)
        return occ

    if stype == "cron":
        if not croniter.is_valid(sval):
            return occ
        itr = croniter(sval, start - timedelta(seconds=1))
        while len(occ) < _MAX_OCC_PER_TASK:
            nxt = itr.get_next(datetime)
            if nxt.tzinfo is None:
                nxt = nxt.replace(tzinfo=timezone.utc)
            if nxt > end:
                break
            if nxt >= start:
                occ.append(nxt)
        return occ

    return occ


def _task_events(task: dict, start: datetime, end: datetime) -> list[dict]:
    """Build calendar event dicts for a task's occurrences in the window."""
    stype = (task.get("schedule_type") or "").strip()
    sval = task.get("schedule_value") or ""
    category = _category_for_task(stype)
    recurrence = _recurrence_label(stype, sval)
    status = task.get("status") or "active"
    events: list[dict] = []
    for fire in _expand_task(task, start, end):
        end_dt = fire + timedelta(minutes=_DEFAULT_EVENT_MINUTES)
        events.append({
            "id": f"{task['id']}@{fire.isoformat()}",
            "task_id": task["id"],
            "source": "task",
            "title": task.get("prompt") or "定时任务",
            "start": fire.isoformat(),
            "end": end_dt.isoformat(),
            "all_day": False,
            "category": category,
            "schedule_type": stype,
            "schedule_value": sval,
            "recurrence": recurrence,
            "status": status,
            "next_run": task.get("next_run"),
            "last_run": task.get("last_run"),
            "permission_mode": task.get("permission_mode") or "workspace_only",
        })
    return events


def _entity_events(entities: list[dict], start: datetime, end: datetime) -> list[dict]:
    """Build all-day deadline events from entities that carry a ``due_date``."""
    events: list[dict] = []
    for ent in entities:
        due = _parse_iso_utc(ent.get("due_date"))
        if not due or due < start or due > end:
            continue
        events.append({
            "id": f"entity:{ent['id']}",
            "entity_id": ent["id"],
            "source": "entity",
            "title": ent.get("title") or "任务截止",
            "start": ent.get("due_date"),
            "end": None,
            "all_day": True,
            "category": "entity_due",
            "entity_type": ent.get("type") or "task",
            "status": ent.get("status") or "active",
            "priority": ent.get("priority"),
        })
    return events


def register_workbench_schedule_routes(router: APIRouter, db_path: str) -> None:
    """Register the Workbench calendar/schedule routes."""
    from cyrene import db as cy_db
    from cyrene.schedule_spec import compute_next_run

    async def _all_tasks() -> list[dict]:
        return await cy_db.get_all_tasks(db_path)

    @router.get("/api/workbench/schedule/tasks")
    async def wb_list_tasks():
        """Raw scheduled tasks (agent 定时任务) for the management/list view."""
        try:
            return {"tasks": await _all_tasks()}
        except Exception as e:  # noqa: BLE001
            return JSONResponse({"error": f"List failed: {e}"}, status_code=400)

    @router.get("/api/workbench/schedule/occurrences")
    async def wb_list_occurrences(start: str = "", end: str = ""):
        """Expand tasks + entity deadlines into dated events within a window.

        ``start`` / ``end`` are ISO-8601 (any tz; naive treated as UTC). When
        omitted the window defaults to the next 60 days from now.
        """
        try:
            now = datetime.now(timezone.utc)
            start_dt = _parse_iso_utc(start) or now - timedelta(days=1)
            end_dt = _parse_iso_utc(end) or now + timedelta(days=60)
            if end_dt < start_dt:
                start_dt, end_dt = end_dt, start_dt

            tasks = await _all_tasks()
            events: list[dict] = []
            for task in tasks:
                events.extend(_task_events(task, start_dt, end_dt))

            try:
                from cyrene.entities import list_entities
                entities = await list_entities(db_path, has_due_date=True, limit=500)
                events.extend(_entity_events(entities, start_dt, end_dt))
            except Exception:  # noqa: BLE001
                # Entities are optional context; never fail the whole calendar
                # because the entity store hiccuped.
                pass

            events.sort(key=lambda ev: str(ev.get("start") or ""))
            return {
                "events": events,
                "start": start_dt.isoformat(),
                "end": end_dt.isoformat(),
            }
        except Exception as e:  # noqa: BLE001
            return JSONResponse({"error": f"Occurrences failed: {e}"}, status_code=400)

    @router.post("/api/workbench/schedule/tasks")
    async def wb_create_task(request: Request):
        """Create a scheduled task. Mirrors the REST policy: workspace_only only
        (full-access scheduled tasks must be created via the chat agent's
        ``schedule_task`` tool, which shows a confirmation dialog)."""
        try:
            body = await request.json()
        except Exception:  # noqa: BLE001
            return JSONResponse({"error": "invalid JSON body"}, status_code=400)

        prompt = str(body.get("prompt") or "").strip()
        stype = str(body.get("schedule_type") or "").strip()
        svalue = str(body.get("schedule_value") or "").strip()
        if not prompt:
            return JSONResponse({"error": "prompt is required"}, status_code=400)
        if not stype or not svalue:
            return JSONResponse({"error": "schedule_type and schedule_value are required"}, status_code=400)

        next_run = str(body.get("next_run") or "").strip()
        if not next_run:
            try:
                next_run = compute_next_run(stype, svalue)
            except ValueError as exc:
                return JSONResponse({"error": str(exc)}, status_code=400)

        try:
            task_id = await cy_db.create_task(
                db_path,
                chat_id=int(body.get("chat_id", _DEFAULT_CHAT_ID)),
                prompt=prompt,
                schedule_type=stype,
                schedule_value=svalue,
                next_run=next_run,
                permission_mode="workspace_only",
            )
            return {"ok": True, "id": task_id, "tasks": await _all_tasks()}
        except Exception as e:  # noqa: BLE001
            return JSONResponse({"error": f"Create failed: {e}"}, status_code=400)

    @router.put("/api/workbench/schedule/tasks/{task_id}")
    async def wb_update_task(task_id: str, request: Request):
        """Update a task's prompt / schedule / status. Recomputes ``next_run``
        when the schedule changes (an invalid schedule is a 400)."""
        try:
            body = await request.json()
        except Exception:  # noqa: BLE001
            return JSONResponse({"error": "invalid JSON body"}, status_code=400)

        stype = body.get("schedule_type")
        svalue = body.get("schedule_value")
        if stype and svalue and "next_run" not in body:
            try:
                body["next_run"] = compute_next_run(stype, svalue)
            except ValueError as exc:
                return JSONResponse({"error": str(exc)}, status_code=400)

        sets: list[str] = []
        vals: list = []
        # permission_mode is intentionally NOT updatable here (REST policy).
        for field in ("prompt", "schedule_type", "schedule_value", "next_run", "status"):
            if field in body:
                sets.append(f"{field} = ?")
                vals.append(body[field])
        if not sets:
            return JSONResponse({"error": "no updatable fields provided"}, status_code=400)

        try:
            async with aiosqlite.connect(db_path) as db:
                await db.execute(
                    f"UPDATE scheduled_tasks SET {', '.join(sets)} WHERE id = ?",
                    (*vals, task_id),
                )
                await db.commit()
            return {"ok": True, "tasks": await _all_tasks()}
        except Exception as e:  # noqa: BLE001
            return JSONResponse({"error": f"Update failed: {e}"}, status_code=400)

    @router.delete("/api/workbench/schedule/tasks/{task_id}")
    async def wb_delete_task(task_id: str):
        try:
            await cy_db.delete_task(db_path, task_id)
            return {"ok": True, "tasks": await _all_tasks()}
        except Exception as e:  # noqa: BLE001
            return JSONResponse({"error": f"Delete failed: {e}"}, status_code=400)

    @router.get("/api/workbench/schedule/tasks/{task_id}/runs")
    async def wb_task_runs(task_id: str, limit: int = 20):
        """Recent run history for a task (from ``task_run_logs``)."""
        try:
            limit = max(1, min(int(limit), 100))
            async with aiosqlite.connect(db_path) as db:
                db.row_factory = aiosqlite.Row
                cursor = await db.execute(
                    "SELECT id, task_id, run_at, duration_ms, status, result, error "
                    "FROM task_run_logs WHERE task_id = ? ORDER BY run_at DESC LIMIT ?",
                    (task_id, limit),
                )
                rows = await cursor.fetchall()
            return {"runs": [dict(r) for r in rows]}
        except Exception as e:  # noqa: BLE001
            return JSONResponse({"error": f"Runs failed: {e}"}, status_code=400)
