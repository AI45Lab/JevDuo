# Slow Orchestrator 框架

提供编排框架、插件协议和可选的 `gui_jev`、`general` 适配器。Subagent Registry 默认空；
按各插件配置显式注册。`game_jev` 尚未实现。配置模型默认使用旧 Desktop 模式，
本工程根目录 `config.yaml` 已显式开启编排和两个插件。

## 开启方式

在评测工程的配置中增加以下字段，由 `src/appcfg.py` 传递给 NorthCUA：

```yaml
harness:
  orchestration:
    enabled: true
    max_cycles: 20
    max_seconds: 600
    max_model_calls: 100
    max_tool_calls: 1000
    min_subagent_steps: 30
    empty_response_retries: 2
```

直接使用 NorthCUA app YAML 时，将 `orchestration` 放在顶层。当前仅支持本地
Python `run_query` 入口，包括连接任务 VM 的 OSWorld 后端。需设置
`evaluation.enabled: false`；OSWorld 外部官方评分不受此开关影响。
远端 WebSocket、单步 `run_round` 和固定输入截图目前明确拒绝新模式。

空池可以初始化、观察、调用 Slow 并输出轨迹。若任务已完成，Slow 可以根据当前
Observation 返回 SUCCESS；需要执行时返回 `configuration_error / no_subagent_available`，
不进入反复规划或自动回退。空池不会完成尚需动作的实际任务。

## 执行与接口

框架沿用 `AgentSessionRuntime → AgentRoundRuntime → AgentStepRuntime`。
`DesktopOrchestrationPorts` 通过现有 `PortStepPipelineExecutor` 连接：

```text
Observation → SlowContext → SlowDecision → Registry/Subagent → CycleRecord
          ↑                                               ↓
          └──────────── History / Note / Budget ────────────┘
```

一个外层 step 是一次 Slow 决策，加 CONTINUE 分支的一次有预算的 Subgoal 尝试。
终态分支不调用 Subagent。新模式的 `max_cycles` 限制编排轮数；旧模式 `max_steps`
含义不变。`agent_result.json.step_unit` 区分两种口径。新模式 step_count 是当前任务累计的
实际编排轮数，包含恢复前保存的轮数，不包含运行前预算终态的 synthetic 记录。内部调用另行计量；
model_calls 在框架最终 provider 调用边界计量，不保证覆盖底层 SDK 内部不可见的网络重试。
tool_calls 计入 runtime.execute 的执行尝试，不包含 Observation 内部探针；环境观察同样受时间预算约束。
Token usage 保留在 LLM trace，目前不提供 token 或金额硬预算。

`min_subagent_steps` 默认 30。每次派发时，框架将子任务的 `max_model_calls` 和
`max_tool_calls` 分别补足到该下限；Slow 给出的更大额度保留。有效预算写入 Contract 和轨迹，
并在 Slow 提示中说明。这里的“步数”按现有模型调用额度计量，工具额度也同步保底；
实际可执行次数仍受全局剩余额度、父 agent 核验预留和时间限制约束，子任务可以提前完成。
设置为 0 可关闭该下限，便于独立的小额度测试；本工程根配置显式设置为 30。

输入输出定义在 `northcua.agentkit.orchestration.contracts`：

| 类型 | 内容 |
|---|---|
| `SlowContext` | Task、当前 Observation 引用、History、Note、上次子任务结果、预算、可用插件 |
| `SlowDecision` | SUCCESS / FAIL / CONTINUE、当前 observation_id、核验说明、证据、可选 Contract 和 Note |
| `SubgoalContract` | ID、Agent ID、目标、成功标准、约束、工具白名单、能力要求、预算、插件参数 |
| `SubagentRequest` | Session/Round/Attempt 身份、原始任务、Contract、初始 Observation、History 和 Note |
| `SubagentResult` | achieved/yielded/blocked/failed、原因、完成声明、证据、最后观察、轨迹引用、Usage、Note 建议 |

