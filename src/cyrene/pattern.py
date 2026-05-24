"""
Pattern — automatic learning of repetitive actions and script generation.

Watches tool calls in real time, detects repeated sequences, abstracts them
into parameterized scripts, and lets the user approve and run them.

Single-file module.  Hooks into the existing tool dispatch and scheduler
heartbeat with minimal glue (2-5 lines per existing file).
"""

import hashlib
import json
import logging
import os
import re
import time
import uuid
import asyncio
from collections import Counter, defaultdict, deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants (overridden by init())
# ---------------------------------------------------------------------------
_DATA_DIR: Path | None = None
_PATTERNS_DIR: Path | None = None
_ACTION_LOG_PATH: Path | None = None
_PATTERN_HISTORY_PATH: Path | None = None
_DETECTION_INTERVAL: int = 600  # seconds between detection runs
_SCAN_LOCK = asyncio.Lock()

# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------


@dataclass
class ActionEntry:
    timestamp: str
    round_id: str
    caller: str
    tool: str
    args: dict[str, Any]
    arg_fingerprint: str
    duration_ms: float


@dataclass
class PatternCandidate:
    sequence: list[dict]
    fingerprint: str
    occurrences: int
    round_ids: list[str]
    first_seen: str
    last_seen: str
    confidence: float
    params: dict[str, dict] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Fingerprint helpers
# ---------------------------------------------------------------------------

_PATH_LIKE = re.compile(r'(?:/[^\s"]*)+|(?:~[^\s"]*)+|(?:\$[A-Z_][A-Z0-9_]*)')
_FILE_EXT = re.compile(r'\.[a-zA-Z]{1,6}$')
_QUOTED_STR = re.compile(r"""[^"]*"|'[^']*'""")


def _is_path_or_file(s: str) -> bool:
    """Check if a string looks like a file path (for abstraction)."""
    return bool(_PATH_LIKE.search(s) or _FILE_EXT.search(s))


def _make_fingerprint(tool: str, args: dict[str, Any]) -> str:
    """Produce a hashable, parameter-abstracted string for a tool call."""
    if tool == "Bash":
        cmd = str(args.get("command", ""))
        cmd = _QUOTED_STR.sub("$ARG", cmd)
        parts = cmd.split()
        abstracted = []
        for i, p in enumerate(parts):
            if i == 0:
                abstracted.append(p)
            elif _is_path_or_file(p):
                abstracted.append("$ARG")
            else:
                abstracted.append(p)
        return f"Bash:{' '.join(abstracted)}"
    if tool in ("Read", "Write", "Edit"):
        p = str(args.get("file_path", args.get("path", "")))
        if _is_path_or_file(p):
            return f"{tool}:$PATH"
        return f"{tool}:{p}"
    if tool == "WebSearch":
        q = str(args.get("query", "$QUERY"))
        return f"WebSearch:{q[:60]}"
    if tool == "WebFetch":
        u = str(args.get("url", "$URL"))
        return f"WebFetch:{u[:60]}"
    if tool == "Grep":
        p = str(args.get("pattern", "$PATTERN"))
        return f"Grep:{p[:40]}"
    if tool == "Glob":
        p = str(args.get("pattern", "$PATTERN"))
        return f"Glob:{p[:40]}"
    return tool


def _compact_value(value: Any, *, depth: int = 0) -> Any:
    """Keep a small, JSON-safe view of tool args for pattern replay."""
    if depth >= 3:
        return "..."
    if isinstance(value, str):
        return value[:280]
    if isinstance(value, (int, float, bool)) or value is None:
        return value
    if isinstance(value, list):
        return [_compact_value(item, depth=depth + 1) for item in value[:10]]
    if isinstance(value, dict):
        compact: dict[str, Any] = {}
        for idx, (key, item) in enumerate(value.items()):
            if idx >= 20:
                break
            compact[str(key)] = _compact_value(item, depth=depth + 1)
        return compact
    return str(value)[:160]


# ---------------------------------------------------------------------------
# Action tracking
# ---------------------------------------------------------------------------

_buffer: deque[ActionEntry] = deque(maxlen=2000)
_track_counter: int = 0  # for lightweight real-time checks


