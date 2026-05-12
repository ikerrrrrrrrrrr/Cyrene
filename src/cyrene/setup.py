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
    """搜索人物生平+说话方式，写入 SOUL.md。"""
    print(f"\n正在搜索 {name} 的信息...")

    # 1. 用 deep_search 搜索生平背景（用中文确保命中中文内容）
    bio = await _deep_search(f"{name} 生平 个人资料 早年经历 职业生涯 成就")

    # 2. 搜索说话方式
    style = await _deep_search(f"{name} 说话方式 语录 经典台词 直播风格 性格")

    # 3. 用 LLM 分析并生成 SOUL.md 内容
    soul_content = await _generate_soul_from_research(name, bio, style)

    # 4. 写入 SOUL.md
    soul_path = get_soul_path()
    soul_path.write_text(soul_content, encoding="utf-8")
    print(f"已为 {name} 创建人格文件！")
    print(f"   文件: {soul_path}")


async def _deep_search(query: str) -> str:
    """使用 deep_search 搜索并返回结果文本。"""
    from cyrene.search import deep_search
    try:
        result = await deep_search(query)
        return result or ""
    except Exception:
        return ""


async def _generate_soul_from_research(name: str, bio: str, style: str) -> str:
    """用 LLM 根据搜索生成 SOUL.md。"""
    from cyrene.agent import _call_llm, _assistant_text

    prompt = f"""Based on the research below, create a SOUL.md file for an AI that will personify {name}.

Research about {name}:
{bio[:3000]}

Speaking style of {name}:
{style[:3000]}

Generate a SOUL.md that captures:
1. SELF:IDENTITY - Who this person is (key traits, background, values)
2. SELF:BELIEFS - Core beliefs and worldview
3. RELATIONSHIP:USER - How they would treat a close friend
4. SPEECH:PATTERNS - Detailed speaking style analysis:
   - Sentence length (short/long/mixed)
   - Vocabulary level (simple/formal/technical)
   - Typical phrases and signature expressions
   - Tone (warm/formal/humorous/serious)
   - Conversation style (direct/indirect, uses questions, interrupts, etc.)
   - Examples of how they would say common things
5. MEMORY:HIGHLIGHT - Key life events and achievements

Format in clean markdown. Be specific and detailed about speaking style - include actual example phrases."""

    try:
        response = await _call_llm([
            {"role": "system", "content": "You create detailed personality profiles. Be specific and include speech pattern examples."},
            {"role": "user", "content": prompt}
        ], tools=None)
        result = _assistant_text(response) or ""
        if result:
            return f"# {name}'s Soul\n\n" + result
    except Exception:
        pass

    # Fallback: 生成一个简单的 SOUL.md
    return f"""# {name}'s Soul

## SELF:IDENTITY
- I embody the persona of {name}.
- My responses reflect {name}'s known personality and communication style.

## RELATIONSHIP:USER
- I treat the user as a trusted friend.

## SPEECH:PATTERNS
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
