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

        async def send_and_read():
            await asyncio.gather(*[
                inbox.send_message(f"sender_{i}", "receiver", "chat", f"payload_{i}")
                for i in range(20)
            ])
            return await inbox.read_messages("receiver", mark_read=False)

        messages = asyncio.run(send_and_read())
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
    asyncio.run(inbox.send_message("alice", "bob", "chat", "Discuss firewall baselines"))

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
                {"role": "user", "content": "start", "round_id": "round_live"},
                {"role": "assistant", "content": "", "tool_calls": [
                    {"id": "call_1", "function": {
                        "name": "spawn_subagent",
                        "arguments": json.dumps({"agent_id": "alice", "task": "review firewall"})
                    }}
                ], "round_id": "round_live"},
                {"role": "tool", "tool_call_id": "call_1", "content": "spawned", "round_id": "round_live"},
                {"role": "assistant", "content": "done", "round_id": "round_live"},
            ]
        }),
        encoding="utf-8",
    )
    asyncio.run(inbox.send_message("alice", "bob", "chat", "Use ufw and fail2ban", round_id="round_live"))

    session = routes._build_current_session()
    subagents = {item["name"]: item for item in session["subagents"]}
    subagent_names = set(subagents)
    flow_titles = {node["title"] for node in session["flow"]["nodes"] if node["kind"] == "subagent"}
    comm_edges = [edge for edge in session["flow"]["edges"] if edge.get("kind") == "comm"]

    assert {"alice", "bob"}.issubset(subagent_names)
    assert session["currentRoundId"] == "round_live"
    assert subagents["alice"]["roundId"] == "round_live"
    assert subagents["bob"]["roundId"] == "round_live"
    assert "subagent · alice" in flow_titles
    assert "subagent · bob" in flow_titles
    assert any(edge["message"]["body"] == "Use ufw and fail2ban" for edge in comm_edges)


async def test_clear_session_id_removes_live_flow_residue(tmp_path, monkeypatch):
    from cyrene import agent
    from cyrene import inbox
    from cyrene import subagent
    from webui import routes

    monkeypatch.setattr(agent, "STATE_FILE", tmp_path / "state.json")
    monkeypatch.setattr(agent, "DATA_DIR", tmp_path)
    monkeypatch.setattr(routes, "STATE_FILE", tmp_path / "state.json")
    monkeypatch.setattr(routes, "DATA_DIR", tmp_path)
    monkeypatch.setattr(inbox, "INBOX_DIR", tmp_path / "inbox")

    agent.STATE_FILE.write_text(
        json.dumps({
            "messages": [
                {"role": "user", "content": "start", "round_id": "round_live"},
                {"role": "assistant", "content": "", "round_id": "round_live", "tool_calls": [
                    {"id": "call_1", "function": {
                        "name": "spawn_subagent",
                        "arguments": json.dumps({"agent_id": "alice", "task": "review firewall"})
                    }}
                ]},
                {"role": "tool", "tool_call_id": "call_1", "content": "spawned", "round_id": "round_live"},
                {"role": "assistant", "content": "waiting", "round_id": "round_live"},
            ]
        }),
        encoding="utf-8",
    )
    await subagent.register("alice", "review firewall", round_id="round_live")
    await inbox.send_message("alice", "bob", "chat", "Use ufw and fail2ban", round_id="round_live")

    before = routes._build_current_session()
    assert before["flow"]["nodes"]

    await agent.clear_session_id()

    after = routes._build_current_session()
    assert after["title"] == "new session"
    assert after["subagents"] == []
    assert after["flow"]["nodes"] == []
    assert after["flow"]["edges"] == []


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
    asyncio.run(inbox.send_message("alice", "bob", "chat", "Round one message"))

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