def record_action(
    tool: str,
    args: dict[str, Any],
    caller: str,
    round_id: str,
    duration_ms: float,
) -> None:
    global _track_counter
    entry = ActionEntry(
        timestamp=datetime.now(timezone.utc).isoformat(),
        round_id=round_id or "",
        caller=caller or "unknown",
        tool=tool,
        args=_compact_value(dict(args) if args else {}),
        arg_fingerprint=_make_fingerprint(tool, args),
        duration_ms=duration_ms,
    )
    _buffer.append(entry)
    _track_counter += 1

    # Append to JSONL
    if _ACTION_LOG_PATH:
        try:
            line = json.dumps({
                "ts": entry.timestamp,
                "rid": entry.round_id,
                "c": entry.caller,
                "t": entry.tool,
                "args": entry.args,
                "fp": entry.arg_fingerprint,
                "d": round(entry.duration_ms, 1),
            }, ensure_ascii=False)
            with open(_ACTION_LOG_PATH, "a", encoding="utf-8") as f:
                f.write(line + "\n")
        except Exception:
            pass


def get_sequences_since(since: datetime | None = None) -> dict[str, list[ActionEntry]]:
    """Return tool-call sequences grouped by round_id, optionally filtered by time."""
    groups: dict[str, list[ActionEntry]] = defaultdict(list)
    for e in _buffer:
        if since and e.timestamp < since.isoformat():
            continue
        if e.round_id:
            groups[e.round_id].append(e)
    return dict(groups)


# ---------------------------------------------------------------------------
# Pattern detection
# ---------------------------------------------------------------------------

_MIN_PATTERN_LEN = 2
_MAX_PATTERN_LEN = 8
_MIN_OCCURRENCES = 2


def _hash_seq(fps: list[str]) -> str:
    return hashlib.sha256("|".join(fps).encode()).hexdigest()[:16]


def _abstract_args(occurrences: list[list[dict]]) -> tuple[list[dict], dict[str, dict]]:
    """Compare occurrences of the same pattern and identify varying positions.

    Returns (abstracted_steps, parameters_dict).
    """
    if not occurrences:
        return [], {}

    template = occurrences[0]
    n = len(template)
    params: dict[str, dict] = {}
    param_idx = 0

    abstracted = []
    for i in range(n):
        step = dict(template[i])
        args_i = step.get("args", {})
        placeholders: list[str] = []

        for key in list(args_i.keys()):
            vals = set()
            for occ in occurrences:
                if i < len(occ):
                    v = occ[i].get("args", {}).get(key)
                    if isinstance(v, (str, int, float)):
                        vals.add(str(v))
            if len(vals) > 1:
                pname = f"${param_idx + 1}"
                param_idx += 1
                placeholders.append(pname)
                params[pname] = {
                    "key": key,
                    "desc": "",
                    "default": args_i[key] if args_i.get(key) else "",
                    "examples": sorted(vals)[:5],
                }

        step["placeholders"] = placeholders
        abstracted.append(step)

    return abstracted, params


def _score_candidate(
    occurrences: int,
    first_seen: datetime,
    last_seen: datetime,
    seq_len: int,
) -> float:
    now = datetime.now(timezone.utc)
    hours_since_last = max(0, (now - last_seen).total_seconds()) / 3600
    span_hours = max(1, (last_seen - first_seen).total_seconds()) / 3600

    rep_score = min(1.0, occurrences / 5)
    recency = max(0, 1.0 - hours_since_last / 72)
    density = min(1.0, occurrences / max(1, span_hours / 24))
    length_bonus = min(1.0, (seq_len - 1) / 4)

    return round(0.3 * rep_score + 0.3 * recency + 0.2 * density + 0.2 * length_bonus, 2)


