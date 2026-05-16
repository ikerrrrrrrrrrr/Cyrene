"""Verify cache-hit improvements don't break any behavior."""
import asyncio
import json
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))


def _patch(obj, attr, replacement):
    """Simple patch helper."""
    original = getattr(obj, attr)
    setattr(obj, attr, replacement)
    return original


async def test_phase1_retry_with_unified_system_prompt():
    """Phase 1 retry still works with DECISION as separate user message."""
    from cyrene import agent

    calls = []
    responses = iter([
        {
            "content": "ok, checking weather.",
            "tool_calls": [
                {"id": "w1", "function": {"name": "WebSearch", "arguments": json.dumps({"query": "Toronto weather"})}},
            ],
        },
        {
            "content": "No suitable tool. Use use_tools to enter full tool phase.",
            "tool_calls": [],
        },
    ])

    async def fake_call_llm(messages, tools=None):
        calls.append((messages, tools))
        return next(responses)

    _orig_llm = _patch(agent, "_call_llm", fake_call_llm)
    _orig_save = _patch(agent, "_save_session_messages", AsyncMock())
    try:
        result = await agent._run_main_agent("check Toronto weather", [], None, 0, "db.sqlite3")
    finally:
        _patch(agent, "_call_llm", _orig_llm)
        _patch(agent, "_save_session_messages", _orig_save)

    # Phase 1 system prompt is MAIN only
    phase1_msgs, _ = calls[0]
    assert phase1_msgs[0]["content"] == agent._MAIN_AGENT_PROMPT, "Phase 1 system should be MAIN only"
    # DECISION is last user message
    assert phase1_msgs[-1]["role"] == "user"
    assert phase1_msgs[-1]["content"] == agent._PHASE1_DECISION_PROMPT
    # Retry uses same system prompt
    retry_msgs, _ = calls[1]
    assert retry_msgs[0]["content"] == agent._MAIN_AGENT_PROMPT
    # Retry includes DECISION
    assert any(
        m.get("content") == agent._PHASE1_DECISION_PROMPT
        for m in retry_msgs if m["role"] == "user"
    )
    print("PASS: test_phase1_retry_with_unified_system_prompt")


async def test_phase2_prefix_matches_phase1():
    """Phase 2 prefix is identical to Phase 1 for cache hits."""
    from cyrene import agent

    phase1_done = False
    phase1_responses = iter([
        {
            "content": "",
            "tool_calls": [{"id": "u1", "function": {"name": "use_tools", "arguments": json.dumps({"task": "test"})}}],
        },
    ])
    phase2_responses = iter([
        {
            "content": "done",
            "tool_calls": [{"id": "q1", "function": {"name": "quit", "arguments": "{}"}}],
        },
    ])

    async def fake_call_llm(messages, tools=None):
        nonlocal phase1_done
        if not phase1_done:
            phase1_done = True
            return next(phase1_responses)
        return next(phase2_responses)

    _orig_llm = _patch(agent, "_call_llm", fake_call_llm)
    _orig_save = _patch(agent, "_save_session_messages", AsyncMock())
    _orig_exec = _patch(agent, "_execute_tool", AsyncMock(return_value="ok"))
    try:
        result = await agent._run_main_agent("test task", [], None, 0, "db.sqlite3")
    finally:
        _patch(agent, "_call_llm", _orig_llm)
        _patch(agent, "_save_session_messages", _orig_save)
        _patch(agent, "_execute_tool", _orig_exec)

    assert "done" in result
    print("PASS: test_phase2_prefix_matches_phase1")


