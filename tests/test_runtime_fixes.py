import asyncio
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