def detect_patterns(
    min_occurrences: int = _MIN_OCCURRENCES,
) -> list[PatternCandidate]:
    """Find repeated tool-call subsequences across rounds."""
    groups = get_sequences_since()
    if len(groups) < min_occurrences:
        return []

    # Build per-round fingerprint lists
    round_fps: dict[str, list[str]] = {}
    for rid, entries in groups.items():
        fps = [e.arg_fingerprint for e in entries]
        if len(fps) >= _MIN_PATTERN_LEN:
            round_fps[rid] = fps

    if len(round_fps) < min_occurrences:
        return []

    # Sliding-window hash counting
    seq_counter: Counter = Counter()
    seq_meta: dict[str, dict] = {}  # hash -> {round_ids, first_seen, last_seen, raw_occurrences}

    for rid, fps in round_fps.items():
        seen_in_round: set[str] = set()
        for length in range(_MIN_PATTERN_LEN, min(_MAX_PATTERN_LEN + 1, len(fps) + 1)):
            for start in range(len(fps) - length + 1):
                sub = fps[start : start + length]
                h = _hash_seq(sub)
                seq_counter[h] += 1
                if h not in seen_in_round:
                    seen_in_round.add(h)
                    if h not in seq_meta:
                        seq_meta[h] = {
                            "round_ids": [],
                            "first_seen": "",
                            "last_seen": "",
                            "raw_occurrences": [],
                        }
                    seq_meta[h]["round_ids"].append(rid)
                    # Store raw occurrence for abstraction
                    entries = groups.get(rid, [])
                    for s2 in range(len(entries) - length + 1):
                        matched_entries = entries[s2 : s2 + length]
                        efps = [e.arg_fingerprint for e in matched_entries]
                        if _hash_seq(efps) == h:
                            first_seen = matched_entries[0].timestamp
                            last_seen = matched_entries[-1].timestamp
                            existing_first = seq_meta[h]["first_seen"]
                            existing_last = seq_meta[h]["last_seen"]
                            if not existing_first or first_seen < existing_first:
                                seq_meta[h]["first_seen"] = first_seen
                            if not existing_last or last_seen > existing_last:
                                seq_meta[h]["last_seen"] = last_seen
                            seq_meta[h]["raw_occurrences"].append([
                                {"tool": e.tool, "args": e.args}
                                for e in matched_entries
                            ])
                            break

    # Filter and score
    candidates: list[PatternCandidate] = []
    for h, count in seq_counter.most_common(100):
        if count < min_occurrences:
            continue
        meta = seq_meta[h]
        raw = meta["raw_occurrences"]
        if len(raw) < min_occurrences:
            continue

        if not meta["first_seen"]:
            continue
        first_dt = datetime.fromisoformat(meta["first_seen"])
        last_dt = datetime.fromisoformat(meta.get("last_seen", meta["first_seen"]))
        seq_len = len(raw[0]) if raw else 0
        confidence = _score_candidate(len(raw), first_dt, last_dt, seq_len)

        abstracted, params = _abstract_args(raw)

        candidates.append(PatternCandidate(
            sequence=abstracted,
            params=params,
            fingerprint=h,
            occurrences=len(raw),
            round_ids=meta["round_ids"],
            first_seen=meta["first_seen"],
            last_seen=last_dt.isoformat(),
            confidence=confidence,
        ))

    # Deduplicate: if candidate A is a subsequence of candidate B, keep B
    def _tools_only(candidate: PatternCandidate) -> list[str]:
        return [str(step.get("tool") or "") for step in candidate.sequence]

    def _is_contained(shorter: list[str], longer: list[str]) -> bool:
        if len(shorter) >= len(longer):
            return False
        for start in range(len(longer) - len(shorter) + 1):
            if longer[start : start + len(shorter)] == shorter:
                return True
        return False

    candidates.sort(key=lambda c: (c.confidence, len(c.sequence), c.occurrences), reverse=True)
    filtered: list[PatternCandidate] = []
    for c in candidates:
        c_tools = _tools_only(c)
        if any(
            c.fingerprint != other.fingerprint
            and _is_contained(c_tools, _tools_only(other))
            and c.confidence <= other.confidence
            for other in candidates
        ):
            continue
        filtered.append(c)

    return filtered[:20]


# ---------------------------------------------------------------------------
# Script management
# ---------------------------------------------------------------------------


def _pid() -> str:
    return "p_" + uuid.uuid4().hex[:8]


def _scripts_dir() -> Path:
    if _PATTERNS_DIR is None:
        raise RuntimeError("pattern module not initialized")
    return _PATTERNS_DIR


def _script_path(script_id: str) -> Path:
    return _scripts_dir() / f"{script_id}.json"


def list_scripts(status: str = "all") -> list[dict]:
    """List scripts, optionally filtered by status."""
    scripts: list[dict] = []
    if not _PATTERNS_DIR or not _PATTERNS_DIR.exists():
        return scripts
    for f in sorted(_PATTERNS_DIR.glob("*.json")):
        try:
            s = json.loads(f.read_text(encoding="utf-8"))
            if status == "all" or s.get("status") == status:
                scripts.append(s)
        except Exception:
            pass
    scripts.sort(key=lambda item: str(item.get("created_at") or ""), reverse=True)
    return scripts