def test_live_flow_prunes_extra_user_only_tail_rounds(monkeypatch):
    from cyrene import debug
    from webui import routes

    monkeypatch.setattr(debug, "get_recent_events", lambda limit=200: [])
    raw_msgs = [
        {"role": "user", "content": "round one", "round_id": "round_1"},
        {"role": "assistant", "content": "done one", "round_id": "round_1"},
        {"role": "user", "content": "round two pending", "round_id": "round_2"},
        {"role": "user", "content": "round three pending", "round_id": "round_3"},
    ]

    flow = routes._build_live_flow(raw_msgs, routes._convert_messages(raw_msgs), [], {})
    input_ids = [node["id"] for node in flow["nodes"] if node["kind"] == "input"]

    assert input_ids == ["r0_n_user", "r1_n_user"]
    assert all(not node["id"].startswith("r2_") for node in flow["nodes"])


def test_live_flow_attaches_live_registry_to_latest_substantive_round(monkeypatch):
    from cyrene import debug
    from webui import routes

    monkeypatch.setattr(debug, "get_recent_events", lambda limit=200: [])
    raw_msgs = [
        {"role": "user", "content": "start debate", "round_id": "round_a"},
        {"role": "assistant", "content": "", "round_id": "round_a", "tool_calls": [
            {"id": "call_1", "function": {"name": "spawn_subagent", "arguments": json.dumps({"agent_id": "alice", "task": "debate"})}},
        ]},
        {"role": "tool", "tool_call_id": "call_1", "content": "spawned", "round_id": "round_a"},
        {"role": "assistant", "content": "working", "round_id": "round_a"},
        {"role": "user", "content": "hello", "round_id": "round_b"},
    ]
    registry = {
        "alice": {
            "task": "debate",
            "status": "running",
            "result": "",
            "messages": [{"role": "assistant", "content": "still working"}],
            "created_at": "2026-05-16T04:00:00+00:00",
            "updated_at": "2026-05-16T04:00:10+00:00",
            "round_id": "round_a",
        }
    }
    subagents = [{
        "id": "alice",
        "name": "alice",
        "status": "running",
        "task": "debate",
        "tokens": 1,
        "elapsed": "00:01",
        "progress": 0.45,
        "result": "",
        "messageCount": 1,
        "createdAt": "12:00:00",
        "updatedAt": "12:00:10",
    }]

    flow = routes._build_live_flow(raw_msgs, routes._convert_messages(raw_msgs), subagents, registry)
    alice = next(node for node in flow["nodes"] if node["title"] == "subagent · alice")

    assert alice["id"].startswith("r0_")
    assert all(not node["title"] == "subagent · alice" or node["id"].startswith("r0_") for node in flow["nodes"])


def test_live_flow_filters_comm_edges_by_round_id(tmp_path, monkeypatch):
    from cyrene import inbox
    from cyrene import debug
    from webui import routes

    monkeypatch.setattr(debug, "get_recent_events", lambda limit=200: [])
    monkeypatch.setattr(routes, "DATA_DIR", tmp_path)
    monkeypatch.setattr(inbox, "INBOX_DIR", tmp_path / "inbox")
    asyncio.run(inbox.send_message("alice", "bob", "chat", "Old round", round_id="round_old"))
    asyncio.run(inbox.send_message("alice", "bob", "chat", "New round", round_id="round_new"))

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


def test_live_flow_filters_recent_events_to_current_round(monkeypatch):
    from cyrene import debug
    from webui import routes

    monkeypatch.setattr(debug, "get_recent_events", lambda limit=200: [
        {
            "type": "tool_call",
            "caller": "main_agent",
            "tool": "old_search",
            "args": {"query": "old"},
            "result_preview": "old result",
            "round_id": "round_old",
        },
        {
            "type": "tool_call",
            "caller": "main_agent",
            "tool": "new_search",
            "args": {"query": "new"},
            "result_preview": "new result",
            "round_id": "round_new",
        },
    ])
    raw_msgs = [
        {"role": "user", "content": "old", "round_id": "round_old"},
        {"role": "assistant", "content": "done old", "round_id": "round_old"},
        {"role": "user", "content": "new", "round_id": "round_new"},
        {"role": "assistant", "content": "working new", "round_id": "round_new"},
    ]

    flow = routes._build_live_flow(raw_msgs, routes._convert_messages(raw_msgs), [], {})
    round1_tools = [
        node["title"]
        for node in flow["nodes"]
        if node["kind"] == "tool" and node["id"].startswith("r1_")
    ]

    assert "new_search" in round1_tools
    assert "old_search" not in round1_tools


