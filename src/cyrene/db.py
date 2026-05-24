"""Database operations for scheduled tasks and persisted daily analytics.

Note: Message history is stored in conversations/ folder (not in DB).
The DB is used for structured data that needs querying and stable aggregates.
"""

import re
import sqlite3
import uuid
from datetime import datetime, timezone

import aiosqlite

_CREATE_TABLES = """
CREATE TABLE IF NOT EXISTS scheduled_tasks (
    id TEXT PRIMARY KEY,
    chat_id INTEGER NOT NULL,
    prompt TEXT NOT NULL,
    schedule_type TEXT NOT NULL,
    schedule_value TEXT NOT NULL,
    next_run TEXT,
    last_run TEXT,
    last_result TEXT,
    status TEXT DEFAULT 'active',
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_scheduled_tasks_next_run ON scheduled_tasks(next_run);
CREATE INDEX IF NOT EXISTS idx_scheduled_tasks_status ON scheduled_tasks(status);

CREATE TABLE IF NOT EXISTS task_run_logs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id TEXT NOT NULL,
    run_at TEXT NOT NULL,
    duration_ms INTEGER NOT NULL,
    status TEXT NOT NULL,
    result TEXT,
    error TEXT,
    FOREIGN KEY (task_id) REFERENCES scheduled_tasks(id)
);
CREATE INDEX IF NOT EXISTS idx_task_run_logs_task_id ON task_run_logs(task_id);

CREATE TABLE IF NOT EXISTS daily_stats (
    day TEXT PRIMARY KEY,
    llm_requests INTEGER NOT NULL DEFAULT 0,
    tool_calls INTEGER NOT NULL DEFAULT 0,
    prompt_tokens INTEGER NOT NULL DEFAULT 0,
    completion_tokens INTEGER NOT NULL DEFAULT 0,
    total_tokens INTEGER NOT NULL DEFAULT 0,
    cache_hit_tokens INTEGER NOT NULL DEFAULT 0,
    cache_miss_tokens INTEGER NOT NULL DEFAULT 0,
    archive_entries INTEGER NOT NULL DEFAULT 0,
    memory_new INTEGER NOT NULL DEFAULT 0,
    memory_mentions INTEGER NOT NULL DEFAULT 0,
    emotion_sum REAL NOT NULL DEFAULT 0,
    emotion_count INTEGER NOT NULL DEFAULT 0,
    activity_00_04 INTEGER NOT NULL DEFAULT 0,
    activity_04_08 INTEGER NOT NULL DEFAULT 0,
    activity_08_12 INTEGER NOT NULL DEFAULT 0,
    activity_12_16 INTEGER NOT NULL DEFAULT 0,
    activity_16_20 INTEGER NOT NULL DEFAULT 0,
    activity_20_24 INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS daily_topic_terms (
    day TEXT NOT NULL,
    term TEXT NOT NULL,
    count INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (day, term)
);
CREATE INDEX IF NOT EXISTS idx_daily_topic_terms_day ON daily_topic_terms(day);

CREATE TABLE IF NOT EXISTS analytics_backfills (
    source TEXT PRIMARY KEY,
    completed_at TEXT NOT NULL
);
"""

_TOPIC_RE = re.compile(r"[\u4e00-\u9fff]{2,}|[a-z][a-z0-9_-]{2,}")
_TOPIC_STOPWORDS = {
    "the", "and", "for", "that", "this", "with", "from", "have", "about",
    "there", "would", "could", "should", "into", "your", "their", "them",
    "they", "what", "when", "where", "which", "while", "were", "been",
    "user", "assistant", "reply", "response", "just", "like", "than",
    "then", "also", "some", "more", "very", "much", "really",
    "一个", "这个", "那个", "我们", "你们", "他们", "以及", "因为", "所以", "就是",
}


async def init_db(db_path: str) -> None:
    async with aiosqlite.connect(db_path) as db:
        await db.executescript(_CREATE_TABLES)
        await db.commit()
    await _maybe_backfill_analytics(db_path)


def _local_tzinfo():
    return datetime.now().astimezone().tzinfo or timezone.utc