def get_script(script_id: str) -> dict | None:
    p = _script_path(script_id)
    if not p.exists():
        return None
    return json.loads(p.read_text(encoding="utf-8"))


def _save_script(script: dict) -> None:
    p = _script_path(script["id"])
    p.write_text(json.dumps(script, ensure_ascii=False, indent=2), encoding="utf-8")


def approve_script(script_id: str) -> bool:
    s = get_script(script_id)
    if s is None or s.get("status") != "pending":
        return False
    s["status"] = "approved"
    s["approved_at"] = datetime.now(timezone.utc).isoformat()
    _save_script(s)
    return True


def reject_script(script_id: str) -> bool:
    s = get_script(script_id)
    if s is None or s.get("status") != "pending":
        return False
    s["status"] = "rejected"
    _save_script(s)
    return True


def delete_script(script_id: str) -> bool:
    p = _script_path(script_id)
    if not p.exists():
        return False
    p.unlink()
    return True


def record_script_use(script_id: str) -> None:
    s = get_script(script_id)
    if s is None:
        return
    s["use_count"] = s.get("use_count", 0) + 1
    s["last_used"] = datetime.now(timezone.utc).isoformat()
    if s.get("status") == "approved":
        s["status"] = "active"
    _save_script(s)


def _find_existing_script(fingerprint: str) -> dict | None:
    for s in list_scripts():
        if s.get("source_fingerprint") == fingerprint:
            return s
    return None


async def create_pending_script(candidate: PatternCandidate) -> dict | None:
    """Generate a named script from a pattern candidate using LLM."""
    if _find_existing_script(candidate.fingerprint):
        return None

    abstracted = candidate.sequence
    params = dict(candidate.params or {})

    # Use LLM to name and describe
    step_desc = "\n".join(
        f"  {i+1}. {s['tool']}({json.dumps(s.get('args', {}), ensure_ascii=False)})"
        for i, s in enumerate(abstracted)
    )

    prompt = f"""Name and describe this automation script.  Reply with exactly one JSON object.

Repeated tool sequence ({candidate.occurrences} occurrences):
{step_desc}

Parameters: {json.dumps(params, ensure_ascii=False) if params else "none"}

Return:
{{"name": "kebab-case-name", "description": "one-line Chinese description"}}"""

    try:
        from cyrene.agent import _call_llm

        resp = await _call_llm([
            {"role": "user", "content": prompt},
        ], tools=None, max_tokens=200)

        text = (resp.get("choices", [{}])[0]
                .get("message", {})
                .get("content", ""))
        # Extract JSON
        m = re.search(r'\{[^}]+\}', text)
        if m:
            info = json.loads(m.group())
        else:
            info = {"name": "auto-script", "description": "自动检测的重复操作"}
    except Exception:
        info = {"name": "auto-script", "description": "自动检测的重复操作"}

    script = {
        "id": _pid(),
        "name": info.get("name", "auto-script"),
        "description": info.get("description", "自动检测的重复操作"),
        "type": "tool_macro",
        "source_fingerprint": candidate.fingerprint,
        "steps": abstracted,
        "params": params,
        "status": "pending",
        "use_count": 0,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "approved_at": None,
        "last_used": None,
        "first_seen": candidate.first_seen,
        "last_seen": candidate.last_seen,
        "confidence": candidate.confidence,
        "occurrences": candidate.occurrences,
        "round_ids": candidate.round_ids,
    }
    _save_script(script)
    return script


def _candidate_has_new_evidence(candidate: PatternCandidate, record: dict[str, Any]) -> bool:
    previous_occurrences = int(record.get("occurrences") or 0)
    previous_last_seen = str(record.get("last_seen") or "")
    return candidate.occurrences > previous_occurrences or candidate.last_seen > previous_last_seen


def _observe_candidate(history: dict[str, Any], candidate: PatternCandidate) -> None:
    history[candidate.fingerprint] = {
        "first_seen": candidate.first_seen,
        "last_seen": candidate.last_seen,
        "occurrences": candidate.occurrences,
        "tools": [step.get("tool", "") for step in candidate.sequence],
        "status": "observed",
    }


