# 阶段执行

通过 `--execution-mode stage` 启用：慢模型规划阶段，JEV/Kev 连续选择 GUI 动作，CLI 执行计划中的命令；优先使用规则验证，证据不足时调用模型 Verify，失败或状态偏离时按需重规划，最终完成仍需独立验证。默认保留原有 `step` 模式。

开发入口（相对 `src/jevduo/`）：

- `planning/stage_planner.py`：阶段计划生成与响应校验。
- `runtime/stages.py`：里程碑进度、当前目标重新绑定及重规划。
- `verification/stage_rules.py`：基于回执与 A11y 的后置条件验证。
- `runtime/episode.py`：回执验证编排与独立终态检查。
- `components.py`：按 `execution_mode` 装配规划器，继续复用相同的 GUI 和模型组件。

目标绑定先尝试精确 name/role/parent，再尝试规范化名称和兼容 role；只有唯一匹配才绑定。
全部提案无法唯一绑定时，不再于 JEV 前直接累计 no_action，而是生成当前 scope 内的
有限恢复候选（click、double-click、Enter/Escape）以及 RETURN_TO_PLANNER。
提案已绑定但无法编译为动作时，也进入同一恢复流程；滚动场景额外提供
PageDown/PageUp 和上/下方向键，覆盖菜单内导航。
`stage_replan` 同时记录 `unbound_proposals`、`available_targets` 和 `recovery_scopes`，
供慢模型在下一次修复中选择不同目标或方法。多个匹配仍然不会被静默猜测。