def test_live_flow_does_not_merge_stale_subagent_card_into_new_round(monkeypatch):
    from cyrene import debug
    from webui import routes

    monkeypatch.setattr(debug, "get_recent_events", lambda limit=200: [])
    raw_msgs = [
        {"role": "user", "content": "old", "round_id": "round_old"},
        {"role": "assistant", "content": "", "round_id": "round_old", "tool_calls": [
            {"id": "old_1", "function": {"name": "spawn_subagent", "arguments": json.dumps({"agent_id": "alice", "task": "old task"})}},
        ]},
        {"role": "tool", "tool_call_id": "old_1", "content": "spawned", "round_id": "round_old"},
        {"role": "assistant", "content": "done old", "round_id": "round_old"},
        {"role": "user", "content": "new", "round_id": "round_new"},
        {"role": "assistant", "content": "", "round_id": "round_new", "tool_calls": [
            {"id": "new_1", "function": {"name": "spawn_subagent", "arguments": json.dumps({"agent_id": "alice", "task": "new task"})}},
        ]},
        {"role": "tool", "tool_call_id": "new_1", "content": "spawned", "round_id": "round_new"},
        {"role": "assistant", "content": "working new", "round_id": "round_new"},
    ]
    subagents = [{
        "id": "alice",
        "name": "alice",
        "status": "done",
        "task": "old task",
        "roundId": "round_old",
        "tokens": 1,
        "elapsed": "00:02",
        "progress": 1.0,
        "result": "old result",
        "messageCount": 1,
        "createdAt": "12:00:00",
        "updatedAt": "12:00:01",
    }]
    registry = {
        "alice": {
            "task": "old task",
            "status": "done",
            "result": "old result",
            "messages": [{"role": "assistant", "content": "old result"}],
            "round_id": "round_old",
        }
    }

    flow = routes._build_live_flow(raw_msgs, routes._convert_messages(raw_msgs), subagents, registry)
    new_alice = next(
        node for node in flow["nodes"]
        if node["title"] == "subagent · alice" and node["id"].startswith("r1_")
    )

    assert new_alice["detail"]["task"] == "new task"
    assert new_alice["detail"]["result"] == ""


def test_infer_subagent_entries_prefers_latest_spawn_round_over_stale_registry():
    from webui import routes

    raw_msgs = [
        {"role": "user", "content": "old", "round_id": "round_old"},
        {"role": "assistant", "content": "", "round_id": "round_old", "tool_calls": [
            {"id": "old_1", "function": {"name": "spawn_subagent", "arguments": json.dumps({"agent_id": "alice", "task": "old task"})}},
        ]},
        {"role": "user", "content": "new", "round_id": "round_new"},
        {"role": "assistant", "content": "", "round_id": "round_new", "tool_calls": [
            {"id": "new_1", "function": {"name": "spawn_subagent", "arguments": json.dumps({"agent_id": "alice", "task": "new task"})}},
        ]},
    ]
    registry = {
        "alice": {
            "task": "old task",
            "status": "done",
            "result": "old result",
            "messages": [{"role": "assistant", "content": "old result"}],
            "round_id": "round_old",
            "created_at": "2026-05-16T04:00:00+00:00",
            "updated_at": "2026-05-16T04:00:10+00:00",
        }
    }

    entries = routes._infer_subagent_entries(raw_msgs, registry)

    assert entries["alice"]["task"] == "new task"
    assert entries["alice"]["round_id"] == "round_new"
    assert entries["alice"]["status"] == "running"
    assert entries["alice"]["result"] == ""
    assert entries["alice"]["messages"] == []


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


