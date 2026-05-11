import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

# Optional runtime configuration
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
OWNER_ID = int(os.environ["OWNER_ID"]) if os.getenv("OWNER_ID") else None
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "dummy")
OPENAI_BASE_URL = os.getenv("OPENAI_BASE_URL", "http://127.0.0.1:1234/v1")
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "google/gemma-4-e4b")

# Optional
ASSISTANT_NAME = os.getenv("ASSISTANT_NAME", "Ape")
SCHEDULER_INTERVAL = int(os.getenv("SCHEDULER_INTERVAL", "60"))

# Paths
BASE_DIR = Path(__file__).resolve().parent.parent.parent
WORKSPACE_DIR = BASE_DIR / "workspace"
STORE_DIR = BASE_DIR / "store"
DATA_DIR = BASE_DIR / "data"
DB_PATH = STORE_DIR / "cyrene.db"
STATE_FILE = DATA_DIR / "state.json"


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
