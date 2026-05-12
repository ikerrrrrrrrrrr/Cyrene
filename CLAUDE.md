# Cyrene — 开发规范

## ⚠️ 铁律：debug 日志

**任何时候开发、调试、测试，都必须带 `--verbose` 运行。**

```bash
python -m cyrene.local_cli --verbose
```

每次 LLM 调用的完整内容（prompt、tool list、response、tool call 参数和结果）会记录到 `data/debug_YYYYMMDD_HHMMSS.jsonl`。

为什么：
- 没有日志 = 靠猜。有日志 = 直接看 LLM 收到了什么、输出了什么。
- 每次让 LLM 重新解释问题 debug = 浪费 token。
- "你看看日志"比"你描述一下发生了什么"快一百倍。

日志文件可能会很大，定期清理不需要的老文件即可。但**在调试完之前不要删当前 session 的日志**。

## 项目结构

```
cyrene/
├── src/cyrene/
│   ├── agent.py          # 主 agent loop + tool 系统 + subagent
│   ├── bot.py            # Telegram 接口
│   ├── config.py         # 环境变量 + 路径
│   ├── conversations.py  # 对话归档
│   ├── db.py             # SQLite 任务管理
│   ├── debug.py          # verbose debug 日志
│   ├── inbox.py          # agent 间 inbox 通信
│   ├── local_cli.py      # CLI 模式
│   ├── memory.py         # 记忆系统初始化
│   ├── scheduler.py      # heartbeat + 抽签 + 定时任务
│   ├── search.py         # 搜索（SearxNG + DDG + Bing + Baidu）
│   ├── setup.py          # 人格注入向导
│   ├── short_term.py     # 短期记忆管理
│   ├── soul.py           # SOUL.md 记忆系统
│   └── subagent.py       # 子 agent 注册表
├── docker-compose.yml    # SearxNG 容器
├── workspace/            # 运行时文件（SOUL.md、conversations/）
├── data/                 # 运行时数据（state.json、short_term.json、debug_*.jsonl）
└── store/                # SQLite 数据库
```

## 架构要点

### Agent 流程

```
用户消息 → Phase 1（use_tools + quit，轻量）
          ├── 纯聊天 → 直接回（1 次 LLM 调用）
          └── 需要工具 → Phase 2（所有 tool，可 spawn subagent）
```

### Phase 2 传参规则

**必须使用原始用户消息，不能用 LLM 改写过的 task。** `use_tools` tool 的参数不保留原意，已多次证实。

### Chat Filter

- 读 SOUL.md → 翻译助理语气为角色语气
- 主 agent 不关心人格设定
- 没有 SOUL.md 时用默认朋友语气

### Subagent

- 用 `spawn_subagent` tool → 不要写 Python 脚本模拟
- Subagent 有和主 agent 相同的 tool 能力
- 通过 `send_agent_message` 互相通信
- 完成任务调 `quit`

## 关键设计决策

| 决策 | 原因 |
|------|------|
| 不用 RAG | 语义检索在伴侣场景不可靠 |
| Quit tool 兜底 | LLM 产出空/乱码时自动回到 loop 起点 |
| 两阶段 agent | 纯聊天不进重循环，省 token |
| SOUL.md → Chat Filter | 主 agent 不受角色语气约束 |
| SearxNG 优先搜索 | 自建搜索，不限流、不劫持 |
| 不要 fallback | 搜索就要真搜，搜不到就等冷却再试 |

## 常见命令

```bash
# CLI 模式
conda activate cyrene
python -m cyrene.local_cli

# 调试模式
python -m cyrene.local_cli --verbose

# 人格注入向导（首次启动自动运行）
# 也可以通过 /h → 1 重新注入