async def test_run_chat_agent_persists_user_visible_reply(monkeypatch, tmp_path):
    from cyrene import agent
    from cyrene import soul

    monkeypatch.setattr(agent, "STATE_FILE", tmp_path / "state.json")
    monkeypatch.setattr(agent, "DATA_DIR", tmp_path)
    monkeypatch.setattr(agent, "get_context", lambda max_chars=5000: "")
    monkeypatch.setattr(soul, "read_shallow_memory", lambda: "")

    async def fake_run_main_agent(user_message, history, bot, chat_id, db_path):
        round_id = agent._current_round_id.get()
        await agent._save_session_messages([
            {"role": "user", "content": user_message, "round_id": round_id},
            {"role": "assistant", "content": "raw final", "round_id": round_id},
        ])
        return "raw final"

    async def fake_chat_filter(text, soul_context=""):
        return "styled final"

    monkeypatch.setattr(agent, "_run_main_agent", fake_run_main_agent)
    monkeypatch.setattr(agent, "_run_chat_filter", fake_chat_filter)

    result = await agent._run_chat_agent("hi", None, 0, "db.sqlite3")
    saved = json.loads(agent.STATE_FILE.read_text(encoding="utf-8"))["messages"]

    assert result == "styled final"
    assert saved[-1]["role"] == "assistant"
    assert saved[-1]["content"] == "styled final"


async def test_run_chat_agent_appends_visible_reply_when_internal_trace_has_no_final_message(monkeypatch, tmp_path):
    from cyrene import agent
    from cyrene import soul

    monkeypatch.setattr(agent, "STATE_FILE", tmp_path / "state.json")
    monkeypatch.setattr(agent, "DATA_DIR", tmp_path)
    monkeypatch.setattr(agent, "get_context", lambda max_chars=5000: "")
    monkeypatch.setattr(soul, "read_shallow_memory", lambda: "")

    async def fake_run_main_agent(user_message, history, bot, chat_id, db_path):
        round_id = agent._current_round_id.get()
        await agent._save_session_messages([
            {"role": "user", "content": user_message, "round_id": round_id},
            {
                "role": "assistant",
                "content": "",
                "round_id": round_id,
                "tool_calls": [{"id": "s1", "function": {"name": "spawn_subagent", "arguments": "{}"}}],
            },
        ])
        return "[Sub-agents are still working in the background. You can continue the conversation.]"

    monkeypatch.setattr(agent, "_run_main_agent", fake_run_main_agent)
    monkeypatch.setattr(agent, "_run_chat_filter", lambda text, soul_context="": asyncio.sleep(0, result=text))

    result = await agent._run_chat_agent("keep going", None, 0, "db.sqlite3")
    saved = json.loads(agent.STATE_FILE.read_text(encoding="utf-8"))["messages"]

    assert result.startswith("[Sub-agents are still working in the background.")
    assert saved[-1]["role"] == "assistant"
    assert saved[-1]["content"] == result


async def test_tool_bash_returns_early_when_interrupted(monkeypatch):
    from cyrene import agent
    from cyrene import tools

    interrupt_event = asyncio.Event()
    monkeypatch.setattr(agent, "_interrupt_event", interrupt_event)

    task = asyncio.create_task(tools._tool_bash(
        {"command": "sleep 30", "timeout_ms": 60000},
        None,
        0,
        "db.sqlite3",
        None,
    ))
    await asyncio.sleep(0.2)
    interrupt_event.set()

    payload = json.loads(await task)

    assert payload["exit_code"] == -1
    assert "interrupted" in payload["stderr"].lower()


