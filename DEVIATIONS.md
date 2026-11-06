# 与旧仓库（SA-CUAharness，冻结于 main@bc290e3）的已知行为差异

> 命名说明：项目已由 Tandem 更名为 JevDuo。下文历史条目中的 `tandem` / `tandem_eval` / `src/tandem/`
> 即现在的 `jevduo` / `jevduo_eval` / `src/jevduo/`，条目正文保持原样。

人工验收与代码评审对照本清单。按计划 §3.1 有意排除的项标注【计划内】，接口移植中的取舍标注【重构】。

## M1（planner_led 主路径移植）

1. 【计划内】**visual_fallback / ground_visual 延后**：planner 提 visual_fallback 或候选不可编译时，旧版经视觉 grounding 恢复；新版 `components.grounder=None`，胜出者 visual_fallback → compile None → 记 no_action，连续 3 次停止。**影响：依赖视觉坐标定位的任务（含全部 drag 场景）在 grounder 回归前会失败**。grounder 作为普通组件回归后此差异消除。
2. 【计划内】**drag 动作不可产生**：旧版 drag 仅经 visual_point（视觉 grounding）产生；M1 无 grounder → 无 drag。协议保留 `drag` 类型，参数 `{x, y, end_x, end_y}`。
3. 【重构】**输出文件存在性检查改为 CLI 探针**：旧版 done 分支经 `env.controller.execute_python_command` 同步查询（不占步数）；协议化后 harness 无法直访 VM，改为一条 `CliAction`（`python3 -c …isfile…`，`want_observation="none"`，timeout 30s），**多消耗 1 步预算**。仅当 requirements 含 output_path 时发生。
4. 【重构】**完成被拒后的重规划发生在同一 act() 内**：旧版 `continue` 后重取环境观察（无动作执行，内容等价）；新版不重读观察，直接再次调用 planner。
5. 【重构】**export_paths 移至 eval**：旧 `RuntimeConfig.export_paths` 参与交付物导出；新版该配置归 tandem_eval job spec（M2），harness 的 `FinishAction.outputs` 只含需求 output_path。
6. 【重构】**pyautogui 代码生成移至执行层**：harness 输出声明式 GuiAction（坐标/文本/键序列），M2 执行层负责展开（type 前的 ctrl+a、table_cell 的 F2、scroll ±5 clicks、多键序列间 sleep 0.2 等语义已编码在 parameters 中）。set_slider 仍编译为按分数位置的 click（与旧一致）。
7. 【重构】**预算强制主责在 runner（M2）**：harness 在 budget 耗尽时返回 `FailAction(max_steps / wall_time_limit)` 作镜像防御；墙钟以 runner 侧计时为准。
8. 【重构】**a11y 缺失（OSW2 JEV 降级）可继续**：旧版假设始终有 accessibility_tree；新版 tree=None 时以空目标列表 + `"<empty/>"` 上下文运行 planner，由 CLI 路由或 no_action 兜底。
9. 【重构】**CLI `want_observation="none"` 时验证截图复用**：AFTER 与 BEFORE 可能同图；验证仍携带 execution_result（回执优先）。旧版 CLI 步骤总有新截图。
10. 【重构】**停止原因由终态动作承载**：旧版 stop_reason 写 episode.json；新版 `FailAction.reason` / Finish 表达，runner（M2）负责落盘。新增 `no_action_limit` 兜底上限（同一观察内最多 NO_ACTION_LIMIT+3 次重规划）。
11. 【阶段】**LLM 全量请求/响应落盘（llm_calls.jsonl）在 M2 recorder 实现**；M1 保持与旧版一致的 kimi_http.jsonl 审计（仅元数据 + 错误路径脱敏 body）。
12. 【重构】**selector opener 可注入**：`JevOmniPolicy(endpoint_file, timeout_seconds, opener=None)`，默认行为与旧版一致（唯一接口差异，为可测试性）。
13. 【重构】**SessionConfig 校验 cli_timeout_seconds ∈ [1,120]**（协议上限）；旧版无此约束（默认 90 未越界，实际无行为差异）。

## 保持一致（重点核对项）

- planner / verifier / requirements 的 system prompt 与响应校验逻辑**逐字一致**；
- 候选构建、focus_candidates、prune_stalled、action_key、toggle_transition、A11yTargetBuilder 逻辑一致；
- 停止条件语义一致（no_action 连续 3 次、完成双确认、failed_toggles 记忆）；
- CLI 回执 schema（returncode/stdout/stderr 截尾 12000/kind/acknowledged）一致；
- JEV 排序两阶段（operation→action）、单候选捷径、1200s 恢复循环一致。

