import json
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

sys.modules.setdefault("PIL", MagicMock())
sys.modules["PIL"].Image = MagicMock()
sys.modules.setdefault("pypdf", MagicMock())

from cyrene.agent.commands import DEEP_REFLECT_COMMAND_ID, parse_deep_reflect_command
from cyrene.agent.deep_reflection import (
    build_reflection_evidence,
    make_reflection_record,
    project_history_for_llm,
    run_clean_reflection,
    serialize_evidence,
)


def _text_blob(messages: list[dict]) -> str:
    parts = []
    for message in messages:
        parts.append(str(message.get("content") or ""))
        for tool_call in message.get("tool_calls") or []:
            parts.append(str((tool_call.get("function") or {}).get("arguments") or ""))
    return "\n".join(parts)


def test_parse_deep_reflect_slash_commands() -> None:
    parsed = parse_deep_reflect_command("/deep-reflect focus on the goal")
    assert parsed["matched"] is True
    assert parsed["command"] == DEEP_REFLECT_COMMAND_ID
    assert parsed["focus"] == "focus on the goal"

    parsed_cn = parse_deep_reflect_command("/深度反思 聚焦用户要求")
    assert parsed_cn["matched"] is True
    assert parsed_cn["focus"] == "聚焦用户要求"

    parsed_multiline = parse_deep_reflect_command("/deep-reflect\nfocus on the goal")
    assert parsed_multiline["matched"] is True
    assert parsed_multiline["focus"] == "focus on the goal"

    assert parse_deep_reflect_command("/deep-research topic")["matched"] is False


def test_evidence_serialization_is_deterministic_and_redacts_args() -> None:
    messages = [
        {"role": "user", "message_id": "u1", "content": "Need the report to cite sources."},
        {
            "role": "assistant",
            "message_id": "a1",
            "content": "Trying a write path.",
            "tool_calls": [
                {
                    "id": "c1",
                    "function": {
                        "name": "Write",
                        "arguments": json.dumps({
                            "path": "out.md",
                            "content": "very long generated content",
                            "api_key": "supersecret",
                        }),
                    },
                }
            ],
        },
        {"role": "tool", "message_id": "t1", "tool_call_id": "c1", "content": "Tool failed: bad output"},
    ]

    evidence_a = build_reflection_evidence(messages, goal_gap="No citations were produced.")
    evidence_b = build_reflection_evidence(messages, goal_gap="No citations were produced.")

    assert serialize_evidence(evidence_a) == serialize_evidence(evidence_b)
    serialized = serialize_evidence(evidence_a)
    assert "supersecret" not in serialized
    assert "[REDACTED]" in serialized
    assert "very long generated content" not in serialized
    assert "chars redacted" in serialized


def test_projection_suppresses_failure_episode_but_preserves_visible_messages() -> None:
    messages = [
        {"role": "user", "message_id": "u1", "content": "Please finish the task."},
        {
            "role": "assistant",
            "message_id": "a1",
            "content": "I tried the wrong direction.",
            "tool_calls": [{"id": "c1", "function": {"name": "Bash", "arguments": '{"cmd":"bad"}'}}],
        },
        {"role": "tool", "message_id": "t1", "tool_call_id": "c1", "content": "BAD FAILURE DETAIL"},
    ]
    packet = {
        "schema": "cyrene.deep_reflection.v1",
        "objective": "Please finish the task.",
        "user_requirements": ["finish the task"],
        "goal_gap": "The attempted direction did not satisfy the task.",
        "current_state": "Prior attempt failed.",
        "compressed_attempts": [{"attempt": "Tried bad command.", "why_bad_for_goal": "Did not help.", "tools": [{"name": "Bash", "args": {"cmd": "bad"}}]}],
        "excluded_paths": ["Do not retry the bad command as-is."],
        "tools_used": [{"name": "Bash", "args": {"cmd": "bad"}}],
        "promising_directions": ["Use a different approach."],
        "next_step": "Continue with a new approach.",
        "open_questions": [],
    }
    record = make_reflection_record(packet, source_message_ids=["u1", "a1"], source_round_ids=[])
    visible = [*messages, record]

    projected = project_history_for_llm(visible)

    assert "BAD FAILURE DETAIL" in _text_blob(visible)
    assert "BAD FAILURE DETAIL" not in _text_blob(projected)
    assert "I tried the wrong direction" not in _text_blob(projected)
    assert "[Deep reflection packet]" in _text_blob(projected)
    assert not any(message.get("role") == "tool" and message.get("tool_call_id") == "c1" for message in projected)


