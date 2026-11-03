# Tandem

**面向 Computer Use 场景的快慢双模 CUA harness。**

慢模型负责**理解与规划**——读长指令、读屏幕、读无障碍树，产出「下一步该做什么」的意图；
快模型（JEV / Kev 这类 System One 决策模型）负责**选择**——在一个已被裁剪过的候选动作空间里，
一次前向就挑出具体动作。Tandem 把这两级拼成一条可评测、可回放、可对比的决策闭环。

```
  观察 Observation
  截图 · a11y 树 · 可选 CDP DOM 快照 · 预算 · 上一步回执
        │
        ▼
┌─────────────────────────────────────────────┐
│慢模型 planner（大脑）    ← 理解与规划       │
│  current_goal / expected_observation        │
│  route / action_proposals / done            │
└─────────────────────────────────────────────┘
        │
        │  意图：提案（操作哪个元素、做什么操作）
        ▼
  候选动作空间（按提案聚焦 + 历史剪枝后）
  每个候选自带：操作 · 目标 · 坐标 · 上下文
        │
        ▼
┌─────────────────────────────────────────────┐
│快模型排序（System One：JEV / Kev）  ← 选择  │
│  两阶段：operation → action                 │
│  winner · operation_margin · latency_ms     │
└─────────────────────────────────────────────┘
        │
        │  winner
        ▼
  编译 → 声明式动作（kind / action_type / parameters）
        │
        ▼
  执行（GUI 或 CLI）
        │
        │  Receipt：returncode / stdout / stderr
        ▼
┌─────────────────────────────────────────────┐
│慢模型 verifier（大脑）    ← 独立验证        │
│  outcome · state_changed · evidence         │
│  逐条需求核验 requirement_checks            │
└─────────────────────────────────────────────┘
        │
        └───► 回到「观察」，直到完成 / 预算耗尽
```

> 内部集群（rjob 提交、OSWorld 真机评测、共享资源）的运行方式不在本文，
> 见 [docs/CLUSTER_RUNTIME.md](docs/CLUSTER_RUNTIME.md)。

## 快慢双模：分工与边界

|  | 慢模型（大脑） | 快模型（小脑） |
|---|---|---|
| 角色 | 需求抽取 / 一步规划 / 独立验证 | 在候选空间里排序选择 |
| 实现 | `planning/planner.py`、`verification/verifier.py`、`models/slow.py`（`SlowModelClient`，OpenAI 兼容方言） | `models/jev.py`（JEV）、`models/kev.py`（Kev） |
| 当前默认 | `minimax-m3.1-flash`（可切 `kimi-k3`） | `jev`（可切 `kev`） |
| 输入 | 指令 + 截图 + a11y 树 + 历史动作 + 需求清单 | 当前目标 + 候选动作（自带上下文描述）+ 截图（仅 JEV 需要） |
| 输出 | 结构化 plan：当前目标 / 预期观察 / 路由 / 动作提案 | 排序结果：winner + operation/action 两级分数 + margin + 延迟 |
| 代价 | 每步一次 LLM 调用，慢且贵 | 单次 HTTP 前向，快且便宜 |
| **不做什么** | 不直接指定点击哪个像素 | **不做全局规划**，不理解任务全貌 |

**关键设计**：快模型拿到的候选集合已经被裁剪过两道——

1. **慢模型的提案聚焦**（`gui.proposals.focused_candidates`）：planner 给出要操作的元素与操作类型，
   候选空间据此收敛；
2. **历史剪枝**（`gui.focus.prune_stalled`）：连续两步对同一动作都没有引起状态变化，就把该动作
   从候选空间剔除，避免原地打转；上一步已判定达成（achieved）的操作也不再重复提议。

剩下的每个候选都带语义描述（角色、名称、坐标、上下文）。这让一个体量很小的 System One 模型
也能在真实桌面任务上做出可用选择，而每一步的全局理解仍然完整地交给慢模型。
排序服务不可用时按 `URLError` 退避重试（45s × 3），不会静默降级。

## 一次决策的链路

`TandemSession.act(observation)` 是唯一的决策入口，状态机语义（`runtime/session.py`）：

1. **读观察**：`Observation` 含截图、a11y 树和可选 CDP DOM 快照引用、预算、上一轮回执。
2. **建目标**：a11y 树解析成 `UITarget`，`gui/semantics.py` 恢复邻近标签、
   分离浏览器外壳与网页内容，并合并 a11y/DOM 元素。
3. **慢模型规划**：`planning.planner.plan_step` 产出 plan——`current_goal`、`expected_observation`、
   `route`（gui/cli）、`action_proposals`、`task_status`、`done`。
4. **终态分支**：planner 声称完成 → `handle_done`：若需求带 `output_path`，先发一条 CLI
   存在性探针（`done_probe`）核对交付物文件是否真的存在，探针通过后才进入终态裁决
   （`terminal_decision`）；未通过则回到规划重来。
