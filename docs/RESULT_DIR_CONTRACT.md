# 结果目录契约（Result Directory Contract）

> eval ↔ demo/分析 的唯一接口。demo/分析工具只读本契约描述的目录；任何一侧改动必须先改本文档。
> 契约版本：`v0.3`。jevduo_eval.recorder 是唯一写入者。

## run 目录（`rjob_logs/<run_id>/`，唯一落盘位置）

```
<run_root>/
├── code_version.json        # harness_version / protocol_version / git_describe / images / run_spec（secrets 段全部 *** 脱敏）
├── heartbeat.json           # {time, done, total, current_task}；runner 逐题刷新
├── summary.json             # run 级汇总（demo 与人工验收入口）
├── platform.status.txt      # rjob get 输出（fetch 写入）
├── platform.tail.log        # rjob 平台日志尾部（fetch 写入）
├── worker/entry.log         # worker 侧运行日志（LLM 审计事件流 + 错误栈）
└── episodes/
    ├── manifest.resolved.json   # 实际执行清单（task_id / domain / episode_dir）
    └── <task_id>/               # episode 目录（有 domain 时为 <domain>-<task_id>）
```

OSWorld 2.1 显式配置替代 judge/用户模拟器时，`code_version.json.resources`
分别记录 `judge` / `user_simulator` 的 `{provider, model, configuration: "alternative"}`。
凭据仅存在私有 job spec；结果中的 `run_spec.secrets` 保持全部脱敏。

## episode 目录（自包含：不依赖共享目录与旧仓库即可复盘）

| 文件 | 内容 | 写入时机 |
|------|------|---------|
| `task.json` | 任务定义完整副本 | episode 开始 |
| `traj.jsonl` | 每执行步一行：`{step_idx, wall_time, protocol_version, observation{screenshot(sha256/path/w/h), accessibility_tree(present/source/digest/node_count), browser_snapshot(present/source/digest/node_count/path 或 null), budget(...), last_receipt}, action{kind, …}}` | 每步 env.execute 后 |
| `llm_calls.jsonl` | 全量 LLM 请求/响应，每行 `{role, time, request{payload, messages}, response, attempt}`；Bearer/api_key 脱敏 | 慢模型客户端成功响应时（llm_sink → LlmTap → 本文件） |
| `events.jsonl` | harness 决策事件（route 字段区分）：`planner`（发给 LLM 的计划全文 + 有效响应）/ `gui`（候选列表 + ranking{operation_request, action_request, operation_ranked, action_ranked, winner, margins, latency_ms, used_image} + 编译后动作）/ `cli`（路由决策 + 命令）/ `gui_verified` / `cli_verified`（验证裁决 + 回执）/ `terminal`（终态双确认）/ `action`（runner 写的执行记录，与 session 事件穿插） | session.act() 后增量排水 |
| `a11y_digests.jsonl` | 每步 a11y sha256 + 节点数（`{step_idx, sha256, node_count}`） | 每步（全量 xml 默认关闭，record.a11y_full=true 时另写 `a11y/step_XX.xml`） |
| `screenshots/step_XX.png` | 逐步截图（**契约正本**） | 每步 env.execute 后 |
| `browser/step_XX.json` | CDP DOM 语义快照（启用且成功时）；traj 的 browser_snapshot.path 指向此正本 | 每步 env.execute 后 |
| `deliverables/` | finish 时从 VM 导出的交付物文件（含 export_paths） | episode 终态 |
| `artifacts.json` | deliverables 导出记录（`[{vm_path, exported, local_path, bytes}]`） | episode 终态 |
| `evaluation.json` | 官方评分（`{source: "official_benchmark_evaluator", raw, score}`） | episode 终态（评分成功时） |
| `result.txt` | 分数（`score\n`） | 同上 |
| `episode.json` | `{task_id, harness, stop_reason, steps, status(scored\|failed_unscored), score, exception_type, error?, error_traceback?}` | episode 终态（必写）。`error`/`error_traceback` 仅异常题有：异常消息与栈尾几行（脱敏）——只记类型事后没法定位 |
| `timing.json` | `{wall_seconds, status}` | episode 终态 |
| `task_memory.json`（goal） | 最新任务记忆：要求、事实、资料覆盖、交付单元、成果、失败与当前阶段 | goal 事件排水及终态 |
| `evidence/`（goal） | 阶段验证引用的截图/CLI 全文正本；临时目录清理后仍可读取 | 阶段验收/记忆落盘 |

`timing.json` 另外提供 `environment_seconds`、`model_usage_by_role`、`fast_model`，分别记录环境
分段时间、逐角色请求时间/token、排序次数/耗时/图像使用次数。缺失 usage 不补零冒充已测值，
另记 `calls_missing_usage`。原字段及 `browser/` 正本规则保持不变。

## 步号语义（demo 侧对齐依据）

以下三条由 recorder 的写入顺序决定，demo 侧按此还原轨迹；改动任一处必须同步本文档。

| 约定 | 说明 |
|------|------|
| 截图索引 0 基 = **动作执行后** | `recorder.screenshot(steps_used, …)` 在 `env.execute` 之后落盘，故 `screenshots/step_NN.png` 是**第 N 个动作之后**的屏幕（NN 从 00 起） |
| `traj.step_idx` = 截图索引 + 1 | `traj.step_idx = N+1` 与 `screenshots/step_NN.png` 是同一次观察（`a11y_digests.jsonl` 的 `step_idx=N` 与 `traj.step_idx=N+1` 的 digest 一致） |
| 验证事件落在**下一步骤**桶 | `gui_verified` / `cli_verified` 在动作之后才产生，排水时归入下一个 `step`；因此动作步 s 的裁决取自 s+1 桶 |