async def _scan_for_session_start_unlocked(force_promote: bool = False) -> dict[str, int]:
    """Scan action history when a new session starts.

    First detection only records a fingerprint. A later scan must observe new
    evidence for the same fingerprint before a pending script is generated.
    """
    try:
        candidates = detect_patterns()
        history = _load_pattern_history()
        observed = 0
        promoted = 0

        for candidate in candidates:
            if candidate.confidence < 0.55:
                continue

            existing_script = _find_existing_script(candidate.fingerprint)
            record = history.get(candidate.fingerprint)
            if record is None:
                _observe_candidate(history, candidate)
                observed += 1
                record = history.get(candidate.fingerprint)
                if not force_promote:
                    continue

            if existing_script is not None:
                if _candidate_has_new_evidence(candidate, record):
                    record["last_seen"] = candidate.last_seen
                    record["occurrences"] = candidate.occurrences
                continue

            if not force_promote and not _candidate_has_new_evidence(candidate, record):
                continue

            script = await create_pending_script(candidate)
            if script:
                if record is None:
                    _observe_candidate(history, candidate)
                    record = history.get(candidate.fingerprint)
                record["status"] = "promoted"
                record["promoted_at"] = datetime.now(timezone.utc).isoformat()
                record["script_id"] = script["id"]
                record["last_seen"] = candidate.last_seen
                record["occurrences"] = candidate.occurrences
                promoted += 1

        _save_pattern_history(history)
        return {
            "observed": observed,
            "promoted": promoted,
            "candidates": len(candidates),
            "forced": int(force_promote),
        }
    except Exception:
        logger.debug("Pattern scan on session start failed", exc_info=True)
        return {"observed": 0, "promoted": 0, "candidates": 0, "forced": int(force_promote)}


async def scan_for_session_start() -> dict[str, int]:
    async with _SCAN_LOCK:
        return await _scan_for_session_start_unlocked()


async def scan_for_manual_learn() -> dict[str, int]:
    """Explicit learn-now path: promote confident historical patterns immediately."""
    async with _SCAN_LOCK:
        return await _scan_for_session_start_unlocked(force_promote=True)


async def run_script(script_id: str, param_overrides: dict[str, str] | None = None) -> str:
    """Execute a script step by step.  Must be called from within an agent loop."""
    s = get_script(script_id)
    if s is None:
        return f"Script '{script_id}' not found."
    if s.get("status") not in ("approved", "active"):
        return f"Script '{script_id}' is {s.get('status')}; must be approved first."

    from cyrene.tools import _execute_tool

    overrides = param_overrides or {}
    params_def = s.get("params", {})
    results: list[str] = []

    for i, step in enumerate(s.get("steps", [])):
        resolved = dict(step.get("args", {}))
        for ph in step.get("placeholders", []):
            val = overrides.get(ph)
            if val is None and ph in params_def:
                val = params_def[ph].get("default", "")
            if val is not None:
                for k, v in resolved.items():
                    if isinstance(v, str) and ph in v:
                        resolved[k] = v.replace(ph, str(val))

        try:
            r = await _execute_tool(step["tool"], resolved, None, 0, "", None)
            results.append(f"[{i+1}/{len(s['steps'])}] {step['tool']}: {str(r)[:500]}")
        except Exception as exc:
            results.append(f"[{i+1}/{len(s['steps'])}] {step['tool']}: ERROR - {exc}")

    record_script_use(script_id)
    return "\n".join(results)


_PENDING_SUMMARY_TEMPLATE = """\
{pending_count} pending automation script(s) detected from repeated actions:
{pending_list}
If any match recent user activity, suggest they review and approve them."""


def get_pending_summary() -> str:
    pending = list_scripts("pending")
    if not pending:
        return ""
    lines = [f"  - {s['name']}: {s.get('description', '')} ({s.get('occurrences', '?')}x)"
             for s in pending[:10]]
    return _PENDING_SUMMARY_TEMPLATE.format(
        pending_count=len(pending),
        pending_list="\n".join(lines),
    )


# ---------------------------------------------------------------------------
# Tool handlers
# ---------------------------------------------------------------------------

