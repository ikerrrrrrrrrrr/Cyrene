import os
from pathlib import Path

from dotenv import load_dotenv

# 从项目根目录加载 .env，不依赖 CWD
_base_dir = Path(__file__).resolve().parent.parent.parent
load_dotenv(_base_dir / ".env")

# === Bot 配置 ===
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
OWNER_ID = int(os.environ["OWNER_ID"]) if os.getenv("OWNER_ID") else None

# === LLM 配置 ===
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
OPENAI_BASE_URL = os.getenv("OPENAI_BASE_URL", "https://api.deepseek.com/v1")
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "deepseek-chat")
# 禁止使用 pro 型号（消耗太快）
if "pro" in OPENAI_MODEL.lower():
    import logging
    logging.getLogger(__name__).warning("Refusing to use Pro model: %s. Falling back to deepseek-v4-flash", OPENAI_MODEL)
    OPENAI_MODEL = "deepseek-v4-flash"

# === Agent 配置 ===
ASSISTANT_NAME = os.getenv("ASSISTANT_NAME", "Cyrene")
MAX_TOOL_ROUNDS = int(os.getenv("MAX_TOOL_ROUNDS", "15"))
MAX_HISTORY_MESSAGES = int(os.getenv("MAX_HISTORY_MESSAGES", "40"))
MAX_TOOL_OUTPUT_CHARS = int(os.getenv("MAX_TOOL_OUTPUT_CHARS", "12000"))

# === Scheduler 配置 ===
HEARTBEAT_INTERVAL = int(os.getenv("HEARTBEAT_INTERVAL", "300"))  # 5 分钟
HEARTBEAT_LOTTERY_INTERVAL = int(os.getenv("HEARTBEAT_LOTTERY_INTERVAL", "1800"))  # 30 分钟
SCHEDULER_INTERVAL = int(os.getenv("SCHEDULER_INTERVAL", "60"))

# === Daytime 配置 ===
DAYTIME_START = int(os.getenv("DAYTIME_START", "6"))    # 6:00
DAYTIME_END = int(os.getenv("DAYTIME_END", "22"))       # 22:00

# === Lottery 配置 ===
LOTTERY_DELTA = float(os.getenv("LOTTERY_DELTA", "0.15"))
LOTTERY_MAX = float(os.getenv("LOTTERY_MAX", "0.85"))

# === 搜索配置 ===
SEARCH_PROXY = os.getenv("SEARCH_PROXY", "")  # 搜索用代理，如 http://127.0.0.1:7890
SEARXNG_URL = os.getenv("SEARXNG_URL", "")  # SearxNG 自建搜索，如 http://localhost:8888
SEARXNG_AUTO_START = os.getenv("SEARXNG_AUTO_START", "1") not in ("0", "false", "no")  # 自动启动 SimpleXNG
SEARXNG_PORT = int(os.getenv("SEARXNG_PORT", "8888"))  # SearXNG 监听端口
SEARXNG_HOST = os.getenv("SEARXNG_HOST", "127.0.0.1")  # SearXNG 绑定地址

# === Steward 配置 ===
STEWARD_INTERVAL = int(os.getenv("STEWARD_INTERVAL", "1800"))  # 30 分钟

# === 路径 ===
BASE_DIR = Path(__file__).resolve().parent.parent.parent
WORKSPACE_DIR = BASE_DIR / "workspace"      # 工作区，存放 SOUL.md、CLAUDE.md 等运行时文件
STORE_DIR = BASE_DIR / "store"              # 持久化存储，数据库文件
DATA_DIR = BASE_DIR / "data"                # 运行时数据，状态文件、收件箱等
DB_PATH = STORE_DIR / "cyrene.db"           # SQLite 数据库路径
STATE_FILE = DATA_DIR / "state.json"        # 运行时状态持久化
LOTTERY_FILE = DATA_DIR / "lottery_state.json"  # 抽奖状态持久化
INBOX_DIR = DATA_DIR / "inbox"              # 收件箱目录，存放外部消息
SOUL_PATH = WORKSPACE_DIR / "SOUL.md"       # 人格/身份文件

# Web UI
WEB_PORT = int(os.getenv("WEB_PORT", "4242"))


# .env 文件路径
_ENV_PATH = _base_dir / ".env"

# 可在 Web UI 中编辑的 key 白名单
_EDITABLE_KEYS = {
    "OPENAI_API_KEY":    {"label": "LLM API Key",   "masked": True},
    "OPENAI_BASE_URL":   {"label": "LLM Endpoint",  "masked": False},
    "OPENAI_MODEL":      {"label": "Model Name",    "masked": False},
    "TELEGRAM_BOT_TOKEN": {"label": "Telegram Token","masked": True},
}


def read_env_file() -> dict[str, str]:
    """Read all editable .env keys (actual values from file, not env vars)."""
    result: dict[str, str] = {}
    if not _ENV_PATH.exists():
        return result
    for line in _ENV_PATH.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            continue
        key, _, val = line.partition("=")
        key = key.strip()
        if key in _EDITABLE_KEYS:
            result[key] = val.strip()
    return result


def write_env_keys(updates: dict[str, str]) -> bool:
    """Write one or more .env keys.  Also update the running os.environ + module globals.
    Returns True on success.
    """
    from dotenv import set_key as dotenv_set_key

    for key, value in updates.items():
        if key not in _EDITABLE_KEYS:
            continue
        dotenv_set_key(str(_ENV_PATH), key, value)
        os.environ[key] = value

    # 更新模块级 globals（让 LLM / bot 调用即时生效，无需重启）
    _apply_env_updates(updates)
    return True


def _apply_env_updates(updates: dict[str, str]) -> None:
    """Reflect .env changes in this module's globals."""
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


def get_env_keys_meta() -> list[dict]:
    """Return editable .env keys with metadata for the Web UI."""
    current = read_env_file()
    result = []
    for key, meta in _EDITABLE_KEYS.items():
        entry = {
            "key": key,
            "label": meta["label"],
            "masked": meta["masked"],
            "value": current.get(key, ""),
        }
        if meta["masked"] and entry["value"]:
            entry["value"] = mask_value(entry["value"])
        result.append(entry)
    return result


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
