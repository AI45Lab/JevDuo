# JevDuo · OSWorld 2.1 集群测评

在 OSWorld 2.1 上用上游 CUA harness 跑 agent 测评，通过内部集群的 `rjob` 提交任务。
本分支是**测评工程分支**，与 `main`（JevDuo harness 本体）相互独立，不 merge、不 rebase。

被测 agent 来自 `src/upstream/`（以 **git subtree** 方式入库的上游精简树，见
[上游源码](#上游源码subtree)），评测链路为：
切题 → 渲染 job spec → 并发提交 rjob → 起 VM → 跑 agent → 官方评分 → 落盘汇总。

当前 `config.yaml` 开启 Slow 编排，由 Slow 选择 `gui_jev` 或 `general` 子代理并核验结果。
编排与插件配置详见 [ORCHESTRATION.md](docs/ORCHESTRATION.md)。

## 环境准备

- 开发机需要已配置权限的 `rjob`、`uv >= 0.13.6`，以及集群可访问的共享盘。
  可用 `JEVDUO_UV=/path/to/uv` 指定 setup 使用的 uv。
- 检查 `config.yaml` 的 `cluster` 和 `benchmark`：镜像、挂载、prepared runtime、
  VM qcow2、assets、共享 `env.sh` 必须在实际 worker 中可用；GitLab token 文件由 driver
  读取并将值写入私有 spec，因此须在 driver 中可读。
- 当前开启 `gui_jev`，需要外部 `jev-desktop` Python 包；其 editable 源码路径也必须在
  所有 worker 的共享挂载中可见。VM 镜像内还需有配置所指的 `agent-desktop` 可执行文件，
  这与开发机上的 Python 包是两个独立依赖。

从仓库根目录安装。`setup_env.sh` 会重建 `venv/`，搬动源码后也需要重建：

```bash
# 改为共享盘上的实际包路径
export JEVDUO_JEV_PACKAGE=/absolute/shared/path/to/agent-desktop/jev
bash scripts/setup_env.sh
venv/bin/python -c 'import northcua, nexau, yaml, jev_desktop'
```

已有 venv 时，也可单独安装插件：

```bash
uv pip install --python venv/bin/python --no-deps -e "$JEVDUO_JEV_PACKAGE"
```

## 凭据配置

首次创建 `.env.local` 模板；已有文件会保留。随后编辑模板中的占位值：

```bash
if [ ! -f .env.local ]; then
  (umask 077; cat > .env.local <<'EOF'
export JEVDUO_KIMI_API_KEY='...'
export JEVDUO_REGISTRY_AUTH='...'
export TYPESAFE_API_KEY='...'
# 按 Jev 服务配置，必要时设置：
# export TYPESAFE_MODEL='jev-latest'
# export TYPESAFE_BASE_URL='...'
# 选择 MiniMax actor 时还需：
# export JEVDUO_MINIMAX_API_KEY='...'
EOF
  )
fi
chmod 600 .env.local
```

`JEVDUO_REGISTRY_AUTH` 支持裸 `user:pass` 或其 base64 编码。
`JEVDUO_KIMI_API_KEY` 用于当前默认 actor，也用于 `evaluation_model` 配置的官方评分模型；
使用 `--model MiniMax-M3.1-Flash` 只切换 actor，不会同时切换评分模型。
`TYPESAFE_API_KEY` 用于 Jev 决策服务。`run.sh` 自动加载 `.env.local`，driver 将明确支持的
`TYPESAFE_API_KEY`、`TYPESAFE_MODEL`、`TYPESAFE_BASE_URL` 写入私有 spec，再注入 agent 进程。

## 提交评测

以下命令会实际提交集群任务；每次新评测使用新的 run ID。

```bash
# 单题冒烟；前台等待任务结束
bash scripts/run.sh --run-id jv-smoke-001 --tasks 001 --jobs 1 --per-job 1

# 全量 108 题：最多 6 个 rjob，各串行 18 题；脱离终端运行
bash scripts/run.sh --run-id jv-osw21-all --jobs 6 --per-job 18 --detach

# 只跑第 1/6 分片（18 题），最多 2 个 rjob
bash scripts/run.sh --run-id jv-osw21-shard1 --shard 1/6 --jobs 2 --per-job 9
```

前台输出写入 `runs/<run_id>/submit.log`；`--detach` 模式写入 `pool.log`，PID 位于 `pool.pid`。
平台任务名及收尾结果位于 `jobs/job-NN/job.json`。当前 `cluster.keep_job: true`，结束后保留
平台任务，由使用者按实际任务名清理；`--keep` 也可要求保留任务。

同一 run ID 默认跳过已有 `episode.json` 的题，包括 `failed_unscored`；
显式加 `--retry-failed` 才重跑其中未评分的题，合法零分不会因此重跑。
`--no-resume` 关闭这项跳过逻辑。配置切换使用 `--config`，actor 切换使用 `--model`。
提交失败没有自动指数退避或自动重新提交；已在运行的同名 job 可由普通任务池接管等待。

## 当前预算与评分

以下为当前 `config.yaml` 的默认值，命令行覆盖仅作用于本次运行：

| 配置 | 默认值与含义 |
|---|---|
| `harness.max_steps` / `orchestration.max_cycles` | 20 个编排轮次，不是 20 次 GUI 动作 |
| `orchestration.subagent_steps_per_cycle` | 每个子任务最多 50 次模型调用、50 次工具调用 |
| `orchestration.max_model_calls` / `max_tool_calls` | 整题分别 1300 / 1000；调用失败也消耗额度 |
| `harness.max_wall_seconds` | 10800 秒，覆盖 VM setup、actor 和官方评分 |
| `harness.evaluation_reserve_seconds` | 在上述总时间内为评分预留 600 秒 |
| `harness.inactivity_timeout_seconds` | 720 秒无活动触发 actor 错误 |
| `orchestration.model_timeout_retries` | 每次模型推理超时最多重试 2 次，仍受总预算约束 |
| `pool.max_parallel` / `tasks_per_job` | 1 / 1；仅在 rjob 级并发，job 内逐题串行 |
| `cluster.cpu` / `memory` | 每 job 4 CPU / 16384 MB |

轮次上限应同时检查 `harness.max_steps` 和 `harness.orchestration.max_cycles`；
`--max-steps` 只覆盖前者。`--max-wall` 覆盖单题墙钟上限，子任务仍受自身及编排时间预算限制。
`models.apis` 登记模型与凭据环境变量；`direct_api: true` 会剔除 actor 子进程的代理环境变量。

`harness.evaluation.enabled: false` 关闭的是上游内部评估器，OSWorld 官方评分仍运行。
`evaluation_model` 单独配置官方评分使用的模型；其故障会使当前题成为 `failed_unscored`，
不会伪装成得分 0，也不会污染同一 job 的后续题。
`scored` 包括合法零分和部分分；actor 返回 `DONE`、job 成功退出都不能代替官方分数。
步数或 actor 时间耗尽后仍可进入官方评分；setup、agent 异常或整题硬截止导致的失败记为未评分。

## 目录

| 路径 | 内容 |
|---|---|
| `config.yaml` | **唯一配置**：模型、harness 行为、并发、集群、基准、日志；不含密钥 |
| `scripts/run.sh` | 常规评测入口（开发机） |
| `scripts/setup_env.sh` | 重建 agent venv（解释器装共享盘 + 路径断言） |
| `infra/worker.sh` | rjob worker 入口：共享 env、dockerd、拉镜像、进 `entry` |
| `src/settings.py` | 读 config.yaml、切题、渲染 job spec（开发机侧，需要 PyYAML） |
| `src/pool.py` | 普通任务池：切片、并发 rjob、接管等待、收日志与汇总 |
| `src/rjob.py` | rjob CLI 封装与状态解析 |
| `src/entry.py` | worker 主循环（冻结解释器，只读 JSON spec） |
| `src/vm.py` | OSWorld VM 生命周期：fetch_task / reset / execute / score / close |
| `src/launch.py` | 起 agent 子进程、超时杀进程组、终态映射 |
| `src/actor_run.py` | 单阶段与多阶段共用的配置生成、actor 调用和终态交付 |
| `src/phase_run.py` | 原生阶段 setup、共享预算、逐阶段评分与 gate |
| `src/appcfg.py` | 生成 app.yaml / agent.yaml（指向 VM 的 osworld 后端） |
| `src/task_run.py` | 单题 agent 运行器（在 agent venv 内跑） |
| `src/recorder.py` | 结果目录契约（轨迹摘要、截图、汇总） |
| `src/scoring.py` | 官方分数归一化与 `failed_unscored` 判定 |
| `src/evaluation_runtime.py` | 官方评分模型配置与按题隔离的模型故障检查 |
| `src/evaluator_dependencies.py` / `src/setup_transport.py` | 评分依赖预检与一次投递的慢 setup 传输 |
| `src/worker_limits.py` | 覆盖 setup、actor 和评分的整题硬截止 |
| `src/recovery_batch.py` | 固定允许任务集的专用恢复 driver，使用不可变源码快照 |
| `docs/ORCHESTRATION.md` | Slow、Jev、general 插件协议及行为说明 |
| `tests/` | 本地逻辑与模拟传输回归，不启动真实 VM 或模型调用 |
| `runs/<run_id>/` | 全部产物（gitignore） |

## 三个解释器，别混

| 角色 | 解释器 | 说明 |
|---|---|---|
| 开发机 `pool` / `settings` | 仓库 `venv/bin/python` | 有 PyYAML，读 `config.yaml` |
| worker `entry` | 共享 `$OSWORLD_PYTHON` | 冻结 venv，**没有 PyYAML**，只读 JSON spec |
| agent 子进程 `task_run` | 仓库 `venv/bin/python` | 上游源码 + NexAU |

因此 `config.yaml` 只在开发机解析；worker 只接收 `settings.py` 渲染好的 `job_specs/*.json`。

## 专用恢复入口

`recovery_batch` 仅接受源码 `TASKS` 中列出的任务，要求 20 轮、每子任务 50 次额度和 10800 秒预算。
它为新 run 建立 `source-snapshot/`，提交时显式使用快照中的 worker，复用当前已安装的 venv。
不会自动重放提交状态不明的请求，也不会自动重新启动停止的任务。

显式选择允许任务后，使用新的 run ID 调用；此入口需自行加载凭据：

```bash
set -a
. ./.env.local
set +a
PYTHONPATH=src venv/bin/python -m recovery_batch \
  --run-id jv-recovery-001 --tasks 005,018 --parallel 1
```

## 产物

```
runs/<run_id>/
├── run.json / submit.log / summary.json / summary.csv
├── job_specs/job-NN.json           # 600；含 key，跑完可删
├── jobs/job-NN/
│   ├── job.json / status.txt / tail.log / heartbeat.json / summary.json
│   ├── worker/{entry,dockerd,docker-pull,deps-check}.log
│   ├── worker/evaluation_model.jsonl     # 配置评分模型时记录调用状态
│   ├── evaluation_dependencies/<task>.json  # 需要预检的评分依赖
│   └── episodes/<task>/
│       ├── task.json / episode.json / evaluation.json
│       ├── traj.jsonl              # 步骤摘要（动作、回执、窗口、截图引用）
│       ├── llm_calls.jsonl         # 相对软链 → agent/<session>/llm_trace.jsonl
│       ├── screenshots/step_NN.png
│       ├── agent/                  # 上游原始产物（steps.jsonl / llm_trace / runtime）
│       ├── config/{app.yaml,agent.yaml}
│       └── agent_result.json / agent_stdout.log
```

多阶段题还会生成 `phases/phase-NN/` 下的配置、actor 结果、stdout、评分和轨迹；
原始 agent 日志位于 `agent/phase-NN/`。`phase_results.json` 保存逐阶段分数，
顶层 `traj.jsonl` 合并阶段摘要并附 `phase_index`，`llm_calls.index.json` 索引各阶段 trace。
原始模型响应与编排 journal 分别见 agent 目录下的 `provider_responses.jsonl`、
`query_NNNN/orchestration.jsonl`；实际位置由 session/query 子目录决定。

`summary.csv` 字段：`task_id,status,score,steps,seconds,stop_reason,model,failure_reason`。
每个 job 独立写汇总，run 汇总从磁盘读取各题最新的 `episode.json`，区分零分与未评分。

## 离线轨迹浏览

`scripts/export_jevduo_trajectories.py` 与同目录 HTML 模板组成独立导出工具，仅需 Python 3.10+ 标准库。
输入路径必填，兼容原生 `runs/<run_id>/jobs/job-NN/episodes/<task>/` 和归档的 `episodes/<task>/`；
也可直接传入某个 `jobs/job-NN/`。默认生成输入目录下的 `trajectories.html`，浏览器可直接打开。

```bash
# 导出指定 run，图片通过相对路径引用
venv/bin/python scripts/export_jevduo_trajectories.py runs/jv-smoke-001

# 单题可携带页面；重复 --task 可选多题
venv/bin/python scripts/export_jevduo_trajectories.py runs/jv-smoke-001 \
  --task 001 --embed-images -o /tmp/jevduo-001.html
```

默认不覆盖已有页面，更新时显式加 `--force`。相对图片模式需保留页面与产物的相对位置；
`--embed-images` 将截图嵌入 HTML，便于复制，但页面会更大。
`--skip-model-observations` 可跳过较大的模型 trace，省略 General 中间观察。

页面区分编排周期、工具执行、子代理返回和后续父代理验证，分别展示零分与未评分。
同题多份执行按来源路径单独展示，页内统计包含每份执行，不等同于 run 汇总中按最新结果去重的统计。
缺失的日志、截图或无法唯一关联的记录会提示，不能据此推断工具已执行或任务成功。
导出页面含模型文本、工具结果和截图，保留原始内容；嵌入图片不会自动脱敏，分享前须检查。

## 本地验证与凭据保护

```bash
uv pip install --python venv/bin/python pytest ruff
venv/bin/python -m pytest -q
venv/bin/python -m ruff check src tests scripts/export_jevduo_trajectories.py
bash -n scripts/setup_env.sh scripts/run.sh infra/worker.sh
git diff --check
```

上述检查不提交集群任务；真实模型、VM 和官方评分链路需通过单题冒烟另行验证。
`.env.local`、`runs/`、`venv/`、`job_specs/` 均已忽略。spec 含 actor、评分模型、Jev 和基准凭据，
按私有文件处理；配置示例只引用环境变量。异常摘要会遮盖 `sk-` 前缀和 Bearer 格式的值，
这是有限格式的脱敏，不代表原始模型、工具、截图等产物可以直接公开。

## 冒烟基线（2026-11-08）

题 001，`k3.1-pro`，100 步上限：

```
[job-00] succeeded
{"total": 1, "scored": 1, "failed_unscored": 0, "score_mean": 0.3333}
```

这是历史 100 步模式的链路记录，不是当前 Slow/Jev/general 编排配置的验收结果。

## 上游源码（subtree）

`src/upstream/` 以 `git subtree` 方式入库，内容是上游仓库的**精简树**（去掉 `docs/`、`tests/`、
`examples/`、CI，仅保留运行必需部分）：

| 项 | 值 |
|---|---|
| 上游仓库 | `https://github.com/beipo-ai/NorthCUA` |
| 导入基线 commit | `7069e43446c21184f493681ef1eddffa3aeff871` |
| 保留内容 | `northcua`、`northcua_protocol`、`northcua_runtime_shared`、`northcua_controller_plugins`、`northcua_device`、`packages`、`config`、`skills`、`pyproject.toml`、`uv.lock`、`LICENSE`、`NOTICE`、`THIRD_PARTY_NOTICES.md` |
| 入库体积 | 压缩后约 2 MB（工作区展开约 97 MB） |

跟随上游升级（在新的精简分支上做，再 pull 进 subtree）：

```bash
# 1. 取上游新版本并精简
UPSTREAM_REPO='https://github.com/beipo-ai/NorthCUA.git'
NEW_COMMIT='填写上游 commit'
JEVDUO_REPO='/共享盘上的/JevDuo'
git clone "$UPSTREAM_REPO" /tmp/ncua-trim
cd /tmp/ncua-trim && git checkout -b trimmed "$NEW_COMMIT"
git rm -r --cached --ignore-unmatch docs tests examples .github
git commit -m "trim upstream"

# 2. 回到本仓库拉进子树
cd "$JEVDUO_REPO"
git subtree pull --prefix=src/upstream /tmp/ncua-trim trimmed --squash
```

升级后若 `uv.lock` 变化，重跑 `bash scripts/setup_env.sh`。

## 已知坑

- **PyYAML 只在开发机**：worker 侧不要 import `settings`。
- **venv 解释器必须在共享盘**：`setup_env.sh` 会断言 `venv/bin/python` 的解引用路径位于仓库内，
  否则 worker 内会 `No such file or directory`。
- **registry 凭据**：`settings.py` 将裸 `user:pass` 规范为 base64 后写入 spec；
  `worker.sh` 将 `cluster.registry_auth[host].auth` 写入 `~/.docker/config.json`。
- **集群容量**：CPU 紧张时 rjob 会长时间排队（事件里是 `Insufficient cpu`），不是代码问题。
- **预算耗尽**：actor 预算停止仍可进入评分；以 `episode.json` 的实际状态和官方分数为准，
  不将整题硬截止或评分异常算作有效零分。

## 分支约定

测评分支保留评测工程及配套文档；harness 本体和 demo 的主线开发属于 `main`。
两个分支保持独立，不从 `main` 合并，也不向 `main` 回推。
