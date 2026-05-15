"""Unit tests for the subagent workflow fixes.

Tests the registry state machine and inbox marking — without hitting the LLM,
so they run fast and deterministically.

Run with: python -m pytest tests/test_subagent_fixes.py -v
"""

import asyncio
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))


async def test_set_waiting_writes_result():
    """Bug 1+2 fix: set_waiting should record the result so a main-agent
    collect during the WAITING phase sees real content, not empty strings."""
    from cyrene import subagent

    await subagent.clear()
    await subagent.register("a1", "task A")
    await subagent.set_waiting("a1", result="A finished with finding X")

    snapshot = await subagent.get_snapshot()
    assert snapshot["a1"]["status"] == "waiting"
    assert "finding X" in snapshot["a1"]["result"], (
        f"Result not persisted, got: {snapshot['a1']['result']!r}"
    )
    print("PASS test_set_waiting_writes_result")


async def test_all_willing_to_quit_unlocks_with_all_waiting():
    """Bug 1 fix: when every agent is in WAITING, the new helper should
    return True so wait_for_others can release them."""
    from cyrene import subagent

    await subagent.clear()
    await subagent.register("a1", "task A")
    await subagent.register("a2", "task B")
    await subagent.set_waiting("a1", "A done")
    await subagent.set_waiting("a2", "B done")

    assert await subagent.all_willing_to_quit() is True, (
        "all_willing_to_quit should be True when everyone is WAITING"
    )
    # Old all_done would return False — verify so the test guards against regressions.
    assert await subagent.all_done() is False, (
        "Sanity: the old all_done is still strict (requires DONE/TIMEOUT only)"
    )
    print("PASS test_all_willing_to_quit_unlocks_with_all_waiting")


async def test_all_willing_to_quit_blocks_with_one_running():
    """Negative test: if anyone is still RUNNING, we should NOT unlock."""
    from cyrene import subagent

    await subagent.clear()
    await subagent.register("a1", "task A")
    await subagent.register("a2", "task B")
    await subagent.set_waiting("a1", "A done")
    # a2 stays RUNNING

    assert await subagent.all_willing_to_quit() is False
    print("PASS test_all_willing_to_quit_blocks_with_one_running")


async def test_wait_for_others_returns_when_others_waiting():
    """End-to-end mini test of the deadlock fix.

    Two subagents both call wait_for_others. With the old code they both
    deadlock until 600s timeout; with the fix, both return "" quickly.
    """
    from cyrene import subagent

    await subagent.clear()
    await subagent.register("a1", "task A")
    await subagent.register("a2", "task B")

    def empty_inbox(_aid: str) -> str:
        return ""

    # Race both wait_for_others calls. We pass a short max_wait so a regression
    # (deadlock) would be visible as a timeout.
    results = await asyncio.gather(
        subagent.wait_for_others("a1", empty_inbox, max_wait=30, result="A done"),
        subagent.wait_for_others("a2", empty_inbox, max_wait=30, result="B done"),
    )
    assert results == ["", ""], (
        f"Expected both to unlock cleanly, got {results}. "
        f"A 'timeout' here means the deadlock fix regressed."
    )

    # And the results must have been persisted in registry.
    snap = await subagent.get_snapshot()
    assert "A done" in snap["a1"]["result"]
    assert "B done" in snap["a2"]["result"]
    print("PASS test_wait_for_others_returns_when_others_waiting")


def test_inbox_mark_all_read():
    """Bug 3 fix: mark_all_read should reset the unread counter so subsequent
    get_inbox_context calls don't re-inject old messages."""
    from cyrene import inbox

    with tempfile.TemporaryDirectory() as tmp:
        # Redirect inbox dir for the test
        inbox.INBOX_DIR = Path(tmp) / "inbox"

        inbox.send_message("sender", "receiver", "chat", "hello")
        inbox.send_message("sender", "receiver", "chat", "world")
        assert inbox.get_unread_count("receiver") == 2

        # Inject — but in real flow, we don't auto-mark. Verify mark_all_read does.
        ctx = inbox.get_inbox_context("receiver")
        assert "hello" in ctx and "world" in ctx
        assert inbox.get_unread_count("receiver") == 2  # still unread

        inbox.mark_all_read("receiver")
        assert inbox.get_unread_count("receiver") == 0

        # Next injection returns empty — old messages don't pollute context
        assert inbox.get_inbox_context("receiver") == ""

        # But the message files are kept as a log
        msgs = inbox.read_messages("receiver", mark_read=False)
        assert len(msgs) == 2
    print("PASS test_inbox_mark_all_read")