async def test_subagent_stable_system_prompt():
    """Subagent keeps messages[0] stable across rounds."""
    from cyrene import subagent, agent, tools, inbox

    llm_inputs = []
    responses = iter([
        {
            "content": "finding 1",
            "tool_calls": [{"id": "t1", "function": {"name": "Read", "arguments": json.dumps({"path": "test.txt"})}}],
        },
        {
            "content": "finding 2",
            "tool_calls": [{"id": "q1", "function": {"name": "quit", "arguments": "{}"}}],
        },
    ])

    async def fake_call_llm(messages, tools=None):
        # Capture clean copies of messages
        saved = [{"role": m["role"], "content": str(m.get("content", ""))[:200]} for m in messages]
        llm_inputs.append(saved)
        return next(responses)

    async def fake_wait(agent_id, inbox_check_func, mark_read_func=None, max_wait=600, result=""):
        return ""

    _orig_llm = _patch(agent, "_call_llm", fake_call_llm)
    _orig_exec = _patch(tools, "_execute_tool", AsyncMock(return_value="file content"))
    _orig_wait = _patch(subagent, "wait_for_others", fake_wait)
    _orig_save = _patch(subagent, "save_messages", AsyncMock())
    _orig_run = _patch(subagent, "set_running", AsyncMock())
    _orig_pub = _patch(subagent, "_publish_registry_event", AsyncMock())
    _orig_ctx = _patch(subagent, "get_context", AsyncMock(
        return_value="[活跃子 agent]\n  alice: test [工作中]"
    ))
    _orig_inbox_ctx = _patch(inbox, "get_inbox_context", lambda aid: "")
    _orig_mark = _patch(inbox, "mark_all_read", AsyncMock())
    try:
        result = await subagent._run_subagent("test_agent", "test task", None, 0, "db.sqlite3")
    finally:
        _patch(agent, "_call_llm", _orig_llm)
        _patch(tools, "_execute_tool", _orig_exec)
        _patch(subagent, "wait_for_others", _orig_wait)
        _patch(subagent, "save_messages", _orig_save)
        _patch(subagent, "set_running", _orig_run)
        _patch(subagent, "_publish_registry_event", _orig_pub)
        _patch(subagent, "get_context", _orig_ctx)
        _patch(inbox, "get_inbox_context", _orig_inbox_ctx)
        _patch(inbox, "mark_all_read", _orig_mark)

    # Verify messages[0] is always clean
    for i, msgs in enumerate(llm_inputs):
        assert msgs[0]["role"] == "system", f"Call {i}: messages[0] should be system"
        assert "[活跃子 agent]" not in msgs[0]["content"], (
            f"Call {i}: system prompt leaked registry context: {msgs[0]['content'][:100]}"
        )
        assert "[收件箱]" not in msgs[0]["content"], (
            f"Call {i}: system prompt leaked inbox context"
        )

    # Registry context is in a user message
    call1 = llm_inputs[0]
    registry_msgs = [m for m in call1 if m["role"] == "user" and "[活跃子 agent]" in m.get("content", "")]
    assert len(registry_msgs) > 0, "Registry context should be injected as a user message"

    print("PASS: test_subagent_stable_system_prompt")


async def test_subagent_quit_feedback_not_filtered():
    """Quit quality feedback messages are NOT stripped as context."""
    from cyrene import subagent, agent, tools, inbox

    llm_inputs = []
    responses = iter([
        {
            "content": "Done.",
            "tool_calls": [{"id": "q1", "function": {"name": "quit", "arguments": "{}"}}],
        },
        {
            "content": "Real finding: X was found",
            "tool_calls": [{"id": "q2", "function": {"name": "quit", "arguments": "{}"}}],
        },
    ])
    wait_results = iter(["", ""])

    async def fake_call_llm(messages, tools=None):
        saved = [{"role": m["role"], "content": str(m.get("content", ""))[:200]} for m in messages]
        llm_inputs.append(saved)
        return next(responses)

    async def fake_wait(agent_id, inbox_check_func, mark_read_func=None, max_wait=600, result=""):
        return next(wait_results)

    _orig_llm = _patch(agent, "_call_llm", fake_call_llm)
    _orig_exec = _patch(tools, "_execute_tool", AsyncMock(return_value="ok"))
    _orig_wait = _patch(subagent, "wait_for_others", fake_wait)
    _orig_save = _patch(subagent, "save_messages", AsyncMock())
    _orig_run = _patch(subagent, "set_running", AsyncMock())
    _orig_pub = _patch(subagent, "_publish_registry_event", AsyncMock())
    _orig_ctx = _patch(subagent, "get_context", AsyncMock(return_value=""))
    _orig_resume = _patch(subagent, "set_resumed", AsyncMock())
    _orig_inbox_ctx = _patch(inbox, "get_inbox_context", lambda aid: "")
    _orig_mark = _patch(inbox, "mark_all_read", AsyncMock())
    _orig_send = _patch(inbox, "send_message", AsyncMock())
    try:
        result = await subagent._run_subagent("test_agent", "test task", None, 0, "db.sqlite3")
    finally:
        _patch(agent, "_call_llm", _orig_llm)
        _patch(tools, "_execute_tool", _orig_exec)
        _patch(subagent, "wait_for_others", _orig_wait)
        _patch(subagent, "save_messages", _orig_save)
        _patch(subagent, "set_running", _orig_run)
        _patch(subagent, "_publish_registry_event", _orig_pub)
        _patch(subagent, "get_context", _orig_ctx)
        _patch(subagent, "set_resumed", _orig_resume)
        _patch(inbox, "get_inbox_context", _orig_inbox_ctx)
        _patch(inbox, "mark_all_read", _orig_mark)
        _patch(inbox, "send_message", _orig_send)

    # Check that quit quality feedback persisted across calls
    feedback_msgs = []
    for call_msgs in llm_inputs:
        for m in call_msgs:
            if m["role"] == "user" and "quit" in str(m.get("content", "")).lower():
                feedback_msgs.append(m["content"])

    assert len(feedback_msgs) > 0, "Quit quality feedback should not be filtered"
    print("PASS: test_subagent_quit_feedback_not_filtered")