5. **CLI 路由**：`route == "cli"` → 直接下发 `CliAction`（bash -lc，cwd=/home/user，1 条 CLI 计 1 步）。
6. **建候选空间**：`gui/candidates.py` 由目标展开动作，再经 `focused_candidates`
   按慢模型提案和元素 scope 聚焦，经 `prune_stalled` 剪枝；网页提案不会补入浏览器外壳候选。
   全部提案无法唯一绑定时，使用当前 scope 内的有限恢复候选和 `RETURN_TO_PLANNER`，
   不在调用快模型前直接计为 no_action。
7. **快模型排序**：两阶段——先选 **operation**（点击还是输入），再在胜出 operation 内选 **action**（具体哪个目标）。
   结果带 `operation_margin`（两级置信度差）与 `latency_ms`。
8. **编译动作**：`gui/compile.py` 把 winner + 目标编译成**声明式** `GuiAction`
   （kind / action_type / parameters / target_id），不做代码生成。
9. **执行与验证**（tandem_eval 层）：环境执行后回传 `Receipt`；下一轮进入
   `runtime.episode.verify_pending`——用新观察独立核验上一步，更新 verified_focus、failed_toggles、
   以及**逐条需求的核验状态**（`requirement_checks`）。
10. **停止条件**：步数/墙钟预算耗尽 → `FailAction`；连续 3 次无可执行动作 → `FailAction(no_action_limit)`。

`session.events` 记录每一步的 planner 计划、候选集、排序细节、动作与验证裁决，
落盘后即 `events.jsonl`——demo 与分析的唯一数据来源。

## 两条执行路由

| 路由 | 触发 | 产物 | 备注 |
|---|---|---|---|
| **gui** | planner 提案指向界面元素 | `GuiAction`（声明式） | 主路径；由快模型在候选空间里选择 |
| **cli** | planner 判断 GUI 绕路，直接用 shell | `CliAction`（命令 + 超时） | `allow_cli` / `route_policy` 可关；与 GUI 共用步数预算 |

## 组件装配

`components.py` 定义依赖注入边界——十个字段，没有插件框架、没有隐藏分发：

| 字段 | 职责 | 默认实现 |
|---|---|---|
| `planner` | 单步或阶段规划 | `planning.planner.plan_step` / `planning.stage_planner.plan_stage` |
| `verifier` | 独立验证 | `verification.verifier.verify_observation` |
| `requirements` | 需求抽取 | `verification.verifier.task_requirements` |
| `selector` | 排序后端 | `models.jev.JevOmniPolicy` / `models.kev.KevPolicy` |
| `target_builder` | a11y 树 → 目标 | `gui.targets.A11yTargetBuilder` |
| `semantic_enricher` | a11y/DOM → 共享语义元素表 | `gui.semantics.enrich_targets` |
| `candidate_builder` | 目标 → 动作候选 | `gui.candidates.ActionCandidateBuilder` |
| `focus_candidates` | 提案聚焦 + 剪枝 | `gui.proposals.focused_candidates` |
| `compile_gui` | winner → 声明式动作 | `gui.compile.gui_action` |
| `grounder` | 视觉兜底 | **None**（按重构计划延后，session 侧判空跳过） |

换排序后端、换 LLM、换候选策略都只改这里，不碰状态机。

## 消息层协议

harness 与环境之间只走 JSON 消息（`src/tandem/protocol/`）：

- **Observation**（环境 → harness）：任务指令、步号、预算、截图引用、a11y 树引用、上一轮回执；
- **Action**（harness → 环境）：`GuiAction` / `CliAction` / `WaitAction` / `FinishAction` / `FailAction`；
- **Receipt**（环境 → harness）：returncode、stdout、stderr、kind。

约束（MCP-ready，从第一天生效）：

- `schema/*.json` 是**唯一真源**，`validate.py` 是运行时强制者，`samples/*.json` 是 golden 样例；
- 消息必须是纯 JSON——二进制一律文件引用（path + sha256），禁止 Python 对象泄漏；
- 校验器严格：未知字段、bool 冒充 int、枚举越界一律拒绝；
- 任何字段增删或语义变化 = bump `protocol_version` 次版本号，并同步 schema 与全部 golden 样例。

自检：

```bash
tandem check      # 协议样例自检（CI 同样跑）
```

## 仓库结构

