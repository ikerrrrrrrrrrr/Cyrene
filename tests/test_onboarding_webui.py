import sys
import types
from pathlib import Path
from unittest.mock import AsyncMock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))


def _patch_paths(monkeypatch, tmp_path, soul_content, default_content):
    from cyrene import onboarding, setup, conversations

    soul_path = tmp_path / "workspace" / "SOUL.md"
    soul_path.parent.mkdir(parents=True, exist_ok=True)
    soul_path.write_text(soul_content, encoding="utf-8")

    monkeypatch.setattr(onboarding, "DATA_DIR", tmp_path)
    monkeypatch.setattr(onboarding, "STATE_FILE", tmp_path / "state.json")
    monkeypatch.setattr(onboarding, "get_soul_path", lambda: soul_path)
    monkeypatch.setattr(onboarding, "read_soul", lambda: soul_path.read_text(encoding="utf-8"))
    monkeypatch.setattr(onboarding, "get_default_soul_content", lambda name=None: default_content)
    monkeypatch.setattr(setup, "DATA_DIR", tmp_path)
    monkeypatch.setattr(setup, "_SETUP_FLAG", None)
    monkeypatch.setattr(conversations, "CONVERSATIONS_DIR", tmp_path / "conversations")
    return soul_path


def test_get_onboarding_status_detects_absolute_fresh_start(monkeypatch, tmp_path):
    from cyrene import onboarding

    default_soul = "# Cyrene's Soul\n\n## SELF:IDENTITY\n- default\n"
    _patch_paths(monkeypatch, tmp_path, default_soul, default_soul)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)

    status = onboarding.get_onboarding_status()

    assert status["needsOnboarding"] is True
    assert status["isAbsoluteFreshStart"] is True
    assert status["activeStep"] == "llm"


def test_get_onboarding_status_infers_existing_setup(monkeypatch, tmp_path):
    from cyrene import onboarding

    default_soul = "# Cyrene's Soul\n\n## SELF:IDENTITY\n- default\n"
    custom_soul = "# Sherlock's Soul\n\n## CORE IDENTITY\n- sharp and theatrical\n"
    _patch_paths(monkeypatch, tmp_path, custom_soul, default_soul)
    monkeypatch.setenv("OPENAI_API_KEY", "secret-key")
    monkeypatch.setenv("OPENAI_BASE_URL", "https://example.test/v1")
    monkeypatch.setenv("OPENAI_MODEL", "example-model")

    status = onboarding.get_onboarding_status()

    assert status["needsOnboarding"] is False
    assert status["activeStep"] == "done"
    assert status["personality"]["configured"] is True
    assert (tmp_path / "onboarding_state.json").exists()


async def test_save_and_test_llm_setup_persists_completion(monkeypatch, tmp_path):
    from cyrene import onboarding

    default_soul = "# Cyrene's Soul\n\n## SELF:IDENTITY\n- default\n"
    _patch_paths(monkeypatch, tmp_path, default_soul, default_soul)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.setattr(onboarding, "write_env_keys", lambda updates: True)
    monkeypatch.setattr(onboarding, "_test_llm_connection", AsyncMock(return_value="OK"))

    payload = await onboarding.save_and_test_llm_setup("", "http://localhost:11434/v1", "qwen3")

    assert payload["ok"] is True
    assert payload["preview"] == "OK"
    assert payload["onboarding"]["llm"]["configured"] is True
    assert payload["onboarding"]["activeStep"] == "personality"


async def test_save_personality_setup_marks_setup_done(monkeypatch, tmp_path):
    from cyrene import onboarding

    default_soul = "# Cyrene's Soul\n\n## SELF:IDENTITY\n- default\n"
    soul_path = _patch_paths(monkeypatch, tmp_path, default_soul, default_soul)
    fake_agent = types.SimpleNamespace(clear_session_id=AsyncMock())
    monkeypatch.setitem(sys.modules, "cyrene.agent", fake_agent)

    onboarding.save_onboarding_state({
        "llm": {
            "completed_at": "2026-05-19T00:00:00+00:00",
            "source": "wizard",
            "base_url": "https://example.test/v1",
            "model": "example-model",
        }
    })

    payload = await onboarding.save_personality_setup("default")

    assert payload["ok"] is True
    assert soul_path.read_text(encoding="utf-8") == default_soul
    assert (tmp_path / ".setup_done").exists()
    assert payload["onboarding"]["needsOnboarding"] is False
    fake_agent.clear_session_id.assert_awaited_once()
