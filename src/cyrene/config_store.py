"""Encrypted unified config store — replaces .env + web_settings.json.

All sensitive and user-editable configuration lives in a single
Fernet-encrypted JSON blob under DATA_DIR / "config.enc".

On first access the store migrates data from the legacy files
(.env and web_settings.json), then writes the encrypted store.
The legacy files are renamed to .bak for safety.
"""

from __future__ import annotations

import json
import logging
import os
import sys
from pathlib import Path

from cryptography.fernet import Fernet

try:
    import keyring
    import keyring.errors as keyring_errors
except Exception:  # pragma: no cover - keyring missing entirely
    keyring = None  # type: ignore[assignment]
    keyring_errors = None  # type: ignore[assignment]

logger = logging.getLogger(__name__)

# OS keyring identifiers for the Fernet encryption key.
_KEYRING_SERVICE = "cyrene"
_KEYRING_USERNAME = "config_key"

# ---------------------------------------------------------------------------
# Path resolution (self-contained — do NOT import from cyrene.config)
# ---------------------------------------------------------------------------


def _bundle_contents_dir() -> Path | None:
    exe = Path(sys.executable).resolve()
    parts = exe.parts
    for idx, part in enumerate(parts):
        if part.endswith(".app") and idx + 2 < len(parts) and parts[idx + 1] == "Contents":
            return Path(*parts[: idx + 2])
    return None


def _is_bundled() -> bool:
    return getattr(sys, "frozen", False) or _bundle_contents_dir() is not None


def _get_user_data_dir() -> Path:
    if sys.platform == "darwin":
        base = Path.home() / "Library" / "Application Support"
    elif sys.platform == "win32":
        base = Path(os.environ.get("APPDATA", Path.home() / "AppData" / "Roaming"))
    else:
        xdg = os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share")
        base = Path(xdg)
    return base / "Cyrene"


def _get_source_root() -> Path:
    if _is_bundled() and hasattr(sys, "_MEIPASS"):
        return Path(sys._MEIPASS)
    bundle_contents = _bundle_contents_dir()
    if bundle_contents is not None:
        for candidate in (bundle_contents / "Resources", bundle_contents / "Frameworks"):
            if (candidate / "pyproject.toml").exists() or (candidate / ".env.example").exists():
                return candidate
    return Path(__file__).resolve().parent.parent.parent


_SOURCE_ROOT = _get_source_root()
if _is_bundled():
    _BASE_DIR = _get_user_data_dir()
else:
    _BASE_DIR = _SOURCE_ROOT

DATA_DIR = _BASE_DIR / "data"
_ENCRYPTED_PATH = DATA_DIR / "config.enc"
_KEY_PATH = DATA_DIR / ".config_key"
_LEGACY_ENV_PATH = _BASE_DIR / ".env"
_LEGACY_SETTINGS_PATH = DATA_DIR / "web_settings.json"

# ---------------------------------------------------------------------------
# Default values
# ---------------------------------------------------------------------------

_DEFAULT_ENV: dict[str, str] = {
    "OPENAI_API_KEY": "",
    "OPENAI_BASE_URL": "https://api.deepseek.com/v1",
    "OPENAI_MODEL": "deepseek-v4-flash",
    "TELEGRAM_BOT_TOKEN": "",
    "WECHAT_BOT_TOKEN": "",
    "WECHAT_OWNER_ID": "",
    "AMAP_API_KEY": "",
    "ASSISTANT_NAME": "Cyrene",
    "MAX_TOOL_ROUNDS": "15",
    "MAX_HISTORY_MESSAGES": "40",
    "MAX_TOOL_OUTPUT_CHARS": "12000",
    "HEARTBEAT_INTERVAL": "300",
    "HEARTBEAT_LOTTERY_INTERVAL": "1800",
    "SCHEDULER_INTERVAL": "60",
    "DAYTIME_START": "6",
    "DAYTIME_END": "22",
    "LOTTERY_DELTA": "0.15",
    "LOTTERY_MAX": "0.85",
    "SEARCH_PROXY": "",
    "SEARXNG_URL": "",
    "SEARXNG_AUTO_START": "1",
    "SEARXNG_PORT": "8888",
    "SEARXNG_HOST": "127.0.0.1",
    "STEWARD_INTERVAL": "1800",
    "PATTERN_DETECTION_INTERVAL": "600",
    "WEB_PORT": "4242",
}

_DEFAULT_MODELS: list[dict[str, str]] = []

