# Cyrene CLI 设计

架构：`cyrene <command>` 是一个薄 HTTP 客户端，通过 `localhost:4242` 与后台守护进程通信。

```bash
# 先启动 daemon
cyrene start

# daemon 启动后，以下命令通过 HTTP 调用
cyrene do "写一个 Python 脚本" --session xxx
cyrene session list
cyrene session status --session xxx
cyrene flow --session xxx --round round_xxx
cyrene memory soul
cyrene status
cyrene mcp list
```

---

## 命令一览

| 命令 | 用途 | 对应 Web UI |
|---|---|---|
| `cyrene start` | 启动后台守护进程 | — |
| `cyrene stop` | 停止后台守护进程 | — |
| `cyrene do` | 向 agent 发送消息，等待回复 | Chat → 输入框 → 发送 |
| `cyrene session list` | 列出所有会话 | Sessions 页面 |
| `cyrene session status` | 查看单个会话详情 | Sessions → 详情面板 |
| `cyrene session delete` | 删除一个会话 | Sessions → 删除按钮 |
| `cyrene flow` | 查看 agent 运行轨迹 | Agent Flow 页面 |
| `cyrene memory soul` | 查看/编辑 SOUL.md | Memory → SOUL.md 标签页 |
| `cyrene memory short-term` | 查看短期记忆 | Memory → Short-Term 标签页 |
| `cyrene memory context` | 查看上下文窗口状态 | Memory → Context Window 标签页 |
| `cyrene status` | 查看系统状态 | Status 页面 |
| `cyrene mcp list` | 列出 MCP 服务器和工具 | Settings → MCP Servers |
| `cyrene mcp add` | 添加 MCP 服务器 | Settings → MCP Servers → Add |
| `cyrene mcp remove` | 删除 MCP 服务器 | Settings → MCP Servers → 删除 |
| `cyrene mcp toggle` | enable/disable MCP 服务器 | Settings → MCP Servers → 开关 |

---

## 详细设计

### cyrene start

**用途**：启动 Cyrene 后台守护进程。启动后会在 `localhost:4242` 提供 HTTP 服务，所有其他 CLI 命令通过该端口通信。

```bash
cyrene start
```

```plain text
Cyrene started at http://localhost:4242

可用 session:
  run_live (当前活跃 session，0 条消息)

可用命令示例：
  cyrene do "你的问题" --session run_live
  cyrene status
  cyrene --help
```

输出说明：
- 第一行：服务地址，CLI 客户端从这里连接
- 可用 session：列出当前所有会话，标出默认的 `run_live`
- 可用命令示例：新手引导

额外说明：
- 这个 terminal 是不退出的，关掉它就是关掉 agent
- 需要另启动新的 terminal 和 Cyrene 对话

---

### cyrene stop

**用途**：向后台守护进程发送关闭信号，优雅退出（断开 MCP、停止 scheduler、关闭事件循环）。

```bash
cyrene stop
```

```plain text
Cyrene stopped.
```

输出说明：
- 仅一行确认信息

额外说明：
- 关闭后无法再使用 `cyrene do` 等命令，需要重新 `cyrene start`

---

### cyrene do

**用途**：向指定 session 的 agent 发送一条消息，等待完整回复后输出。这是最核心的命令 — coding agent 通过它让 Cyrene 干活。

参数：
- `--session` / `-s` — session ID（**必填**，不存在则报错）

```bash
cyrene do "搜索AI Agent框架" --session run_live
```

example 0：

(query: hello)
```plain text
▶ Thinking...

Cyrene: 你好，我是Cyrene，一个AI Agent。

---
Session: Say Hello | Tokens: 1 in / 8 out | Duration: 1.1s
```

example 1：

(query: 搜索AI Agent框架)
```plain text
▶ Searching: AI Agent framework...

Cyrene: 以下是关于AI Agent框架的搜索结果：

1. LangChain - 一个用于构建LLM应用的框架
2. AutoGPT - 自主AI Agent

---
Session: run_live | Tokens: 1,234 in / 567 out | Duration: 12.3s
```

example 2：
(query: 生成几个subagent，进行圆桌会议，讨论如何提升程序员的代码水平)

```plain text
▶ Thinking...

Cyrene: 好的，我会生成几个subagent，进行圆桌会议，讨论如何提升程序员的代码水平。

▶ Generating subagents...

Cyrene: 好的，我会生成几个subagent，进行圆桌会议，讨论如何提升程序员的代码水平。

▶ Generated subagent David
▶ Generated subagent John
▶ Generated subagent Mary

Cyrene: Subagents已经开始讨论了，让我们现在等一下回复。

▶ Waiting for subagents...

Cyrene: Subagents的结果都出来了，下面是总结：

xxx xxx xxx

---
Session: run_live | Tokens: 42,424,242 in / 567,765 out | Duration: 128.3s
```
输出说明：
- `▶ Searching:` — 中间进度提示（来自 agent 的 `send_message`）
- 这里，一开始先显示一个thinking，调用了tool就显示 ▶ Tool： xxx called
- 特殊：Searching, Subagents 本质上也是tool，但是这两个tool调用的时候就单独显示searching/generated
- `Cyrene:` — agent 的最终回复（Markdown 文本）
- `---` — 结束分隔线，附带会话元信息