后续插件实现 `spec: SubagentSpec` 和
`async run(request, runtime) -> SubagentResult`，通过
`app.facade.runtime_factory.subagents.register(plugin)` 显式注册。
`input_model` 使用 Pydantic 校验插件参数，Registry 校验协议、能力和必需工具。
没有动态发现；`gui_jev` 和 `general` 由配置显式注册，测试中的 Fake 不注册到生产环境。

`runtime.observe()`、`execute(tool_name, arguments)`、`infer(prompt)` 绑定当前任务环境。
普通 execute 显式绑定 `DesktopNativeActionExecutor` 并透传桌面坐标策略。
`gui_jev` 的 `agent_desktop` 有独立的当前 VM 路由；其他 CLI 或工具不会通过名称
自动落到控制器主机 Shell。
工具白名单为空时禁止工具。结构化工具权限、次数与 deadline 由框架检查；自然语言约束
仍需要插件及 Slow 遵守。插件是受信任的进程内代码，这一接口不提供安全沙箱。
插件返回或取消后，handle 关闭并取消、等待其在途异步操作。底层已投递请求或线程中的
物理动作无法通过取消撤销，其结果仍需要观察核验。Subagent `achieved` 是待核验声明，下一轮 Slow 必须基于新观察
核验；最后一轮不能再分发子目标，因为需要保留核验轮数。预算不足时保留 unverified 状态，
不能将其记作全局成功。Subagent 的 Note 只是建议，由下一次 Slow 决定是否写入。

Slow 复用 `AgentKitModelStepExecutor` 的模型配置、fallback、上下文管理与 trace，清空
环境工具、原生子 Agent、MCP 和原有 hooks/middleware，抑制 app MCP 注入，并检查实际
provider 工具表。输出必须是符合 schema 的 JSON，SUCCESS 必须引用当前 observation_id。
这项结构校验不替代任务语义核验和官方评分。

### 空响应恢复与完整响应记录

Slow、Jev 共享模型推理及 `general` 的模型调用，在非流式 OpenAI Chat SDK 返回后、NexAU
转换为 message 前，将完整响应正文追加到 session artifact 根目录的 `provider_responses.jsonl`。
记录保留 `choices[].finish_reason`、`message`（含 reasoning/tool calls）、原始 `usage` 和扩展字段，
并包含 cycle/session/round/agent_path、`inference_id`、从 1 开始的 `attempt` 及同一尝试内的
`response_index`。文件权限为 0600，每条写入加锁并落盘；遇到不完整的既有 JSONL 或保存失败时终止推理，
不因此重复请求模型。日志只保存 SDK 响应正文，不收集请求凭据或 HTTP headers。
正常返回的 `llm_trace.response` 保留现有 message 字段，另增加 `provider_response` 完整响应、
`inference_id` 和 `attempt`，并将 `llm_trace.stop_reason` 设置为该响应的 `finish_reason`。
被空响应策略中断的尝试只记录在 `provider_responses.jsonl` 中，避免被后一次成功响应覆盖。

`empty_response_retries` 默认 2，即初次请求后最多再试 2 次；允许 0–5。只有正文为空（含纯空白）、
没有 tool/function calls 的响应才触发此策略；reasoning 非空不算有效正文。
空响应带 `refusal` 或 `finish_reason=content_filter` 时直接终止。非空但不合 schema 的 JSON、
工具调用及其他异常不触发这项重试。相同的 prompt/tools 在新尝试中保持不变，所有尝试仍经过现有
provider guard，消耗全局/子目标 model_calls，并受剩余时间、子目标期限和后续核验预留预算限制。
内部专用中断信号使空响应绕过 NexAU 的通用重试及模型 fallback，防止重试次数叠加；
底层 SDK 的网络错误重试仍遵循既有配置，不计为这里的空响应重试。
次数耗尽时返回包含尝试次数、finish_reason 和记录路径的 `EmptyModelResponseError`。
取消仍向外传播，不重试或吞掉取消。
捕获范围绑定本次模型步骤实际使用的 caller；上下文摘要等辅助模型的响应不会被误记或触发此重试。