## M2（tandem_eval + rjob 自动化）

14. 【重构】**tandem_eval 第 7 个模块 rjob.py**：计划 §五"6 文件封顶"指 worker 侧适配层
    （entry/env/runner/recorder/scoring/manifest 仍为 6）；rjob.py 是开发机侧生命周期客户端
    （sync→spec→submit→watch→fetch→cleanup），职责不同，特此记录。
15. 【阶段】**demo 为最小渲染器**（tools/render_summary.py：summary.json → 单文件 HTML）；
    完整 demo studio 自 feature/demo-studio 平移推迟到 M4 前的独立步骤，读取契约不变。
16. 【阶段】**真实基准执行链（osworld1/2）在 M3 落地**：worker.sh 真实分支当前只加载共享
    env.sh 并显式退出（exit 3）；mock 基准全链路已可用。
17. 【重构】**rjob get 状态解析为容忍式**（成功/失败/已删除/运行中关键词 + 中英文），
    首次真机提交时需按实际输出校准 `_STATUS_PATTERNS`（rjob.py）。
18. 【重构】**llm_calls.jsonl 只记成功调用**（请求 payload+messages、原始响应、attempt）；
    传输错误的完整请求落盘在 M3 补（现仅审计日志记录错误元数据）。

## M3 补充（真机首跑发现，2026-10-20）

19. 【重构·已修】**rjob logs 在日志未聚合时 CLI 自身崩溃**（brainpp 对 None 迭代）；
    fetch 视为非致命，cleanup 无条件执行（commit 4ef446f）。
20. 【重构】**want_observation="none" 当前仍返回完整观察**：实现在一次 controller 读取中
    同时取截图与 a11y，逐项跳过收益低且易引入观察不同步；协议字段保留，严格跳过
    待真机 profiling 证明收益后再实装。
21. 【环境·已修】**平台预注入的 HTTP_PROXY 不可用**（httpproxy-headless-dev-test 无法
    解析，毒化 HF 下载与外网 LLM 调用）：worker.sh 一律覆盖为已知可用代理，不继承。
22. 【环境·已修】**OSW1 setup 走共享缓存播种**（旧 runtime.py:532-539 语义，整目录拷贝），
    避免任务 setup 依赖外网下载；Kimi 凭据改用旧 smoke 实跑验证过的 kimi-workspace.json。
23. 【行为】**收尾默认删除 rjob**（旧仓库只 stop，job 记录留在集群上）：本仓库 cleanup
    默认 stop + delete，且 delete 带重试（stop 是异步的，刚 stop 完可能删不掉）；
    submit 之后的任何结局——成功、失败、超时、平台报错、submit 失败、中途 Ctrl-C——
    都保证走到收尾，不在集群上留残留。要保留现场用 `--keep`。
24. 【重构】**rjob.py 拆成三块**（原 322 行超单模块 300 行纪律）：
    rjob.py（生命周期客户端）/ rjob_spec.py（job spec 与 submit 参数）/
    rjob_status.py（`rjob get` 状态解析）；旧的 `tandem_eval.rjob` 导入路径保持可用。

## 配置与命名重构（2026-10-22，human in the loop）

25. 【重构】**配置 schema 角色化**：`infra/config.json` 从平铺键（`kimi` / `jev_endpoint` /
    `kev_endpoint` / `jev_endpoint_file` / `kev_endpoint_file` / `osw1_root` …）改为三段结构
    ——`harness`（慢模型/快模型/路由/预算）、`eval`（集群与基准）、`log`（结果落盘）；
    `_comment` 等说明字段删除（说明属于文档，不属于配置）。
    慢模型与快模型都改为**命名 API 登记表**（`harness.slow_model.apis` /
    `harness.fast_model.apis`），启动命令用 `--slow-model <名字>` / `--fast-model <名字>` 挑，
    缺省取各自的 `default`——为"不同大脑 × 不同小脑"的对比实验留出入口。
    CLI 旧参数 `--selector jev|kev` → `--fast-model <名字>`，`--kimi-max-tokens` /
    `--kimi-reasoning` → `--slow-max-tokens` / `--slow-reasoning`。
    默认行为不变（jev + kimi-k3）；但单题预算从 rjob_spec 硬编码的 15/900 改为
    config 里的 `harness.budget`（30/1800，与批量脚本实际用值一致）。