_DEFAULT_VISION_MODELS: list[dict[str, str]] = []

_DEFAULT_ENABLED_TOOLS: dict[str, bool] = {
    "Read": True, "Write": True, "Edit": True, "Glob": True, "Grep": True,
    "Bash": True, "StartShell": True, "SendShell": True, "ListShells": True,
    "CloseShell": True, "WebFetch": True, "WebSearch": True,
    "spawn_subagent": True, "send_agent_message": True,
    "schedule_task": True, "list_tasks": True, "pause_task": True,
    "resume_task": True, "cancel_task": True,
    "send_message": True, "send_file": True, "send_wechat_file": True,
    "ask_user": True, "PromptClaudeCode": True,
    "send_telegram": False, "query_round": True,
    "CheckClaudeCode": True, "StartClaudeCode": True,
}

_DEFAULT_SETTINGS: dict = {
    "search_mode": "builtin",
    "search_external_url": "",
    "spawn_policy": "conservative",
    "heartbeat_interval": 1800,
    "write_permission_mode": "workspace_only",
    "models": _DEFAULT_MODELS,
    "vision_models": _DEFAULT_VISION_MODELS,
    "secondary_model": {"model": "", "name": "", "api_key": "", "base_url": "", "ctx_limit": 0, "max_concurrency": 0},
    "enabled_tools": _DEFAULT_ENABLED_TOOLS,
    "workspace_history": [],
    "workspace_active": True,
    "soul_active": True,
    "agent_proactive": True,
    "max_tool_rounds": 15,
    "redact_secrets": True,
    "notify_telegram": True,
    "notify_wechat": True,
}

_EDITABLE_ENV_KEYS = {
    "OPENAI_API_KEY", "OPENAI_BASE_URL", "OPENAI_MODEL",
    "TELEGRAM_BOT_TOKEN", "WECHAT_BOT_TOKEN", "AMAP_API_KEY",
}

# ---------------------------------------------------------------------------
# Encryption
# ---------------------------------------------------------------------------



# ---------------------------------------------------------------------------
# Key storage
#
# The Fernet key is stored in the OS-backed secret store via the `keyring`
# library (macOS Keychain, Windows Credential Locker, Linux Secret Service).
# This keeps the key out of any plaintext file that a co-located process can
# read.
#
# DEGRADED MODE: keyring may be unavailable (package missing) or raise
# NoKeyringError/KeyringError — e.g. headless Linux with no Secret Service
# daemon, or a locked keyring. In that case we fall back to the legacy
# behavior of writing the key to a plaintext file (.config_key, chmod 0600)
# and emit a WARNING. The key is then NOT encrypted at rest, providing only
# filesystem-permission protection.
# ---------------------------------------------------------------------------


def _keyring_available() -> bool:
    return keyring is not None and keyring_errors is not None


def _keyring_get() -> bytes | None:
    """Return the stored Fernet key from the OS keyring, or None.

    Returns None on a clean "not present"; raises on backend errors so the
    caller can decide whether to fall back to degraded mode.
    """
    if not _keyring_available():
        return None
    value = keyring.get_password(_KEYRING_SERVICE, _KEYRING_USERNAME)
    if value is None:
        return None
    return value.encode("ascii")


def _keyring_set(key: bytes) -> bool:
    """Store the Fernet key in the OS keyring and verify with a read-back.

    Returns True on verified success, False if keyring is unavailable or the
    backend raised an error (degraded mode).
    """
    if not _keyring_available():
        return False
    try:
        keyring.set_password(_KEYRING_SERVICE, _KEYRING_USERNAME, key.decode("ascii"))
        return _keyring_get() == key
    except keyring_errors.KeyringError:
        logger.warning("OS keyring unavailable when storing encryption key", exc_info=True)
        return False


def _write_plaintext_key(key: bytes) -> None:
    """Degraded-mode fallback: persist the key to a 0600 plaintext file."""
    _KEY_PATH.parent.mkdir(parents=True, exist_ok=True)
    _KEY_PATH.write_bytes(key)
    os.chmod(_KEY_PATH, 0o600)
    logger.warning(
        "Storing encryption key in plaintext at %s (degraded mode — OS keyring "
        "unavailable). Secrets are protected only by filesystem permissions, "
        "not encrypted at rest.",
        _KEY_PATH,
    )


def _store_key(key: bytes) -> None:
    """Persist a Fernet key, preferring the OS keyring over plaintext."""
    if _keyring_set(key):
        return
    _write_plaintext_key(key)