上述完整响应记录和空响应恢复针对当前配置 `stream: false`。流式调用保持既有行为；
尚未返回 SDK completion 的 HTTP/传输错误也没有完整响应正文可记录。

## 状态与中断

RoundState 与原 SessionManager 保存编排状态，只有外层 Runtime 推进全局计数。
已有 ArtifactStore 下的 `query_NNNN/orchestration.jsonl` 记录调用消耗、dispatch 起止
和状态 checkpoint；调用消耗先于动作写入，包括失败尝试。主 `steps.jsonl` 保留 CycleRecord，
`llm_trace.jsonl` 用 agent_path 区分 Slow 和子 Agent 调用。每次观察的截图另存独立引用，
避免同一步内探针覆盖原图。

取消继续向上传播，未完成 attempt 的意图与调用消耗留在 journal。持久化环境允许
跨进程恢复；Session backend 本身为 memory 时，不能把进程退出后的 Session 视为仍可用。
已存在的持久化 Session 可以用相同 query、session_id 和
`DesktopRunRequest(metadata={"resume": True})` 显式恢复。
恢复会对账主步骤，重新观察，将未完成 attempt 标为 interrupted 后交回 Slow；不会自动
重放子目标。已终止任务不能 resume，应创建新轮次或 Session。

环境动作与本地状态不是原子事务，不保证 exactly-once。日志损坏或尾部撕裂会明确拒绝恢复，
不会覆盖或接续写入坏记录，应保留证据并使用新 Session。

## 接入 agent-desktop Jev

`gui_jev` 直接导入外部 `jev-desktop` 包，复用 `JevLoop`、`AgentDesktop`、
候选集、动作选择、文本写入前重新观察、值核验和资源清理。
没有复制或修改 agent-desktop 源码。当前适配目标是本工程的 OSWorld VM，
不支持 NorthCUA 的 native 后端。

在仓库根目录安装用户提供的包：

```bash
uv pip install --python venv/bin/python --no-deps -e ../agent-desktop/jev
```

也可在重建环境时指定 `JEVDUO_JEV_PACKAGE=/absolute/path/to/agent-desktop/jev`，
由 `scripts/setup_env.sh` 在原依赖安装完成后安装。所有运行节点需要能读取该 editable
路径。未启用插件时无需安装此包。

在现有 `harness.orchestration` 中增加：

```yaml
gui_jev:
  enabled: true
  launch_args:
    google-chrome.desktop: [--force-renderer-accessibility]
  desktop:
    binary: /path/in/vm/agent-desktop
    headed: true
    # snapshot_timeout_ms: 15000
    # verify_ms: 2000
  options:
    cursor: false
    values: true
    confirm_destructive: false
    # scope: {app: SomeApp, window_id: window-id}
    # text: [explicit field content]
  limits:
    steps: 40
    calls: 80
    stalled_turns: 3
```

直接使用 NorthCUA app YAML 时，该段在 `orchestration.gui_jev` 下。
`desktop` 使用原 `DesktopConfig.from_env(remote=True, overrides=...)`；
显式配置覆盖 `AGENT_DESKTOP_BIN`、`AGENT_DESKTOP_SNAPSHOT_TIMEOUT_MS`、
`AGENT_DESKTOP_SESSION`、`AGENT_DESKTOP_AGENT_ID`、`JEV_VERIFY_MS`。
`options` 对应原 RunOptions（`text_model` 固定为共享 Slow），`limits` 对应原 Limits；
省略的字段沿用包内默认值。VM 中需要已安装 agent-desktop，并具有可用的桌面会话、
辅助功能接口；自动发现应用还需要原 Jev 要求的 python3-gi/Gio。

