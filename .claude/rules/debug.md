# Debug logging — 铁律

任何时候开发、调试、测试，都必须带 `--verbose` 运行：

```bash
python -m cyrene.local_cli --verbose
```

每次 LLM 调用的完整内容（prompt、tool list、response、tool call 参数和结果）记录到 `data/debug_YYYYMMDD_HHMMSS.jsonl`。

没有日志 = 靠猜。每次让 LLM 重新解释问题 debug = 浪费 token。