def _normalize_day(day: str | None = None, timestamp: str | None = None) -> str:
    if day:
        return str(day).strip()[:10]
    if timestamp:
        raw = str(timestamp).strip()
        try:
            dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.astimezone(_local_tzinfo()).strftime("%Y-%m-%d")
        except Exception:
            return raw[:10]
    return datetime.now(_local_tzinfo()).strftime("%Y-%m-%d")


def _activity_column(hour: int) -> str:
    if hour < 4:
        return "activity_00_04"
    if hour < 8:
        return "activity_04_08"
    if hour < 12:
        return "activity_08_12"
    if hour < 16:
        return "activity_12_16"
    if hour < 20:
        return "activity_16_20"
    return "activity_20_24"


def _extract_topic_terms(text: str, limit: int = 12) -> list[str]:
    source = str(text or "").lower()
    if not source:
        return []
    results: list[str] = []
    seen: set[str] = set()
    for token in _TOPIC_RE.findall(source):
        if token in _TOPIC_STOPWORDS:
            continue
        if token.isascii() and len(token) < 4:
            continue
        if token in seen:
            continue
        seen.add(token)
        results.append(token)
        if len(results) >= limit:
            break
    return results


def _ensure_day_row_sync(db: sqlite3.Connection, day: str) -> None:
    db.execute("INSERT OR IGNORE INTO daily_stats (day) VALUES (?)", (day,))


def record_memory_touch_sync(db_path: str, *, day: str | None = None, emotional_valence: float = 0, is_new: bool = False) -> None:
    target_day = _normalize_day(day=day)
    with sqlite3.connect(db_path) as db:
        _ensure_day_row_sync(db, target_day)
        db.execute(
            """
            UPDATE daily_stats
            SET memory_mentions = memory_mentions + 1,
                memory_new = memory_new + ?,
                emotion_sum = emotion_sum + ?,
                emotion_count = emotion_count + 1
            WHERE day = ?
            """,
            (1 if is_new else 0, float(emotional_valence or 0), target_day),
        )
        db.commit()


async def record_runtime_usage(db_path: str, timestamp: str, usage: dict | None = None) -> None:
    day = _normalize_day(timestamp=timestamp)
    usage = usage if isinstance(usage, dict) else {}
    async with aiosqlite.connect(db_path) as db:
        await db.execute("INSERT OR IGNORE INTO daily_stats (day) VALUES (?)", (day,))
        await db.execute(
            """
            UPDATE daily_stats
            SET llm_requests = llm_requests + 1,
                prompt_tokens = prompt_tokens + ?,
                completion_tokens = completion_tokens + ?,
                total_tokens = total_tokens + ?,
                cache_hit_tokens = cache_hit_tokens + ?,
                cache_miss_tokens = cache_miss_tokens + ?
            WHERE day = ?
            """,
            (
                int(usage.get("prompt_tokens") or 0),
                int(usage.get("completion_tokens") or 0),
                int(usage.get("total_tokens") or 0),
                int(usage.get("prompt_cache_hit_tokens") or 0),
                int(usage.get("prompt_cache_miss_tokens") or 0),
                day,
            ),
        )
        await db.commit()


async def record_tool_call(db_path: str, timestamp: str) -> None:
    day = _normalize_day(timestamp=timestamp)
    async with aiosqlite.connect(db_path) as db:
        await db.execute("INSERT OR IGNORE INTO daily_stats (day) VALUES (?)", (day,))
        await db.execute(
            "UPDATE daily_stats SET tool_calls = tool_calls + 1 WHERE day = ?",
            (day,),
        )
        await db.commit()


async def record_archive_exchange(
    db_path: str,
    *,
    timestamp: str,
    user_message: str,
    assistant_response: str,
) -> None:
    day = _normalize_day(timestamp=timestamp)
    try:
        dt = datetime.fromisoformat(str(timestamp).replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        hour = int(dt.astimezone(_local_tzinfo()).strftime("%H"))
    except Exception:
        hour = 0
    activity_col = _activity_column(hour)
    topic_terms = _extract_topic_terms(" ".join([user_message or "", assistant_response or ""]))
    async with aiosqlite.connect(db_path) as db:
        await db.execute("INSERT OR IGNORE INTO daily_stats (day) VALUES (?)", (day,))
        await db.execute(
            f"""
            UPDATE daily_stats
            SET archive_entries = archive_entries + 1,
                {activity_col} = {activity_col} + 1
            WHERE day = ?
            """,
            (day,),
        )
        for term in topic_terms:
            await db.execute(
                """
                INSERT INTO daily_topic_terms (day, term, count)
                VALUES (?, ?, 1)
                ON CONFLICT(day, term) DO UPDATE SET count = count + 1
                """,
                (day, term),
            )
        await db.commit()


async def get_daily_stats_range(db_path: str, day_from: str, day_to: str) -> list[dict]:
    async with aiosqlite.connect(db_path) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "SELECT * FROM daily_stats WHERE day >= ? AND day <= ? ORDER BY day ASC",
            (day_from, day_to),
        )
        rows = await cursor.fetchall()
        return [dict(r) for r in rows]


