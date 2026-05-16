import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))


async def test_execution_agent_returns_quit_text(monkeypatch):
    from cyrene import agent

    async def fake_call_llm(messages, tools=None):
        return {
            "content": "scheduled task completed",
            "tool_calls": [{"id": "q1", "function": {"name": "quit", "arguments": "{}"}}],
        }

    monkeypatch.setattr(agent, "_call_llm", fake_call_llm)
    result = await agent._run_execution_agent("do something", None, 0, "db.sqlite3")
    assert result == "scheduled task completed"


async def test_execute_tool_awaits_event_publish(monkeypatch):
    from cyrene import tools

    seen = {"published": False}

    async def fake_handler(arguments, bot, chat_id, db_path, notify_state):
        return "ok"

    async def fake_publish_event(event):
        seen["published"] = True
        seen["event"] = event

    monkeypatch.setitem(tools.TOOL_HANDLERS, "__test_tool__", fake_handler)

    from cyrene import debug

    monkeypatch.setattr(debug, "publish_event", fake_publish_event)
    result = await tools._execute_tool("__test_tool__", {}, None, 0, "db.sqlite3", None)

    assert result == "ok"
    assert seen["published"] is True
    assert seen["event"]["type"] == "tool_call"

    tools.TOOL_HANDLERS.pop("__test_tool__", None)


async def test_save_session_messages_emits_session_update(tmp_path, monkeypatch):
    from cyrene import agent
    from cyrene import debug

    seen = []

    async def fake_publish_event(event):
        seen.append(event)

    monkeypatch.setattr(agent, "STATE_FILE", tmp_path / "state.json")
    monkeypatch.setattr(agent, "DATA_DIR", tmp_path)
    monkeypatch.setattr(debug, "publish_event", fake_publish_event)

    await agent._save_session_messages([
        {"role": "user", "content": "hi", "round_id": "round_1"},
        {"role": "assistant", "content": "hello", "round_id": "round_1"},
    ])

    assert seen
    assert seen[-1]["type"] == "session_update"
    assert seen[-1]["message_count"] == 2
    assert seen[-1]["last_role"] == "assistant"
    assert seen[-1]["round_id"] == "round_1"


async def test_send_agent_message_redirects_main_alias():
    from cyrene import tools

    result = await tools._tool_send_agent_message(
        {"to": "danny", "content": "final answer"},
        None,
        0,
        "db.sqlite3",
        None,
    )

    assert "Main agent does not receive inbox messages" in result
    assert "quit response" in result


def test_inbox_send_message_is_serialized():
    from cyrene import inbox
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        inbox.INBOX_DIR = Path(tmp) / "inbox"

        async def send_many():
            await asyncio.gather(*[
                asyncio.to_thread(inbox.send_message, f"sender_{i}", "receiver", "chat", f"payload_{i}")
                for i in range(20)
            ])

        asyncio.run(send_many())

        messages = inbox.read_messages("receiver", mark_read=False)
        ids = [m["message_id"] for m in messages]
        assert len(messages) == 20
        assert len(set(ids)) == 20
        assert inbox.get_unread_count("receiver") == 20


async def test_subagent_registry_emits_update_events(monkeypatch):
    from cyrene import debug
    from cyrene import subagent

    seen = []

    async def fake_publish_event(event):
        seen.append(event)

    monkeypatch.setattr(debug, "publish_event", fake_publish_event)
    await subagent.clear()
    await subagent.register("alice", "review ssh", round_id="round_live")
    await subagent.save_messages("alice", [{"role": "assistant", "content": "checking"}])
    await subagent.set_waiting("alice", result="draft ready")
    await subagent.set_resumed("alice")
    await subagent.mark_done("alice", result="done")

    event_types = [event["type"] for event in seen]
    statuses = [event.get("status") for event in seen if event.get("type") == "subagent_update"]

    assert "subagent_update" in event_types
    assert statuses[0] == "running"
    assert "waiting" in statuses
    assert "resumed" in statuses
    assert statuses[-1] == "done"
    assert seen[-1]["round_id"] == "round_live"
    assert seen[-1]["message_count"] == 1


