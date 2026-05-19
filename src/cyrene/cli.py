"""
Cyrene CLI — thin HTTP client for the Cyrene daemon.

Usage:
    cyrene start                         Start daemon (background)
    cyrene stop                          Stop daemon
    cyrene do <text> --session <id>      Send message to agent
    cyrene session list                  List sessions
    cyrene session status --session <id> Session details
    cyrene session delete --session <id> Delete session
    cyrene flow --session <id>           List rounds
    cyrene flow --session <id> --round <r>  Round timeline
    cyrene flow --session <id> --round <r> --id <e>  Event details
    cyrene memory soul [--edit <path>]   View/edit SOUL.md
    cyrene memory short-term             Short-term memory
    cyrene memory context                Context window
    cyrene status                        System status
    cyrene mcp list/add/remove/toggle    MCP servers
"""

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import httpx

DAEMON_URL = "http://localhost:4242"
CLIENT_TIMEOUT = 300.0  # 5 min default for long tasks


def _api(path: str, method: str = "GET", **kwargs) -> httpx.Response:
    """Make an API call to the daemon."""
    url = f"{DAEMON_URL}{path}"
    kwargs.setdefault("timeout", CLIENT_TIMEOUT)
    client = httpx.Client()
    try:
        if method == "GET":
            resp = client.get(url, **kwargs)
        elif method == "POST":
            resp = client.post(url, **kwargs)
        elif method == "PUT":
            resp = client.put(url, **kwargs)
        elif method == "DELETE":
            resp = client.delete(url, **kwargs)
        else:
            raise ValueError(f"Unsupported method: {method}")
        resp.raise_for_status()
        return resp
    except httpx.ConnectError:
        print("Error: Cannot connect to Cyrene daemon at", DAEMON_URL)
        print("Start it with: cyrene start")
        sys.exit(1)
    except httpx.HTTPStatusError as e:
        try:
            detail = e.response.json()
        except Exception:
            detail = {"error": str(e)}
        print(f"Error ({e.response.status_code}): {detail.get('error', str(e))}")
        sys.exit(1)


def _api_json(path: str, method: str = "GET", **kwargs) -> dict | list:
    return _api(path, method, **kwargs).json()


# ---------------------------------------------------------------------------
# start / stop
# ---------------------------------------------------------------------------


