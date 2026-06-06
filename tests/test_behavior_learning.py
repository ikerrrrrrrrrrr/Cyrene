import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))


async def _fake_llm_json(_prompt: str, *, caller: str = "behavior_learning"):
    return {}


async def _init_behavior(tmp_path, monkeypatch):
    from cyrene import behavior_learning as bl

    await bl.init(tmp_path, tmp_path)
    monkeypatch.setattr(bl, "_call_llm_json", _fake_llm_json)
    return bl


async def _record_code_fix_turn(bl, *, session_id: str, round_id: str, user_message: str):
    context = await bl.begin_turn(
        session_id=session_id,
        round_id=round_id,
        user_message=user_message,
        history=[],
        session_title="Behavior test session",
    )
    await bl.record_action("read_file", {"path": "src/app.py"}, "main_agent", round_id, 12, result="file content", success=True)
    await bl.record_action(
        "edit_file",
        {"path": "src/app.py", "old_string": "return raw", "new_string": "return exported"},
        "main_agent",
        round_id,
        20,
        result="patched",
        success=True,
    )
    await bl.record_action(
        "run_shell",
        {"command": "pytest -q tests/test_export.py"},
        "main_agent",
        round_id,
        80,
        result="1 passed",
        success=True,
    )
    await bl.complete_turn(
        turn_id=context["turn_id"],
        assistant_response="已修复并验证。",
        session_title="Behavior test session",
        round_title=round_id,
    )
    bl.clear_turn_context(context)


async def test_behavior_learning_promotes_to_active_skill(tmp_path, monkeypatch):
    bl = await _init_behavior(tmp_path, monkeypatch)

    for index in range(1, 6):
        await _record_code_fix_turn(
            bl,
            session_id="session-alpha",
            round_id=f"round-{index}",
            user_message="请检查 src/app.py 并修复导出逻辑，然后给我总结",
        )

    stats = await bl.process_unprocessed_turns(force=True)
    patterns = await bl.list_patterns()
    skills = await bl.list_learned_skills()

    assert stats["processed_turns"] == 5
    assert stats["merged_patterns"] == 4
    assert len(patterns) == 1
    assert patterns[0]["description"] == "edit_resource / code_change / source_code_file / workspace_file"
    assert patterns[0]["prototype_fingerprint"]["action_sequence"][0]["subtype"] == "read_file"
    assert len(skills) == 1
    assert skills[0]["status"] == "active"
    assert skills[0]["skill_type"] == "parameterized"
    # Shadow validation backfills every eligible historical turn before activation,
    # so the counter reflects total successful dry runs, not the promotion threshold.
    assert skills[0]["run_statistics"]["shadow_success"] == 4


async def test_behavior_learning_manual_edit_and_rollback(tmp_path, monkeypatch):
    bl = await _init_behavior(tmp_path, monkeypatch)

    for index in range(1, 3):
        await _record_code_fix_turn(
            bl,
            session_id="session-beta",
            round_id=f"round-{index}",
            user_message="请检查 src/app.py 并修复导出逻辑，然后给我总结",
        )

    await bl.process_unprocessed_turns(force=True)
    skills = await bl.list_learned_skills()
    skill = skills[0]
    updated = await bl.update_learned_skill(
        skill["id"],
        {"description": "manual edit description"},
        reason="manual test edit",
    )

    assert updated is not None
    assert updated["description"] == "manual edit description"
    assert updated["version"] == 2

    rollback = await bl.rollback_learned_skill(skill["id"], 1)
    restored = await bl.get_learned_skill(skill["id"])

    assert rollback["ok"] is True
    assert restored is not None
    assert restored["version"] == 3
    assert restored["description"] != "manual edit description"


async def test_behavior_learning_patch_application_and_vocabulary_snapshot(tmp_path, monkeypatch):
    bl = await _init_behavior(tmp_path, monkeypatch)

    for index in range(1, 3):
        await _record_code_fix_turn(
            bl,
            session_id="session-gamma",
            round_id=f"round-{index}",
            user_message="请检查 src/app.py 并修复导出逻辑，然后给我总结",
        )

    await bl.process_unprocessed_turns(force=True)
    skills = await bl.list_learned_skills()
    skill = skills[0]

    await bl._maybe_propose_patch(skill["id"], int(skill["version"]), "missing parameters: path")
    patches = await bl.list_learned_skill_patches(skill["id"])
    patch = patches[0]
    applied = await bl.apply_skill_patch(skill["id"], patch["patch_id"])
    refreshed = await bl.get_learned_skill(skill["id"])
    vocabulary = await bl.vocabulary_snapshot()

    assert applied["ok"] is True
    assert refreshed is not None
    assert refreshed["fallback_policy"]["on_missing_args"] == "ask_user"
    result = await bl.list_learned_skill_patches(skill["id"])
    assert result[0]["status"] == "applied"
    assert vocabulary["vocabulary_version"] == 1
    assert any(item["label_type"] == "intent_type" for item in vocabulary["unknown_labels"])


# ---------------------------------------------------------------------------
# Issue #46 regression: high-risk skill replay must not execute silently
# ---------------------------------------------------------------------------