| 路径 | 定位 |
|---|---|
| `src/tandem/` | **仓库主体**：快慢双模 harness（决策状态机、规划/验证、候选与排序、协议层、组件装配） |
| `src/tandem_eval/` | 测评薄适配层：worker 侧 6 文件（entry/env/runner/recorder/scoring/manifest）+ 开发机侧 `rjob.py` 生命周期客户端 |
| `infra/` | 内部集群适配：唯一配置文件、worker 入口脚本（详见 [集群运行手册](docs/CLUSTER_RUNTIME.md)） |
| `demo/` | 轨迹回放与对比站点：只读结果目录，不 import harness、不调模型、不碰 VM |
| `tests/` | `protocol/` `harness/` `eval/` `demo/` 四组 |
| `docs/` | 结果目录契约、集群运行手册 |

边界规则：harness ↔ eval 只走消息层协议；eval ↔ demo 只读结果目录；
仓库 ↔ 共享资源只读、零新增依赖。

## 快速开始

```bash
# 安装（Python ≥ 3.10；harness 本身零第三方依赖，只用标准库）
# dev 额外装 pytest + ruff；Pillow 是白名单内唯一第三方依赖，仅 demo 构建期用
pip install -e '.[dev]'

# 三件套：lint + 单测 + 协议自检
ruff check src tests && pytest && tandem check
```

把 harness 当库用：

```python
from tandem.components import build_default
from tandem.config import SessionConfig
from tandem.runtime.session import TandemSession

components = build_default(
    client=slow_client,                       # models.slow.SlowModelClient（慢模型客户端）
    endpoint="http://<排序服务>/rank",
    config=SessionConfig(fast_backend="jev"),
)
session = TandemSession(components)
session.begin("把 FYP 答辩安排逐条加进日历", None)   # None → 慢模型抽取需求
action = session.act(observation)                     # Observation → Action
```

在真实环境上跑一题（提交到内部集群）：

```bash
tandem-eval rjob run --run-id <name> --benchmark osworld2 --tasks <task-id> \
  --repo . --config infra/config.json --poll 60 --timeout 3600
```

换大脑 / 换小脑只改启动参数：`infra/config.json` 里登记多套 API（`harness.slow_model.apis` /
`harness.fast_model.apis`），运行时用 `--slow-model <名字> --fast-model <名字>` 挑，
用来比较"不同大脑 × 不同小脑"的性能差距。

跑完**自动删除 rjob**——无论成功、失败、超时还是中途异常，都不在集群上留残留；
要保留现场（事后查 `rjob logs`）加 `--keep`。

完整参数表与运行细节见 [docs/CLUSTER_RUNTIME.md](docs/CLUSTER_RUNTIME.md)。

## 会话配置

`SessionConfig`（`src/tandem/config.py`，frozen dataclass，构造即校验）：

| 字段 | 默认 | 说明 |
|---|---|---|
| `allow_cli` | `True` | 是否允许 planner 走 CLI 路由 |
| `route_policy` | `"auto"` | `auto` / `gui` / `cli` |
| `cli_timeout_seconds` | `90` | CLI 超时（协议上限 120） |
| `fast_timeout_seconds` | `300` | 快模型排序服务超时（jev 与 kev 共用） |
| `slow_max_tokens` | `16384` | 慢模型单次响应上限 |
| `slow_timeout_seconds` | `120` | 慢模型超时 |
| `slow_direct_api` | `False` | 直连 API（不经代理） |
| `slow_reasoning_effort` | `None` | 服务端默认，或 `off` / `low` / `medium` / `high` / `max` |
| `fast_backend` | `"jev"` | `jev`（`/rank`，需截图）/ `kev`（System One，纯文本） |

**换慢模型厂商**：在 `infra/config.json` 的 `harness.slow_model.apis` 里登记多套 API，
启动时用 `--slow-model <名字>` 挑（缺省取 `default`），不改代码。

## 命名实验与日志目录

`scripts/run_experiment.sh` 将一次实验的全部产物归档到 `rjob_logs/<experiment>/`。
实验名只能使用小写字母、数字和连字符，且每个实验目录必须唯一。

```bash
bash scripts/run_experiment.sh \
  --experiment kimi12-retry-20261030 \
  --tasks 024,029,037,045,057,059,069,082,088,093,005,031 \
  --concurrency 12 --slow-model kimi-k3 --slow-key-ids 1,2,3,4 \
  --max-steps 500 --max-wall 14400
```

目录结构：`manifest.json`（实验参数与 git describe）、`batch.log`、
`drivers/<task>.driver.log` 和 `<experiment>-<task>/`（worker、模型、轨迹与结果）。
worker 的 adpc 挂载只读时仍使用 evobox 暂存，任务结束后立即归档到该实验目录。

集群内使用模板配置时，`api_key` 留空会使用默认号池，也可以手动填写自己的 `api_key`。

需要指定号池中的 key 时，在原启动命令后追加：

```bash
--slow-key-ids 1,2,3,4
```

也可以在所选慢模型条目的 `key_pool` 中配置，命令行参数优先：

```json
{"key_ids": [1, 2, 3, 4]}
```

编号对应凭据列表中的第 1、2、3、4 把 key，不是密钥内容：