async def test_run_main_agent_returns_background_notice_when_monitoring_is_interrupted(monkeypatch):
    from cyrene import agent
    from cyrene import inbox
    from cyrene import subagent

    interrupt_event = asyncio.Event()
    monkeypatch.setattr(agent, "_interrupt_event", interrupt_event)

    responses = iter([
        {
            "content": "",
            "tool_calls": [{"id": "u1", "function": {"name": "use_tools", "arguments": '{"task":"check"}'}}],
        },
        {
            "content": "",
            "tool_calls": [{"id": "s1", "function": {"name": "spawn_subagent", "arguments": '{"agent_id":"alice","task":"research"}'}}],
        },
    ])
    saved = []

    async def fake_call_llm(messages, tools=None):
        return next(responses)

    async def fake_execute_tool(name, args, bot, chat_id, db_path, notify_state):
        return "spawned"

    async def fake_save(messages):
        saved.append(messages)

    async def fake_snapshot():
        return {"alice": {"status": "running", "task": "research"}}

    monkeypatch.setattr(agent, "_call_llm", fake_call_llm)
    monkeypatch.setattr(agent, "_execute_tool", fake_execute_tool)
    monkeypatch.setattr(agent, "_save_session_messages", fake_save)
    monkeypatch.setattr(subagent, "collect_results", lambda: asyncio.sleep(0, result="summary"))
    monkeypatch.setattr(subagent, "clear", lambda: asyncio.sleep(0))
    monkeypatch.setattr(subagent, "get_snapshot", fake_snapshot)
    monkeypatch.setattr(subagent, "get_raw_messages", lambda aid: asyncio.sleep(0, result=[]))
    monkeypatch.setattr(subagent, "reactivate", lambda aid: asyncio.sleep(0, result=False))
    monkeypatch.setattr(inbox, "get_unread_count", lambda aid: 0)

    task = asyncio.create_task(agent._run_main_agent("check", [], None, 0, "db.sqlite3"))
    await asyncio.sleep(0.1)
    interrupt_event.set()
    result = await task

    assert result == "[Sub-agents are still working in the background. You can continue the conversation.]"
    assert saved


async def test_run_main_agent_retries_invalid_phase1_tool_and_returns_model_explanation(monkeypatch):
    from cyrene import agent

    calls = []
    responses = iter([
        {
            "content": "好的，先看天气。",
            "tool_calls": [
                {"id": "w1", "function": {"name": "WebSearch", "arguments": '{"query":"Toronto weather today"}'}},
            ],
        },
        {
            "content": "当前阶段没有合适工具，请改用 use_tools 进入完整工具阶段。",
            "tool_calls": [],
        },
    ])
    saved = []

    async def fake_call_llm(messages, tools=None):
        calls.append(tools)
        return next(responses)

    async def fake_save(messages):
        saved.append(messages)

    monkeypatch.setattr(agent, "_call_llm", fake_call_llm)
    monkeypatch.setattr(agent, "_save_session_messages", fake_save)

    result = await agent._run_main_agent("现在先看看多伦多的天气", [], None, 0, "db.sqlite3")

    assert result == "当前阶段没有合适工具，请改用 use_tools 进入完整工具阶段。"
    assert calls[0] is agent._LIGHT_TOOL_DEFS
    assert calls[1] is agent._LIGHT_TOOL_DEFS
    assert saved


async def test_refresh_session_labels_persists_titles(monkeypatch, tmp_path):
    from cyrene import agent

    monkeypatch.setattr(agent, "STATE_FILE", tmp_path / "state.json")
    monkeypatch.setattr(agent, "DATA_DIR", tmp_path)

    await agent._save_session_messages([
        {"role": "user", "content": "讨论加密货币辩论结构", "round_id": "round_1"},
        {"role": "assistant", "content": "ok", "round_id": "round_1"},
    ])

    async def fake_call_llm(messages, tools=None):
        return {"content": '{"round_title":"加密货币辩论","session_title":"加密货币多代理讨论"}'}

    monkeypatch.setattr(agent, "_call_llm", fake_call_llm)

    await agent._refresh_session_labels("讨论加密货币辩论结构", "round_1")
    state = json.loads(agent.STATE_FILE.read_text(encoding="utf-8"))
    labels = agent.get_session_labels()

    assert state["session_title"] == "加密货币多代理讨论"
    assert all(msg.get("round_title") == "加密货币辩论" for msg in state["messages"] if msg.get("round_id") == "round_1")
    assert labels["round_title"] == "加密货币辩论"
    assert labels["session_title"] == "加密货币多代理讨论"

    await agent._save_session_messages(state["messages"])
    preserved = json.loads(agent.STATE_FILE.read_text(encoding="utf-8"))
    assert preserved["session_title"] == "加密货币多代理讨论"