def test_live_flow_contains_tool_nodes_and_comm_edges(tmp_path, monkeypatch):
    from cyrene import debug
    from cyrene import inbox
    from webui import routes

    inbox.INBOX_DIR = tmp_path / "inbox"
    inbox.send_message("alice", "bob", "chat", "Discuss firewall baselines")

    monkeypatch.setattr(debug, "get_recent_events", lambda limit=200: [
        {
            "type": "tool_call",
            "caller": "main_agent",
            "tool": "spawn_subagent",
            "args": {"agent_id": "alice"},
            "result_preview": "spawned",
        }
    ])

    raw_msgs = [
        {"role": "user", "content": "do work"},
        {"role": "assistant", "content": "", "reasoning_content": "thinking", "usage": {"prompt_tokens": 120, "completion_tokens": 40}, "tool_calls": [
            {"id": "t1", "function": {"name": "spawn_subagent", "arguments": '{"agent_id":"alice"}'}}
        ]},
        {"role": "tool", "tool_call_id": "t1", "content": "spawned"},
        {"role": "assistant", "content": "final answer", "usage": {"prompt_tokens": 30, "completion_tokens": 12}},
    ]
    ui_msgs = routes._convert_messages(raw_msgs)
    subagents = [{
        "id": "alice",
        "name": "alice",
        "status": "running",
        "task": "task A",
        "tokens": 2,
        "elapsed": "00:01",
        "progress": 0.45,
        "result": "",
        "messageCount": 2,
        "createdAt": "12:00:00",
        "updatedAt": "12:00:01",
    }, {
        "id": "bob",
        "name": "bob",
        "status": "queued",
        "task": "task B",
        "tokens": 1,
        "elapsed": "00:01",
        "progress": 0.82,
        "result": "",
        "messageCount": 1,
        "createdAt": "12:00:00",
        "updatedAt": "12:00:01",
    }]
    registry = {
        "alice": {"messages": [{"role": "assistant", "content": "a", "usage": {"prompt_tokens": 18, "completion_tokens": 7}}], "result": "", "status": "running"},
        "bob": {"messages": [], "result": "", "status": "waiting"},
    }

    monkeypatch.setattr(routes, "DATA_DIR", tmp_path)
    flow = routes._build_live_flow(raw_msgs, ui_msgs, subagents, registry)

    tool_nodes = [node for node in flow["nodes"] if node["kind"] == "tool"]
    comm_edges = [edge for edge in flow["edges"] if edge.get("kind") == "comm"]
    output_nodes = [node for node in flow["nodes"] if node["kind"] == "output"]

    assert any(node["title"] == "spawn_subagent" for node in tool_nodes)
    assert any(edge["message"]["body"] == "Discuss firewall baselines" for edge in comm_edges)
    assert output_nodes and output_nodes[0]["detail"]["content"] == "final answer"
    main_node = next(node for node in flow["nodes"] if node["id"] == "n_main")
    alice_node = next(node for node in flow["nodes"] if node["title"] == "subagent · alice")
    assert main_node["detail"]["tokensIn"] == 150
    assert main_node["detail"]["tokensOut"] == 52
    assert alice_node["detail"]["tokensIn"] == 18
    assert alice_node["detail"]["tokensOut"] == 7


def test_build_sessions_skips_today_archive_when_live_session_exists(tmp_path, monkeypatch):
    from webui import routes

    monkeypatch.setattr(routes, "CONVERSATIONS_DIR", tmp_path / "conversations")
    monkeypatch.setattr(routes, "STATE_FILE", tmp_path / "state.json")
    routes.CONVERSATIONS_DIR.mkdir(parents=True, exist_ok=True)

    today = routes.datetime.now(routes.timezone.utc).strftime("%Y-%m-%d")
    (routes.CONVERSATIONS_DIR / f"{today}.md").write_text(
        "# Conversations\n\n## 08:00:00 UTC\n\n**User**: hi\n\n**Ape**: archived\n\n---\n",
        encoding="utf-8",
    )
    routes.STATE_FILE.write_text(
        '{"messages":[{"role":"user","content":"live hi"},{"role":"assistant","content":"live reply"}]}',
        encoding="utf-8",
    )

    sessions = routes._build_sessions()
    ids = [session["id"] for session in sessions]

    assert ids[0] == "run_live"
    assert f"day_{today}" not in ids


def test_build_current_session_recovers_subagents_from_state_and_inbox(tmp_path, monkeypatch):
    from cyrene import inbox
    from webui import routes

    monkeypatch.setattr(routes, "STATE_FILE", tmp_path / "state.json")
    monkeypatch.setattr(routes, "DATA_DIR", tmp_path)
    monkeypatch.setattr(inbox, "INBOX_DIR", tmp_path / "inbox")

    routes.STATE_FILE.write_text(
        json.dumps({
            "messages": [
                {"role": "user", "content": "start"},
                {"role": "assistant", "content": "", "tool_calls": [
                    {"id": "call_1", "function": {
                        "name": "spawn_subagent",
                        "arguments": json.dumps({"agent_id": "alice", "task": "review firewall"})
                    }}
                ]},
                {"role": "tool", "tool_call_id": "call_1", "content": "spawned"},
                {"role": "assistant", "content": "done"},
            ]
        }),
        encoding="utf-8",
    )
    inbox.send_message("alice", "bob", "chat", "Use ufw and fail2ban")

    session = routes._build_current_session()
    subagent_names = {item["name"] for item in session["subagents"]}
    flow_titles = {node["title"] for node in session["flow"]["nodes"] if node["kind"] == "subagent"}
    comm_edges = [edge for edge in session["flow"]["edges"] if edge.get("kind") == "comm"]

    assert {"alice", "bob"}.issubset(subagent_names)
    assert "subagent · alice" in flow_titles
    assert "subagent · bob" in flow_titles
    assert any(edge["message"]["body"] == "Use ufw and fail2ban" for edge in comm_edges)


