import json

import pytest

from cyrene import agent


def test_dedupe_messages_by_id_keeps_latest_occurrence_in_original_position() -> None:
    messages = [
        {"role": "user", "message_id": "msg_1", "content": "hello"},
        {"role": "assistant", "message_id": "msg_2", "content": "world"},
        {"role": "user", "message_id": "msg_1", "content": "hello updated", "round_title": "latest"},
    ]

    deduped = agent._dedupe_messages_by_id(messages)

    assert deduped == [
        {"role": "user", "message_id": "msg_1", "content": "hello updated", "round_title": "latest"},
        {"role": "assistant", "message_id": "msg_2", "content": "world"},
    ]


@pytest.mark.asyncio
async def test_save_session_messages_does_not_regress_final_reply(tmp_path) -> None:
    state_file = tmp_path / "state.json"
    data_dir = tmp_path

    old_state_file = agent.STATE_FILE
    old_data_dir = agent.DATA_DIR
    old_base = agent._persist_base_messages.get()
    old_merge_live = agent._persist_merge_live_state.get()
    old_prefix = agent._persist_history_prefix_len.get()
    old_insert = agent._persist_insert_at.get()
    old_round_id = agent._current_round_id.get()
    try:
        agent.STATE_FILE = state_file
        agent.DATA_DIR = data_dir

        existing = [
            {
                "role": "assistant",
                "message_id": "msg_prev_assistant",
                "content": "done",
                "round_id": "round_prev",
            },
            {
                "role": "user",
                "message_id": "msg_current_user",
                "content": "why no reply",
                "round_id": "round_now",
            },
            {
                "role": "assistant",
                "message_id": "msg_final_assistant",
                "content": "reply restored",
                "round_id": "round_now",
            },
        ]
        state_file.write_text(
            json.dumps({"archive_session_id": "session_test", "messages": existing}, ensure_ascii=False),
            encoding="utf-8",
        )

        history = [existing[0]]
        stale_messages = [
            history[0],
            {
                "role": "user",
                "message_id": "msg_current_user",
                "content": "why no reply",
                "round_id": "round_now",
            },
        ]

        agent._persist_base_messages.set(None)
        agent._persist_merge_live_state.set(True)
        agent._persist_history_prefix_len.set(len(history))
        agent._persist_insert_at.set(len(history))
        agent._current_round_id.set("round_now")

        await agent._save_session_messages(stale_messages)

        saved = json.loads(state_file.read_text(encoding="utf-8"))
        saved_messages = saved["messages"]
        assert [msg["message_id"] for msg in saved_messages] == [
            "msg_prev_assistant",
            "msg_current_user",
            "msg_final_assistant",
        ]
    finally:
        agent.STATE_FILE = old_state_file
        agent.DATA_DIR = old_data_dir
        agent._persist_base_messages.set(old_base)
        agent._persist_merge_live_state.set(old_merge_live)
        agent._persist_history_prefix_len.set(old_prefix)
        agent._persist_insert_at.set(old_insert)
        agent._current_round_id.set(old_round_id)


@pytest.mark.asyncio
async def test_save_session_messages_with_persist_base_preserves_concurrent_messages(tmp_path) -> None:
    state_file = tmp_path / "state.json"
    data_dir = tmp_path

    old_state_file = agent.STATE_FILE
    old_data_dir = agent.DATA_DIR
    old_base = agent._persist_base_messages.get()
    old_merge_live = agent._persist_merge_live_state.get()
    old_prefix = agent._persist_history_prefix_len.get()
    old_insert = agent._persist_insert_at.get()
    try:
        agent.STATE_FILE = state_file
        agent.DATA_DIR = data_dir

        base_messages = [
            {"role": "user", "message_id": "u1", "content": "first"},
            {"role": "assistant", "message_id": "a1", "content": "reply"},
        ]
        current_messages = [
            *base_messages,
            {"role": "user", "message_id": "g1", "content": "queued guidance", "queued_guidance_id": "guide_1"},
        ]
        state_file.write_text(
            json.dumps({"archive_session_id": "session_test", "messages": current_messages}, ensure_ascii=False),
            encoding="utf-8",
        )

        incoming = [
            *base_messages,
            {"role": "user", "message_id": "u2", "content": "new question"},
            {"role": "assistant", "message_id": "a2", "content": "new answer"},
        ]

        agent._persist_base_messages.set(base_messages)
        agent._persist_merge_live_state.set(False)
        agent._persist_history_prefix_len.set(len(base_messages))
        agent._persist_insert_at.set(len(base_messages))

        await agent._save_session_messages(incoming)

        saved = json.loads(state_file.read_text(encoding="utf-8"))["messages"]
        assert [msg["message_id"] for msg in saved] == ["u1", "a1", "g1", "u2", "a2"]
    finally:
        agent.STATE_FILE = old_state_file
        agent.DATA_DIR = old_data_dir
        agent._persist_base_messages.set(old_base)
        agent._persist_merge_live_state.set(old_merge_live)
        agent._persist_history_prefix_len.set(old_prefix)
        agent._persist_insert_at.set(old_insert)


def test_get_session_labels_persists_generated_archive_session_id(tmp_path) -> None:
    state_file = tmp_path / "state.json"
    data_dir = tmp_path

    old_state_file = agent.STATE_FILE
    old_data_dir = agent.DATA_DIR
    try:
        agent.STATE_FILE = state_file
        agent.DATA_DIR = data_dir
        state_file.write_text(json.dumps({"messages": []}, ensure_ascii=False), encoding="utf-8")

        labels_first = agent.get_session_labels()
        labels_second = agent.get_session_labels()
        saved_state = json.loads(state_file.read_text(encoding="utf-8"))

        assert labels_first["archive_session_id"]
        assert labels_first["archive_session_id"] == labels_second["archive_session_id"]
        assert saved_state["archive_session_id"] == labels_first["archive_session_id"]
    finally:
        agent.STATE_FILE = old_state_file
        agent.DATA_DIR = old_data_dir