额外说明：
- 支持 `--json` 参数，输出原始 JSON 而非格式化文本，适合 coding agent 解析
- `--session` 是必填参数，session 不存在则报错

---

### cyrene session list

**用途**：列出所有会话（当前的 + 归档的），快速概览每个会话的状态和消息量。

```bash
cyrene session list
```

```plain text
ID                                     标题              状态      消息数   耗时
run_live                               当前会话          running    5条      12.3s
archive_2026-05-19_session_abc123      debug 搜索问题    done    12条     —
archive_2026-05-18_session_def456      日常对话           done    3条      —
```

输出说明：
- `ID`：会话唯一标识，给其他命令的 `--session` 参数使用
- `标题`：自动生成的会话摘要标题
- `状态`：running / done / 错误
- `消息数`：该会话的消息总数
- `耗时`：运行时间（已结束的会话显示 —）

额外说明：
- `run_live` 始终存在，表示当前活跃会话
- 归档会话以 `archive_YYYY-MM-DD_` 开头

---

### cyrene session status

**用途**：查看某个会话的完整详情 — 消息列表、rounds、subagents、shells。

```bash
cyrene session status --session run_live
```

```plain text
Session: run_live
  title: "当前会话"
  status: running
  messages: 5
  tokens: 1,234 in / 567 out
  started: 14:32:00
  duration: 12.3s

Rounds（对话轮次）:
  round_1747000000000    running    12.3s    "搜索AI Agent框架"
  round_1746999900000    done    5.2s     "你好"

Subagents（子 Agent）:
  searcher-1    running    8.1s    "搜索AI Agent框架最新进展"
  summarizer    done    3.2s    "总结搜索结果"

Shells（持久化 Shell）:
  shell_0    running    workspace/    "npm run build"
```

输出说明：
- `Session`：会话元信息（标题、状态、消息数、token 消耗、启动时间、耗时）
- `Rounds`：该会话内的所有对话轮次。每轮对应一次 `cyrene do`
- `Subagents`：该会话中活跃的子 agent
- `Shells`：该会话中的持久化 shell 会话

额外说明：
- `--session` 是必填参数

---

### cyrene session delete

**用途**：删除一个会话。`run_live` 会执行清除（压缩到短期记忆），归档会话会永久删除。

```bash
cyrene session delete --session archive_2026-05-18_session_def456
```

```plain text
Session archive_2026-05-18_session_def456 deleted.
```

输出说明：
- 确认被删除的 session ID

额外说明：
- 删除 `run_live` 会清空当前对话并压缩到短期记忆
- 归档会话删除后不可恢复

---

### cyrene flow

**用途**：查看 agent 的运行轨迹 — 相当于 Web UI 的 Agent Flow 页面的文本版。展示 Phase 1/2 决策、工具调用、subagent 生命周期、最终输出。

```bash
cyrene flow --session run_live
```

```plain text
Rounds（最新优先）:
  round_1747000000000    running    12.3s    "搜索AI Agent框架"
  round_1746999900000    done    5.2s     "你好"
```

```bash
cyrene flow --session run_live --round round_1747000000000
```

特别说明：本处是cyrene最牛逼的功能，用于debug，需要好好对待

```plain text
Round: round_1747000000000
  status: running
  prompt: "搜索AI Agent框架"
  duration: 12.3s
  started_at: 2026-05-19T14:32:00

执行轨迹:
  [Phase 1] [2026-05-19 14:32:00][main_agent][LLM call] 决策 → 需要工具 (42ms) id:000001

  [Phase 2] 执行:
    [2026-05-19 14:32:01][main_agent][LLM call] id:000002
    [2026-05-19 14:32:01][main_agent][Tool call] WebSearch(query="AI Agent框架")  → 3 条结果 (1.2s) id:000003
    [2026-05-19 14:32:02][main_agent][Tool call] spawn_subagent(searcher-1, "搜索框架对比")  → 已启动 (0.1s) id:000004


    [2026-05-19 14:32:04][subagent_searcher-1][LLM call] id:000005
    [2026-05-19 14:32:05][subagent_searcher-1][Tool call] WebSearch(query="AI Agent框架")  → 3 条结果 (1.2s) id:000006
    [2026-05-19 14:32:06][subagent_searcher-1][LLM call] id:000007

    [2026-05-19 14:32:03][main_agent][llm call]  id:000008

    [2026-05-19 14:32:03][main_agent][Tool call] spawn_subagent(summarizer, "总结")  → 已启动 (0.1s) id:000009

    [2026-05-19 14:32:07][subagent_summarizer][LLM call] id:000010

    [2026-05-19 14:32:08][main_agent][llm call]  id:000011
    [2026-05-19 14:32:08][main_agent][tool call] quit id:000012



工具调用: xxx
Subagents: 2 done / 0 running
```