def test_build_user_reads_local_username(monkeypatch):
    from webui import routes

    monkeypatch.setenv("USER", "localtester")
    monkeypatch.delenv("USERNAME", raising=False)
    monkeypatch.delenv("LOGNAME", raising=False)
    monkeypatch.setattr(routes.getpass, "getuser", lambda: "ignored-user")

    user = routes._build_user()

    assert user["name"] == "localtester"
    assert user["handle"] == "localtester"
    assert user["initials"] == "L"


def test_live_flow_staggers_subagents_when_tool_stacks_are_tall(monkeypatch):
    from cyrene import debug
    from webui import routes

    monkeypatch.setattr(debug, "get_recent_events", lambda limit=200: [])
    raw_msgs = [
        {"role": "user", "content": "plan"},
        {"role": "assistant", "content": "final"},
    ]
    ui_msgs = routes._convert_messages(raw_msgs)
    subagents = [
        {
            "id": "alpha",
            "name": "alpha",
            "status": "done",
            "task": "task alpha",
            "tokens": 0,
            "elapsed": "00:01",
            "progress": 1.0,
            "result": "",
            "messageCount": 0,
            "createdAt": "12:00:00",
            "updatedAt": "12:00:01",
        },
        {
            "id": "beta",
            "name": "beta",
            "status": "done",
            "task": "task beta",
            "tokens": 0,
            "elapsed": "00:01",
            "progress": 1.0,
            "result": "",
            "messageCount": 0,
            "createdAt": "12:00:02",
            "updatedAt": "12:00:03",
        },
    ]
    registry = {
        "alpha": {
            "messages": [
                {"role": "assistant", "content": "", "tool_calls": [
                    {"id": "a1", "function": {"name": "search", "arguments": "{}"}},
                    {"id": "a2", "function": {"name": "bash", "arguments": "{}"}},
                    {"id": "a3", "function": {"name": "read", "arguments": "{}"}},
                ]},
                {"role": "tool", "tool_call_id": "a1", "content": "ok"},
                {"role": "tool", "tool_call_id": "a2", "content": "ok"},
                {"role": "tool", "tool_call_id": "a3", "content": "ok"},
            ],
            "result": "",
            "status": "done",
        },
        "beta": {
            "messages": [],
            "result": "",
            "status": "done",
        },
    }

    flow = routes._build_live_flow(raw_msgs, ui_msgs, subagents, registry)
    alpha = next(node for node in flow["nodes"] if node["title"] == "subagent · alpha")
    beta = next(node for node in flow["nodes"] if node["title"] == "subagent · beta")

    assert beta["y"] - alpha["y"] >= 300


def test_live_flow_stacks_multiple_rounds_and_keeps_comm_edges(tmp_path, monkeypatch):
    from cyrene import inbox
    from cyrene import debug
    from webui import routes

    monkeypatch.setattr(debug, "get_recent_events", lambda limit=200: [])
    monkeypatch.setattr(routes, "DATA_DIR", tmp_path)
    monkeypatch.setattr(inbox, "INBOX_DIR", tmp_path / "inbox")
    inbox.send_message("alice", "bob", "chat", "Round one message")

    raw_msgs = [
        {"role": "user", "content": "round one"},
        {"role": "assistant", "content": "", "tool_calls": [
            {"id": "call_1", "function": {"name": "spawn_subagent", "arguments": json.dumps({"agent_id": "alice", "task": "task a"})}},
            {"id": "call_2", "function": {"name": "spawn_subagent", "arguments": json.dumps({"agent_id": "bob", "task": "task b"})}},
        ]},
        {"role": "tool", "tool_call_id": "call_1", "content": "spawned"},
        {"role": "tool", "tool_call_id": "call_2", "content": "spawned"},
        {"role": "assistant", "content": "round one done"},
        {"role": "user", "content": "round two"},
        {"role": "assistant", "content": "round two done"},
    ]

    flow = routes._build_live_flow(raw_msgs, routes._convert_messages(raw_msgs), [], {})

    round0_main = next(node for node in flow["nodes"] if node["id"] == "r0_n_main")
    round1_main = next(node for node in flow["nodes"] if node["id"] == "r1_n_main")
    comm_edges = [edge for edge in flow["edges"] if edge.get("kind") == "comm"]

    assert round1_main["y"] > round0_main["y"]
    assert any(edge["message"]["body"] == "Round one message" for edge in comm_edges)