def cmd_start(args: argparse.Namespace) -> None:
    """Start the Cyrene daemon in background."""
    # Check if already running
    try:
        resp = httpx.get(f"{DAEMON_URL}/api/status", timeout=5.0)
        if resp.status_code == 200:
            print(f"Cyrene is already running at {DAEMON_URL}")
            return
    except Exception:
        pass

    # Launch daemon as subprocess
    proc = subprocess.Popen(
        [sys.executable, "-m", "cyrene.local_cli", "--web"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0,
    )

    # Wait for it to be ready
    for _ in range(30):
        try:
            resp = httpx.get(f"{DAEMON_URL}/api/ui-data", timeout=3.0)
            if resp.status_code == 200:
                break
        except Exception:
            pass
        time.sleep(1)
    else:
        print("Error: Daemon failed to start within 30s")
        proc.kill()
        sys.exit(1)

    data = resp.json()
    sessions = data.get("sessions", [])
    live = [s for s in sessions if s.get("id") == "run_live"]
    session_info = live[0] if live else {}
    msg_count = len(session_info.get("chat", {}).get("messages", []))

    print(f"Cyrene started at {DAEMON_URL}")
    print()
    print("Available sessions:")
    for s in sessions[:5]:
        sid = s.get("id", "?")
        title = s.get("title", "?")
        status = s.get("status", "?")
        n = s.get("summary", {}).get("tokens", "—")
        print(f"  {sid}  ({title}, {status}, {n})")
    if len(sessions) > 5:
        print(f"  ... and {len(sessions) - 5} more")
    print()
    print("Available commands:")
    print('  cyrene do "your question" --session run_live')
    print("  cyrene status")
    print("  cyrene --help")
    print()
    print("Extra notes:")
    print("  This terminal is now running the daemon. Close it to stop the agent.")
    print("  Open a new terminal to run CLI commands.")


def cmd_stop(args: argparse.Namespace) -> None:
    """Stop the Cyrene daemon."""
    try:
        _api("/api/shutdown", method="POST")
    except Exception:
        pass
    print("Cyrene stopped.")


# ---------------------------------------------------------------------------
# do
# ---------------------------------------------------------------------------


def cmd_do(args: argparse.Namespace) -> None:
    """Send a message to the agent and print the response."""
    session_id = args.session
    text = args.text

    payload = {"message": text, "session_id": session_id}
    resp = _api_json("/api/chat", method="POST", json=payload)

    if args.json:
        print(json.dumps(resp, ensure_ascii=False, indent=2))
    else:
        response = resp.get("response", "")
        if response:
            print(f"Cyrene: {response}")
        print(f"---")
        labels = _api_json("/api/sessions")
        current = next((s for s in labels.get("sessions", []) if s.get("id") == "run_live"), {})
        summary = current.get("summary", {})
        print(f"Session: {session_id} | Tokens: {summary.get('tokens', '—')} | Duration: {summary.get('spend', '—')}")


# ---------------------------------------------------------------------------
# session
# ---------------------------------------------------------------------------


def cmd_session_list(args: argparse.Namespace) -> None:
    """List all sessions."""
    data = _api_json("/api/sessions")
    sessions = data.get("sessions", [])

    if args.json:
        print(json.dumps(sessions, ensure_ascii=False, indent=2))
        return

    print(f"{'ID':<45} {'Title':<24} {'Status':<10} {'Messages':<10} {'Duration':<10}")
    print(f"{'-'*45} {'-'*24} {'-'*10} {'-'*10} {'-'*10}")
    for s in sessions:
        sid = s.get("id", "?")
        title = (s.get("title") or "?")[:24]
        status = s.get("status", "?")
        summary = s.get("summary", {})
        tokens = summary.get("tokens", "—")
        dur = s.get("dur", "—")
        print(f"{sid:<45} {title:<24} {status:<10} {tokens:<10} {dur:<10}")


def cmd_session_status(args: argparse.Namespace) -> None:
    """Show detailed session status."""
    session_id = args.session

    data = _api_json(f"/api/sessions")
    sessions = data.get("sessions", [])
    session = next((s for s in sessions if s.get("id") == session_id), None)

    if session is None:
        # Try API
        print(f"Session '{session_id}' not found.")
        sys.exit(1)

    if args.json:
        print(json.dumps(session, ensure_ascii=False, indent=2))
        return

    chat = session.get("chat", {})
    msgs = chat.get("messages", [])
    summary = session.get("summary", {})

    print(f"Session: {session_id}")
    print(f"  title: {session.get('title', '?')}")
    print(f"  status: {session.get('status', '?')}")
    print(f"  messages: {len(msgs)}")
    print(f"  tokens: {summary.get('tokens', '—')}")
    print(f"  started: {session.get('started', '—')}")
    print(f"  duration: {session.get('dur', '—')}")
    print()

    rounds = session.get("liveRounds", [])
    if rounds:
        print("Rounds:")
        for r in rounds:
            rid = r.get("id", "?")
            status = r.get("status", "?")
            elapsed = r.get("elapsed", "—")
            prompt = (r.get("prompt") or "")[:40]
            print(f"  {rid:<24} {status:<10} {elapsed:<10} \"{prompt}\"")

    subagents = session.get("subagents", [])
    if subagents:
        print(f"\nSubagents ({len(subagents)}):")
        for sa in subagents:
            name = sa.get("name", "?")
            status = sa.get("status", "?")
            elapsed = sa.get("elapsed", "—")
            task = (sa.get("task") or "")[:40]
            print(f"  {name:<16} {status:<10} {elapsed:<10} \"{task}\"")


def cmd_session_delete(args: argparse.Namespace) -> None:
    """Delete a session."""
    session_id = args.session
    _api(f"/api/sessions/{session_id}", method="DELETE")
    print(f"Session {session_id} deleted.")


# ---------------------------------------------------------------------------
# flow
# ---------------------------------------------------------------------------


def cmd_flow(args: argparse.Namespace) -> None:
    """Show agent run timeline. Rebuilds from persisted session messages."""
    session_id = args.session
    round_id = args.round
    event_id = args.id

    # Event detail (from in-memory full event store)
    if event_id:
        if not round_id:
            print("Error: --round is required when using --id")
            sys.exit(1)
        event = _api_json(f"/api/events/{event_id}")
        if args.json or True:
            print(json.dumps(event, ensure_ascii=False, indent=2))
        else:
            etype = event.get("type", "?")
            caller = event.get("caller", "?")
            duration = event.get("duration_ms", 0)
            print(f"Event: {event_id}")
            print(f"  type: {etype}")
            print(f"  caller: {caller}")
            print(f"  duration: {duration}ms")
            if etype == "llm_call":
                msgs = event.get("messages", [])
                resp = event.get("response", {})
                print(f"  messages: {len(msgs)}")
                print(f"  response tokens: {resp.get('usage', {}).get('completion_tokens', '?')}")
                if args.verbose:
                    print("\nFull messages:")
                    print(json.dumps(msgs, ensure_ascii=False, indent=2))
                    print("\nFull response:")
                    print(json.dumps(resp, ensure_ascii=False, indent=2))
            elif etype == "tool_call":
                print(f"  tool: {event.get('tool', '?')}")
                print(f"  args: {json.dumps(event.get('args', {}), ensure_ascii=False)}")
                if args.verbose:
                    print(f"\nFull result:")
                    print(event.get("result", "")[:2000])
        return

    # Fetch raw session messages (includes round_id, tool_calls)
    history = _api_json("/api/chat/state")
    raw_messages = history.get("messages", [])

    # Build round map from persisted messages
    round_messages: dict[str, list] = {}
    for msg in raw_messages:
        rid = str(msg.get("round_id", "")).strip()
        if not rid:
            continue
        round_messages.setdefault(rid, []).append(msg)

    if not round_messages:
        print(f"No rounds found for session '{session_id}'.")
        return

    # List all rounds
    if not round_id:
        sorted_rounds = sorted(round_messages.keys(), reverse=True)
        if args.json:
            print(json.dumps(sorted_rounds, ensure_ascii=False, indent=2))
            return
        print("Rounds (most recent first):")
        for rid in sorted_rounds:
            msgs = round_messages[rid]
            # Find the user prompt
            prompt = ""
            for m in msgs:
                if m.get("role") == "user" and m.get("content", "").strip():
                    prompt = m["content"].strip()[:40]
                    break
            n_msgs = len(msgs)
            n_tools = sum(len(m.get("tool_calls") or []) for m in msgs if m.get("tool_calls"))
            print(f"  {rid:<24} {n_msgs:<4} msgs  {n_tools} tools  \"{prompt}\"")
        return

    # Show single round timeline
    msgs = round_messages.get(round_id, [])
    if not msgs:
        print(f"Round '{round_id}' not found in session '{session_id}'.")
        sys.exit(1)

    if args.json:
        print(json.dumps(msgs, ensure_ascii=False, indent=2))
        return

    prompt = ""
    for m in msgs:
        if m.get("role") == "user" and m.get("content", "").strip():
            prompt = m["content"].strip()
            break
    print(f"Round: {round_id}")
    print(f"  prompt: \"{prompt}\"")
    print(f"  messages: {len(msgs)}")
    print()

    seq = 0
    for m in msgs:
        role = m.get("role", "?")
        content = m.get("content", "").strip()
        tool_calls = m.get("tool_calls") or []

        if role == "user":
            continue  # prompt already shown above

        if role == "assistant":
            seq += 1
            if tool_calls:
                for tc in tool_calls:
                    fn = tc.get("function", {})
                    name = fn.get("name", "?")
                    raw_args = fn.get("arguments", "")
                    try:
                        args_display = json.loads(raw_args) if isinstance(raw_args, str) else raw_args
                    except Exception:
                        args_display = raw_args
                    args_str = json.dumps(args_display, ensure_ascii=False) if isinstance(args_display, dict) else str(args_display)
                    if len(args_str) > 80:
                        args_str = args_str[:80] + "…"
                    print(f"  [{seq:03d}] Tool call: {name}({args_str})")
            if content:
                content_preview = content[:200].replace("\n", " ")
                print(f"  [{seq:03d}] Response: \"{content_preview}\"")

        if role == "tool":
            tool_name = m.get("tool_call_id", "?")
            tool_content = m.get("content", "").strip()[:100]
            print(f"  [   ] Tool result ({tool_name}): {tool_content}…")

    n_tools = sum(len(m.get("tool_calls") or []) for m in msgs if m.get("tool_calls"))
    print(f"\nTools called: {n_tools}")


# ---------------------------------------------------------------------------
# memory
# ---------------------------------------------------------------------------


def cmd_memory_soul(args: argparse.Namespace) -> None:
    """View or edit SOUL.md."""
    if args.edit:
        edit_path = Path(args.edit)
        if not edit_path.exists():
            print(f"Error: file not found: {edit_path}")
            sys.exit(1)
        content = edit_path.read_text(encoding="utf-8")
        _api("/api/settings/soul", method="PUT", json={"content": content})
        sections = content.count("## ")
        print(f"✅ SOUL.md updated ({sections} sections, {len(content)} chars).")
        return

    data = _api_json("/api/settings/soul")
    print(data.get("content", ""))


def cmd_memory_short_term(args: argparse.Namespace) -> None:
    """View short-term memory."""
    data = _api_json("/api/memory")
    st = data.get("short_term", {})
    entries = st.get("entries", [])

    if args.json:
        print(json.dumps(entries, ensure_ascii=False, indent=2))
        return

    print(f"{'Type':<12} {'Content':<48} {'Count':<6} {'Valence':<8} {'First':<10} {'Last':<10}")
    print(f"{'-'*12} {'-'*48} {'-'*6} {'-'*8} {'-'*10} {'-'*10}")
    for e in entries:
        etype = e.get("type", "?")
        content = (e.get("content", "") or "")[:48]
        count = e.get("mention_count", 0)
        valence = e.get("emotional_valence", 0)
        valence_str = f"+{valence}" if valence > 0 else str(valence)
        first = (e.get("first_seen") or "—")
        last = (e.get("last_mentioned") or "—")
        print(f"{etype:<12} {content:<48} {count:<6} {valence_str:<8} {first:<10} {last:<10}")


def cmd_memory_context(args: argparse.Namespace) -> None:
    """View context window status."""
    data = _api_json("/api/memory")
    cw = data.get("context_window", {})
    current = cw.get("messages", 0)
    max_msgs = cw.get("max", 40)
    threshold = max_msgs + 5

    if args.json:
        print(json.dumps(cw, ensure_ascii=False, indent=2))
        return

    print(f"Context Window: {current} / {max_msgs} messages")
    print(f"  Compression trigger: {threshold} messages")
    need_compress = "yes" if current >= threshold else "— (below threshold)"
    print(f"  Next action: {need_compress}")


# ---------------------------------------------------------------------------
# status
# ---------------------------------------------------------------------------


def cmd_status(args: argparse.Namespace) -> None:
    """Show system status."""
    data = _api_json("/api/status")

    if args.json:
        print(json.dumps(data, ensure_ascii=False, indent=2))
        return

    model = data.get("model", "?")
    base_url = data.get("base_url", "?")
    print(f"Model: {model}")
    print(f"Endpoint: {base_url}")
    print()

    workers = data.get("workers", [])
    if workers:
        print(f"{'Worker':<20} {'Role':<16} {'Status':<10} {'Uptime':<12} {'Tokens':<12}")
        print(f"{'-'*20} {'-'*16} {'-'*10} {'-'*12} {'-'*12}")
        for w in workers:
            wid = w.get("id", "?")
            role = w.get("role", "?")
            status = w.get("status", "?")
            uptime = w.get("uptime", "—")
            tokens = w.get("tokens", "—")
            print(f"{wid:<20} {role:<16} {status:<10} {uptime:<12} {tokens:<12}")

    metrics = data.get("metrics", [])
    if metrics:
        print(f"\nMetrics:")
        for m in metrics:
            label = m.get("label", "?")
            value = m.get("value", "?")
            unit = m.get("unit", "")
            sub = m.get("sub", "")
            print(f"  {label}: {value}{unit} ({sub})")

    services = data.get("services", [])
    if services:
        print(f"\nServices:")
        for svc in services:
            name = svc.get("name", "?")
            status = svc.get("status", "?")
            latency = svc.get("latency", "—")
            note = svc.get("note", "")
            note_str = f" — {note}" if note else ""
            print(f"  {name:<40} {status:<6} {latency:<10}{note_str}")


# ---------------------------------------------------------------------------
# mcp (reuse existing implementation via HTTP)
# ---------------------------------------------------------------------------


def _mcp_call(method: str = "GET", **kwargs) -> dict | list:
    return _api_json(f"/api/settings/mcp", method=method, **kwargs)


def cmd_mcp_list(args: argparse.Namespace) -> None:
    """List MCP servers."""
    data = _api_json("/api/settings/mcp")
    servers = data.get("servers", [])

    if args.json:
        print(json.dumps(servers, ensure_ascii=False, indent=2))
        return

    if not servers:
        print("No MCP servers configured.")
        return

    print(f"{'Name':<20} {'Transport':<10} {'Status':<14} {'Tools':<6} Endpoint")
    print(f"{'-'*20} {'-'*10} {'-'*14} {'-'*6} {'-'*40}")
    for s in servers:
        name = s.get("name", "?")
        transport = s.get("transport", "?")
        status = s.get("status", "disconnected")
        tools = s.get("tool_count", 0)
        endpoint = s.get("command", "") if transport == "stdio" else s.get("url", "")
        print(f"{name:<20} {transport:<10} {status:<14} {tools:<6} {endpoint}")


def cmd_mcp_add(args: argparse.Namespace) -> None:
    """Add an MCP server."""
    data = _api_json("/api/settings/mcp")
    configs = data.get("configs", [])

    name = args.name
    transport = args.transport
    if transport == "stdio":
        cmd_parts = list(args.rest) if args.rest else []
        command = cmd_parts[0] if cmd_parts else ""
        extra_args = cmd_parts[1:]
        server = {"name": name, "transport": "stdio", "command": command, "args": extra_args, "enabled": True}
    elif transport == "sse":
        url = args.rest[0] if args.rest else ""
        server = {"name": name, "transport": "sse", "url": url, "enabled": True}
    else:
        print(f"Unknown transport: {transport}")
        sys.exit(1)

    configs = [s for s in configs if s.get("name") != name]
    configs.append(server)
    _api("/api/settings/mcp", method="PUT", json={"servers": configs})

    # Refresh to get status
    data = _api_json("/api/settings/mcp")
    live = next((s for s in data.get("servers", []) if s.get("name") == name), {})
    tools = live.get("tool_count", 0)
    print(f"✅ MCP server '{name}' added ({tools} tools available).")


def cmd_mcp_remove(args: argparse.Namespace) -> None:
    """Remove an MCP server."""
    data = _api_json("/api/settings/mcp")
    configs = data.get("configs", [])
    name = args.name
    configs = [s for s in configs if s.get("name") != name]
    _api("/api/settings/mcp", method="PUT", json={"servers": configs})
    print(f"✅ MCP server '{name}' deleted.")


def cmd_mcp_toggle(args: argparse.Namespace) -> None:
    """Toggle an MCP server on/off."""
    data = _api_json("/api/settings/mcp")
    configs = data.get("configs", [])
    name = args.name
    for s in configs:
        if s.get("name") == name:
            s["enabled"] = not s.get("enabled", True)
            break
    _api("/api/settings/mcp", method="PUT", json={"servers": configs})
    # Refresh
    data = _api_json("/api/settings/mcp")
    live = next((s for s in data.get("servers", []) if s.get("name") == name), {})
    status = "enabled" if live.get("enabled", True) else "disabled"
    print(f"✅ MCP server '{name}' {status}.")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="cyrene", description="Cyrene AI Agent CLI")
    parser.add_argument("--json", action="store_true", help="Output raw JSON")
    parser.add_argument("--verbose", "-v", action="store_true", help="Verbose output")

    sub = parser.add_subparsers(dest="command", required=True)

    # start
    sub.add_parser("start", help="Start the Cyrene daemon")

    # stop
    sub.add_parser("stop", help="Stop the Cyrene daemon")

    # do
    do_parser = sub.add_parser("do", help="Send a message to the agent")
    do_parser.add_argument("text", help="Message text")
    do_parser.add_argument("--session", "-s", required=True, help="Session ID (required)")

    # session
    session_parser = sub.add_parser("session", help="Session management")
    session_sub = session_parser.add_subparsers(dest="subcommand", required=True)

    session_list = session_sub.add_parser("list", help="List all sessions")
    session_list.add_argument("--json", action="store_true")

    session_status = session_sub.add_parser("status", help="Session details")
    session_status.add_argument("--session", "-s", required=True)
    session_status.add_argument("--json", action="store_true")

    session_delete = session_sub.add_parser("delete", help="Delete a session")
    session_delete.add_argument("--session", "-s", required=True)
    session_delete.add_argument("--json", action="store_true")

    # flow
    flow_parser = sub.add_parser("flow", help="Agent run timeline")
    flow_parser.add_argument("--session", "-s", required=True, help="Session ID (required)")
    flow_parser.add_argument("--round", "-r", help="Round ID")
    flow_parser.add_argument("--id", help="Event ID for deep debug")
    flow_parser.add_argument("--json", action="store_true")

    # memory
    memory_parser = sub.add_parser("memory", help="Memory system")
    memory_sub = memory_parser.add_subparsers(dest="subcommand", required=True)

    mem_soul = memory_sub.add_parser("soul", help="View or edit SOUL.md")
    mem_soul.add_argument("--edit", help="Path to a file to write as SOUL.md")
    mem_soul.add_argument("--json", action="store_true")

    mem_st = memory_sub.add_parser("short-term", help="View short-term memory")
    mem_st.add_argument("--json", action="store_true")

    mem_ctx = memory_sub.add_parser("context", help="View context window")
    mem_ctx.add_argument("--json", action="store_true")

    # status
    status_parser = sub.add_parser("status", help="System status")
    status_parser.add_argument("--json", action="store_true")

    # mcp
    mcp_parser = sub.add_parser("mcp", help="MCP server management")
    mcp_sub = mcp_parser.add_subparsers(dest="subcommand", required=True)

    mcp_list = mcp_sub.add_parser("list", help="List MCP servers")
    mcp_list.add_argument("--json", action="store_true")

    mcp_add = mcp_sub.add_parser("add", help="Add an MCP server")
    mcp_add.add_argument("name")
    mcp_add.add_argument("transport", choices=["stdio", "sse"])
    mcp_add.add_argument("rest", nargs=argparse.REMAINDER, default=[], help="Command + args (stdio) or URL (sse)")

    mcp_remove = mcp_sub.add_parser("remove", help="Remove an MCP server")
    mcp_remove.add_argument("name")

    mcp_toggle = mcp_sub.add_parser("toggle", help="Enable/disable an MCP server")
    mcp_toggle.add_argument("name")

    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()

    cmd = args.command

    if cmd == "start":
        cmd_start(args)
    elif cmd == "stop":
        cmd_stop(args)
    elif cmd == "do":
        cmd_do(args)
    elif cmd == "session":
        sub = args.subcommand
        if sub == "list":
            cmd_session_list(args)
        elif sub == "status":
            cmd_session_status(args)
        elif sub == "delete":
            cmd_session_delete(args)
    elif cmd == "flow":
        cmd_flow(args)
    elif cmd == "memory":
        sub = args.subcommand
        if sub == "soul":
            cmd_memory_soul(args)
        elif sub == "short-term":
            cmd_memory_short_term(args)
        elif sub == "context":
            cmd_memory_context(args)
    elif cmd == "status":
        cmd_status(args)
    elif cmd == "mcp":
        sub = args.subcommand
        if sub == "list":
            cmd_mcp_list(args)
        elif sub == "add":
            cmd_mcp_add(args)
        elif sub == "remove":
            cmd_mcp_remove(args)
        elif sub == "toggle":
            cmd_mcp_toggle(args)


if __name__ == "__main__":
    main()
