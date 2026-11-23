# 截图与评分服务的边界

## MCP 截图超时

OSWorld 的 HTTP 截图配置为每次 30 秒、最多 3 次读取、间隔 3 秒，但原 MCP 数据工具
固定 10 秒超时。FastMCP 会在同步读取返回后检查截止时间，因此正常取得的慢截图也可能
被丢弃并中止 episode。101 的首次运行记录了这种错误，属于 `failed_unscored`。

OSWorld backend 现在提供实例化的数据工具超时：桌面截图覆盖 HTTP 读取及只读重试的
有限时间窗口，加 5 秒图像处理余量；窗口截图另外覆盖读取窗口几何信息的时间。
30 秒、3 次、间隔 3 秒时分别为 101 秒和 136 秒。调用方的更短截止时间仍由调用方控制。
原生 backend 的默认值、其他数据工具以及所有写操作的重试规则均保留。

回归使用真实 FastMCP FunctionTool 执行一次 10.05 秒的脚本截图读取，验证结果被保留，
backend 只被调用一次。它是本地夹具检查，不是实机任务评分。

## 内置 AX 与 native AX

VM 包装器原先硬编码 `require_a11y_tree=True`，即使 harness 已关闭内置 AX，reset/terminal
observation 仍会抓取它。现在该值遵循 `harness.observation.a11y_enabled`。
这只影响 OSWorld 内置观察，不影响 Jev 通过 agent-desktop 获取 native AX。

## 评分服务检查

055 首次评分时，guest 的 AX 和文件请求反复连接重置。评估器取不到 MLT 后返回
`missing_mlt` 与零分；这不足以证明 MLT 实际不存在。原始 score/日志保留，并单独标记
取件可靠性问题。原服务重置的原因没有得到独立复现，不将其直接归因于 AX 抓取或模型。

现在评分前后会通过已知只读的 `POST /file` 读取 Ubuntu 的 `/etc/os-release`，验证同一文件
服务可用。每次检查最多尝试 2 次，每次 30 秒，仍受单题端到端硬截止时间约束。
检查使用直接连接，且不执行 GUI 或 guest shell 命令。健康服务下的官方零分原样接受；
检查失败时不接受本次返回分数，episode 进入异常路径，保留已获得的暂定 raw 结果。

逐次记录在 `jobs/job-XX/evaluation_transport/<task>.jsonl`，权限 600、追加写入。
`status=verified` 仅表示前后服务检查通过，不表示任务成功，也不能证明每次中间取件均成功。
这不改变官方 rubric。渲染、语义匹配及产物有效性仍由官方评估器判断。

运行中的 worker 不热更新。修复作用于新启动的 worker；需要补跑时使用新 run ID，
保留旧 attempt 与源码快照，并分别统计首次和补跑的 token。此轮 024 的问答接线修复见
[官方用户模拟器通道](USER_INTERACTION.md)。