def test_live_flow_filters_comm_edges_by_round_id(tmp_path, monkeypatch):
    from cyrene import inbox
    from cyrene import debug
    from webui import routes

    monkeypatch.setattr(debug, "get_recent_events", lambda limit=200: [])
    monkeypatch.setattr(routes, "DATA_DIR", tmp_path)
    monkeypatch.setattr(inbox, "INBOX_DIR", tmp_path / "inbox")
    inbox.send_message("alice", "bob", "chat", "Old round", round_id="round_old")
    inbox.send_message("alice", "bob", "chat", "New round", round_id="round_new")

    raw_msgs = [
        {"role": "user", "content": "old", "round_id": "round_old"},
        {"role": "assistant", "content": "", "round_id": "round_old", "tool_calls": [
            {"id": "old_1", "function": {"name": "spawn_subagent", "arguments": json.dumps({"agent_id": "alice", "task": "a"})}},
            {"id": "old_2", "function": {"name": "spawn_subagent", "arguments": json.dumps({"agent_id": "bob", "task": "b"})}},
        ]},
        {"role": "assistant", "content": "done old", "round_id": "round_old"},
        {"role": "user", "content": "new", "round_id": "round_new"},
        {"role": "assistant", "content": "", "round_id": "round_new", "tool_calls": [
            {"id": "new_1", "function": {"name": "spawn_subagent", "arguments": json.dumps({"agent_id": "alice", "task": "a"})}},
            {"id": "new_2", "function": {"name": "spawn_subagent", "arguments": json.dumps({"agent_id": "bob", "task": "b"})}},
        ]},
        {"role": "assistant", "content": "done new", "round_id": "round_new"},
    ]

    flow = routes._build_live_flow(raw_msgs, routes._convert_messages(raw_msgs), [], {})
    old_edges = [edge for edge in flow["edges"] if edge.get("kind") == "comm" and edge["from"].startswith("r0_")]
    new_edges = [edge for edge in flow["edges"] if edge.get("kind") == "comm" and edge["from"].startswith("r1_")]

    assert any(edge["message"]["body"] == "Old round" for edge in old_edges)
    assert all(edge["message"]["body"] != "New round" for edge in old_edges)
    assert any(edge["message"]["body"] == "New round" for edge in new_edges)


def test_live_flow_uses_registry_when_state_is_empty(monkeypatch):
    from cyrene import debug
    from webui import routes

    monkeypatch.setattr(debug, "get_recent_events", lambda limit=200: [
        {"type": "phase_transition", "detail": "Live subagents spawned"},
    ])
    registry = {
        "alice": {
            "task": "discuss architecture",
            "status": "running",
            "result": "",
            "messages": [{"role": "assistant", "content": "working"}],
            "created_at": "2026-05-15T09:00:00+00:00",
            "updated_at": "2026-05-15T09:00:10+00:00",
            "round_id": "round_live",
        }
    }
    subagents = [{
        "id": "alice",
        "name": "alice",
        "status": "running",
        "task": "discuss architecture",
        "tokens": 1,
        "elapsed": "00:01",
        "progress": 0.45,
        "result": "",
        "messageCount": 1,
        "createdAt": "17:00:00",
        "updatedAt": "17:00:10",
    }]

    flow = routes._build_live_flow([], [], subagents, registry)

    assert any(node["kind"] == "main" for node in flow["nodes"])
    assert any(node["title"] == "subagent · alice" for node in flow["nodes"])


def test_convert_messages_keeps_assistant_entries_with_thinking_or_tools():
    from webui import routes

    raw_msgs = [
        {"role": "user", "content": "start"},
        {"role": "assistant", "content": "", "reasoning_content": "thinking"},
        {"role": "assistant", "content": "", "tool_calls": [
            {"id": "t1", "function": {"name": "spawn_subagent", "arguments": "{}"}}
        ]},
    ]

    ui_msgs = routes._convert_messages(raw_msgs)

    assert len(ui_msgs) == 3
    assert ui_msgs[1]["role"] == "agent"
    assert ui_msgs[1]["thinking"] == "thinking"
    assert ui_msgs[2]["role"] == "agent"
    assert ui_msgs[2]["tools"][0]["name"] == "spawn_subagent"
