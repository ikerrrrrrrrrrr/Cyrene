"""Persistent onboarding helpers for Web UI first-run setup."""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx

from cyrene.config import OPENAI_BASE_URL, OPENAI_MODEL, DATA_DIR, STATE_FILE, write_env_keys
from cyrene.setup import mark_setup_done, normalize_custom_soul_content
from cyrene.soul import get_default_soul_content, get_soul_path, read_soul

logger = logging.getLogger(__name__)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _onboarding_state_path() -> Path:
    return DATA_DIR / "onboarding_state.json"


def _setup_flag_path() -> Path:
    return DATA_DIR / ".setup_done"


def _normalize_state(raw: Any) -> dict[str, Any]:
    state = raw if isinstance(raw, dict) else {}
    llm = state.get("llm") if isinstance(state.get("llm"), dict) else {}
    personality = state.get("personality") if isinstance(state.get("personality"), dict) else {}
    return {
        "version": 1,
        "completed_at": str(state.get("completed_at") or "").strip(),
        "llm": {
            "completed_at": str(llm.get("completed_at") or "").strip(),
            "source": str(llm.get("source") or "").strip(),
            "base_url": str(llm.get("base_url") or "").strip(),
            "model": str(llm.get("model") or "").strip(),
        },
        "personality": {
            "completed_at": str(personality.get("completed_at") or "").strip(),
            "source": str(personality.get("source") or "").strip(),
            "mode": str(personality.get("mode") or "").strip(),
            "label": str(personality.get("label") or "").strip(),
        },
    }


def load_onboarding_state() -> dict[str, Any]:
    path = _onboarding_state_path()
    if not path.exists():
        return _normalize_state({})
    try:
        return _normalize_state(json.loads(path.read_text(encoding="utf-8")))
    except Exception:
        logger.warning("Failed to read onboarding state, treating as empty")
        return _normalize_state({})


def save_onboarding_state(state: dict[str, Any]) -> dict[str, Any]:
    normalized = _normalize_state(state)
    path = _onboarding_state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(normalized, ensure_ascii=False, indent=2), encoding="utf-8")
    return normalized


def reset_onboarding_state() -> None:
    """Remove persisted onboarding markers so setup appears as fresh again."""
    for path in (_onboarding_state_path(), _setup_flag_path()):
        try:
            if path.exists():
                path.unlink()
        except Exception:
            logger.exception("Failed to remove onboarding state file: %s", path)


def _api_key_present() -> bool:
    return bool(os.environ.get("OPENAI_API_KEY", "").strip())


def _is_default_soul(content: str) -> bool:
    return content.strip() == get_default_soul_content().strip()


def _personality_inferred_configured(content: str) -> bool:
    if _setup_flag_path().exists():
        return True
    return bool(content.strip()) and not _is_default_soul(content)


def _has_runtime_activity() -> bool:
    if STATE_FILE.exists():
        try:
            data = json.loads(STATE_FILE.read_text(encoding="utf-8"))
            if isinstance(data, dict) and data.get("messages"):
                return True
        except Exception:
            return True

    from cyrene.conversations import CONVERSATIONS_DIR

    return CONVERSATIONS_DIR.exists() and any(CONVERSATIONS_DIR.glob("*.md"))


def _merge_inferred_state(state: dict[str, Any]) -> tuple[dict[str, Any], str]:
    merged = _normalize_state(state)
    soul_content = read_soul()
    dirty = False

    llm_configured = bool(merged["llm"]["completed_at"]) or _api_key_present()
    if not merged["llm"]["completed_at"] and _api_key_present():
        merged["llm"]["completed_at"] = _now_iso()
        merged["llm"]["source"] = "legacy-env"
        merged["llm"]["base_url"] = os.environ.get("OPENAI_BASE_URL", OPENAI_BASE_URL).strip()
        merged["llm"]["model"] = os.environ.get("OPENAI_MODEL", OPENAI_MODEL).strip()
        dirty = True

    personality_configured = bool(merged["personality"]["completed_at"]) or _personality_inferred_configured(soul_content)
    if not merged["personality"]["completed_at"] and _personality_inferred_configured(soul_content):
        merged["personality"]["completed_at"] = _now_iso()
        merged["personality"]["source"] = "legacy-flag" if _setup_flag_path().exists() else "legacy-soul"
        merged["personality"]["mode"] = "custom" if soul_content.strip() and not _is_default_soul(soul_content) else "default"
        dirty = True

    if llm_configured and personality_configured and not merged["completed_at"]:
        merged["completed_at"] = _now_iso()
        dirty = True

    if dirty:
        merged = save_onboarding_state(merged)
    return merged, soul_content


