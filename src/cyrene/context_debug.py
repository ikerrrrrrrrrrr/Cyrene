"""Inspect context traces stored in Cyrene debug JSONL logs."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

DATA_DIR = Path.cwd() / "data"


def _debug_logs() -> list[Path]:
    if not DATA_DIR.exists():
        return []
    return sorted(DATA_DIR.glob("debug_*.jsonl"), reverse=True)


def _load_events(path: Path) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if event.get("type") == "llm_call":
                events.append(event)
    return events


def _resolve_log(path_text: str) -> Path:
    if path_text:
        return Path(path_text).expanduser().resolve()
    logs = _debug_logs()
    if not logs:
        raise SystemExit("No debug_*.jsonl files found in data/. Run with --verbose first.")
    return logs[0]


def _select_event(events: list[dict[str, Any]], call: str) -> dict[str, Any]:
    if not events:
        raise SystemExit("No llm_call events found.")
    target = str(call or "latest").strip()
    if target == "latest":
        return events[-1]
    if target.isdigit():
        index = int(target)
        if index < 0:
            index = len(events) + index
        if 0 <= index < len(events):
            return events[index]
        raise SystemExit(f"Call index out of range: {target}")
    for event in events:
        if str(event.get("event_id") or "") == target:
            return event
    raise SystemExit(f"LLM call not found: {target}")


def _print_event(event: dict[str, Any], *, show_messages: bool = False) -> None:
    trace = event.get("context_trace") if isinstance(event.get("context_trace"), dict) else {}
    included = trace.get("included") if isinstance(trace.get("included"), list) else []
    print(f"event_id: {event.get('event_id', '')}")
    print(f"time: {event.get('timestamp', '')}")
    print(f"caller: {event.get('caller', '')}")
    print(f"phase: {event.get('phase', '')}")
    print(f"model: {event.get('model', '') or (event.get('response') or {}).get('model', '')}")
    print(f"messages: {len(event.get('messages') or [])}")
    print(f"tools: {', '.join(_tool_names(event.get('tools') or []))}")
    print(f"tokens_est: {trace.get('total_tokens_est', '')}")
    token_by_type = trace.get("token_by_type") if isinstance(trace.get("token_by_type"), dict) else {}
    if token_by_type:
        print("\ntokens by type:")
        for key, value in sorted(token_by_type.items(), key=lambda item: (-int(item[1] or 0), item[0])):
            print(f"  {key}: {value}")
    print("\nincluded context blocks:")
    for block in included:
        if not isinstance(block, dict):
            continue
        transforms = ",".join(block.get("transforms") or [])
        transform_text = f" transforms={transforms}" if transforms else ""
        print(
            f"  [{block.get('message_index')}] {block.get('id')} "
            f"type={block.get('type')} tokens={block.get('tokens_est')} "
            f"source={block.get('source')}{transform_text}"
        )
        reason = str(block.get("reason") or "").strip()
        if reason:
            print(f"      reason: {reason}")
    if show_messages:
        print("\nrendered messages:")
        for index, message in enumerate(event.get("messages") or []):
            role = str((message or {}).get("role") or "")
            content = str((message or {}).get("content") or "")
            preview = content.replace("\n", "\\n")[:200]
            print(f"  [{index}] {role}: {preview}")


def _print_diff(left: dict[str, Any], right: dict[str, Any]) -> None:
    def block_ids(event: dict[str, Any]) -> set[str]:
        trace = event.get("context_trace") if isinstance(event.get("context_trace"), dict) else {}
        included = trace.get("included") if isinstance(trace.get("included"), list) else []
        return {str(block.get("id") or "") for block in included if isinstance(block, dict)}

    left_ids = block_ids(left)
    right_ids = block_ids(right)
    print(f"left: {left.get('event_id', '')}")
    print(f"right: {right.get('event_id', '')}")
    print("\nadded:")
    for item in sorted(right_ids - left_ids):
        print(f"  + {item}")
    print("\nremoved:")
    for item in sorted(left_ids - right_ids):
        print(f"  - {item}")


def _tool_names(tools: list[Any]) -> list[str]:
    names: list[str] = []
    for tool in tools:
        if isinstance(tool, str):
            names.append(tool)
        elif isinstance(tool, dict):
            name = str((tool.get("function") or {}).get("name") or "").strip()
            names.append(name or str(tool.get("name") or "tool"))
        else:
            names.append(str(tool))
    return names


def main() -> None:
    parser = argparse.ArgumentParser(description="Inspect Cyrene LLM context traces.")
    parser.add_argument("log", nargs="?", help="Debug JSONL file. Defaults to latest data/debug_*.jsonl.")
    parser.add_argument("--call", default="latest", help="event_id, zero-based index, negative index, or latest.")
    parser.add_argument("--messages", action="store_true", help="Also print rendered message previews.")
    parser.add_argument("--diff", nargs=2, metavar=("LEFT", "RIGHT"), help="Compare context block IDs between two calls.")
    args = parser.parse_args()

    log_path = _resolve_log(args.log or "")
    events = _load_events(log_path)
    print(f"log: {log_path}")
    if args.diff:
        _print_diff(_select_event(events, args.diff[0]), _select_event(events, args.diff[1]))
        return
    _print_event(_select_event(events, args.call), show_messages=args.messages)


if __name__ == "__main__":
    main()
