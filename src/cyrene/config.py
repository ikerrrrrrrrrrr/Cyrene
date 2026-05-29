import os
import sys
from pathlib import Path

from cyrene import config_store as _store


def _strip_wrapping_quotes(value: str | None) -> str:
    text = str(value or "").strip()
    if len(text) >= 2 and text[0] == text[-1] and text[0] in {"'", '"'}:
        return text[1:-1].strip()
    return text


def _bundle_contents_dir() -> Path | None:
    """Return ``.../MyApp.app/Contents`` when running from a macOS app bundle."""
    exe = Path(sys.executable).resolve()
    parts = exe.parts
    for idx, part in enumerate(parts):
        if part.endswith(".app") and idx + 2 < len(parts) and parts[idx + 1] == "Contents":
            return Path(*parts[: idx + 2])
    return None


def _is_bundled() -> bool:
    """检测是否为 PyInstaller 打包后的运行环境。"""
    return getattr(sys, "frozen", False) or _bundle_contents_dir() is not None


def _get_user_data_dir() -> Path:
    """返回平台特定的用户数据目录。"""
    if sys.platform == "darwin":
        base = Path.home() / "Library" / "Application Support"
    elif sys.platform == "win32":
        base = Path(os.environ.get("APPDATA", Path.home() / "AppData" / "Roaming"))
    else:
        xdg = os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share")
        base = Path(xdg)
    return base / "Cyrene"


def _get_source_root() -> Path:
    """返回源码根目录或打包资源根目录。"""
    if _is_bundled() and hasattr(sys, "_MEIPASS"):
        return Path(sys._MEIPASS)
    bundle_contents = _bundle_contents_dir()
    if bundle_contents is not None:
        for candidate in (bundle_contents / "Resources", bundle_contents / "Frameworks"):
            if (candidate / "pyproject.toml").exists() or (candidate / ".env.example").exists():
                return candidate
    return Path(__file__).resolve().parent.parent.parent


SOURCE_ROOT = _get_source_root()

# 确定 BASE_DIR：打包模式用用户数据目录，源码模式用项目根目录
if _is_bundled():
    BASE_DIR = _get_user_data_dir()
else:
    BASE_DIR = SOURCE_ROOT

# 路径
WORKSPACE_DIR = BASE_DIR / "workspace"      # 工作区，存放 SOUL.md、CLAUDE.md 等运行时文件
STORE_DIR = BASE_DIR / "store"              # 持久化存储，数据库文件
DATA_DIR = BASE_DIR / "data"                # 运行时数据，状态文件、收件箱等
DB_PATH = STORE_DIR / "cyrene.db"           # SQLite 数据库路径
STATE_FILE = DATA_DIR / "state.json"        # 运行时状态持久化
LOTTERY_FILE = DATA_DIR / "lottery_state.json"  # 抽奖状态持久化
INBOX_DIR = DATA_DIR / "inbox"              # 收件箱目录，存放外部消息
SOUL_PATH = WORKSPACE_DIR / "SOUL.md"       # 人格/身份文件

# Pattern (automatic script learning)
PATTERNS_DIR = WORKSPACE_DIR / "patterns"

# —— 从加密配置加载环境变量并注入 os.environ ——
_env = _store.get_all_env()
for _k, _v in _env.items():
    if _v:
        os.environ.setdefault(_k, _v)

# === Bot 配置 ===
TELEGRAM_BOT_TOKEN = _store.get_env("TELEGRAM_BOT_TOKEN") or None
OWNER_ID = int(os.environ["OWNER_ID"]) if os.environ.get("OWNER_ID") else None

# === WeChat 配置 ===
WECHAT_BOT_TOKEN = _store.get_env("WECHAT_BOT_TOKEN", "")
WECHAT_OWNER_ID = _store.get_env("WECHAT_OWNER_ID", "")

# === LLM 配置 ===
DEFAULT_OPENAI_BASE_URL = "https://api.deepseek.com/v1"
DEFAULT_OPENAI_MODEL = "deepseek-v4-flash"

OPENAI_API_KEY = _strip_wrapping_quotes(_store.get_env("OPENAI_API_KEY", ""))
OPENAI_BASE_URL = _strip_wrapping_quotes(_store.get_env("OPENAI_BASE_URL", DEFAULT_OPENAI_BASE_URL))
OPENAI_MODEL = _strip_wrapping_quotes(_store.get_env("OPENAI_MODEL", DEFAULT_OPENAI_MODEL))
# 禁止使用 pro 型号（消耗太快）
if "pro" in OPENAI_MODEL.lower():
    import logging
    logging.getLogger(__name__).warning("Refusing to use Pro model: %s. Falling back to deepseek-v4-flash", OPENAI_MODEL)
    OPENAI_MODEL = "deepseek-v4-flash"

# === Agent 配置 ===
ASSISTANT_NAME = _store.get_env("ASSISTANT_NAME", "Cyrene")
MAX_TOOL_ROUNDS = int(_store.get_env("MAX_TOOL_ROUNDS", "15"))
MAX_HISTORY_MESSAGES = int(_store.get_env("MAX_HISTORY_MESSAGES", "40"))
MAX_TOOL_OUTPUT_CHARS = int(_store.get_env("MAX_TOOL_OUTPUT_CHARS", "12000"))

