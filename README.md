<p align="center">
  <img src="docs/assets/jevduo-wordmark.svg" alt="JevDuo" width="360">
</p>

<p align="center"><strong>JEV 驱动的开源计算机操作 Harness</strong></p>

<p align="center">
  <a href="https://xxxyxun.github.io/jevduo/">项目主页</a> ·
  <a href="https://xxxyxun.github.io/jevduo/#film">演示影片</a> ·
  <a href="https://xxxyxun.github.io/jevduo/downloads/JevDuo-v16-4K.mp4">下载 4K 影片</a>
</p>

https://github.com/user-attachments/assets/f48203cb-eb2b-496e-8020-83c1ffbf7838

## OSWorld 2.1 集群测评

在 OSWorld 2.1 上用上游 CUA harness 跑 agent 测评，通过内部集群的 `rjob` 提交任务。
本分支是**测评工程分支**，与 `main`（JevDuo harness 本体）相互独立，不 merge、不 rebase。

被测 agent 来自 `src/upstream/`（以 **git subtree** 方式入库的上游精简树，见
[上游源码](#上游源码subtree)），评测链路为：
切题 → 渲染 job spec → 并发提交 rjob → 起 VM → 跑 agent → 官方评分 → 落盘汇总。

当前 `config.yaml` 开启 Slow 编排，由 Slow 选择 `gui_jev` 或 `general` 子代理并核验结果。
`gui_jev` 的有限选择当前由 `k3.1-pro` 的 Chat Completions 接口完成，保留 Jev 的原始
候选输入和回答格式；它独立复用 `models.apis.k3.1-pro`，不再要求 TypeSafe 凭据。
编排与插件配置详见 [ORCHESTRATION.md](docs/ORCHESTRATION.md)。
可选的 [Fast Router](docs/FAST_ROUTER.md) 在共享预算内为同一目标选择和转派执行者。
`gui_jev` 使用的原生桌面工具源码随仓库保存在 `jev_tools/agent-desktop/`，
构建和部署方法见[使用仓库内的 agent-desktop](#使用仓库内的-agent-desktop)。

## 获取测评代码

这套测评工程和两份 `jev_tools/` 依赖位于 **`dev` 分支**。
当前 GitHub 仓库的默认分支是 `master`，复现时请明确选择 `dev`：

```bash
git clone --branch dev https://github.com/k4ngzy/JevDuo.git
cd JevDuo
git branch --show-current
```

最后一条命令应输出 `dev`。两份工具已通过 subtree 收录，clone 后即可获得源码。

## 环境准备

- 开发机需要已配置权限的 `rjob`、`uv >= 0.13.6`，以及集群可访问的共享盘。
  可用 `JEVDUO_UV=/path/to/uv` 指定 setup 使用的 uv。
- 检查 `config.yaml` 的 `cluster` 和 `benchmark`：镜像、挂载、prepared runtime、
  VM qcow2、assets、共享 `env.sh` 必须在实际 worker 中可用；GitLab token 文件由 driver
  读取并将值写入私有 spec，因此须在 driver 中可读。
- 当前开启 `gui_jev`，所需 Python 包已收录在 `jev_tools/jev-desktop/`，
  `setup_env.sh` 默认以 editable 方式安装；所有 worker 需要能读取同一仓库路径。
  它与 `jev_tools/agent-desktop/` 中的 Rust CLI 是两个独立组件。
  VM 镜像内还需安装下文构建的 CLI 和 Linux helper，并将配置指向客体中的 CLI 路径。

从仓库根目录安装。`setup_env.sh` 会重建 `venv/`，搬动源码后也需要重建：

```bash
bash scripts/setup_env.sh
venv/bin/python -c 'import northcua, nexau, yaml; from jev_desktop import AgentDesktop, JevLoop'
```

已有 venv 时，也可单独安装插件：

```bash
uv pip install --python venv/bin/python --no-deps -e jev_tools/jev-desktop
```

需要替换 Python 包来源时，可设置 `JEVDUO_JEV_PACKAGE=/absolute/shared/path/to/jev-desktop`
再运行 `setup_env.sh`；相对路径按仓库根目录解析。脚本会在重建 venv 前检查包的安装元数据
和入口源码，并在安装后验证 `AgentDesktop`、`JevLoop` 等导出，避免残留 editable 记录掩盖源码缺失。

## 使用仓库内的 agent-desktop

`jev_tools/agent-desktop/` 通过 **git subtree** 引入自
[`k4ngzy/agent-desktop` 的 `ubuntu2204` 分支](https://github.com/k4ngzy/agent-desktop/tree/ubuntu2204)，
导入基线为 `b7f968003746d90432de8d92b00692d068407419`。普通 clone 即包含源码，
无需初始化 submodule。复现实验时使用本仓库固定的版本；其中的 `Cargo.lock`、
`rust-toolchain.toml` 和 `LICENSE` 一并保留。

配套的 Python 运行库独立放在 `jev_tools/jev-desktop/`，同样以 subtree 收录。
Python 运行库已于 2026-11-19 更新为 CLI 轨迹适配后的版本，包含有条件的剪贴板整字段替换、
扩充的有限候选、显式文件参数绑定、动作后证据和完成核验。源码和测试均随仓库提供，
不依赖原来的外部 checkout；包元数据和许可证保留。
使用说明见 [Python 包 README](jev_tools/jev-desktop/README.md)，
接入边界见 [本次适配说明](docs/JEV_CLI_ADAPTATION.md)。

### 构建 Linux CLI 和 helper

下面以 OSWorld 的 Ubuntu 22.04、x86_64 客体为例。在仓库根目录执行，
通过 Docker 在 Ubuntu 22.04 中构建，避免开发机较新的 glibc 成为客体运行依赖。
构建机需要 Docker 及拉取镜像、Rust 工具链和 Cargo 依赖的网络权限：

```bash
docker run --rm --platform linux/amd64 \
  -v "$PWD/jev_tools/agent-desktop:/src" -w /src ubuntu:22.04 \
  bash -euo pipefail -c '
    export DEBIAN_FRONTEND=noninteractive
    apt-get update
    apt-get install -y --no-install-recommends \
      ca-certificates curl build-essential binutils git xz-utils
    curl --proto "=https" --tlsv1.2 -sSf https://sh.rustup.rs | \
      sh -s -- -y --profile minimal --default-toolchain 1.89.0
    . /root/.cargo/env
    cargo build --locked --release -p agent-desktop
    cargo build --locked --release -p agent-desktop-linux --bins
    for executable in agent-desktop agent-desktop-linux-clipboard-owner; do
      bash scripts/check-elf-glibc-floor.sh "target/release/$executable" 2.35
    done
    target/release/agent-desktop version
  '
```

两份产物位于宿主机的 `jev_tools/agent-desktop/target/release/`：
`agent-desktop` 和 `agent-desktop-linux-clipboard-owner`。Linux CLI 从自身所在目录查找
helper，部署时将同一次构建的两份文件放在一起。glibc 检查读取 ELF 符号版本，
用于确认构建产物要求的 glibc 版本不高于 2.35；这不等同于 GUI 功能已通过验证。

### 安装到 OSWorld VM

CLI 运行在 **VM 客体**中；只在开发机或 rjob 外层容器安装不能替代客体部署。
先启动准备环境用的 VM，从实际 provider/连接信息取得其 HTTP 服务地址和端口。
下面的上传命令在能够访问该服务的构建机或 worker 上、从本仓库根目录执行：

```bash
# 替换为实际 VM 服务地址；不是集群的 SSH 登录地址
JEVDUO_VM_SERVER='http://VM_IP:SERVER_PORT'
for executable in agent-desktop agent-desktop-linux-clipboard-owner; do
  curl --fail --show-error --noproxy '*' \
    -F "file_path=/tmp/$executable" \
    -F "file_data=@jev_tools/agent-desktop/target/release/$executable" \
    "$JEVDUO_VM_SERVER/setup/upload"
done
```

随后在 **VM 内的终端**安装两份文件及运行依赖：

```bash
sudo apt-get update
sudo apt-get install -y --no-install-recommends at-spi2-core dbus-x11 python3-gi
sudo install -d -m 0755 /opt/jev/agent-desktop/bin
sudo install -m 0755 \
  /tmp/agent-desktop /tmp/agent-desktop-linux-clipboard-owner \
  /opt/jev/agent-desktop/bin/
```

在 VM **桌面用户的现有图形会话终端**中检查 CLI 和可访问的应用：

```bash
JEVDUO_DESKTOP_BIN=/opt/jev/agent-desktop/bin/agent-desktop
"$JEVDUO_DESKTOP_BIN" version
"$JEVDUO_DESKTOP_BIN" status
"$JEVDUO_DESKTOP_BIN" list-apps
"$JEVDUO_DESKTOP_BIN" list-windows
# 将窗口 ID 换成 list-windows 实际返回的值
"$JEVDUO_DESKTOP_BIN" snapshot --window-id '<window-id>' --max-depth 3 -i
```

观察与动作需要同一桌面会话的 `DISPLAY`、session D-Bus 和运行目录；通过 SSH 或 HTTP
执行时也要继承该会话环境，不能凭空指定另一个 D-Bus 会话。OSWorld 的 headed 操作使用
X11；Chromium 启动参数需包含 `--force-renderer-accessibility`，当前 `gui_jev.launch_args`
已经为 Chrome 配置该参数。应用本身仍须暴露 AT-SPI 接口。

### 接入 JevDuo 测评

将以下字段合入现有 `config.yaml`，其余模型、编排预算和基准配置保留：

```yaml
harness:
  orchestration:
    gui_jev:
      enabled: true
      desktop:
        binary: /opt/jev/agent-desktop/bin/agent-desktop
        headed: true
benchmark:
  vm: /absolute/shared/path/to/unified-with-agent-desktop.qcow2
```

`desktop.binary` 是 **VM 内路径**，不是共享盘上的 `target/release/agent-desktop`。
测评会 reset VM，因此应将安装完成的环境保存为独立的 prepared qcow2，并把 `benchmark.vm`
指向它；只向某次运行中的 VM 上传文件不会更新基础镜像。`scripts/setup_env.sh` 只准备
agent 的 Python 环境，并默认安装 `jev_tools/jev-desktop/`；不会构建 Rust 工具或自动将文件安装进 VM。

### 更新此工具的 subtree

在工作区干净时，从仓库根目录执行：

```bash
git subtree pull --prefix=jev_tools/agent-desktop \
  https://github.com/k4ngzy/agent-desktop.git ubuntu2204 --squash
```

更新后重新构建 CLI/helper、准备对应 VM 镜像，并记录本次使用的仓库版本。
更多命令与平台限制见工具自身的[安装文档](jev_tools/agent-desktop/docs/install.md)和
[Linux 平台说明](jev_tools/agent-desktop/apps/docs/content/docs/platforms/linux.mdx)。

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
`TYPESAFE_API_KEY` 用于 Fast Router 和 TypeSafe 后端的 Jev 决策服务。
`run.sh` 自动加载 `.env.local`，driver 将支持的 TypeSafe 环境变量写入私有 spec，再注入 agent 进程。

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
提交失败不会自动重新提交；结果不明时保留记录供人工核对，不按推导名操作平台任务。
同一 run ID 可按保存的实际平台任务名接管等待；短暂状态查询失败继续等待，监控中不自动重启任务。
显式重跑已结束的题会分配新的 job 目录，保留旧轨迹与日志。

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

Desktop 唯一执行模式为 Slow 编排，`orchestration.enabled` 总开关已删除。
`harness.orchestration` 必须提供 `max_cycles`、`max_seconds`、`max_model_calls`、`max_tool_calls`；
`gui_jev.enabled`、`general.enabled` 只控制插件注册。多阶段任务共用整题预算，并要求完整 usage。

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
| `docs/ORCHESTRATION.md` | Slow、Jev、general 插件协议及行为说明 |
| `jev_tools/agent-desktop/` | `ubuntu2204` 分支的 Rust CLI/helper 源码，以 git subtree 管理；在 VM 内运行 |
| `jev_tools/jev-desktop/` | Jev Python 决策循环和 CLI 传输包，以 git subtree 管理；安装在 agent venv 中 |
| `tests/` | 本地逻辑与模拟传输回归，不启动真实 VM 或模型调用 |
| `runs/<run_id>/` | 全部产物（gitignore） |

## 三个解释器，别混

| 角色 | 解释器 | 说明 |
|---|---|---|
| 开发机 `pool` / `settings` | 仓库 `venv/bin/python` | 有 PyYAML，读 `config.yaml` |
| worker `entry` | 共享 `$OSWORLD_PYTHON` | 冻结 venv，**没有 PyYAML**，只读 JSON spec |
| agent 子进程 `task_run` | 仓库 `venv/bin/python` | 上游源码 + NexAU |

因此 `config.yaml` 只在开发机解析；worker 只接收 `settings.py` 渲染好的 `job_specs/*.json`。

## 补跑指定任务

补跑统一使用 `scripts/run.sh`。修改代码、模型或配置后，使用新的 run ID，并显式选择任务：

```bash
bash scripts/run.sh --run-id jv-rerun-001 --tasks 005,018 --jobs 1 --per-job 1
```

预算来自 `config.yaml`，支持普通入口的 `--config`、`--model`、`--max-steps` 和 `--max-wall`。
同一 run ID 下重跑未评分题使用 `--retry-failed`，不包含已评分的零分题。
任务直接使用当前共享工作区的 `src/`、`infra/` 和 `venv/`，不再复制源码或根目录说明文件；
运行期间应保持这些代码和环境不变。补跑会新建 VM 执行，不从旧轨迹或旧 VM 状态继续。

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
