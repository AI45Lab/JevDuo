# JevDuo · 集群测评（master 分支）

在 OSWorld 2.1 上用上游 CUA harness 跑 agent 测评，通过内部集群的 `rjob` 提交任务。
本分支是**测评工程分支**，与 `main`（JevDuo harness 本体）相互独立，不 merge、不 rebase。

被测 agent 来自 `src/upstream/`（以 **git subtree** 方式入库的上游精简树，见
[上游源码](#上游源码subtree)），本工程只负责：
切题 → 渲染 job spec → 并发提交 rjob → 起 VM → 跑 agent → 官方评分 → 落盘汇总。

## 快速开始

```bash
# 1. 建 venv（解释器装在共享盘，worker 内可见）
bash scripts/setup_env.sh

# 3. 填密钥（不进仓库，权限 600）
cat > .env.local <<'EOF'
export JEVDUO_KIMI_API_KEY='...'
export JEVDUO_MINIMAX_API_KEY='...'
export JEVDUO_REGISTRY_AUTH='base64(user:pass)'
EOF
chmod 600 .env.local

# 4. 跑单题冒烟
bash scripts/run.sh --run-id jv-smoke-001 --tasks 001 --jobs 1 --per-job 1

# 5. 跑分片 / 全量
bash scripts/run.sh --run-id jv-osw21-a --shard 1/6 --jobs 6 --per-job 18
```

`run.sh` 是唯一入口，会自动加载 `.env.local`。

## 目录

| 路径 | 内容 |
|---|---|
| `config.yaml` | **唯一配置**：模型、harness 行为、并发、集群、基准、日志；不含密钥 |
| `scripts/run.sh` | 唯一运行入口（开发机） |
| `scripts/setup_env.sh` | 重建 agent venv（解释器装共享盘 + 路径断言） |
| `infra/worker.sh` | rjob worker 入口：共享 env、dockerd、拉镜像、进 `entry` |
| `src/settings.py` | 读 config.yaml、切题、渲染 job spec（开发机侧，需要 PyYAML） |
| `src/pool.py` | 任务池：切片、并发 rjob、watch、收日志、合并、重试 |
| `src/rjob.py` | rjob CLI 封装与状态解析 |
| `src/entry.py` | worker 主循环（冻结解释器，只读 JSON spec） |
| `src/vm.py` | OSWorld VM 生命周期：fetch_task / reset / execute / score / close |
| `src/launch.py` | 起 agent 子进程、超时杀进程组、终态映射 |
| `src/appcfg.py` | 生成 app.yaml / agent.yaml（指向 VM 的 osworld 后端） |
| `src/task_run.py` | 单题 agent 运行器（在 agent venv 内跑） |
| `src/recorder.py` | 结果目录契约（轨迹摘要、截图、汇总） |
| `src/scoring.py` | 官方分数归一化与 `failed_unscored` 判定 |
| `tests/test_pool.py` | 纯逻辑单测：切片、切题、spec 渲染、状态解析 |
| `runs/<run_id>/` | 全部产物（gitignore） |

## 三个解释器，别混

| 角色 | 解释器 | 说明 |
|---|---|---|
| 开发机 `pool` / `settings` | 仓库 `venv/bin/python` | 有 PyYAML，读 `config.yaml` |
| worker `entry` | 共享 `$OSWORLD_PYTHON` | 冻结 venv，**没有 PyYAML**，只读 JSON spec |
| agent 子进程 `task_run` | 仓库 `venv/bin/python` | 上游源码 + NexAU |

因此 `config.yaml` 只在开发机解析；worker 只接收 `settings.py` 渲染好的 `job_specs/*.json`。

## 配置要点

- `models.apis`：命名 API 登记表，`--model` 按名字选，缺省取 `models.default`。
  当前登记 `k3.1-pro` 与 `MiniMax-M3.1-Flash`，均走内部网关。
- `direct_api: true`：起 agent 子进程时剔除所有 `*_proxy`，等价于 `curl --noproxy '*'`。
- `harness.*`：步数/墙钟预算、坐标空间、桌面超时、prompt 窗口、评估器开关（默认关闭，
  分数只来自 OSWorld 官方评分器）。
- `pool.max_parallel` / `pool.tasks_per_job`：rjob 级并发；worker 内串行（一题一个 VM）。
- `cluster.*`：cpu/memory/gpu/镜像/mount/registry 凭据环境变量名/轮询间隔。
- `benchmark.*`：OSWorld 2.1 的 task_root、VM qcow2、assets、inner 镜像 digest、代理。

## 并发模型

只在 rjob 级并发，每题独占一个 VM：

```
max_parallel=6, tasks_per_job=18  →  6 个 rjob 并发，各 18 题（108 题）
```

每 job 4C/16G；提交失败指数退避。`failed_unscored` 不会自动重排队，
需要重跑时用 `--retry-failed` 续跑（只重跑上次未评分的题）。
每个 job 独立写 `jobs/job-NN/summary.json`，最后一并合成为 `runs/<run_id>/summary.json`。

## 产物

```
runs/<run_id>/
├── run.json / submit.log / summary.json / summary.csv
├── job_specs/job-NN.json           # 600；含 key，跑完可删
├── jobs/job-NN/
│   ├── job.json / status.txt / tail.log / heartbeat.json / summary.json
│   ├── worker/{entry,dockerd,docker-pull,deps-check}.log
│   └── episodes/<task>/
│       ├── task.json / episode.json / evaluation.json
│       ├── traj.jsonl              # 步骤摘要（动作、回执、窗口、截图引用）
│       ├── llm_calls.jsonl         # 相对软链 → agent/<session>/llm_trace.jsonl
│       ├── screenshots/step_NN.png
│       ├── agent/                  # 上游原始产物（steps.jsonl / llm_trace / runtime）
│       ├── config/{app.yaml,agent.yaml}
│       └── agent_result.json / agent_stdout.log
```

`summary.csv` 字段：`task_id,status,score,steps,seconds,stop_reason,model`。

## 冒烟基线（2026-11-08）

题 001，`k3.1-pro`，100 步上限：

```
[job-00] succeeded
{"total": 1, "scored": 1, "failed_unscored": 0, "score_mean": 0.3333}
```

同轮验证到：镜像拉取、dockerd、VM setup、agent 子进程、官方评分、产物落盘全链路。

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
git clone --shared <上游仓库> /tmp/ncua-trim
cd /tmp/ncua-trim && git checkout -b trimmed <新 commit>
git rm -r --cached docs tests examples .github
git commit -m "trim upstream"

# 2. 回到本仓库拉进子树
cd <JevDuo>
git subtree pull --prefix=src/upstream /tmp/ncua-trim trimmed --squash
```

升级后若 `uv.lock` 变化，重跑 `bash scripts/setup_env.sh`。

## 已知坑

- **PyYAML 只在开发机**：worker 侧不要 import `settings`。
- **venv 解释器必须在共享盘**：`setup_env.sh` 会断言 `venv/bin/python` 的解引用路径位于仓库内，
  否则 worker 内会 `No such file or directory`。
- **registry 凭据**：`worker.sh` 直接读 spec 里 `cluster.registry_auth[host].auth`（base64 或裸
  `user:pass`，后者由 `settings.py` 自动编码），写入 `~/.docker/config.json`。
- **集群容量**：CPU 紧张时 rjob 会长时间排队（事件里是 `Insufficient cpu`），不是代码问题。
- **步数上限**：`harness.max_steps` 耗尽时 agent 记 `max_steps`，仍按官方评分器打分为 `scored`。

## 分支约定

`master` 只保留测评工程（本文件所述范围）。harness 本体、demo、docs 等属于 `main`，不从
`main` 合并，也不向 `main` 回推。