- 不填编号或配置 `[]`：使用默认优先级。
- 填一个编号：只使用这一把。
- 填多个编号：这些 key 按同级轮询分配给 worker，正常请求固定使用分配到的 key；故障仅在指定集合内切换。

不同实验可指定不重叠的编号集合。这里均分的是 worker，不保证请求量严格相等；编号选择与手填完整 `api_key` 二选一。

慢模型的调用纪律（`models/slow.py`）：

- 思考强度取所选 API 的配置；空正文（全留在思考通道）→ 下一次重试自动升档；
- 叙述文字包裹 JSON → 自动剥围栏取首个平衡 `{…}` 抢救；
- `finish_reason=length` → 明确报错，提示调大 `slow_max_tokens`。

## 排序后端

两个后端跑**同一套**两阶段排序（operation → action），harness 侧一行开关切换：

| 后端 | 协议 | 截图 | 形态 |
|---|---|---|---|
| **jev**（默认） | `GET /` + `POST /rank` | 需要（暂存到中转目录，payload 带服务宿主机视角路径） | 视觉-语言决策模型 |
| **kev** | System One `POST /v1/systemone` | 不需要（纯文本，候选描述含坐标/上下文） | 小体量决策模型 |

两者的分数都是 **softmax 输出，不是校准正确率**——`score_is_calibrated_correctness_probability`
恒为 `false`，对比分析时不能当作概率读。排序延迟 `latency_ms` 是服务端实测值。

## 结果目录

所有产物落在**唯一位置** `rjob_logs/<run_id>/`（无副本）。`obs_stage/`（episode 内工作区）结束即清。

```
rjob_logs/<run_id>/
├── summary.json           # run 级汇总（demo / 人工验收入口）
├── heartbeat.json         # 进度心跳
├── code_version.json      # git describe + 协议版本 + 镜像 digest + run spec（secrets 脱敏为 ***）
├── worker/entry.log       # worker 侧运行日志（LLM 审计 + 事件流）
└── episodes/<task_id>/
    ├── task.json          # 任务定义副本（自包含）
    ├── traj.jsonl         # 每步观察 + 动作 + 回执
    ├── llm_calls.jsonl    # 全量 LLM 请求/响应（role: task_requirements / plan_step / verify_observation）
    ├── events.jsonl       # 决策事件（route: planner/gui/cli/gui_verified/cli_verified/done_probe/terminal）
    ├── screenshots/       # 逐步截图
    ├── a11y_digests.jsonl # 每步 a11y 摘要（sha256 + 节点数）
    ├── deliverables/      # finish 时导出的 VM 文件
    ├── evaluation.json    # 官方评分（source/raw/score）
    └── episode.json       # 终态（score/status/stop_reason/steps）
```

字段级定义见 [docs/RESULT_DIR_CONTRACT.md](docs/RESULT_DIR_CONTRACT.md)（eval ↔ demo 的唯一接口契约）。

## Demo

`demo/` 把结果目录变成静态站点：逐步回放桌面轨迹与模型操作链，横向对比多个 run 的
**成本 / 耗时 / 准确率**。

```bash
python demo/build_site.py --runs-root rjob_logs --out demo/site --benchmark osworld2
python -m http.server 8899 --directory demo/site    # 本地预览
```

披露口径：未出分如实标注、不折算 0 分、不计入均分；排序分数标注 softmax 非校准概率；
成本只用 token 与调用次数表达，不臆造货币单价；截图标明是离散帧非录屏。

详见 [demo/README.md](demo/README.md)。

## 文档

| 文档 | 内容 |
|---|---|
| [docs/CLUSTER_RUNTIME.md](docs/CLUSTER_RUNTIME.md) | 内部集群运行手册：配置、run 生命周期、worker 入口、共享资源、已知坑 |
| [docs/RESULT_DIR_CONTRACT.md](docs/RESULT_DIR_CONTRACT.md) | 结果目录契约（字段级定义 + 步号语义） |
| [DEVIATIONS.md](DEVIATIONS.md) | 与旧仓库 SA-CUAharness 的已知行为差异清单 |
| [AGENTS.md](AGENTS.md) | 开发纪律与目录地图 |
| [demo/README.md](demo/README.md) | demo 站点说明 |

## 阶段执行（可选）

默认保持逐步规划；启用 `--execution-mode stage` 后，由慢模型规划阶段、快模型连续选择动作，
规则验证优先，仅在必要时调用模型验证或重规划。
`--execution-mode goal` 可启用带任务记忆的局部连续执行，阶段结束再独立 Verify；
保留 A11y/CDP 语义融合及 JEV 服务恢复，默认 step/stage 行为不变。
配置、执行边界、开发入口和验收方法见 [阶段执行说明](docs/STAGE_EXECUTION.md)。