@pytest.mark.asyncio
async def test_clean_reflection_falls_back_when_worker_returns_invalid_json(monkeypatch) -> None:
    import cyrene.agent.deep_reflection as deep_reflection

    evidence = build_reflection_evidence(
        [
            {"role": "user", "message_id": "u1", "content": "Fix the broken export."},
            {
                "role": "assistant",
                "message_id": "a1",
                "content": "I tried the old export path.",
                "tool_calls": [{"id": "c1", "function": {"name": "Bash", "arguments": '{"cmd":"old-export"}'}}],
            },
            {"role": "tool", "message_id": "t1", "tool_call_id": "c1", "content": "Tool failed: export not found"},
        ],
        goal_gap="The export is still broken.",
        focus="Find the current export path.",
    )

    call_kwargs: dict = {}

    async def fake_clean_llm(messages, tools=None, max_tokens=1800, secondary=True, **kwargs):
        call_kwargs.update(kwargs)
        return {"content": "I should reflect, but this is not JSON.", "usage": {"total_tokens": 3}, "model": "fake-secondary"}

    monkeypatch.setattr(deep_reflection, "_call_llm", fake_clean_llm)

    packet, usage = await run_clean_reflection(evidence)

    assert packet["schema"] == "cyrene.deep_reflection.v1"
    assert packet["objective"] == "Fix the broken export."
    assert packet["goal_gap"] == "The export is still broken."
    assert "Find the current export path." in packet["next_step"]
    assert packet["compressed_attempts"]
    assert usage["total_tokens"] == 3
    assert call_kwargs["thinking"] == "disabled"


