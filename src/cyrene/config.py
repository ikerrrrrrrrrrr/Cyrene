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