输出说明：
- `Round`：轮次元信息（状态、任务、耗时、启动时间）
- `执行轨迹`：扁平时间线，按实际发生顺序排列
  - 每行格式：`[Phase] [时间][调用者][事件类型] 详情 (耗时) id:XXXXXX`
  - `调用者`：`main_agent` 或 `subagent_xxx`，多个 agent 的事件按时间交错
  - `事件类型`：`LLM call`（模型调用）、`Tool call`（工具调用）
  - `id`：每条事件的唯一标识，用于 `--id` 深度查看
- 底部汇总：工具调用总数、subagent 完成情况

额外说明：
- 不指定 `--round` 时只列出 rounds，不展示详细轨迹
- 支持 `--json` 参数输出原始 JSON

### cyrene flow --id（深度 debug）

**用途**：查看某条事件的完整原始输入输出。如果 id 对应的是 LLM call，展示该次 LLM 调用的完整 prompt + response + tool_calls。如果对应的是 tool call，展示该次工具调用的完整参数和返回结果。

```bash
cyrene flow --session run_live --round round_1747000000000 --id 000002
```

```json
{
  "id": "000002",
  "caller": "main_agent",
  "event_type": "LLM call",
  "input": "raw messages/prompt for LLM call here",
  "output": "raw response from LLM here",
  "tool_calls": [
    {"name": "WebSearch", "args": {"query": "AI Agent框架"}}
  ],
  "duration": "1.2s"
}
```

额外说明：
- `--session` 和 `--round` 是必填的，不能省略
- 此功能需要后端补全才能实现（见下文"后端改造"），非纯套壳




---

### cyrene memory soul

**用途**：查看 SOUL.md 内容（agent 的人格和长期记忆）。

```bash
cyrene memory soul
```

```plain text
# Cyrene's Soul

## SELF:IDENTITY
- I am Cyrene, a personal AI companion.

## RELATIONSHIP:USER
- Trust level: neutral
- Communication style: casual, direct

## TEMPORARY
- User mentioned interest in AI agents (2026-05-19)
```

```bash
cyrene memory soul --edit /path/to/new_content.md
```

```plain text
✅ SOUL.md updated (5 sections, 320 chars).
```

输出说明：
- 不带 `--edit` 时直接输出 SOUL.md 原始内容
- 带 `--edit` 时读取指定文件的内容并覆盖写入 SOUL.md

额外说明：
- SOUL.md 由 Steward Agent 每 30 分钟自动更新
- TEMPORARY 条目在 24 小时后自动过期

---

### cyrene memory short-term

**用途**：查看短期记忆条目，每条带类型标记、情感效价、提及次数。帮助理解 agent 记住了用户的什么信息。

```bash
cyrene memory short-term
```

```plain text
类型        内容                                        提及次数   情感   首次    最近
偏好        用户对AI Agent框架感兴趣                       3        +2     05-19   05-19
事实        用户使用 Windows                              2         0     05-18   05-19
情绪        用户对项目进度感到焦虑                         1        -2     05-19   05-19
```

输出说明：
- `类型`：偏好 / 事实 / 情绪 / 模式
- `内容`：记忆的具体信息
- `提及次数`：该信息被提及的次数
- `情感`：情感效价值（正值积极、负值消极、0 中性）
- `首次`：首次记录日期
- `最近`：最近提及日期

额外说明：
- 支持 `--json` 参数输出原始 JSON
- 高频条目（≥3 次）和情感极值条目自动保留
- 超过 7 天未提及的一次性闲聊条目会被清除

---

### cyrene memory context

**用途**：查看上下文窗口状态 — 当前消息数、上限、压缩阈值。

```bash
cyrene memory context
```

```plain text
Context Window: 5 / 40 条消息
  压缩阈值: 45 条
  下次操作: —（未达阈值）
```

输出说明：
- 当前消息数 / 上限
- 压缩阈值：超过此数量触发后台压缩
- 下次操作：当前是否需要压缩

额外说明：
- 超过 45 条消息时会自动压缩最早的消息到短期记忆

---

### cyrene status

**用途**：查看系统当前状态 — 模型、worker、指标、服务健康度。

```bash
cyrene status
```