async def _tool_list_scripts(
    args: dict[str, Any],
    bot: Any, chat_id: int, db_path: str, notify_state: dict | None,
) -> str:
    status = str(args.get("status", "all"))
    scripts = list_scripts(None if status == "all" else status)
    if not scripts:
        return "No scripts found." if status == "all" else f"No {status} scripts."
    lines = []
    for s in scripts:
        lines.append(
            f"- [{s['status']}] {s['id']}  {s['name']}: {s.get('description', '')}  "
            f"(used {s.get('use_count', 0)}x)"
        )
    return "\n".join(lines)


async def _tool_run_script(
    args: dict[str, Any],
    bot: Any, chat_id: int, db_path: str, notify_state: dict | None,
) -> str:
    script_id = str(args.get("script_id", ""))
    if not script_id:
        return "Error: 'script_id' is required."
    params = args.get("params", None)
    if params is not None and not isinstance(params, dict):
        params = None
    return await run_script(script_id, params)


async def _tool_approve_script(
    args: dict[str, Any],
    bot: Any, chat_id: int, db_path: str, notify_state: dict | None,
) -> str:
    script_id = str(args.get("script_id", ""))
    if not script_id:
        return "Error: 'script_id' is required."
    ok = approve_script(script_id)
    return f"Script '{script_id}' approved." if ok else f"Script '{script_id}' not found or not in pending status."


async def _tool_reject_script(
    args: dict[str, Any],
    bot: Any, chat_id: int, db_path: str, notify_state: dict | None,
) -> str:
    script_id = str(args.get("script_id", ""))
    if not script_id:
        return "Error: 'script_id' is required."
    ok = reject_script(script_id)
    return f"Script '{script_id}' rejected." if ok else f"Script '{script_id}' not found or not in pending status."


async def _tool_learn_patterns(
    args: dict[str, Any],
    bot: Any, chat_id: int, db_path: str, notify_state: dict | None,
) -> str:
    stats = await scan_for_manual_learn()
    return (
        "Pattern learning completed. "
        f"Observed {int(stats.get('observed') or 0)} new pattern(s), "
        f"promoted {int(stats.get('promoted') or 0)} pattern(s) to pending scripts, "
        f"from {int(stats.get('candidates') or 0)} candidate(s)."
    )


