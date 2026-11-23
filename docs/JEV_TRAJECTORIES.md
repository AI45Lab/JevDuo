# Jev 逐步轨迹

三类 agent 的调用与 token 独立记账见 [模型用量](MODEL_USAGE.md)。Jev 选择和文本回调可分别统计。

新启动的 JevDuo agent 默认保存 Jev 独立轨迹，并在每个语义动作前后归档截图。
截图不送入 Jev 选择模型或字段文本回调；原来的纯文本选择输入保持不变。
general 的轨迹链路和父代理观察逻辑不变。

## 目录与配置

```text
episodes/<task>/agent/<session>/
  query_0001/
    orchestration.jsonl
    observations/<observation_id>.png
    subagents/gui_jev/<attempt_id>/trajectory.jsonl
```

多阶段任务保留各自的 phase/session 目录。每个 attempt 使用独立目录，已有目录不会被覆盖。
JSONL 以追加方式逐条 flush/fsync，目录权限 700、日志权限 600，已配置的模型凭据会脱敏，
不记录 Authorization headers。记录不再依赖长度有限的 `SubagentResult.summary`。

当前根配置显式启用：

```yaml
harness:
  orchestration:
    gui_jev:
      trajectory:
        enabled: true
        screenshots: true
```

`screenshots: false` 关闭新增的逐动作截图，仍记录选择、AX、动作和回执；父代理和子目标
边界的原有观察仍会进行。`enabled: false` 关闭独立轨迹及新增截图，保留原有摘要日志。
直接使用 NorthCUA app YAML 时字段位于 `orchestration.gui_jev.trajectory`。

## 保存的证据

| 事件 | 内容 |
|---|---|
| `attempt_started` / `agent_started` | 原始任务、合同、输入、历史、配置及有效 scope/keys/resources 等 |
| `ax_observation` | SDK 实际返回的 AX 节点及 screen 信息，保留其 complete/truncated 等边界 |
| `decision_requested` | 实际完整 Jev 请求：state、questions、当前候选、目标/操作/选项/核验问题 |
| `model_request` / `model_response` | 每次 HTTP 尝试的请求正文、状态码和原始文本响应正文，含供应商返回的 usage/reasoning |
| `decision_returned` / `decision_failed` | 选择模型返回的 answers，或失败类型；不把返回回答自动视为已执行或训练正例 |
| `text_requested` / `text_returned` | 字段内容回调的输入与原始返回，保留空白与多行文本 |
| `action_started` / `action_finished` | 操作、scope、目标、文字参数、相关 decision_ids、实际结果、交付证据和异常 |
| `native_started` / `native_finished` / `native_interrupted` | 每条实际 CLI argv、超时、收到的原始 JSON 回执、native_call_id、所属 action_id |
| `screenshot` / `screenshot_unavailable` | before_action/after_action 图片路径及观察 ID，或缺失原因；始终标记非策略输入 |
| `loop_event` | SDK 原有事件逐条保存，包括恢复、动作、停止等；不做摘要级截断 |
| `subgoal_observation` / `attempt_finished` | 现有子目标返回观察、最终或中断结果、真实调用用量和独立轨迹引用 |

这里的“一步”指 Jev 的语义操作或应用启动，包括观察导航操作；不是每次 AX 读取，
也不是剪贴板后端的每个 Ctrl+A/C/V。低层命令各自保存 argv/回执，共享外层动作 ID，
避免把同一个 OPEN_RESOURCE 内的 launch 重复记成两个语义动作。

图片只沿用现有桌面观察通道，不添加截图 HTTP 入口。额外的归档读取受原有墙钟预算
限制，不消耗额外模型或 GUI 工具调用额度，也不替换返回给父代理的正常 last_observation。
AX 与截图是分开的读取，不声称二者原子同步；应使用阶段和事件时间判断其时序。

## 中断与失败

动作回执先保存，再尝试 after_action 截图。已提交命令在取消时仍等待已有请求结束，
记录收到的回执并保留原有 replay guard，不为补日志或图片重发动作。取消发生在
before_action 截图期间且尚未进入执行器时，`dispatch_invoked: false`，不误报未知写入。
`dispatch_invoked: true` 只表示进入执行器，不证明物理交付；必须继续检查 result/receipt。

截图失败用 `screenshot_unavailable` 明确记录，不拿其他时刻图片冒充，也不重复动作。
轨迹写入失败会停止正常执行并保留已有回执/防重放信息；所需资源清理仍会尝试。
损坏尾部不被截掉或覆盖。文件系统故障或强制终止可能使轨迹只留下可读前缀，不能从
开始事件推断动作已完成。正常取消与超时的部分结果仍进入原有 orchestration journal。

## 查看

现有导出命令会自动识别新日志，按 attempt_id/cycle_id/decision_id/action_id 关联：

```bash
venv/bin/python scripts/export_jevduo_trajectories.py runs/<run_id> --embed-images
```

页面中的“Jev 动作与选择证据”显示完整动作及相关选项、回答；“每次候选输入与模型回答”
也包含没有执行动作的决策。逐动作图片进入截图浏览器。完整原始 HTTP 文本、AX 与
低层命令记录仍以 JSONL 为准，页面不重复嵌入成功回复的完整原始 HTTP 包。

旧日志继续使用摘要展示，不补造缺失选项和图片；身份冲突不猜测关联，尾部损坏时显示
可读前缀和警告。此功能只对新启动的进程生效，无法补回旧轨迹中从未保存的数据。

验证示例位于 `runs/jev-trajectory-20261119/demo-v2/trajectories.html`，使用脚本化
AX、模型和 PNG fixture，证明运行时、落盘和查看器的连接，不代表真实 GUI 或官方评分。
轨迹归档不自动生成训练正例；官方分数、实际候选合法性、交付及语义前后条件仍需另行审核。