async def get_topic_counts_range(db_path: str, day_from: str, day_to: str, limit: int = 18) -> list[dict]:
    async with aiosqlite.connect(db_path) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            """
            SELECT term, SUM(count) AS count
            FROM daily_topic_terms
            WHERE day >= ? AND day <= ?
            GROUP BY term
            ORDER BY count DESC, term ASC
            LIMIT ?
            """,
            (day_from, day_to, int(limit)),
        )
        rows = await cursor.fetchall()
        return [dict(r) for r in rows]


async def count_stat_days(db_path: str) -> int:
    async with aiosqlite.connect(db_path) as db:
        cursor = await db.execute("SELECT COUNT(*) FROM daily_stats WHERE archive_entries > 0")
        row = await cursor.fetchone()
        return int(row[0] or 0) if row else 0


async def _backfill_runtime_logs(db_path: str) -> None:
    from cyrene.config import DATA_DIR

    if not DATA_DIR.exists():
        return
    async with aiosqlite.connect(db_path) as db:
        for log_path in sorted(DATA_DIR.glob("debug_*.jsonl")):
            try:
                for line in log_path.read_text(encoding="utf-8").splitlines():
                    if not line.strip():
                        continue
                    entry = json.loads(line)
                    timestamp = str(entry.get("timestamp") or "").strip()
                    if not timestamp:
                        continue
                    day = _normalize_day(timestamp=timestamp)
                    await db.execute("INSERT OR IGNORE INTO daily_stats (day) VALUES (?)", (day,))
                    if entry.get("type") == "llm_call":
                        usage = entry.get("usage")
                        if not isinstance(usage, dict):
                            response = entry.get("response")
                            usage = response.get("usage") if isinstance(response, dict) else {}
                        usage = usage if isinstance(usage, dict) else {}
                        await db.execute(
                            """
                            UPDATE daily_stats
                            SET llm_requests = llm_requests + 1,
                                prompt_tokens = prompt_tokens + ?,
                                completion_tokens = completion_tokens + ?,
                                total_tokens = total_tokens + ?,
                                cache_hit_tokens = cache_hit_tokens + ?,
                                cache_miss_tokens = cache_miss_tokens + ?
                            WHERE day = ?
                            """,
                            (
                                int(usage.get("prompt_tokens") or 0),
                                int(usage.get("completion_tokens") or 0),
                                int(usage.get("total_tokens") or 0),
                                int(usage.get("prompt_cache_hit_tokens") or 0),
                                int(usage.get("prompt_cache_miss_tokens") or 0),
                                day,
                            ),
                        )
                    elif entry.get("type") == "tool_call":
                        await db.execute(
                            "UPDATE daily_stats SET tool_calls = tool_calls + 1 WHERE day = ?",
                            (day,),
                        )
            except Exception:
                continue
        await db.execute(
            "INSERT OR REPLACE INTO analytics_backfills (source, completed_at) VALUES (?, ?)",
            ("runtime_logs_v1", datetime.now(timezone.utc).isoformat()),
        )
        await db.commit()