每次未固定 `window_id` 或 `root` 的子任务先选择应用，即使当前已有 Writer 等窗口，
`app` 为空也会执行。适配器复用原 `applications()` 的 Gio 安装目录，并读取原生
`list-apps` 和 `list-windows`；Jev 从现有窗口或已安装应用中选择，`app` 仅作为偏好提示。
原 `launch_request`、`validate_choice` 负责候选分页和选择校验，没有按应用名称猜别名。

选中安装应用时，通过同一共享运行时执行一次 `agent-desktop launch <desktop-id>`。
`launch_args` 的键是安装目录中的精确 desktop entry ID，值按 `--arg=<value>` 传递；
参数来自配置，不能由 Contract 随意生成。上例会向 Chrome 传入原辅助功能启动标志。
运行时代码没有 Chrome 专用分支，未配置的应用不增加参数。

launch 后重新获取运行中应用与窗口列表，仅保留启动结果所证明的 PID、process_instance
及应用名对应的窗口，再让 Jev 选择。暂未出现窗口时在 20 秒观察窗口内轮询，单次调用仍受共享 deadline 限制；不重放 launch，
不会回退到任意新窗口或当前窗口。选定后以真实 `app_name/window_id` 进入未改动的原
JevLoop，不额外要求前置焦点确认；语义动作按其能力执行，物理输入仍由原生动作验证
窗口和焦点。明确给出 `window_id/root` 时保持严格 scope，
跳过应用选择；失败不会悄悄切换到其他窗口。

应用选择和启动沿用共享预算与取消机制；选择调用会从原 Jev `limits.calls` 中扣除。
返回结果保留应用选择阶段、启动动作及具体失败信息，便于下一轮 Slow 重新规划。
原生动作报告 `frame_not_active` 且文本尚未投递时，结果为 `blocked`，退出原因是
`window_activation_unconfirmed`。Slow 应诊断或报告环境阻塞，不能仅换子任务 ID 重复派发。

`headed: true` 显式允许原包的物理文本替换：对于可读、可编辑但缺少 SetValue 的字段，
先确认窗口和焦点、证明全选范围，再键入并核验完整值。支持 SetValue 的字段继续使用
语义写入。只读、密码、隐藏字段和含控制字符的目标文本会拒绝；无法证明完整选区时
也会停止（包括部分已有 emoji 内容）。没有剪贴板或粘贴回退，不自动重放未知投递结果。

目标/动作选择继续使用 `TYPESAFE_MODEL`（默认 `jev-latest`）、`TYPESAFE_BASE_URL` 和
`TYPESAFE_API_KEY`。这些环境变量需提供给实际运行 agent 的进程；集群 driver 的环境
不会自动成为远端 worker 的环境。不要把 key 写进 app YAML。文本生成只调用当前
NorthCUA 的 Slow 模型及其凭据，不读取 `TEXT_MODEL_*`。显式 text 优先，耗尽后才生成。

为支持异步预算和取消，Jev HTTP 调用由薄适配器发送，沿用 TypeSafeModel 的配置解析、
请求体、回答结构以及 429/503/529 的有限退避规则；每次 HTTP 尝试单独计入 model_calls。
原 JevLoop 仍校验模型只能从提供的选项中选择，不新增置信度执行阈值。
慢模型返回 JSON 中的 text 字段，避免通用响应规范化改变文本首尾空白。

Slow 会看到 `gui_jev` 的描述和输入 schema。CONTINUE Contract 必须设置
`agent_id: gui_jev` 和 `allowed_tools: [agent_desktop]`；`agent_input` 可提供
`app`、`root`、`window_id`、`text`，不能更改 VM、二进制或模型凭据。
子任务 goal、success_criteria、constraints 一起交给 Jev。预算同时受原 Jev Limits、
SubgoalBudget 和全局预算限制。Jev 的 done 仅映射为 achieved 声明，下一轮 Slow
仍须用新的 Observation 核验。子任务不能消耗全局最后一次模型调用，该次调用保留给
后续 Slow 验证；时间预算仍是整个任务的硬上限。
正常退出 JevLoop 并完成清理后，适配器再获取一次 NorthCUA Observation，作为统一结果的
last_observation；异常或取消前未获取该观察时，该字段为空，派发前观察仍单独保留在请求里。

