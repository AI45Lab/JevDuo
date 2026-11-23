# 分 agent 的模型用量

新进程在 `agent/<session>/model_usage.jsonl` 增量保存模型调用账本。每次实际进入
SDK Chat 调用或 Jev HTTP 调用前保存 `request`，得到回复后保存 `response`，异常时保存
`failure`。`call_id` 关联同一次调用；重试使用新的 ID。记录逐次 `raw_usage`、模型、用途、
cycle、attempt 和时间。账本不包含鉴权头或 key；原始输入输出仍在现有模型轨迹中。

| agent_id | purpose | 归属 |
| --- | --- | --- |
| slow | routing | Slow 路由及核验 |
| general | tools | 通用子 agent 推理 |
| gui_jev | selection | Jev 候选选择，包括应用选择和风险问题 |
| gui_jev | text | Jev 文本填写回调；虽共用主模型，费用归 Jev |

`input_tokens` 来自 `usage.prompt_tokens`，`output_tokens` 来自
`usage.completion_tokens`。`cache_input_tokens` 来自
`usage.prompt_tokens_details.cached_tokens`，兼容显式的 `prompt_cache_hit_tokens`。
两个缓存字段冲突或缓存数超过输入数时，保留原始 usage、标记冲突，归一化缓存数为 null。
不根据字符数、总 token 数或其他调用估计缺失字段。

缓存输入是 input 的子集，reasoning token 是 output 的子集，都不能再次加到总量上。
`uncached_input_tokens` 只在 input 和 cache input 均已知时相减。接口未返回缓存字段时，
记为 null；明确返回 0 时才记为 0。

```bash
venv/bin/python scripts/summarize_model_usage.py runs/<run_id>
# 运行中刷新已有导出
venv/bin/python scripts/summarize_model_usage.py runs/<run_id> --force
```

输出：

- `model-usage/calls.jsonl`：每次调用的用途、原始 usage、归一化 token、状态和来源。
- `model-usage/summary.json`：按 agent、agent/用途、题号/agent 分组。
- `model-usage/by-task-agent.csv`：各题每个 agent 的调用数和 token 汇总。

每个 token 指标同时报告 `reported_sum`、`known_calls`、`missing_calls` 和 `complete`。
`reported_sum` 仅合计已报告值，不代表缺失调用免费；全部缺失时为 null。
无响应的超时、中断和进程被杀只能保留调用尝试，无法从客户端恢复服务端实际计费。
失败回复和无法解析的模型回答仍计入已收到的 usage；不会为了记账重发请求。
汇总只统计 actor，官方评分模型不混入三类 agent。损坏尾行保留可读前缀并报告 warning。

流式响应未在此账本中收集；本仓库当前 Slow、general、Jev 配置均为非流式。

可选的[官方用户模拟器通道](USER_INTERACTION.md)使用独立 usage 文件，汇总在
`external_services`，不混入三个 actor 的总量。逐次调用导出以 `scope` 区分两者。