@pytest.mark.asyncio
async def test_deep_reflect_tool_projects_next_llm_call_without_cleaning_saved_transcript(monkeypatch) -> None:
    import cyrene.agent.agent as agent_core
    import cyrene.agent.deep_reflection as deep_reflection
    import cyrene.behavior_learning as behavior_learning

    history = [
        {"role": "user", "message_id": "u_old", "content": "Original hard goal"},
        {
            "role": "assistant",
            "message_id": "a_old",
            "content": "I used the failed approach.",
            "tool_calls": [{"id": "old_call", "function": {"name": "Bash", "arguments": '{"cmd":"bad"}'}}],
        },
        {"role": "tool", "message_id": "t_old", "tool_call_id": "old_call", "content": "BAD FAILURE DETAIL"},
    ]

    clean_packet = {
        "schema": "cyrene.deep_reflection.v1",
        "objective": "Original hard goal",
        "user_requirements": ["Original hard goal"],
        "goal_gap": "The work has not met the user's goal.",
        "current_state": "Failure details compressed.",
        "compressed_attempts": [],
        "excluded_paths": ["Do not repeat the failed approach."],
        "tools_used": [],
        "promising_directions": ["Try a better path."],
        "next_step": "Continue from the better path.",
        "open_questions": [],
    }

    async def fake_clean_llm(messages, tools=None, max_tokens=1800, secondary=True, **kwargs):
        return {"content": json.dumps(clean_packet), "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}}

    llm_inputs: list[list[dict]] = []
    responses = iter([
        {"content": "", "tool_calls": [{"id": "use1", "function": {"name": "use_tools", "arguments": '{"task":"continue"}'}}]},
        {
            "content": "",
            "tool_calls": [
                {
                    "id": "reflect1",
                    "function": {
                        "name": "DeepReflect",
                        "arguments": json.dumps({"goal_gap": "The work has not met the user's goal."}),
                    },
                }
            ],
        },
        {"content": "continued", "tool_calls": [{"id": "quit1", "function": {"name": "quit", "arguments": "{}"}}]},
    ])

    async def fake_main_llm(messages, tools=None, max_tokens=32000, **kwargs):
        llm_inputs.append(messages)
        return next(responses)

    save_calls: list[list[dict]] = []

    async def fake_save(messages):
        save_calls.append(messages)

    monkeypatch.setattr(agent_core, "_call_llm", fake_main_llm)
    monkeypatch.setattr(agent_core, "_save_session_messages", fake_save)
    monkeypatch.setattr(agent_core, "_publish_runtime_event", AsyncMock())
    monkeypatch.setattr(deep_reflection, "_call_llm", fake_clean_llm)
    monkeypatch.setattr(deep_reflection, "_publish_runtime_event", AsyncMock())
    monkeypatch.setattr(behavior_learning, "try_route_and_execute_skill", AsyncMock(return_value=None))

    result = await agent_core._run_main_agent("continue", history, None, 0, "db.sqlite3")

    assert result == "continued"
    assert len(llm_inputs) >= 3
    next_after_reflection = _text_blob(llm_inputs[-1])
    assert "BAD FAILURE DETAIL" not in next_after_reflection
    assert "[Deep reflection packet]" in next_after_reflection
    assert any("BAD FAILURE DETAIL" in _text_blob(call) for call in save_calls)


@pytest.mark.asyncio
async def test_deep_reflect_mixed_tool_turn_compresses_same_turn_tool_results(monkeypatch) -> None:
    import cyrene.agent.agent as agent_core
    import cyrene.agent.deep_reflection as deep_reflection
    import cyrene.behavior_learning as behavior_learning

    clean_packet = {
        "schema": "cyrene.deep_reflection.v1",
        "objective": "Fix the issue",
        "user_requirements": ["Fix the issue"],
        "goal_gap": "The same-turn tool attempt failed.",
        "current_state": "Failure details compressed.",
        "compressed_attempts": [],
        "excluded_paths": ["Do not repeat the failed command."],
        "tools_used": [],
        "promising_directions": ["Try a different command."],
        "next_step": "Continue from the different command.",
        "open_questions": [],
    }

    async def fake_clean_llm(messages, tools=None, max_tokens=1800, secondary=True, **kwargs):
        return {"content": json.dumps(clean_packet), "usage": {}}

    llm_inputs: list[list[dict]] = []
    responses = iter([
        {"content": "", "tool_calls": [{"id": "use1", "function": {"name": "use_tools", "arguments": '{"task":"fix"}'}}]},
        {
            "content": "",
            "tool_calls": [
                {"id": "bad1", "function": {"name": "NoSuchTool", "arguments": "{}"}},
                {
                    "id": "reflect1",
                    "function": {
                        "name": "DeepReflect",
                        "arguments": json.dumps({"goal_gap": "The same-turn tool attempt failed."}),
                    },
                },
            ],
        },
        {"content": "continued", "tool_calls": [{"id": "quit1", "function": {"name": "quit", "arguments": "{}"}}]},
    ])

    async def fake_main_llm(messages, tools=None, max_tokens=32000, **kwargs):
        llm_inputs.append(messages)
        return next(responses)

    save_calls: list[list[dict]] = []

    async def fake_save(messages):
        save_calls.append(messages)

    monkeypatch.setattr(agent_core, "_call_llm", fake_main_llm)
    monkeypatch.setattr(agent_core, "_save_session_messages", fake_save)
    monkeypatch.setattr(agent_core, "_publish_runtime_event", AsyncMock())
    monkeypatch.setattr(deep_reflection, "_call_llm", fake_clean_llm)
    monkeypatch.setattr(deep_reflection, "_publish_runtime_event", AsyncMock())
    monkeypatch.setattr(behavior_learning, "try_route_and_execute_skill", AsyncMock(return_value=None))

    result = await agent_core._run_main_agent("Fix the issue", [], None, 0, "db.sqlite3")

    assert result == "continued"
    next_after_reflection = _text_blob(llm_inputs[-1])
    assert "Tool failed:" not in next_after_reflection
    assert "NoSuchTool" not in next_after_reflection
    assert "[Deep reflection packet]" in next_after_reflection
    assert any("Tool failed:" in _text_blob(call) for call in save_calls)


@pytest.mark.asyncio
async def test_deep_reflect_mixed_with_quit_preserves_tool_result_pairing(monkeypatch) -> None:
    import cyrene.agent.agent as agent_core
    import cyrene.agent.deep_reflection as deep_reflection
    import cyrene.behavior_learning as behavior_learning

    clean_packet = {
        "schema": "cyrene.deep_reflection.v1",
        "objective": "Fix the issue",
        "user_requirements": ["Fix the issue"],
        "goal_gap": "Reflection was requested before finishing.",
        "current_state": "Failure details compressed.",
        "compressed_attempts": [],
        "excluded_paths": [],
        "tools_used": [],
        "promising_directions": ["Continue after reflection."],
        "next_step": "Continue after reflection.",
        "open_questions": [],
    }

    async def fake_clean_llm(messages, tools=None, max_tokens=1800, secondary=True, **kwargs):
        return {"content": json.dumps(clean_packet), "usage": {}}

    responses = iter([
        {"content": "", "tool_calls": [{"id": "use1", "function": {"name": "use_tools", "arguments": '{"task":"fix"}'}}]},
        {
            "content": "",
            "tool_calls": [
                {
                    "id": "reflect1",
                    "function": {
                        "name": "DeepReflect",
                        "arguments": json.dumps({"goal_gap": "Reflection was requested before finishing."}),
                    },
                },
                {"id": "quit1", "function": {"name": "quit", "arguments": "{}"}},
            ],
        },
        {"content": "continued", "tool_calls": [{"id": "quit2", "function": {"name": "quit", "arguments": "{}"}}]},
    ])

    async def fake_main_llm(messages, tools=None, max_tokens=32000, **kwargs):
        return next(responses)

    save_calls: list[list[dict]] = []

    async def fake_save(messages):
        save_calls.append(messages)

    monkeypatch.setattr(agent_core, "_call_llm", fake_main_llm)
    monkeypatch.setattr(agent_core, "_save_session_messages", fake_save)
    monkeypatch.setattr(agent_core, "_publish_runtime_event", AsyncMock())
    monkeypatch.setattr(deep_reflection, "_call_llm", fake_clean_llm)
    monkeypatch.setattr(deep_reflection, "_publish_runtime_event", AsyncMock())
    monkeypatch.setattr(behavior_learning, "try_route_and_execute_skill", AsyncMock(return_value=None))

    result = await agent_core._run_main_agent("Fix the issue", [], None, 0, "db.sqlite3")

    assert result == "continued"
    saved_after_reflection = save_calls[0]
    tool_result_ids = {
        str(message.get("tool_call_id") or "")
        for message in saved_after_reflection
        if message.get("role") == "tool"
    }
    assert {"reflect1", "quit1"}.issubset(tool_result_ids)
    assert any(message.get("deep_reflection_record") for message in saved_after_reflection)


@pytest.mark.asyncio
async def test_manual_deep_reflect_continues_main_agent_after_record(monkeypatch) -> None:
    from cyrene.agent import agent as agent_core
    from cyrene.agent import coordinator as agent_coordinator
    import cyrene.agent.deep_reflection as deep_reflection

    clean_packet = {
        "schema": "cyrene.deep_reflection.v1",
        "objective": "Finish the original task",
        "user_requirements": ["Finish the original task"],
        "goal_gap": "The user manually requested deep reflection.",
        "current_state": "Prior failure details compressed.",
        "compressed_attempts": [],
        "excluded_paths": ["Do not stop after saying reflection is done."],
        "tools_used": [],
        "promising_directions": ["Continue automatically."],
        "next_step": "Continue automatically.",
        "open_questions": [],
    }

    async def fake_clean_llm(messages, tools=None, max_tokens=1800, secondary=True, **kwargs):
        return {"content": json.dumps(clean_packet), "usage": {}}

    saved_messages: list[list[dict]] = []
    seen: dict[str, object] = {}
    history = [
        {"role": "user", "message_id": "u1", "content": "Finish the original task", "round_id": "round_old"},
        {"role": "assistant", "message_id": "a1", "content": "I got stuck.", "round_id": "round_old"},
    ]

    async def fake_save(messages):
        saved_messages.append(messages)

    async def fake_run_main_agent(user_message, history, bot, chat_id, db_path, system_prompt="", **kwargs):
        seen["user_message"] = user_message
        seen["history"] = history
        seen["persist_user_message"] = kwargs.get("persist_user_message")
        seen["public_user_message"] = kwargs.get("public_user_message")
        return "continued automatically"

    monkeypatch.setattr(deep_reflection, "_call_llm", fake_clean_llm)
    monkeypatch.setattr(deep_reflection, "_publish_runtime_event", AsyncMock())
    monkeypatch.setattr(agent_coordinator, "_load_session_messages", lambda: list(history))
    monkeypatch.setattr(agent_coordinator, "_save_session_messages", fake_save)
    monkeypatch.setattr(agent_coordinator, "get_context", lambda max_chars=5000: "")
    monkeypatch.setattr(agent_coordinator, "get_memory_context", lambda include_short_term=True: "")
    monkeypatch.setattr(agent_coordinator, "build_skill_prompt_block", lambda: "")
    monkeypatch.setattr(agent_coordinator, "_schedule_session_label_refresh", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(agent_coordinator, "_publish_runtime_event", AsyncMock())
    monkeypatch.setattr(agent_core, "_run_main_agent", fake_run_main_agent)

    result = await agent_coordinator._run_chat_agent(
        "/深度反思 聚焦原任务",
        None,
        0,
        "db.sqlite3",
        client_request_id="req_manual_reflect",
    )

    assert result == "continued automatically"
    assert "已完成深度反思" not in result
    assert saved_messages
    assert any(message.get("content") == "/深度反思 聚焦原任务" for message in saved_messages[0])
    assert any(message.get("deep_reflection_record") for message in saved_messages[0])
    assert seen["persist_user_message"] is False
    assert seen["public_user_message"] is None
    assert "继续自动工作" in str(seen["user_message"])
    assert any(message.get("deep_reflection_record") for message in seen["history"])
