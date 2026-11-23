# Fast Router

Slow 负责规划和验收。它提供有成功条件、约束和预算的 `SubgoalIntent`。
Fast Router 根据当前界面、已有进展和候选能力，在同一目标内选择、续跑或转派
`gui_jev` 和 `general`。最终成功仍由 Slow 根据新观察确认。

## 启用

根目录 `config.yaml` 中的 Fast Router 默认关闭。使用集群评测时，将下面的配置合入
`harness.orchestration`。直接使用 NorthCUA app YAML 时，将它放到顶层
`orchestration` 下。

```yaml
fast_router:
  enabled: true
  max_attempts: 3
  attempt_model_calls: 20
  attempt_tool_calls: 25
```

在 `.env.local` 配置 `TYPESAFE_API_KEY`。Router 使用 Jev System One 协议，
从 `TYPESAFE_BASE_URL` 和 `TYPESAFE_MODEL` 读取可选覆盖。可以用
`fast_router.model` 单独覆盖 Router 的模型名。远程 HTTP 端点需要显式设置
`TYPESAFE_ALLOW_HTTP=1`。Jev 子代理本身仍可使用单独配置的 Chat Completions 后端。

## 执行与恢复

- Slow 的目标意图随每次派发交给执行者，包括应用要求和精确文本。框架校验插件输入，
  并按插件声明和可选的 `allowed_tools` 白名单绑定工具权限。
- 已注册官方用户澄清服务且允许 `ask_user` 时，Slow 可以通过 `user_question` 提出一个
  明确问题。框架直接派发这次澄清，并将回答交回 Slow。
- Router 使用只读原生界面概况、最近的执行证据和剩余预算。截图留给 Slow 验收。
  优先执行者须达到能力阈值，并且与最高分的差距在 `preference_margin` 内。
  候选分数都低于阈值时，可尝试配置的 `fallback_agent`，默认为 `general`。
- 一次目标共享模型、工具和时间预算。固定周期配置下，
  `subagent_steps_per_cycle` 是整个目标的额度。Router 调用、重试、读取和子代理尝试
  都在对应预算内计量，框架预留 Slow 验收所需的调用和时间。
- 局部额度耗尽时，执行者交回当前进展。发生阻塞时可以改派。完成声明、未确认投递、
  预算耗尽或路由服务故障会把控制权交回 Slow。恢复时先获取新观察，再核对中断前的尝试。

`orchestration.jsonl` 记录 Router 决策和读取。`model_usage.jsonl` 以 `fast_router` 记录
每次模型请求、响应和失败，包含重试。每个周期的 `attempts`
包含各次执行合同和结果，`routing` 包含选择与分数，`subagent_result` 保留最后一次
结果。可以用根 README 中的轨迹导出命令查看这些记录。