def _get_fernet() -> Fernet:
    # 1. Prefer the OS keyring.
    try:
        keyring_key = _keyring_get()
    except Exception:  # NoKeyringError / KeyringError / backend hiccup
        logger.warning("OS keyring unavailable when reading encryption key", exc_info=True)
        keyring_key = None
    if keyring_key is not None:
        return Fernet(keyring_key)

    # 2. Migrate a legacy plaintext key file into the keyring (delete only
    #    after a verified keyring write+read-back).
    if _KEY_PATH.exists():
        key = _KEY_PATH.read_bytes()
        if _keyring_set(key):
            try:
                _KEY_PATH.unlink()
            except OSError:
                logger.warning("Could not remove legacy plaintext key file %s", _KEY_PATH)
            logger.info("Migrated encryption key from plaintext file into OS keyring")
        return Fernet(key)

    # 3. First-ever setup: generate a new key and store it.
    key = Fernet.generate_key()
    _store_key(key)
    return Fernet(key)


_fernet: Fernet | None = None


def _cipher() -> Fernet:
    global _fernet
    if _fernet is None:
        _fernet = _get_fernet()
    return _fernet


# ---------------------------------------------------------------------------
# In-memory cache
# ---------------------------------------------------------------------------

_cache: dict | None = None
_migrated: bool = False


# ---------------------------------------------------------------------------
# Migration
# ---------------------------------------------------------------------------


def _parse_legacy_env(path: Path) -> dict[str, str]:
    result: dict[str, str] = {}
    if not path.exists():
        return result
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        key = key.strip()
        val = val.strip().strip("'\"").strip()
        if key in _DEFAULT_ENV:
            result[key] = val
    return result


