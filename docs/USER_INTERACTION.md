# 官方用户模拟器通道

OSWorld 任务可配置 `user_simulator`，用来回答执行过程中缺失的用户信息。
例如 024 的资金不足需要向用户询问更新材料。未暴露此通道时，actor 无法通过正常提问
取得模拟用户掌握的信息。此次修复保留原始失败记录，在独立运行中检验通道效果。

仅在本次运行配置中显式开启：

```yaml
harness:
  user_interaction:
    enabled: true
```

worker 从当前任务提取原有 `user_simulator` 配置，写入权限 600 的私有
`user_interaction.spec.json`。该文件包含模型鉴权信息，不应复制到公开目录。
launcher 仅为该 actor 设置 `JEVDUO_USER_SIMULATOR_SPEC`；未开启的 actor 会清除此继承变量。
没有用户模拟器的任务不会注册提问服务。

Slow 可见的 `user` 是用户问答服务，不是第三个 GUI 执行器。它只接受
`allowed_tools: [ask_user]` 和 `agent_input: {question: "..."}`，占用一个现有 cycle 和工具调用，
不操作桌面。回答返回父 agent 后，再由 Jev 或 general 继续执行。
它不会把整份模拟器知识加入 Slow、general 或 Jev 的输入；必须先提出实际问题。

服务导入配置指向的官方 runtime 中的 `desktop_env.user_simulator`，兼容 prepared runtime
中的包目录软链接，拒绝已经加载的其他 desktop_env。官方用户知识、persona 和提示不改写；
LLM 连接使用本次 actor 解析后的模型和地址，输出预算 8192，客户端重试为 0，官方后端尝试数为 1。
提问走官方 `respond()`，没有自写的关键词回答器。服务回复只是补充信息，不是任务成功证明。

请求受子任务剩余时间约束。已提交的问题在取消时会等待原请求收尾并保存已收到的回复；
不会为补日志再次发送。尚未发出的过期请求不会调用模型。服务的临时 provider 包装会在 finally
恢复，多个实例的官方 facade 访问串行执行。

私有轨迹位于 `episodes/<task>/user_interactions/`：

- `conversation.jsonl`：问题、回答和交互 ID。
- `user_simulator_responses.jsonl`：原始模型回复；不含 Authorization。
- `user_simulator_usage.jsonl`：独立调用与 token 账本。
- `provider_failures.jsonl`：失败记录，不伪造回答。

`summarize_model_usage.py` 将模拟用户放在 `external_services` 中，继续保持三类 actor
的 `overall`、`by_agent` 和逐题 CSV 口径不变。详细调用导出使用 `scope` 区分
`actor` 与 `external_service`。缓存字段缺失时继续标为 null，不视为零成本。
