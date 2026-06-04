"""Shared scheduling spec — the single source of truth for what a schedule means.

All three task entry points compute ``next_run`` through here so a task fires at
the same wall-clock time no matter how it was created:

* the REST API (``POST/PUT /api/tasks`` in ``webui/routes.py``),
* the agent ``schedule_task`` tool (``cyrene/tools.py``), and
* the scheduler runner that re-arms recurring tasks after each run
  (``cyrene/scheduler.py``).

**Interval unit is seconds.** This matches the Web UI hint shown next to the
field (``"seconds (e.g. 3600)"``). The runner and the agent tool previously
treated the value as *milliseconds*, so a task the user created as "every 3600
seconds" re-fired every 3.6 seconds after its first run — see issue #50.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from croniter import croniter

SCHEDULE_TYPES = ("cron", "interval", "once")


def normalize_datetime(raw_value: str) -> str:
    """Normalize a user-facing datetime string to UTC ISO-8601.

    A naive datetime is interpreted in the machine's local timezone so Web UI
    scheduling like "2 minutes from now" behaves the way the user expects.
    """
    parsed = datetime.fromisoformat(raw_value)
    if parsed.tzinfo is None:
        local_tz = datetime.now().astimezone().tzinfo or timezone.utc
        parsed = parsed.replace(tzinfo=local_tz)
    return parsed.astimezone(timezone.utc).isoformat()


def parse_interval_seconds(raw_value: str) -> int:
    """Parse an interval value (in **seconds**) into a positive int.

    Raises ``ValueError`` for non-integer or non-positive input so callers can
    reject the task instead of silently scheduling something nonsensical.
    """
    try:
        seconds = int(str(raw_value).strip())
    except (TypeError, ValueError):
        raise ValueError(f"interval must be an integer number of seconds: {raw_value!r}")
    if seconds <= 0:
        raise ValueError(f"interval seconds must be positive: {seconds}")
    return seconds


def compute_next_run(
    schedule_type: str,
    schedule_value: str,
    *,
    now: datetime | None = None,
) -> str:
    """Return the next fire time as a UTC ISO-8601 string.

    Args:
        schedule_type: ``"cron"``, ``"interval"``, or ``"once"``.
        schedule_value: cron expression, integer seconds, or an ISO datetime.
            For ``"once"`` an empty value means "as soon as possible" (now).
        now: reference time (defaults to ``datetime.now(timezone.utc)``); inject
            for deterministic tests.

    Raises:
        ValueError: unknown ``schedule_type`` or an unparseable value. Callers
            (e.g. the REST API) should surface this as a 400, not fall back to
            scheduling immediately.
    """
    now = now or datetime.now(timezone.utc)
    stype = (schedule_type or "").strip()
    svalue = (schedule_value or "").strip()

    if stype == "cron":
        if not croniter.is_valid(svalue):
            raise ValueError(f"invalid cron expression: {svalue!r}")
        return croniter(svalue, now).get_next(datetime).isoformat()

    if stype == "interval":
        seconds = parse_interval_seconds(svalue)
        return (now + timedelta(seconds=seconds)).isoformat()

    if stype == "once":
        if not svalue:
            return now.isoformat()
        return normalize_datetime(svalue)

    raise ValueError(f"unknown schedule_type: {schedule_type!r}")