# === Scheduler 配置 ===
HEARTBEAT_INTERVAL = int(_store.get_env("HEARTBEAT_INTERVAL", "300"))
HEARTBEAT_LOTTERY_INTERVAL = int(_store.get_env("HEARTBEAT_LOTTERY_INTERVAL", "1800"))
SCHEDULER_INTERVAL = int(_store.get_env("SCHEDULER_INTERVAL", "60"))

# === Daytime 配置 ===
DAYTIME_START = int(_store.get_env("DAYTIME_START", "6"))
DAYTIME_END = int(_store.get_env("DAYTIME_END", "22"))

# === Lottery 配置 ===
LOTTERY_DELTA = float(_store.get_env("LOTTERY_DELTA", "0.15"))
LOTTERY_MAX = float(_store.get_env("LOTTERY_MAX", "0.85"))

# === 搜索配置 ===
SEARCH_PROXY = _store.get_env("SEARCH_PROXY", "")
SEARXNG_URL = _store.get_env("SEARXNG_URL", "")
SEARXNG_AUTO_START = (os.environ.get("SEARXNG_AUTO_START") or _store.get_env("SEARXNG_AUTO_START", "1")) not in ("0", "false", "no")
SEARXNG_PORT = int(_store.get_env("SEARXNG_PORT", "8888"))
SEARXNG_HOST = _store.get_env("SEARXNG_HOST", "127.0.0.1")

# === Steward 配置 ===
STEWARD_INTERVAL = int(_store.get_env("STEWARD_INTERVAL", "1800"))

PATTERN_DETECTION_INTERVAL = int(_store.get_env("PATTERN_DETECTION_INTERVAL", "600"))

# Web UI
WEB_PORT = int(os.environ.get("WEB_PORT") or _store.get_env("WEB_PORT", "4242"))


# 可在 Web UI 中编辑的 key 白名单
_EDITABLE_KEYS = {
    "OPENAI_API_KEY":    {"label": "LLM API Key",   "masked": True},
    "OPENAI_BASE_URL":   {"label": "LLM Endpoint",  "masked": False},
    "OPENAI_MODEL":      {"label": "Model Name",    "masked": False},
    "TELEGRAM_BOT_TOKEN": {"label": "Telegram Token","masked": True},
    "WECHAT_BOT_TOKEN":  {"label": "WeChat Token",  "masked": True},
}


def read_env_file() -> dict[str, str]:
    """Read all editable .env keys from the encrypted store."""
    all_env = _store.get_all_env()
    return {k: v for k, v in all_env.items() if k in _EDITABLE_KEYS}


def write_env_keys(updates: dict[str, str]) -> bool:
    """Write one or more env keys to the encrypted store.  Also update os.environ + module globals."""
    filtered = {}
    for key, value in updates.items():
        if key not in _EDITABLE_KEYS and key != "WECHAT_OWNER_ID":
            continue
        filtered[key] = _strip_wrapping_quotes(value)

    if not filtered:
        return True

    _store.set_env_many(filtered)
    _apply_env_updates(filtered)
    return True


def _apply_env_updates(updates: dict[str, str]) -> None:
    """Reflect env changes in this module's globals."""
    import sys as _sys
    _mod = _sys.modules[__name__]
    for key, value in updates.items():
        if key == "OPENAI_API_KEY":
            _mod.OPENAI_API_KEY = value
        elif key == "OPENAI_BASE_URL":
            _mod.OPENAI_BASE_URL = value
        elif key == "OPENAI_MODEL":
            _mod.OPENAI_MODEL = value
        elif key == "TELEGRAM_BOT_TOKEN":
            _mod.TELEGRAM_BOT_TOKEN = value
        elif key == "WECHAT_BOT_TOKEN":
            _mod.WECHAT_BOT_TOKEN = value
        elif key == "WECHAT_OWNER_ID":
            _mod.WECHAT_OWNER_ID = value


def get_env_keys_meta() -> list[dict]:
    """Return editable .env keys with metadata for the Web UI."""
    return _store.get_editable_env_meta()


def mask_value(value: str, show: int = 4) -> str:
    """Mask a secret value, showing only the last N chars."""
    if len(value) <= show:
        return "•" * min(len(value), 4)
    return "•" * min(len(value) - show, 24) + value[-show:]


def get_chat_workspace(chat_id: int) -> Path:
    """Get workspace directory for a specific chat.

    Currently all chats share the same workspace (single-user mode).
    Future: Each chat can have isolated workspace for multi-user/group support.

    Example future structure:
        workspace/
        └── chats/
            ├── 123456/       # user chat
            │   ├── CLAUDE.md
            │   └── conversations/
            └── -987654/      # group chat (negative ID)
                ├── CLAUDE.md
                └── conversations/
    """
    # Single-user mode: all chats use the same workspace
    return WORKSPACE_DIR

    # Future multi-user mode (uncomment when needed):
    # chat_dir = WORKSPACE_DIR / "chats" / str(chat_id)
    # chat_dir.mkdir(parents=True, exist_ok=True)
    # return chat_dir