所有 CLI 请求（含 snapshot、应用发现和 launch）通过共享 handle 访问
`desktop.osworld.vm_ip/server_port` 指定的当前 VM；`AGENT_DESKTOP_VM_URL` 不参与路由。
每个 `/execute` POST 仅发送一次；不确定的写入不会自动重试或切换到 paste。
CLI 请求/响应在 journal 中标为
redacted，Jev 返回的动作摘要沿用原包的文本脱敏；Slow 模型 trace 仍包含实际模型输入输出。

框架按每次 `snapshot --timeout-ms` 的实际参数设置 HTTP 超时下限，至少比原生快照预算多
5 秒。自动恢复延长快照预算时，这一下限同步延长；父任务的共享 deadline 继续生效。
子任务摘要中的 `execution_diagnostics` 保留最近八次执行的预算、耗时、HTTP 状态和失败阶段，
以及快照返回的完整性标志。该字段不保存命令参数、异常消息或响应正文；读取失败时仍可据此
区分传输超时、HTTP 状态错误、响应 JSON、执行封装和原生命令输出错误。
普通工具抛异常或被取消时，journal 写入 `tool_failed`，记录工具名、异常类型和耗时后继续抛出；
这条记录不声明动作是否已投递，也不授权重放。

同步 JevLoop 在单个线程运行，模型和动作经桥接进入异步 handle。取消会关闭后续调用，
等待已提交的命令返回或超时、完成原 run_context 清理并等待线程退出，再交回编排器。
不能撤销已经投递到 VM 的动作，丢失响应时仍需重新观察。
当前 Jev 只清理光标 overlay，不保存或恢复剪贴板；运行时清理通道每个 attempt
最多两次、每次最多五秒，独立计入 `cleanup_tool_calls`，可在正常
预算耗尽后执行；它不提供普通动作或新模型调用。清理失败会留下 journal 记录并使子任务失败。

## 本地验证

从仓库根目录执行：

```bash
venv/bin/python -m pytest -q
venv/bin/python -m ruff check src/appcfg.py src/entry.py src/launch.py src/recorder.py src/task_run.py \
  src/upstream/northcua/agents/desktop/subagents/ tests/test_orchestration.py tests/test_jev_subagent.py
```

框架测试使用 Fake Model/Subagent；可选的 `tests/test_jev_subagent.py` 使用真实 JevLoop
和内存 HTTP transport，验证模型分工、同 VM 路由、输入、预算、取消和清理。
未安装外部包时该测试文件跳过。测试不启动 VM，不访问模型 API，不代表真实 GUI 或
OSWorld 任务成功。

## 通用补位 Subagent

`general` 参考同目录树下 `OSWorld/mm_agents/kimi` 的 GUI/hybrid 能力与原生工具调用协议，
独立实现有预算的子目标循环，不导入整个 OSWorld 工程，不增加模型密钥或新的模型选择。
它复用父 Slow 的模型配置、fallback、上下文管理、reasoning replay 策略和 LLM trace。
在父 Slow 的 provider 工具面仍为空的同时，general 的单次推理单独声明原生 function tools，
推理层不能直接执行工具；返回后统一通过 ScopedRuntime 执行。

```yaml
harness:
  orchestration:
    enabled: true
    general:
      enabled: true
      mode: hybrid  # gui 只开放截图和 GUI 操作
    gui_jev:
      enabled: true
```