推论（demo 直接依赖）：

- 动作步 s 的「执行后」帧 = `step_{s:02d}.png`，「执行前」帧 = `step_{s-1:02d}.png`；
- 第 0 步没有执行前帧（初始观察只在 `obs_stage/`，episode 结束即清）；
- 终态动作（finish/fail）不写截图，故最后一步没有执行后帧；
- 异常题（`failed_unscored`）在 `fetch_task` 阶段失败时没有任何帧与 LLM 调用。

## llm_calls.jsonl 的 role 值

| role | 调用方 | 触发时机 |
|------|--------|---------|
| `task_requirements` | verifier.task_requirements | episode 开始时抽取任务需求（1-12 条） |
| `plan_step` | planner.plan_step | 每步规划（截图+可见控件+需求+近期动作→下一步决策） |
| `plan_stage` | stage_planner.plan_stage / goal_planner.plan_goal | stage/goal 的阶段开始或修复；goal 允许读取已存资料后继续规划 |
| `verify_stage` | goal_verifier.verify_goal | goal 阶段结束时独立验证或提取资料增量；明确执行失败的本地检查不算模型调用 |
| `ground_visual` | grounding.ground_visual | goal 按需生成当前截图的有限候选，不直接执行慢模型坐标 |
| `gui_choice` | planner_choice.PlannerChoicePolicy | 仅显式启用 planner 选择后端时，在相同候选与截图上选 ID；独立记录时间/token，不冒充 JEV 排序分数 |
| `verify_observation` | verifier.verify_observation | 每步动作执行后独立验证（BEFORE/AFTER 截图→achieved/not_achieved/uncertain）|

`verify_observation` 不信任 planner 的自我宣称：它拿前后截图+预期变化独立裁决，驱动 recent 历史、no_action 连击计数、toggle 检查、以及终态双确认（terminal_claim 模式下逐条核验 requirements + 输出文件存在性）。

## events.jsonl 的 route 值

| route | 内容 |
|-------|------|
| `planner` | plan 的完整请求负载（不含截图二进制）与 LLM 有效响应（done/task_status/goal/proposals 或 route=cli 的 command） |
| `gui` | 候选空间（candidates 列表含描述/坐标）+ ranking（**排序服务的完整输入输出**：两阶段请求全文 + 每选项分数 + margin + latency + used_image）+ 编译后动作 |
| `cli` | planner 的 route=cli 决策 + 命令 |
| `gui_verified` / `cli_verified` | 验证裁决（outcome/task_status/evidence/state_changed）+ 回执 |
| `terminal` | 终态裁决（planner 声称完成后的 verifier 双确认全文） |
| `stage_local` | 消费已生成的阶段计划；含 stage_goal、milestone 和当帧重绑定后的 plan，不代表新的模型调用 |
| `stage_progress` | 里程碑推进，含 stage_goal、milestone、验证 source |
| `stage_replan` | 阶段修复请求，含原因及当前里程碑；target_unbound 时另含 `unbound_proposals`、`available_targets`、`recovery_scopes`；不等于调用已经完成 |
| `task_memory` / `memory_update_rejected` | goal 的完整记忆快照/拒绝理由；不是规划模型自行宣称的完成 |
| `goal_action` | 真实回执、当前控件、画面变化与局部效果；effect_verified=false，不等于业务验收通过 |
| `stage_verified` | goal 的阶段裁决、证据引用、结束原因与用时，区分阶段成功和单元完成 |
| `observation_refresh` | 刷新观察而未执行动作，action_replayed=false；不增加动作步数 |
| `recovery` / `goal_stalled` | goal 的视觉候选补全/无可执行进展终止原因 |
| `environment_timing` | setup/observe/execute/export/evaluate 分段耗时 |
| `action`（kind 字段） | runner 写的执行记录 `{step, kind: "action", action: {…协议动作…}}` |

stage 模式的 planner 事件只在真实规划调用时写入，增加 mode、reason、seconds；
gui/cli 中的 plan 带 planner_called，表示本轮是否刚调用规划模型。
gui_verified/cli_verified 的 verification 增加 source（rule_receipt / rule_a11y / model）
与 seconds。规则检查不写 llm_calls，不能计为一次慢模型 Verify。
其他文件和动作/观察/回执协议不变；详细触发规则见 [STAGE_EXECUTION.md](STAGE_EXECUTION.md)。

## failed_unscored 纪律

API/VM/评分异常 ≠ 0 分——异常题不写 `result.txt`，`episode.json.status=failed_unscored`，run 继续下一题。`summary.json.failed_unscored` 单独计数，不计入均分。

## stop_reason 取值

| 值 | 含义 |
|---|------|
| `done` | planner 声称完成且 verifier 双确认通过（FinishAction） |
| `<planner 的 infeasible 理由>` | planner 声称不可行且 verifier 确认（FailAction） |
| `max_steps` | 步数预算耗尽 |
| `wall_time_limit` | 墙钟预算耗尽 |
| `no_action_limit` | 连续 3 步无有效动作 |
| `exception` | 环境异常（API/VM/评分/网络） |

## 变更规则

- 新增字段允许（向后兼容）；改名/删除/语义变化 = bump 契约版本并同步 recorder 与全部读取方；
- demo 侧永远不得假设未在本文档列出的文件存在。
