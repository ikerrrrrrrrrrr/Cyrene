"""
Personality setup wizard. Runs once on first startup.
Sets up SOUL.md with user's chosen personality.
"""

import asyncio
import logging

from cyrene.config import DATA_DIR
from cyrene.soul import get_soul_path

logger = logging.getLogger(__name__)

_SETUP_FLAG = None  # Path to .setup_done


def init_setup_flag():
    global _SETUP_FLAG
    _SETUP_FLAG = DATA_DIR / ".setup_done"


def is_setup_done() -> bool:
    """Check if personality setup has been completed."""
    if _SETUP_FLAG is None:
        return True  # 未初始化时默认已完成，避免阻塞
    return _SETUP_FLAG.exists()


def mark_setup_done() -> None:
    """Create the .setup_done flag file."""
    if _SETUP_FLAG is None:
        return
    try:
        _SETUP_FLAG.parent.mkdir(parents=True, exist_ok=True)
        _SETUP_FLAG.write_text("setup_complete", encoding="utf-8")
    except Exception:
        logger.exception("Failed to mark setup done")


async def run_setup() -> None:
    """Run the interactive personality setup wizard."""
    print()
    print("=" * 50)
    print("欢迎使用 Cyrene！")
    print("=" * 50)
    print()
    print("你可以为 Cyrene 注入一个人格，让它模仿某个特定人物的性格和说话方式。")
    print()
    print("请选择：")
    print("  1) 输入一个名字 → 我上网查这个人的生平、说话方式，然后模仿")
    print("  2) 自己编写 SOUL.md（人格文件）")
    print("  3) 跳过，使用默认人格")
    print()

    choice = input("选择 (1/2/3): ").strip()

    if choice == "1":
        name = input("请输入名字（可以是真实人物或虚构角色）: ").strip()
        if name:
            await _setup_from_name(name)
        else:
            print("名字不能为空，使用默认人格。")
    elif choice == "2":
        await _setup_custom()
    else:
        print("使用默认人格。")

    mark_setup_done()
    print()
    print("设置完成！你可以开始和 Cyrene 聊天了。")
    print()


async def _setup_from_name(name: str) -> None:
    """生成人物的人格文件，直接靠模型内部知识，不搜索。"""
    print(f"\n正在生成 {name} 的人格...")

    soul_content = await _generate_soul_profile(name)

    soul_path = get_soul_path()
    soul_path.write_text(soul_content, encoding="utf-8")
    print(f"已为 {name} 创建人格文件！")
    print(f"   文件: {soul_path}")


async def _generate_soul_profile(name: str) -> str:
    """直接用模型知识生成行为人格文件。"""
    from cyrene.agent import _call_llm, _assistant_text

    prompt = f"""Create a BEHAVIOR PROFILE for an AI that will personify {name}.

This is NOT a biography. This is a set of behavioral rules that tells the AI exactly how to speak and act like {name}.

Use your knowledge about this person. Focus on what makes them unique.

Include EXACTLY these sections:

## CORE IDENTITY
- Who they are in 1 sentence
- Their archetype

## SPEECH PATTERNS
- Sentence structure (short/long, fragmented/complete)
- Verbal tics and fillers
- Signature phrases and catchphrases (exact quotes)
- Vocabulary and tone

## CONTRADICTIONS
- Things they say then immediately deny
- Self-defeating logic loops
- The "30-second self-destruct" pattern

## FIXED MISTAKES
- Words they consistently mispronounce or misspell
- Numbers they always get wrong
- Facts they consistently misremember

## BEHAVIORAL LOOPS
- Circular argument patterns
- Go-to deflection strategies
- Repetitive question cycles

## CLASSIC EXCHANGES
- 2-3 example dialogues showing how they respond to common questions

Write in concise bullet points with exact example quotes. No markdown formatting.
Write the ENTIRE profile in Chinese."""

    try:
        response = await _call_llm([
            {"role": "system", "content": "You create character behavior profiles. Focus on exact speech patterns and behavioral quirks. Use concrete examples."},
            {"role": "user", "content": prompt}
        ], tools=None)
        result = _assistant_text(response) or ""
        if result and len(result) > 100:
            return f"# {name}'s Soul\n\n{result}"
    except Exception:
        pass

    return f"""# {name}'s Soul

## CORE IDENTITY
- I personify {name}.

## SPEECH PATTERNS
- Speaking style modeled after {name}.
"""


async def _setup_custom() -> None:
    """让用户自己提供 SOUL.md。"""
    print("\n请粘贴或输入 SOUL.md 的内容（输入完成后，在新的一行输入 END 并回车）：")
    print("（SOUL.md 定义了 AI 的人格、信念、说话方式等）")
    print()

    lines = []
    while True:
        line = input()
        if line.strip() == "END":
            break
        lines.append(line)

    content = "\n".join(lines).strip()
    if content:
        soul_path = get_soul_path()
        # 确保有标题
        if not content.startswith("# "):
            content = "# Custom Soul\n\n" + content
        soul_path.write_text(content, encoding="utf-8")
        print(f"已写入自定义人格文件！")
    else:
        print("内容为空，使用默认人格。")