def test_build_current_session_uses_saved_session_and_round_titles(tmp_path, monkeypatch):
    from webui import routes

    monkeypatch.setattr(routes, "STATE_FILE", tmp_path / "state.json")
    monkeypatch.setattr(routes, "DATA_DIR", tmp_path)

    routes.STATE_FILE.write_text(
        json.dumps({
            "session_title": "加密货币多代理讨论",
            "messages": [
                {"role": "user", "content": "讨论加密货币辩论结构", "round_id": "round_1", "round_title": "加密货币辩论"},
                {"role": "assistant", "content": "ok", "round_id": "round_1", "round_title": "加密货币辩论"},
            ],
        }, ensure_ascii=False),
        encoding="utf-8",
    )

    session = routes._build_current_session()
    user_node = next(node for node in session["flow"]["nodes"] if node["kind"] == "input")

    assert session["title"] == "加密货币多代理讨论"
    assert session["currentRoundId"] == "round_1"
    assert session["currentRoundTitle"] == "加密货币辩论"
    assert user_node["title"] == "加密货币辩论"


def test_build_current_session_uses_latest_round_id_for_chat_sidebar(tmp_path, monkeypatch):
    from webui import routes

    monkeypatch.setattr(routes, "STATE_FILE", tmp_path / "state.json")
    monkeypatch.setattr(routes, "DATA_DIR", tmp_path)

    routes.STATE_FILE.write_text(
        json.dumps({
            "messages": [
                {"role": "user", "content": "first round", "round_id": "round_1", "round_title": "第一轮"},
                {"role": "assistant", "content": "done", "round_id": "round_1", "round_title": "第一轮"},
                {"role": "user", "content": "second round", "round_id": "round_2", "round_title": "第二轮"},
            ],
        }, ensure_ascii=False),
        encoding="utf-8",
    )

    session = routes._build_current_session()

    assert session["currentRoundId"] == "round_2"
    assert session["currentRoundTitle"] == "第二轮"


def test_build_archive_sessions_reads_titles_and_splits_rounds(tmp_path, monkeypatch):
    from webui import routes

    monkeypatch.setattr(routes, "CONVERSATIONS_DIR", tmp_path / "conversations")
    monkeypatch.setattr(routes, "STATE_FILE", tmp_path / "state.json")
    routes.CONVERSATIONS_DIR.mkdir(parents=True, exist_ok=True)
    routes.STATE_FILE.write_text('{"messages":[]}', encoding="utf-8")

    date_str = "2026-05-15"
    (routes.CONVERSATIONS_DIR / f"{date_str}.md").write_text(
        "# Conversations - 2026-05-15\n\n"
        "<!-- session_title: 加密货币多代理讨论 -->\n\n"
        "## 08:00:00 UTC\n\n"
        "<!-- round_id: round_a -->\n"
        "<!-- round_title: 设计辩论角色 -->\n\n"
        "**User**: 先设计角色\n\n"
        "**Ape**: 好\n\n"
        "---\n\n"
        "## 08:05:00 UTC\n\n"
        "<!-- round_id: round_b -->\n"
        "<!-- round_title: 让双方开始辩论 -->\n\n"
        "**User**: 现在开始辩论\n\n"
        "**Ape**: 开始\n\n"
        "---\n",
        encoding="utf-8",
    )

    sessions = routes._build_archive_sessions()
    flow = sessions[0]["flow"]
    input_titles = [node["title"] for node in flow["nodes"] if node["kind"] == "input"]

    assert sessions[0]["title"] == "加密货币多代理讨论"
    assert input_titles == ["设计辩论角色", "让双方开始辩论"]