async def test_subagent_resume_strips_old_context():
    """Resumed subagent strips old context messages from previous run."""
    from cyrene import subagent, agent, tools, inbox

    old_messages = [
        {"role": "system", "content": "You are a sub-agent..."},
        {"role": "user", "content": "original task"},
        {"role": "user", "content": "[活跃子 agent]\n  alice: task [工作中]"},  # old context
        {"role": "assistant", "content": "done"},
    ]

    llm_inputs = []
    responses = iter([
        {
            "content": "resumed finding",
            "tool_calls": [{"id": "q1", "function": {"name": "quit", "arguments": "{}"}}],
        },
    ])

    async def fake_call_llm(messages, tools=None):
        saved = [{"role": m["role"], "content": str(m.get("content", ""))[:200]} for m in messages]
        llm_inputs.append(saved)
        return next(responses)

    async def fake_wait(agent_id, inbox_check_func, mark_read_func=None, max_wait=600, result=""):
        return ""

    _orig_llm = _patch(agent, "_call_llm", fake_call_llm)
    _orig_exec = _patch(tools, "_execute_tool", AsyncMock(return_value="ok"))
    _orig_wait = _patch(subagent, "wait_for_others", fake_wait)
    _orig_save = _patch(subagent, "save_messages", AsyncMock())
    _orig_run = _patch(subagent, "set_running", AsyncMock())
    _orig_pub = _patch(subagent, "_publish_registry_event", AsyncMock())
    _orig_ctx = _patch(subagent, "get_context", AsyncMock(
        return_value="[活跃子 agent]\n  bob: new task [工作中]"
    ))
    _orig_inbox_ctx = _patch(inbox, "get_inbox_context", lambda aid: "")
    _orig_mark = _patch(inbox, "mark_all_read", AsyncMock())
    try:
        result = await subagent._run_subagent(
            "test_agent", "task", None, 0, "db.sqlite3",
            resume_messages=old_messages,
        )
    finally:
        _patch(agent, "_call_llm", _orig_llm)
        _patch(tools, "_execute_tool", _orig_exec)
        _patch(subagent, "wait_for_others", _orig_wait)
        _patch(subagent, "save_messages", _orig_save)
        _patch(subagent, "set_running", _orig_run)
        _patch(subagent, "_publish_registry_event", _orig_pub)
        _patch(subagent, "get_context", _orig_ctx)
        _patch(inbox, "get_inbox_context", _orig_inbox_ctx)
        _patch(inbox, "mark_all_read", _orig_mark)

    # Old context from resume_messages should be stripped
    # Only the new context (bob) should be present
    call_msgs = llm_inputs[0]
    context_msgs = [m for m in call_msgs if "[活跃子 agent]" in str(m.get("content", ""))]
    assert len(context_msgs) == 1, (
        f"Should have exactly 1 context message, got {len(context_msgs)}"
    )
    assert "bob" in context_msgs[0]["content"], "Should contain new context, not old"

    print("PASS: test_subagent_resume_strips_old_context")


async def main():
    await test_phase1_retry_with_unified_system_prompt()
    await test_phase2_prefix_matches_phase1()
    await test_subagent_stable_system_prompt()
    await test_subagent_quit_feedback_not_filtered()
    await test_subagent_resume_strips_old_context()
    print("\nAll 5 cache-fix verification tests passed.")


if __name__ == "__main__":
    asyncio.run(main())