async def test_can_receive_allows_done_agents():
    """DONE/TIMEOUT 的 agent 仍然可以接收消息（会被主 agent 监控循环唤醒）。"""
    from cyrene import subagent

    await subagent.clear()
    await subagent.register("a1", "task A")
    await subagent.mark_done("a1", "A done")

    assert await subagent.can_receive("a1") is True, (
        "can_receive should accept DONE agents — they get woken up on new messages"
    )
    # Unregistered agents still rejected
    assert await subagent.can_receive("nonexistent") is False
    print("PASS test_can_receive_allows_done_agents")


async def test_reactivate_dormant_agent():
    """reactivate() 把 DONE 改回 RESUMED；对 RUNNING 的 agent 无效。"""
    from cyrene import subagent

    await subagent.clear()
    await subagent.register("a1", "task A")
    await subagent.mark_done("a1", "first result")

    ok = await subagent.reactivate("a1")
    assert ok is True
    assert await subagent.get_status("a1") == "resumed"

    # Try reactivating a RUNNING agent — should be a no-op
    await subagent.register("a2", "task B")
    ok2 = await subagent.reactivate("a2")
    assert ok2 is False, "reactivate should not touch RUNNING agents"
    print("PASS test_reactivate_dormant_agent")


async def test_mark_done_accumulates_result():
    """被唤醒的 agent 第二次 mark_done 时，result 应当追加而不是覆盖。"""
    from cyrene import subagent

    await subagent.clear()
    await subagent.register("a1", "task A")
    await subagent.mark_done("a1", "initial finding: X")

    # Simulated wake-up cycle
    await subagent.reactivate("a1")
    await subagent.mark_done("a1", "follow-up: Y")

    snap = await subagent.get_snapshot()
    assert "initial finding: X" in snap["a1"]["result"], (
        f"Initial result lost! Got: {snap['a1']['result']!r}"
    )
    assert "follow-up: Y" in snap["a1"]["result"], (
        f"Follow-up result missing! Got: {snap['a1']['result']!r}"
    )
    print("PASS test_mark_done_accumulates_result")


async def test_get_raw_messages_returns_full_history():
    """get_raw_messages 应该返回完整原始消息（含 system 和 tool_calls），
    用于唤醒时续跑。"""
    from cyrene import subagent

    await subagent.clear()
    await subagent.register("a1", "task A")
    full_msgs = [
        {"role": "system", "content": "long system prompt " * 50},
        {"role": "user", "content": "do the task"},
        {"role": "assistant", "content": "ok", "tool_calls": [
            {"id": "c1", "function": {"name": "search", "arguments": '{"q":"x"}'}}
        ]},
        {"role": "tool", "tool_call_id": "c1", "content": "results"},
    ]
    await subagent.save_messages("a1", full_msgs)
    raw = await subagent.get_raw_messages("a1")
    assert len(raw) == 4
    assert "long system prompt" in raw[0]["content"]
    # system content NOT trimmed (unlike get_snapshot which trims to 200 chars)
    assert len(raw[0]["content"]) > 200
    # tool_calls preserved with arguments (snapshot strips arguments)
    assert raw[2]["tool_calls"][0]["function"]["arguments"] == '{"q":"x"}'
    print("PASS test_get_raw_messages_returns_full_history")


async def main():
    await test_set_waiting_writes_result()
    await test_all_willing_to_quit_unlocks_with_all_waiting()
    await test_all_willing_to_quit_blocks_with_one_running()
    await test_wait_for_others_returns_when_others_waiting()
    test_inbox_mark_all_read()
    await test_can_receive_allows_done_agents()
    await test_reactivate_dormant_agent()
    await test_mark_done_accumulates_result()
    await test_get_raw_messages_returns_full_history()
    print("\nAll 9 tests passed.")


if __name__ == "__main__":
    asyncio.run(main())