_PATTERN_TOOL_DEFS = [
    {
        "type": "function",
        "function": {
            "name": "ListScripts",
            "description": "List learned automation scripts. Filter by status: pending, approved, active, rejected, or all.",
            "parameters": {
                "type": "object",
                "properties": {
                    "status": {
                        "type": "string",
                        "enum": ["pending", "approved", "active", "rejected", "all"],
                    },
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "RunScript",
            "description": "Run an approved automation script with optional parameter overrides. Scripts are learned from repeated tool-call patterns.",
            "parameters": {
                "type": "object",
                "properties": {
                    "script_id": {"type": "string", "description": "Script ID from ListScripts."},
                    "params": {
                        "type": "object",
                        "description": "Optional parameter overrides, e.g. {\"$1\": \"myfile.py\"}.",
                    },
                },
                "required": ["script_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "ApproveScript",
            "description": "Approve a pending learned script, making it available for use. User should review before approving.",
            "parameters": {
                "type": "object",
                "properties": {
                    "script_id": {"type": "string", "description": "Script ID to approve."},
                },
                "required": ["script_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "RejectScript",
            "description": "Reject a pending learned script.",
            "parameters": {
                "type": "object",
                "properties": {
                    "script_id": {"type": "string", "description": "Script ID to reject."},
                },
                "required": ["script_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "LearnPatterns",
            "description": "Actively scan recorded tool-call history, learn repeated behavior patterns, and create pending scripts when enough evidence exists. Use this when the user explicitly asks the agent to learn from recent behavior now.",
            "parameters": {
                "type": "object",
                "properties": {},
            },
        },
    },
]

_PATTERN_HANDLERS = {
    "ListScripts": _tool_list_scripts,
    "RunScript": _tool_run_script,
    "ApproveScript": _tool_approve_script,
    "RejectScript": _tool_reject_script,
    "LearnPatterns": _tool_learn_patterns,
}


def register_tools() -> bool:
    """Inject pattern tools into the global TOOL_DEFS and TOOL_HANDLERS.

    Returns True on success, False if tools.py is not importable yet.
    """
    try:
        from cyrene.tools import TOOL_DEFS, TOOL_HANDLERS
    except ImportError:
        logger.debug("tools module not available, skipping tool registration")
        return False

    existing = {td["function"]["name"] for td in TOOL_DEFS}
    for td in _PATTERN_TOOL_DEFS:
        if td["function"]["name"] not in existing:
            TOOL_DEFS.append(td)

    for name, handler in _PATTERN_HANDLERS.items():
        if name not in TOOL_HANDLERS:
            TOOL_HANDLERS[name] = handler

    return True


# ---------------------------------------------------------------------------
# Pattern history — records fingerprints we've seen before so we only
# create pending scripts on the SECOND (or later) occurrence of a pattern,
# avoiding false positives from one-off behaviors.
# ---------------------------------------------------------------------------

_detection_ticks: int = 0
_detection_last_run: float = 0.0


def _load_pattern_history() -> dict:
    if _PATTERN_HISTORY_PATH and _PATTERN_HISTORY_PATH.exists():
        try:
            return json.loads(_PATTERN_HISTORY_PATH.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {}


def _save_pattern_history(history: dict) -> None:
    if _PATTERN_HISTORY_PATH:
        tmp = _PATTERN_HISTORY_PATH.with_suffix(".json.tmp")
        try:
            tmp.write_text(
                json.dumps(history, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            tmp.replace(_PATTERN_HISTORY_PATH)
        finally:
            try:
                if tmp.exists():
                    tmp.unlink()
            except Exception:
                pass


async def tick(bot: Any, db_path: str) -> None:
    """Call from scheduler heartbeat.  Runs detection on a throttled interval.

    Only creates pending scripts on the SECOND+ occurrence of a pattern
    (first occurrence is recorded in pattern history for reference).
    """
    global _detection_ticks, _detection_last_run

    _detection_ticks += 1
    now = time.monotonic()
    if now - _detection_last_run < _DETECTION_INTERVAL:
        return
    _detection_last_run = now

    try:
        stats = await scan_for_session_start()
        new_scripts = int(stats.get("promoted") or 0)
        if new_scripts:
            logger.info("Pattern detection: %d new pending script(s)", new_scripts)
            try:
                from cyrene.debug import publish_event
                await publish_event({
                    "type": "pattern_detected",
                    "new_scripts": new_scripts,
                    "total_candidates": int(stats.get("candidates") or 0),
                })
            except Exception:
                pass
    except Exception:
        logger.debug("Pattern detection error", exc_info=True)


# ---------------------------------------------------------------------------
# Initialization
# ---------------------------------------------------------------------------


async def init(data_dir: Path, workspace_dir: Path) -> None:
    """Initialize the pattern module.  Call once at startup."""
    global _DATA_DIR, _PATTERNS_DIR, _ACTION_LOG_PATH, _PATTERN_HISTORY_PATH, _DETECTION_INTERVAL

    _DATA_DIR = data_dir
    _PATTERNS_DIR = workspace_dir / "patterns"
    _PATTERNS_DIR.mkdir(parents=True, exist_ok=True)
    _ACTION_LOG_PATH = _DATA_DIR / "action_log.jsonl"
    _PATTERN_HISTORY_PATH = _DATA_DIR / "pattern_history.json"

    # Load interval from config if available
    try:
        from cyrene.config import PATTERN_DETECTION_INTERVAL
        _DETECTION_INTERVAL = PATTERN_DETECTION_INTERVAL
    except ImportError:
        pass

    # Restore buffer from tail of JSONL
    if _ACTION_LOG_PATH.exists():
        try:
            lines = _ACTION_LOG_PATH.read_text(encoding="utf-8").strip().splitlines()
            for line in lines[-2000:]:
                try:
                    d = json.loads(line)
                    entry = ActionEntry(
                        timestamp=d.get("ts", ""),
                        round_id=d.get("rid", ""),
                        caller=d.get("c", "unknown"),
                        tool=d.get("t", ""),
                        args=d.get("args", {}) if isinstance(d.get("args", {}), dict) else {},
                        arg_fingerprint=d.get("fp", ""),
                        duration_ms=d.get("d", 0),
                    )
                    _buffer.append(entry)
                except Exception:
                    pass
        except Exception:
            pass

    register_tools()
    logger.info("Pattern module initialized (log=%s, scripts=%s, detection_interval=%ds)",
                _ACTION_LOG_PATH, _PATTERNS_DIR, _DETECTION_INTERVAL)
