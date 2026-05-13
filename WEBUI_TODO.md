# Web UI 开发文档

## Vision: Agent Timeline

一个实时更新的 SVG 画布，展示 agent 内部的工作流程。每个 agent（包括主 agent 和 subagent）是一列，事件按时间从上到下排列，框子之间通过连线展示通信关系。

```
时间 ▼  main_agent    historian      engineer       critic
       ┌──────────┐
       │ Phase 1  │
       │ use_tools│
       └────┬─────┘
            │ spawn
       ┌────┴─────┐┌──────────┐┌──────────┐┌──────────┐
       │ Phase 2  ││WebSearch ││WebSearch ││WebSearch │
       │spawn × 3 ││          ││          ││          │
       └──────────┘├──────────┤├──────────┤├──────────┤
                   │Write .md ││ ...      ││ ...      │
                   ├────┬─────┤├──────────┤├──────────┤
                   │ msg│→────┼───────────→│ Read .md │
                   └────┴─────┘└──────────┘└──────────┘
       ┌──────────┐
       │Synthesis │
       └──────────┘
```

- 框子按 agent 分列，并行 subagent 并排放
- LLM 调用、tool 执行、inbox 消息各用不同颜色
- Inbox 消息用虚线箭头从发送者指向接收者
- Hover 框子显示完整输入输出
- 时间轴从上往下流动

## 当前问题

## 当前问题

### 1. 页面加载卡住
打开 `http://localhost:4242` 时页面一直 loading，进不去。
要等后端发一条消息后才能正常访问。

可能原因：SSE 端点 `/api/events` 在页面加载时被连接，但事件队列为空导致请求挂起，阻塞了其他路由的处理。`StreamingResponse` 不应阻塞其他请求，但需要排查。

### 2. CLI 消息不同步到 Web UI
CLI 中发送消息后，Web UI 的消息列表不会自动更新。
当前修法：agent.py 发 `chat_message` SSE 事件 → 前端 `reloadMessages()` 调 `/api/chat-history` 重载。
但实际目前无法验证（因为问题 1 导致页面进不去）。

### 3. Agent Timeline 面板为空
点击 "Agent Timeline" 后显示空白面板，没有 SVG 渲染。
可能原因：
- SSE 连接失败（前端有 auto-reconnect，但首次可能没连上）
- 事件队列没被激活
- JS 报错导致 `addEvent` / `drawTimeline` 没执行

## 待实现

- [ ] 修复页面加载卡住问题
- [ ] 验证 CLI → Web UI 消息同步
- [ ] 修复 Agent Timeline 面板为空
- [ ] 添加 Web UI 发消息 → CLI 同步（双向同步）
- [ ] Timeline 事件截断（超过 50 条后折叠 oldest）
- [ ] 优化 Timeline 增量渲染（目前每次全量重绘，事件多了会卡）
- [ ] Timeline 上显示 subagent 之间的 inbox 连线动画
- [ ] 退出 subagent 时显示最终结果摘要