async def _backfill_conversation_archives(db_path: str) -> None:
    from cyrene.conversations import CONVERSATIONS_DIR, _parse_archive_sections

    if not CONVERSATIONS_DIR.exists():
        return
    async with aiosqlite.connect(db_path) as db:
        for filepath in sorted(CONVERSATIONS_DIR.glob("*.md")):
            date_str = filepath.stem
            try:
                sections = _parse_archive_sections(filepath.read_text(encoding="utf-8"), date_str)
            except Exception:
                continue
            for section in sections:
                day = str(section.get("date") or date_str).strip()[:10]
                await db.execute("INSERT OR IGNORE INTO daily_stats (day) VALUES (?)", (day,))
                stamp = str(section.get("timestamp") or "").strip()
                try:
                    hour = int(stamp[:2])
                except Exception:
                    hour = 0
                activity_col = _activity_column(hour)
                await db.execute(
                    f"""
                    UPDATE daily_stats
                    SET archive_entries = archive_entries + 1,
                        {activity_col} = {activity_col} + 1
                    WHERE day = ?
                    """,
                    (day,),
                )
                topic_terms = _extract_topic_terms(" ".join([
                    str(section.get("user_body") or ""),
                    str(section.get("assistant_body") or ""),
                ]))
                for term in topic_terms:
                    await db.execute(
                        """
                        INSERT INTO daily_topic_terms (day, term, count)
                        VALUES (?, ?, 1)
                        ON CONFLICT(day, term) DO UPDATE SET count = count + 1
                        """,
                        (day, term),
                    )
        await db.execute(
            "INSERT OR REPLACE INTO analytics_backfills (source, completed_at) VALUES (?, ?)",
            ("conversation_archives_v1", datetime.now(timezone.utc).isoformat()),
        )
        await db.commit()


async def _maybe_backfill_analytics(db_path: str) -> None:
    async with aiosqlite.connect(db_path) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute("SELECT source FROM analytics_backfills")
        rows = await cursor.fetchall()
        completed = {str(row["source"]) for row in rows}
    if "runtime_logs_v1" not in completed:
        await _backfill_runtime_logs(db_path)
    if "conversation_archives_v1" not in completed:
        await _backfill_conversation_archives(db_path)


# --- Task CRUD ---

async def create_task(db_path: str, chat_id: int, prompt: str, schedule_type: str, schedule_value: str, next_run: str) -> str:
    task_id = uuid.uuid4().hex[:8]
    async with aiosqlite.connect(db_path) as db:
        await db.execute(
            "INSERT INTO scheduled_tasks (id, chat_id, prompt, schedule_type, schedule_value, next_run, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (task_id, chat_id, prompt, schedule_type, schedule_value, next_run, datetime.now(timezone.utc).isoformat()),
        )
        await db.commit()
    return task_id


async def get_all_tasks(db_path: str) -> list[dict]:
    async with aiosqlite.connect(db_path) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute("SELECT * FROM scheduled_tasks")
        rows = await cursor.fetchall()
        return [dict(r) for r in rows]


async def get_due_tasks(db_path: str) -> list[dict]:
    now = datetime.now(timezone.utc).isoformat()
    async with aiosqlite.connect(db_path) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "SELECT * FROM scheduled_tasks WHERE status = 'active' AND next_run <= ?",
            (now,),
        )
        rows = await cursor.fetchall()
        return [dict(r) for r in rows]


async def update_task_status(db_path: str, task_id: str, status: str) -> bool:
    async with aiosqlite.connect(db_path) as db:
        cursor = await db.execute(
            "UPDATE scheduled_tasks SET status = ? WHERE id = ?",
            (status, task_id),
        )
        await db.commit()
        return cursor.rowcount > 0


async def delete_task(db_path: str, task_id: str) -> bool:
    async with aiosqlite.connect(db_path) as db:
        cursor = await db.execute("DELETE FROM scheduled_tasks WHERE id = ?", (task_id,))
        await db.commit()
        return cursor.rowcount > 0


async def update_task_after_run(db_path: str, task_id: str, last_result: str, next_run: str | None, status: str = "active") -> None:
    now = datetime.now(timezone.utc).isoformat()
    async with aiosqlite.connect(db_path) as db:
        await db.execute(
            "UPDATE scheduled_tasks SET last_run = ?, last_result = ?, next_run = ?, status = ? WHERE id = ?",
            (now, last_result, next_run, status, task_id),
        )
        await db.commit()


async def log_task_run(db_path: str, task_id: str, duration_ms: int, status: str, result: str | None = None, error: str | None = None) -> None:
    async with aiosqlite.connect(db_path) as db:
        await db.execute(
            "INSERT INTO task_run_logs (task_id, run_at, duration_ms, status, result, error) VALUES (?, ?, ?, ?, ?, ?)",
            (task_id, datetime.now(timezone.utc).isoformat(), duration_ms, status, result, error),
        )
        await db.commit()