26. 【重构】**SessionConfig 字段角色化**：`kimi_max_tokens` / `kimi_reasoning_effort` /
    `kimi_timeout_seconds` / `kimi_direct_api` → `slow_*`；`jev_timeout_seconds` →
    `fast_timeout_seconds`；`selector_backend` → `fast_backend`。
    `KimiClient` 类名当时保留（后改名为 `SlowModelClient`，慢模型客户端），审计日志的 provider 字段
    由 `"kimi"` 改为 `"slow_model"`，worker 侧审计文件名 `kimi_http.jsonl` →
    `slow_model_http.jsonl`。
27. 【重构】**开发机侧新增 `config_schema.py`**：config 的校验与 API 选取独立成模块
    （worker 侧仍是 6 文件封顶；rjob.py 不再背着 schema 知识）。
    `demo/` 同步角色化：run 文档 `selector_backend` → `fast_model`（新增 `slow_model` 名），
    排序调用计数 `jev_calls` → `fast_calls`、`jev_latency_ms_total` → `fast_latency_ms_total`
    （这两个是 demo 层自算字段，不在结果目录契约里）。

## 可选阶段执行（2026-10-24）

28. 【行为，可选】`execution_mode=stage` 时，慢模型一次规划当前阶段的多个里程碑，
    快模型依据每帧新观察重新选择动作；规则后置条件优先，证据不足才调用模型 Verify。
    阶段完成、目标无法绑定、执行失败或阶段动作上限触发后返回慢模型。
    不采用严格只规划一次的模式；阶段完成不等于任务完成，原有独立终态检查保留。
    默认 `step` 路径不变，动作/观察/回执协议不变。
    此为旧阶段执行实验的组件化移植，不包含其传输重试、部署或环境准备逻辑。
    详细实现边界见 [STAGE_EXECUTION.md](docs/STAGE_EXECUTION.md)。

## Harness 按职责分包（2026-10-27）

29. 【重构，无运行行为变化】`src/tandem/` 的平铺模块迁入 `runtime`、`planning`、
    `gui`、`models`、`verification`；`protocol` 结构不变。
    Python 导入路径随目录迁移更新，不保留旧模块兼容壳。
    `Ranking`、`build_choice_requests` 和 `ranking_dict` 从原 JEV 模块移到
    `models/ranking.py`，供 JEV/Kev 共用；现有函数体、prompt 和排序语义保持不变。
    配置字段、启动命令、消息协议、结果目录以及 step/stage 执行行为均不变。

## 共享语义感知（2026-10-29）

30. 【行为】a11y/DOM 先形成慢模型与快模型共用的语义元素表：恢复邻近标签、分离
    `browser_chrome` / `web_content` / `dialog` / `menu` / `native`，当前输入值不再冒充
    控件名称。CDP 是 Task 008 等已启用远程调试的 Chrome 的可选增强，失败时回退 a11y；
    协议新增 `browser_snapshot` 文件引用并升至 0.2。候选备选只在提案 scope 内生成，
    不再补入全局 click/keypress；无 grounder 时不提供 visual_fallback。JEV 近期历史只保留
    动作、目标、结果和状态变化，不再携带完整 verification。
31. 【修复】慢模型对端提前断开（`RemoteDisconnected` / `ConnectionError`）纳入现有
    有界重试，不再把一次 transient 断连直接记为整题异常；重试次数和退避策略不变。
32. 【行为】阶段目标绑定增加规范化名称和兼容 role 的唯一匹配；全部提案未绑定时，
    改为当前 scope 内的有限恢复候选 + RETURN_TO_PLANNER，不再在 JEV 前直接计 no_action。
    `stage_replan` 记录失败提案和可用目标。滚动提案的 schema 重试反馈定位到具体字段。
33. 【行为】恢复条件扩展到“已绑定但不可执行”的提案；菜单滚动增加 PageDown/PageUp
    和方向键候选。CLI milestone 必须顶层携带 command，operation=cli 获得专门的重试反馈。
34. 【修复】JEV 断线恢复支持 endpoint 重新发现、30 秒健康检查、瞬态 HTTP 状态与
    Retry-After；恢复等待继续受 1200 秒总窗口限制，并记录 attempt/endpoint/error_type。
35. 【重构】新增 `scripts/run_experiment.sh`：按唯一实验名归档 manifest、batch、driver
    和 task 结果到 `rjob_logs/<experiment>/`，避免不同实验日志混写。