本工程 `config.yaml` 已同时开启 `orchestration.enabled`、`gui_jev.enabled` 和 `general.enabled`，
运行时两个插件一起注册，由 Slow 选择每轮执行者。当前默认模型为 `k3.1-pro`，general 继承
该模型配置，与父 Slow 共用。NorthCUA app 示例和配置模型中插件仍默认关闭。
当前只支持 `desktop.backend: osworld`，所有 GUI/Python/Bash 都发往当前任务 VM 的
`desktop.osworld.vm_ip/server_port`；没有控制器主机执行或另起 VM 的回退。

父 Slow 可从 Registry 发现以下能力和工具，自行决定何时从 `gui_jev` 切换：

| 模式 | 能力 | 子目标 allowed_tools |
|---|---|---|
| gui | 截图坐标点击、拖拽、画布操作、快捷键、文本 | `execute_pyautogui_code` |
| hybrid | 上述 GUI 操作，加文件检查、Bash/Python 脚本及结果核验 | `execute_pyautogui_code`，需要命令时另加 `run_command` |

等待动作需要额外允许 `wait_for_state`。`take_screenshot`、`finish_task` 是子循环的观察与终止接口，
不需要加入 execute 白名单。`agent_id` 为 `general`，`agent_input` 必须为 `{}`。
GUI 模式在模型工具面和执行端均拒绝 `run_command`；hybrid 也受合同工具白名单约束。
主 Slow 继续进行语义规划和验收；支持的语义 GUI 操作优先交给 `gui_jev`，其他操作可作为
有界子目标交给 general，不会在 Jev 失败时隐式重放其动作。

模型每轮必须返回一个原生 tool call；多调用响应不会执行其中任何动作，会为每个 call ID
回放未执行说明。纯文本不会被当作代码解析。执行后回放真实的 stdout、stderr、退出码、
超时和截断标志，并附上新的截图；历史保留真实工具结果和最近三张截图。
GUI 坐标一律为原始截图的绝对像素，不套用父 agent 的 0–1000 坐标策略。
PyAutoGUI 代码采用结构校验，支持直线调用和字面量参数，支持 `pyperclip.copy` 输入 Unicode，
不支持循环、赋值或任意 Python；需要这些能力时在 hybrid 模式调用 `run_command`。
VM 需要安装 PyAutoGUI、pyperclip 并具有活动桌面会话。

命令通过 VM `/execute` 的一次 POST 投递；不进行写入重试。guest wrapper 使用进程组执行，
命令最多 90 秒，GUI 代码最多 30 秒，同时缩短至子任务剩余时间。超时会杀死该进程组，
输出通过临时文件采集，每路返回最多 32768 字节并标明截断。此机制不是任意 shell 的沙箱，
自行脱离进程组的后台服务不在进程组终止保证之内。
取消会停止后续调用，并等待已提交 HTTP 操作有界结束；丢失响应时还会等待完整 guest 执行窗口，
避免马上恢复父级动作。此收尾等待可能超过原 deadline。unknown/timeout 返回 blocked，
父 Slow 必须重新观察，不得重放可能已执行的写入。
嵌套 deadline 将取消转换为 TimeoutError 之前，会先持久化该 attempt 的投递结果和
`unsafe_to_replay` 标记；恢复时保留这条结果，不会被普通 interrupted 状态覆盖。
用户主动取消仍向上传播，不继续进行下一次模型或工具调用。

`achieved` 仍只是子目标声明，不能把退出码 0 当作任务成功。结果摘要保留最近三条工具结果，
其中 stdout/stderr 各最多 4096 字符并标明截断，完整返回值留在 journal/模型回放中。
文件等非视觉任务需要检查这些实际命令证据，必要时另派只读核验，最终以官方评分为准。
主 Slow 始终保留下一次模型调用和新观察来验收。

`tests/test_general_subagent.py` 覆盖原生 provider 请求序列化、图片输入和真实结果配对、
同 VM 路由、GUI/hybrid 权限、预算、输出截断、guest 进程组超时和取消。
这些是本地 scripted/模拟传输测试，不包含真实模型、VM GUI 或 OSWorld 官方评分。