def get_onboarding_status() -> dict[str, Any]:
    state, soul_content = _merge_inferred_state(load_onboarding_state())
    llm_configured = bool(state["llm"]["completed_at"]) or _api_key_present()
    personality_configured = bool(state["personality"]["completed_at"]) or _personality_inferred_configured(soul_content)
    needs_onboarding = not (llm_configured and personality_configured)
    active_step = "done"
    if not llm_configured:
        active_step = "llm"
    elif not personality_configured:
        active_step = "personality"

    return {
        "needsOnboarding": needs_onboarding,
        "isAbsoluteFreshStart": (
            not _onboarding_state_path().exists()
            and not _setup_flag_path().exists()
            and not _api_key_present()
            and (not soul_content.strip() or _is_default_soul(soul_content))
            and not _has_runtime_activity()
        ),
        "activeStep": active_step,
        "completedAt": state.get("completed_at", ""),
        "llm": {
            "configured": llm_configured,
            "hasApiKey": _api_key_present(),
            "baseUrl": os.environ.get("OPENAI_BASE_URL", OPENAI_BASE_URL).strip(),
            "model": os.environ.get("OPENAI_MODEL", OPENAI_MODEL).strip(),
            "completedAt": state["llm"].get("completed_at", ""),
        },
        "personality": {
            "configured": personality_configured,
            "completedAt": state["personality"].get("completed_at", ""),
            "mode": state["personality"].get("mode", ""),
            "label": state["personality"].get("label", ""),
            "isDefaultSoul": bool(soul_content.strip()) and _is_default_soul(soul_content),
            "path": str(get_soul_path()),
            "currentContent": soul_content,
        },
    }


async def _test_llm_connection(api_key: str, base_url: str, model: str) -> str:
    endpoint = f"{base_url.rstrip('/')}/chat/completions"
    headers = {"Content-Type": "application/json"}
    if api_key.strip():
        headers["Authorization"] = f"Bearer {api_key.strip()}"
    payload = {
        "model": model,
        "messages": [{"role": "user", "content": "Reply with OK."}],
        "max_tokens": 16,
    }
    transport = httpx.AsyncHTTPTransport(retries=1)
    async with httpx.AsyncClient(transport=transport, timeout=30.0) as client:
        response = await client.post(endpoint, headers=headers, json=payload)
        response.raise_for_status()
        data = response.json()
    choices = data.get("choices") or []
    if not choices:
        raise ValueError("LLM endpoint returned no choices")
    message = choices[0].get("message") or {}
    content = str(message.get("content") or "").strip()
    return content or "OK"


async def save_and_test_llm_setup(api_key: str, base_url: str, model: str) -> dict[str, Any]:
    clean_base_url = base_url.strip()
    clean_model = model.strip()
    clean_api_key = api_key.strip()
    if not clean_base_url:
        raise ValueError("LLM endpoint is required")
    if not clean_model:
        raise ValueError("Model name is required")

    preview = await _test_llm_connection(clean_api_key, clean_base_url, clean_model)
    write_env_keys({
        "OPENAI_API_KEY": clean_api_key,
        "OPENAI_BASE_URL": clean_base_url,
        "OPENAI_MODEL": clean_model,
    })

    state = load_onboarding_state()
    state["llm"] = {
        "completed_at": _now_iso(),
        "source": "wizard",
        "base_url": clean_base_url,
        "model": clean_model,
    }
    save_onboarding_state(state)
    return {
        "ok": True,
        "preview": preview,
        "onboarding": get_onboarding_status(),
    }


async def save_personality_setup(mode: str, name: str = "", content: str = "") -> dict[str, Any]:
    clean_mode = mode.strip().lower()
    if clean_mode == "default":
        soul_content = get_default_soul_content()
        label = "Default persona"
    elif clean_mode == "custom":
        soul_content = normalize_custom_soul_content(content)
        label = "Custom SOUL.md"
    elif clean_mode == "name":
        clean_name = name.strip()
        if not clean_name:
            raise ValueError("Personality name is required")
        from cyrene.setup import create_soul_profile_from_name

        soul_content = await create_soul_profile_from_name(clean_name)
        label = clean_name
    else:
        raise ValueError("Unsupported personality mode")

    if clean_mode != "name":
        soul_path = get_soul_path()
        soul_path.parent.mkdir(parents=True, exist_ok=True)
        soul_path.write_text(soul_content, encoding="utf-8")

    mark_setup_done()

    state = load_onboarding_state()
    state["personality"] = {
        "completed_at": _now_iso(),
        "source": "wizard",
        "mode": clean_mode,
        "label": label,
    }
    save_onboarding_state(state)

    from cyrene.agent import clear_session_id

    await clear_session_id()
    return {
        "ok": True,
        "soulContent": soul_content,
        "onboarding": get_onboarding_status(),
    }