def _parse_legacy_settings(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        logger.warning("Corrupted web_settings.json, skipping migration for settings")
        return {}


def _migrate_if_needed() -> dict:
    global _migrated
    if _migrated:
        return _cache or {"env": dict(_DEFAULT_ENV), "settings": dict(_DEFAULT_SETTINGS)}

    env_from_legacy: dict[str, str] = {}
    settings_from_legacy: dict = {}

    for env_path in (_LEGACY_ENV_PATH, _LEGACY_ENV_PATH.with_suffix(".env.bak")):
        if env_path.exists():
            env_from_legacy = _parse_legacy_env(env_path)
            break
    for settings_path in (_LEGACY_SETTINGS_PATH, _LEGACY_SETTINGS_PATH.with_suffix(".json.bak")):
        if settings_path.exists():
            settings_from_legacy = _parse_legacy_settings(settings_path)
            break

    merged_env = dict(_DEFAULT_ENV)
    merged_env.update(env_from_legacy)
    merged_settings = dict(_DEFAULT_SETTINGS)
    if settings_from_legacy:
        for key, val in settings_from_legacy.items():
            if key in merged_settings and isinstance(merged_settings[key], dict) and isinstance(val, dict):
                merged_settings[key] = {**merged_settings[key], **val}
            else:
                merged_settings[key] = val

    config = {"env": merged_env, "settings": merged_settings}
    _generate_key_if_missing()
    _persist(config)

    for legacy_path in (_LEGACY_ENV_PATH, _LEGACY_SETTINGS_PATH):
        if legacy_path.exists():
            try:
                legacy_path.rename(legacy_path.with_suffix(legacy_path.suffix + ".bak"))
            except OSError:
                pass

    _migrated = True
    logger.info("Migrated legacy config to encrypted store at %s", _ENCRYPTED_PATH)
    return config


def _generate_key_if_missing() -> None:
    """Ensure an encryption key exists. Called on recovery paths where
    the key may have been deleted but the encrypted store is intact."""
    try:
        existing = _keyring_get()
    except Exception:
        existing = None
    if existing is not None or _KEY_PATH.exists():
        return
    key = Fernet.generate_key()
    _store_key(key)
    global _fernet
    _fernet = Fernet(key)
    logger.warning("Generated new encryption key — previously encrypted data is lost")


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------


def _persist(config: dict) -> None:
    _ENCRYPTED_PATH.parent.mkdir(parents=True, exist_ok=True)
    plain = json.dumps(config, ensure_ascii=False, indent=2).encode("utf-8")
    encrypted = _cipher().encrypt(plain)
    tmp = _ENCRYPTED_PATH.with_suffix(".enc.tmp")
    try:
        tmp.write_bytes(encrypted)
        tmp.replace(_ENCRYPTED_PATH)
    finally:
        try:
            if tmp.exists():
                tmp.unlink()
        except OSError:
            pass


def _read_config() -> dict:
    if not _ENCRYPTED_PATH.exists():
        return _migrate_if_needed()
    try:
        encrypted = _ENCRYPTED_PATH.read_bytes()
        plain = _cipher().decrypt(encrypted)
        config = json.loads(plain.decode("utf-8"))
        return _apply_settings_migrations(config)
    except Exception:
        logger.exception("Failed to decrypt config store, attempting migration")
        return _migrate_if_needed()


_SETTINGS_MIGRATIONS_DONE: bool = False


def _apply_settings_migrations(config: dict) -> dict:
    """One-time migrations for renamed/deprecated settings keys."""
    global _SETTINGS_MIGRATIONS_DONE
    if _SETTINGS_MIGRATIONS_DONE:
        return config

    settings = config.setdefault("settings", {})
    changed = False

    # v1 → v2: wechat_notify_scheduled merged into notify_wechat
    if "wechat_notify_scheduled" in settings and "notify_wechat" not in settings:
        settings["notify_wechat"] = settings.pop("wechat_notify_scheduled")
        changed = True

    if changed:
        _persist(config)
        logger.info("Applied settings migration")

    _SETTINGS_MIGRATIONS_DONE = True
    return config


def _ensure_loaded() -> dict:
    global _cache
    if _cache is None:
        _cache = _read_config()
    return _cache


# ---------------------------------------------------------------------------
# Public API — Env
# ---------------------------------------------------------------------------


def get_env(key: str, default: str = "") -> str:
    config = _ensure_loaded()
    return config.get("env", {}).get(key, _DEFAULT_ENV.get(key, default))


def set_env(key: str, value: str) -> None:
    config = _ensure_loaded()
    config.setdefault("env", {})[key] = str(value)
    _persist(config)
    os.environ[key] = str(value)


def set_env_many(updates: dict[str, str]) -> None:
    config = _ensure_loaded()
    for key, value in updates.items():
        config.setdefault("env", {})[key] = str(value)
        os.environ[key] = str(value)
    _persist(config)


def get_all_env() -> dict[str, str]:
    config = _ensure_loaded()
    return dict(config.get("env", {}))


def get_editable_env_meta() -> list[dict]:
    config = _ensure_loaded()
    env = config.get("env", {})
    meta = [
        {"key": "OPENAI_API_KEY", "label": "LLM API Key", "masked": True},
        {"key": "OPENAI_BASE_URL", "label": "LLM Endpoint", "masked": False},
        {"key": "OPENAI_MODEL", "label": "Model Name", "masked": False},
        {"key": "TELEGRAM_BOT_TOKEN", "label": "Telegram Token", "masked": True},
        {"key": "WECHAT_BOT_TOKEN", "label": "WeChat Token", "masked": True},
        {"key": "AMAP_API_KEY", "label": "高德地图 Key", "masked": True},
    ]
    result = []
    for m in meta:
        value = env.get(m["key"], _DEFAULT_ENV.get(m["key"], ""))
        entry = {"key": m["key"], "label": m["label"], "masked": m["masked"], "value": value}
        if m["masked"] and value:
            entry["value"] = _mask_value(value)
        result.append(entry)
    return result


def _mask_value(value: str, show: int = 4) -> str:
    if len(value) <= show:
        return "•" * min(len(value), 4)
    return "•" * min(len(value) - show, 24) + value[-show:]


# ---------------------------------------------------------------------------
# Public API — Settings
# ---------------------------------------------------------------------------


def get_setting(key: str, default=None):
    config = _ensure_loaded()
    return config.get("settings", {}).get(key, _DEFAULT_SETTINGS.get(key, default))


def set_setting(key: str, value) -> None:
    config = _ensure_loaded()
    config.setdefault("settings", {})[key] = value
    _persist(config)


def get_all_settings() -> dict:
    config = _ensure_loaded()
    settings = dict(_DEFAULT_SETTINGS)
    saved = config.get("settings", {})
    for key, val in saved.items():
        if key in settings and isinstance(settings[key], dict) and isinstance(val, dict):
            settings[key] = {**settings[key], **val}
        else:
            settings[key] = val
    return settings


def reset_all() -> None:
    global _cache
    _cache = {"env": dict(_DEFAULT_ENV), "settings": dict(_DEFAULT_SETTINGS)}
    _persist(_cache)


# ---------------------------------------------------------------------------
# Specific settings accessors (used by callers that need typed returns)
# ---------------------------------------------------------------------------


def get_models() -> list[dict]:
    return get_setting("models", _DEFAULT_MODELS)


def save_models(models: list[dict]) -> None:
    set_setting("models", list(models))


def get_vision_models() -> list[dict]:
    return get_setting("vision_models", _DEFAULT_VISION_MODELS)


def _parse_ctx_str(ctx_str: str) -> int:
    """Parse '128K' / '1M' / '200000' into an int token count. 0 if unknown."""
    s = str(ctx_str or "").strip().upper()
    if not s:
        return 0
    try:
        if s.endswith("M"):
            return int(float(s[:-1]) * 1_000_000)
        if s.endswith("K"):
            return int(float(s[:-1]) * 1_000)
        return int(float(s))
    except ValueError:
        return 0


def get_current_ctx_limit() -> int:
    """Context-window size (in tokens) of the active primary model. 0 if unknown."""
    from cyrene import config
    model_name = str(getattr(config, "OPENAI_MODEL", "") or "").strip()
    for model in (get_models() or []):
        if model.get("model") == model_name or model.get("name") == model_name:
            limit = _parse_ctx_str(model.get("ctx", ""))
            if limit:
                return limit
    for model in (get_vision_models() or []):
        if model.get("model") == model_name or model.get("name") == model_name:
            limit = _parse_ctx_str(model.get("ctx", ""))
            if limit:
                return limit
    ml = model_name.lower()
    if "claude" in ml or any(x in ml for x in ("opus-4", "sonnet-4", "haiku-4")):
        return 200_000
    if "gpt-4" in ml:
        return 128_000
    if "gpt-3.5" in ml:
        return 16_000
    if any(x in ml for x in ("deepseek", "qwen")):
        return 128_000
    if "gemini" in ml:
        return 1_000_000
    return 0


def save_vision_models(models: list[dict]) -> None:
    set_setting("vision_models", list(models))


def get_secondary_model() -> dict:
    return get_setting("secondary_model", {"model": "", "name": "", "api_key": "", "base_url": "", "ctx_limit": 0, "max_concurrency": 0})


def save_secondary_model(model: dict) -> None:
    set_setting("secondary_model", {
        "model": str(model.get("model") or "").strip(),
        "name": str(model.get("name") or str(model.get("model") or "")).strip(),
        "api_key": str(model.get("api_key") or "").strip(),
        "base_url": str(model.get("base_url") or "").strip(),
        "ctx_limit": int(model.get("ctx_limit") or 0),
        "max_concurrency": int(model.get("max_concurrency") or 0),
    })


def get_enabled_tools() -> dict[str, bool]:
    return dict(get_setting("enabled_tools", _DEFAULT_ENABLED_TOOLS))


def save_enabled_tools(tools: dict[str, bool]) -> None:
    protected = {"quit"}
    clean = {k: v for k, v in tools.items() if k not in protected}
    set_setting("enabled_tools", clean)


def is_tool_enabled(name: str) -> bool:
    if name == "quit":
        return True
    return get_enabled_tools().get(name, True)


def get_spawn_policy() -> str:
    value = str(get_setting("spawn_policy", "conservative") or "conservative").strip().lower()
    return value if value in {"aggressive", "conservative", "off"} else "conservative"


def get_workspace_history() -> list[str]:
    return get_setting("workspace_history", [])


def add_workspace_to_history(path: str) -> None:
    history = [p for p in get_workspace_history() if p != path]
    history.insert(0, path)
    if len(history) > 10:
        history = history[:10]
    set_setting("workspace_history", history)


def is_workspace_active() -> bool:
    return get_setting("workspace_active", True)


def set_workspace_active(active: bool) -> None:
    set_setting("workspace_active", active)


def get_write_permission_mode() -> str:
    value = str(get_setting("write_permission_mode", "workspace_only") or "workspace_only").strip().lower()
    return value if value in {"workspace_only", "full_access"} else "workspace_only"


def set_write_permission_mode(mode: str) -> None:
    normalized = str(mode or "workspace_only").strip().lower()
    if normalized not in {"workspace_only", "full_access"}:
        normalized = "workspace_only"
    set_setting("write_permission_mode", normalized)


def is_soul_active() -> bool:
    return get_setting("soul_active", True)


def set_soul_active(active: bool) -> None:
    set_setting("soul_active", active)


def get_heartbeat_interval() -> int:
    return int(get_setting("heartbeat_interval", 1800) or 1800)