def _make_skill_with_steps(steps: list[dict], *, risk_level: str = "none") -> dict:
    """Build a minimal skill dict for replay testing."""
    return {
        "skill_id": "test-skill-001",
        "name": "Test Skill",
        "description": "test",
        "version": 1,
        "status": "active",
        "skill_type": "deterministic",
        "risk_level": risk_level,
        "requires_llm": False,
        "trigger": {"base_fingerprint": {}, "min_match_score": 0.75},
        "input_schema": [],
        "parameter_extractor": {"mode": "hybrid", "llm_fallback": False},
        "steps": steps,
        "guards": {"risk_level": risk_level, "required_context": [], "forbidden_conditions": [], "confidence_threshold": 0.75},
        "fallback_policy": {},
        "tests": [],
        "editable_fields": [],
        "created_from": {},
        "run_statistics": {},
        "pattern_id": "pat-001",
        "created_at": "2026-01-01T00:00:00+00:00",
        "updated_at": "2026-01-01T00:00:00+00:00",
    }


def _make_step(tool_name: str) -> dict:
    return {
        "enabled": True,
        "implementation_kind": "tool_call",
        "implementation_reference": {"tool_name": tool_name, "args_template": {}},
    }


async def test_high_risk_skill_blocked_from_replay(tmp_path, monkeypatch):
    """Skills with high-risk steps must not be auto-replayed (issue #46)."""
    bl = await _init_behavior(tmp_path, monkeypatch)

    # Patch match_active_skill to return a Bash-containing skill
    risky_skill = _make_skill_with_steps([_make_step("Bash")], risk_level="high")
    monkeypatch.setattr(
        bl,
        "match_active_skill",
        AsyncMock(return_value={
            "skill": risky_skill,
            "similarity": {"total": 0.92, "hard_fail": False},
        }),
    )
    # Patch extract_skill_parameters to return complete extraction
    monkeypatch.setattr(
        bl,
        "extract_skill_parameters",
        AsyncMock(return_value={"complete": True, "params": {}, "confidence": 0.92, "missing_required": []}),
    )

    context = await bl.begin_turn(
        session_id="session-risk",
        round_id="round-risk-1",
        user_message="run the build script",
        history=[],
    )

    result = await bl.try_route_and_execute_skill(
        user_message="run the build script",
        visible_user_entry={"role": "user", "content": "run the build script"},
        llm_user_entry={"role": "user", "content": "run the build script"},
        history=[],
        bot=MagicMock(),
        chat_id=1,
        db_path=str(tmp_path / "test.db"),
        effective_system="",
        client_request_id="req-1",
        round_id="round-risk-1",
        lang="en",
    )

    # Must fall back to agent — not execute silently
    assert result is None, "High-risk skill should not auto-execute (expected None fallback)"

    bl.clear_turn_context(context)


async def test_safe_skill_still_executes(tmp_path, monkeypatch):
    """Skills with only safe (read-only) steps should still auto-execute."""
    bl = await _init_behavior(tmp_path, monkeypatch)

    # A read_file step is not in _HIGH_RISK_TOOLS
    safe_skill = _make_skill_with_steps([_make_step("read_file")], risk_level="none")
    monkeypatch.setattr(
        bl,
        "match_active_skill",
        AsyncMock(return_value={
            "skill": safe_skill,
            "similarity": {"total": 0.92, "hard_fail": False},
        }),
    )
    monkeypatch.setattr(
        bl,
        "extract_skill_parameters",
        AsyncMock(return_value={"complete": True, "params": {}, "confidence": 0.92, "missing_required": []}),
    )
    # Stub tool execution and LLM reply — patch the module that behavior_learning imports from
    import cyrene.tools as tools_mod
    monkeypatch.setattr(tools_mod, "_execute_tool", AsyncMock(return_value="file content"))

    async def _fake_final_reply(_messages, **_kw):
        return "Done."

    import cyrene.agent.guidance as guidance
    monkeypatch.setattr(guidance, "_final_user_reply_from_history", _fake_final_reply)

    import cyrene.agent.message as msg_mod
    monkeypatch.setattr(msg_mod, "_apply_assistant_meta", lambda x: x)

    context = await bl.begin_turn(
        session_id="session-safe",
        round_id="round-safe-1",
        user_message="show me app.py",
        history=[],
    )

    result = await bl.try_route_and_execute_skill(
        user_message="show me app.py",
        visible_user_entry={"role": "user", "content": "show me app.py"},
        llm_user_entry={"role": "user", "content": "show me app.py"},
        history=[],
        bot=MagicMock(),
        chat_id=1,
        db_path=str(tmp_path / "test.db"),
        effective_system="",
        client_request_id="req-2",
        round_id="round-safe-1",
        lang="en",
    )

    # Safe skill should proceed (result is not None)
    assert result is not None, "Safe skill should auto-execute (expected non-None result)"
    assert result["final_text"] == "Done."

    bl.clear_turn_context(context)


async def test_skill_risk_level_inferred_on_creation(tmp_path, monkeypatch):
    """Skills containing high-risk tools must get risk_level='high' at creation time."""
    from cyrene import behavior_learning as bl

    # Directly test the helper — no DB needed
    assert bl._infer_skill_risk_level([]) == "none"
    assert bl._infer_skill_risk_level([_make_step("read_file")]) == "none"
    assert bl._infer_skill_risk_level([_make_step("Bash")]) == "high"
    assert bl._infer_skill_risk_level([_make_step("Write")]) == "high"
    assert bl._infer_skill_risk_level([_make_step("schedule_task")]) == "high"
    assert bl._infer_skill_risk_level([_make_step("start_shell")]) == "high"
    # Mixed: one safe + one risky → high
    assert bl._infer_skill_risk_level([_make_step("read_file"), _make_step("Edit")]) == "high"
    # Disabled risky step should not count
    disabled_bash = {**_make_step("Bash"), "enabled": False}
    assert bl._infer_skill_risk_level([disabled_bash]) == "none"
