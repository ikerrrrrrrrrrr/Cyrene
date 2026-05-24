import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))


def test_resolve_cc_session_name_prefers_env(monkeypatch, tmp_path):
    from cyrene import cc_bridge

    monkeypatch.setenv("CYRENE_CC_TMUX_SESSION", "claude-explicit")

    resolved = cc_bridge.resolve_cc_session_name(tmp_path / "repo")

    assert resolved == "claude-explicit"


def test_resolve_cc_session_name_falls_back_for_invalid_env(monkeypatch, tmp_path):
    from cyrene import cc_bridge

    monkeypatch.setenv("CYRENE_CC_TMUX_SESSION", "bad session name")

    resolved = cc_bridge.resolve_cc_session_name(tmp_path / "My Repo")

    assert resolved == "claude-my-repo"


def test_get_cc_status_only_matches_expected_session(monkeypatch, tmp_path):
    from cyrene import cc_bridge

    repo = tmp_path / "repo"
    repo.mkdir()
    monkeypatch.setenv("CYRENE_CC_TMUX_SESSION", "claude-repo")
    monkeypatch.setattr(cc_bridge, "sync_cc_shell_status", lambda: None)
    monkeypatch.setattr(cc_bridge, "find_claude_project_dir", lambda cwd=None: None)
    monkeypatch.setattr(cc_bridge, "find_latest_jsonl", lambda project_dir: None)
    monkeypatch.setattr(cc_bridge, "tmux_available", lambda: True)
    monkeypatch.setattr(
        cc_bridge,
        "list_tmux_sessions",
        lambda: [{"name": "build-shell", "attached": False, "activity": 1, "window_count": 1}],
    )

    status = cc_bridge.get_cc_status(repo)

    assert status["available"] is False
    assert status["can_launch"] is True
    assert status["expected_session"] == "claude-repo"
    assert status["tmux_session"] == ""
    assert "claude-repo" in status["reason"]


def test_get_cc_status_registers_expected_running_session(monkeypatch, tmp_path):
    from cyrene import cc_bridge

    repo = tmp_path / "repo"
    repo.mkdir()
    monkeypatch.delenv("CYRENE_CC_TMUX_SESSION", raising=False)
    monkeypatch.setattr(cc_bridge, "sync_cc_shell_status", lambda: None)
    monkeypatch.setattr(cc_bridge, "find_claude_project_dir", lambda cwd=None: None)
    monkeypatch.setattr(cc_bridge, "find_latest_jsonl", lambda project_dir: None)
    monkeypatch.setattr(cc_bridge, "tmux_available", lambda: True)
    monkeypatch.setattr(
        cc_bridge,
        "list_tmux_sessions",
        lambda: [{"name": "claude-repo", "attached": False, "activity": 1, "window_count": 1}],
    )
    seen: list[tuple[str, Path]] = []
    monkeypatch.setattr(cc_bridge, "_register_cc_shell", lambda name, cwd: seen.append((name, cwd)))

    status = cc_bridge.get_cc_status(repo)

    assert status["available"] is True
    assert status["tmux_session"] == "claude-repo"
    assert seen == [("claude-repo", repo.resolve())]


def test_launch_cc_tmux_reuses_existing_session_and_registers(monkeypatch, tmp_path):
    from cyrene import cc_bridge

    repo = tmp_path / "repo"
    repo.mkdir()
    monkeypatch.setattr(cc_bridge, "tmux_available", lambda: True)
    monkeypatch.setattr(
        cc_bridge,
        "list_tmux_sessions",
        lambda: [{"name": "claude-repo", "attached": False, "activity": 1, "window_count": 1}],
    )
    seen: list[tuple[str, Path]] = []
    monkeypatch.setattr(cc_bridge, "_register_cc_shell", lambda name, cwd: seen.append((name, cwd)))

    result = cc_bridge.launch_cc_tmux(cwd=repo)

    assert result["ok"] is True
    assert result["session"] == "claude-repo"
    assert "already exists" in result["detail"]
    assert seen == [("claude-repo", repo.resolve())]