```plain text
模型: deepseek-chat
端点: https://api.deepseek.com/v1
运行时间: 2h 34m

Workers:
  main         orchestrator    running    2h 34m    2,345 tokens
  searcher-1   subagent        running    8.1s      204 tokens

指标:
  Sessions: 3（1 活跃，2 归档）
  Subagents: 2（1 running）
  MCP 服务器: 2（2 connected，8 个工具）
  定时任务: 5

服务:
  OpenAI API: ok (120ms)
  SOUL.md: 已加载（5 个章节）
  MCP filesystem: connected（3 个工具）
  MCP github: connected（5 个工具）
```

输出说明：
- 第一段：模型、API 端点、运行时间
- `Workers`：所有 worker（main orchestrator + subagents）的状态
- `指标`：sessions、subagents、MCP、定时任务的汇总
- `服务`：各个外部服务的健康状态和延迟

额外说明：
- 适合 coding agent 在调试前快速了解系统状况

---

### cyrene mcp list

**用途**：列出所有配置的 MCP 服务器及其连接状态、可用工具数量。

```bash
cyrene mcp list
```

```plain text
名称             传输方式    状态         工具数   地址
filesystem       stdio      connected       3        npx -y @modelcontextprotocol/server-filesystem .
github           sse        connected       5        http://localhost:3000/mcp
```

输出说明：
- `名称`：服务器标识名
- `传输方式`：stdio / sse
- `状态`：connected / 断开 / 错误
- `工具数`：该服务器暴露的工具数量
- `地址`：连接命令或 URL

额外说明：
- 显示所有配置的服务器（无论是否connected）
- 状态为"断开"的服务器不会提供工具给 agent

---

### cyrene mcp add

**用途**：添加一个新的 MCP 服务器并自动连接。

```bash
cyrene mcp add filesystem stdio npx -y @modelcontextprotocol/server-filesystem .
```

```plain text
✅ MCP 服务器 'filesystem' added并连接（3 个工具可用）
```

```bash
cyrene mcp add github sse http://localhost:3000/mcp
```

```plain text
✅ MCP 服务器 'github' added并连接（5 个工具可用）
```

输出说明：
- 确认添加结果，显示实际连接到的工具数

额外说明：
- stdio 类型需要提供可执行命令和参数
- SSE 类型需要提供 HTTP URL
- 添加后 agent 立即可以使用新工具

---

### cyrene mcp remove

**用途**：删除一个 MCP 服务器配置并断开连接。

```bash
cyrene mcp remove filesystem
```

```plain text
✅ MCP 服务器 'filesystem' deleted。
```

输出说明：
- 确认被删除的服务器名称

额外说明：
- 删除后 agent 不再能看到该服务器的工具

---

### cyrene mcp toggle

**用途**：enable/disable一个 MCP 服务器（不删除配置）。

```bash
cyrene mcp toggle github
```

```plain text
✅ MCP 服务器 'github' disabled。
```

输出说明：
- 确认该服务器当前状态（enable/disable）

额外说明：
- 禁用后 agent 暂时无法使用该服务器的工具
- 配置保留，再次 toggle 可恢复

---

## 后端改造

为了让 `flow --id` 能深度 debug，需要补以下几处改动：

### debug.py

- `publish_event()` 为每条 llm_call 和 tool_call 事件生成唯一 `event_id`（uuid 或递增序号）
- 新增 `_full_events: dict[str, dict]` 字典，存储最近 N 条事件的完整数据（不截断），以 `event_id` 为 key
- `log_llm_call()` 和 `log_tool_call()` 将完整数据同时写入 `_full_events` 和原有的 JSONL 文件

### agent.py

- `_call_llm()` 调用 `publish_event()` 时传入完整数据（messages + response），而非截断版
- `_execute_tool()` 同理，传入完整 args + result

### routes.py

- 新增 `GET /api/events/{event_id}` 端点，从 `_full_events` 字典中按 ID 查询完整事件详情
- 返回 JSON：LLM call 则包含完整 messages/response/tool_calls，tool call 则包含完整 args/result

### CLI 客户端

- `cyrene flow --session xxx --round xxx --id xxx` → 调用 `GET /api/events/{event_id}` → 输出 JSON

---

## 开放问题

1. **`cyrene do` 超时** — `POST /api/chat` 复杂任务可能跑几分钟。默认超时设置多少？
答：不设置。通过cyrene status之类的可以获取最新进度（可以查看agent timeline/flow，看看目前处于哪个阶段）
2. **端口冲突** — 4242 被占用怎么办？自动递增还是报错？
答：不解决。出了问题再说
3. **Session 自动创建** — `cyrene do "task"` 没指定 `--session` 时，复用 `run_live` 还是新建一个？
答：若没指定，则不创建。必须创建
若重名，也拒绝执行任务。必须不重名
