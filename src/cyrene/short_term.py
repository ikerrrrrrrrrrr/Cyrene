"""
Short-term memory management.
Stores compressed conversation summaries that persist across sessions.
Entry lifecycle: conversation -> compressed -> short_term -> (via Steward) -> long_term
"""

import json
import logging
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger(__name__)

# 文件路径由 init_short_term 设置
_SHORT_TERM_FILE: Path | None = None


def init_short_term(data_dir: Path) -> None:
    """初始化短期记忆文件路径。在 __main__.py 启动时调用。"""
    global _SHORT_TERM_FILE
    _SHORT_TERM_FILE = data_dir / "short_term.json"


def load_entries() -> list[dict]:
    """从 short_term.json 加载所有条目。文件不存在时返回空列表。"""
    if _SHORT_TERM_FILE is None or not _SHORT_TERM_FILE.exists():
        return []
    try:
        data = json.loads(_SHORT_TERM_FILE.read_text(encoding="utf-8"))
        return data if isinstance(data, list) else []
    except Exception:
        logger.exception("Failed to load short-term memory")
        return []


def save_entries(entries: list[dict]) -> None:
    """保存条目到 short_term.json。"""
    if _SHORT_TERM_FILE is None:
        return
    try:
        _SHORT_TERM_FILE.parent.mkdir(parents=True, exist_ok=True)
        _SHORT_TERM_FILE.write_text(
            json.dumps(entries, ensure_ascii=False, indent=2),
            encoding="utf-8"
        )
    except Exception:
        logger.exception("Failed to save short-term memory")


def touch_entry(content_keyword: str, metadata: dict | None = None) -> None:
    """
    更新已有条目的 last_mentioned 和 mention_count。
    如果 content_keyword 匹配已有条目，+1 count + 更新时间。
    如果不存在且 metadata 提供，新增条目。
    """
    entries = load_entries()
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    kw_lower = content_keyword.lower()
    found = False
    for entry in entries:
        entry_content = entry.get("content", "").lower()
        # Exact match or one is a near-complete substring of the other
        if kw_lower == entry_content or (
            len(kw_lower) >= len(entry_content) * 0.7 and kw_lower in entry_content
        ) or (
            len(entry_content) >= len(kw_lower) * 0.7 and entry_content in kw_lower
        ):
            entry["last_mentioned"] = now
            entry["mention_count"] = entry.get("mention_count", 1) + 1
            found = True
            break

    if not found and metadata:
        entries.append({
            "content": metadata.get("content", content_keyword),
            "type": metadata.get("type", "fact"),
            "first_seen": now,
            "last_mentioned": now,
            "mention_count": 1,
            "emotional_valence": metadata.get("emotional_valence", 0),
        })

    save_entries(entries)


def get_context(max_chars: int = 5000) -> str:
    """
    格式化短期记忆条目为一个字符串，用于注入 context。
    按 last_mentioned 倒序（最近的最靠前）。
    不超过 max_chars 字符。
    """
    entries = load_entries()
    if not entries:
        return ""

    # 按 last_mentioned 倒序
    sorted_entries = sorted(entries, key=lambda e: e.get("last_mentioned", ""), reverse=True)

    parts: list[str] = ["[Previous context:]"]
    chars_used = len(parts[0])

    for entry in sorted_entries:
        line = f"- {entry.get('content', '')}"
        if chars_used + len(line) + 1 > max_chars:
            break
        parts.append(line)
        chars_used += len(line) + 1

    return "\n".join(parts)


def clear_old_entries(days: int = 7) -> None:
    """
    清除超过 days 天未提及的一次性闲聊条目。
    保留高频（mention_count >= 3）、情感极值（|valence| >= 3）、事实类型条目。
    """
    entries = load_entries()
    now = datetime.now(timezone.utc)

    kept = []
    for e in entries:
        last_str = e.get("last_mentioned", "")
        mention_count = e.get("mention_count", 1)
        valence = e.get("emotional_valence", 0)

        # 保留高频/情感/事实
        if mention_count >= 3 or abs(valence) >= 3 or e.get("type") in ("fact", "preference"):
            kept.append(e)
            continue

        # 检查是否超期
        try:
            last_dt = datetime.strptime(last_str, "%Y-%m-%d")
            if (now - last_dt).days > days:
                continue  # 丢弃
        except (ValueError, TypeError):
            pass
        kept.append(e)

    save_entries(kept)
