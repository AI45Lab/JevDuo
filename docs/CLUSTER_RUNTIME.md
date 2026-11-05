# 内部集群运行手册

本文覆盖 **Tandem 在内部集群（adpc / evobox）上的运行方式**：配置、run 生命周期、
worker 入口、共享资源与已知坑。

产品定位与架构说明见根目录 [README.md](../README.md)；结果目录字段级定义见
[RESULT_DIR_CONTRACT.md](RESULT_DIR_CONTRACT.md)。

---

## 1. 适用范围与前提

| 组件 | 位置 | 职责 |
|---|---|---|
| `src/tandem/` | 仓库主体 | harness 本体，与集群无关（见 README） |
| `src/tandem_eval/` | 薄适配层 | worker 侧 6 文件（entry/env/runner/recorder/scoring/manifest）+ 开发机侧 `rjob.py` / `rjob_spec.py` / `rjob_status.py` / `config_schema.py` |
| `infra/config.json` | 唯一配置 | 全绝对路径、含密钥 → **已 gitignore**，模板 `infra/config.example.json` 提交 |
| `infra/worker.sh` | worker 入口 | rjob worker 内被调用，按 benchmark 分 mock / 真机两条路 |
| `scripts/` | 批量脚本 | `run_osw2_20x5.sh` 等批量运行 |

Harness 内部已按 `runtime` / `planning` / `gui` / `models` / `verification` / `protocol`
分包，见 [README](../README.md)。
这次目录整理不改变本手册中的启动命令、配置字段、worker 生命周期或结果目录。

原则（AGENTS.md 配置纪律）：

- **只有一个配置文件** `infra/config.json` —— 禁止环境档案、占位符展开、多配置文件；
- 所有值为**绝对路径**，且开发机与 worker 内**完全一致**（adpc 与 evobox 两卷挂载点两侧相同）；
- 凭据（LLM 三件套、docker registry auth）内联在 config 里，经 job spec 传入 worker，
  落盘时在 `code_version.json` 的 run_spec 副本中**脱敏为 `***`**；
- 共享资源**只读**，共享 venv **冻结**——不在其中安装/升级任何包。

## 2. 配置文件

`infra/config.json` 分三段：**harness**（大脑/小脑/路由/预算）、**eval**（集群与基准）、
**log**（结果落盘）。模板见 `infra/config.example.json`（真实文件含密钥，已 gitignore）。

### harness —— 快慢双模与运行旋钮

| 键 | 说明 |
|---|---|
| `harness.slow_model.default` | 默认慢模型（大脑）API 名 |
| `harness.slow_model.apis.<名字>` | 慢模型 API 条目：`base_url` / `api_key` / `model` + `max_tokens` / `timeout_seconds` / `reasoning_effort` / `direct_api`。当前登记 `minimax-m3.1-flash`（默认）与 `kimi-k3`（备选） |
| `harness.fast_model.default` | 默认快模型（小脑）API 名 |
| `harness.fast_model.apis.<名字>` | 快模型 API 条目：`backend`（`jev` / `kev`）+ `endpoint` / `endpoint_file` + `timeout_seconds` |
| `harness.fast_model.image_stage.write` / `.read` | 截图中转目录的**两侧视角**：写入侧在 adpc，读取侧在 evobox（服务端宿主机要能读到） |
| `harness.route.policy` / `.allow_cli` / `.cli_timeout_seconds` | 路由策略与 CLI 开关 |
| `harness.execution.mode` / `.stage_max_actions` | 可选：step（默认）或 stage；阶段内动作上限默认 24。见 [阶段执行说明](STAGE_EXECUTION.md) |
| `harness.budget.max_steps` / `.max_wall_seconds` | 单题预算默认值（命令行可覆盖） |

**慢模型与快模型都是命名 API 登记表**：登记多套大脑/小脑，启动命令用 `--slow-model` /
`--fast-model` 按名字挑（缺省取各自的 `default`），用于横向比较"不同大脑 × 不同小脑"的
性能差距。名字写错时错误信息会列出全部可选名。

### eval —— 集群与基准

| 键 | 说明 |
|---|---|
| `eval.repo_root` | 本仓库路径（开发机） |
| `eval.deploy_root` | rsync 部署副本路径（worker 读代码；两侧挂载点一致） |
| `eval.mounts` | rjob `--mount` 参数（gpfs 源 → 容器内挂载点）。**同一 source 不能挂两次**（平台报 `the value of source path repeats`），所以 evobox 卷只挂一次 |
| `eval.rjob` | `charged_group` / `private_machine` / `cpu` / `memory` / `gpu` / `image` |
| `eval.inner_image` | OSWorld VM 用的 inner 镜像（worker 内 dockerd 拉取） |
| `eval.benchmarks.osworld1.root` / `.vm` | OSWorld1 基准根与 qcow2 |
| `eval.benchmarks.osworld2.root` / `.vm` | OSWorld2 基准根与 qcow2 |
| `eval.benchmarks.osworld2.asset_root` | OSW2 任务资产根（`OSWORLD_FILE_BASE_URL` 指向它，避免外网下载） |
| `eval.shared_env` | 旧环境（OSW1/OSW2）的 `env.sh`（脚本自相对，两种视角都行） |
| `eval.benchmarks.osworld21.root` / `.vm` / `.asset_root` | OSW2.1 的 runtime（prepared 软链层）、qcow2 与资产根 |
| `eval.benchmarks.osworld21.shared_env` | OSW2.1 独立 `env.sh`（**不与旧环境 env.sh 混用**） |
| `eval.benchmarks.osworld21.inner_image` | OSW2.1 运行镜像，必须发布固定（`<repo>@sha256:…`，禁 `latest`）；留空时跑 osworld21 会明确报错 |
| `eval.benchmarks.osworld21.proxy` | OSW2.1 用的代理（覆盖 worker.sh 的默认代理） |
| `eval.benchmarks.osworld21.volume_size` | 传给官方接口的**卷参数**，不等于根盘容量（实测根盘约 49GB、可用约 21GB） |
| `eval.benchmarks.osworld21.no_proxy` | 可选，逗号分隔的直连API主机；只追加到OSW2.1 worker的代理绕过列表 |
| `eval.benchmarks.osworld21.gitlab.private_token` | 可选，内联GitLab token；非空时优先于 `private_token_file` |
| `eval.benchmarks.osworld21.gitlab.private_token_file` | 可选，共享测试token文件的绝对路径；提交任务时读取，经私有job spec传给setup/evaluator |
| `eval.benchmarks.osworld21.judge` / `.user_simulator` | 可选，各自配置 `provider` / `base_url` / `api_key` / `model`；judge还可指定 `reasoning_effort` / `max_tokens`，任务内显式参数优先 |
| `eval.registry` | docker registry auth（内联传给 worker 写 `~/.docker/config.json`） |

### log —— 结果落盘

| 键 | 说明 |
|---|---|
| `log.results_root` | 唯一落盘位置，run 目录 = `<results_root>/<run_id>/` |
| `log.record.a11y_full` | 是否落每步完整 a11y XML（默认只落摘要） |

缺项/非法值由 `tandem_eval.config_schema` 统一校验（一次列出全部问题）；
必填路径清单见 `config_schema.REQUIRED_PATHS`。

## 3. 一次 run 的生命周期

`tandem-eval rjob run` 一站式完成 **sync → spec → submit → watch → fetch → cleanup**
（`rjob.run_job`）：

```
sync     rsync -a --delete 仓库 → deploy_root
         排除 .git/ .venv/ __pycache__/ .pytest_cache/ .ruff_cache/ rjob_logs/ job_specs/
         （job_specs 排除是必须的：否则并发 run 会删掉在跑任务的 spec）
spec     组装 job spec → deploy_root/job_specs/<run_id>.json
         spec 段：env（基准根 / VM / 资产 / 启动等待）/ secrets（slow_model 凭据 /
         fast_model 地址 / registry）/ image_stage（截图中转目录）/ harness（含本次用的
         models：慢模型名 + 快模型名）/ harness_config（行为旋钮）/ budget / record /
         code（git describe + 内外镜像）
submit   rjob submit（先 delete 同名残留做防御性清理）
watch    轮询至终态（--poll 间隔，--timeout 上限）
fetch    取平台日志补写进同一 run 目录（尽力而为，失败不阻断 cleanup）
cleanup  stop（释放资源）+ delete（永久删除 rjob 记录）——默认行为
```

### 收尾默认删 rjob

**跑完就删，不在集群上留残留。** `submit` 之后的任何结局都会走到收尾：

| 结局 | status | 收尾 |
|---|---|---|
| 正常完成 | `succeeded` / `failed` | stop + delete |
| watch 超时 | `timeout` | stop + delete |
| 平台侧报错（`rjob get` 失败等） | `error` | stop + delete |
| submit 失败 | `error` | stop + delete（不留半成品） |
| 中途 Ctrl-C | — | stop + delete（`finally` 保证） |

细节：

- **顺序固定**：先 `fetch` 取平台日志，**再** delete —— 日志取完才敢删；
- **delete 带重试**：`rjob stop` 是异步的，刚 stop 完立刻 delete 可能被平台以
  「仍在终止中」拒绝，故 delete 重试 3 次（间隔 5s）；
- **删不掉不抛异常**：收尾失败不该盖掉真正的运行结果，状态如实回报在
  `result["cleanup"] = {"stopped": bool, "deleted": bool}`；
- **要保留现场**用 `--keep`：不 stop 不 delete，事后可查 `rjob logs` / `rjob get`。
  注意保留的 job 会继续占集群配额，用完记得自己 `rjob delete`。

常用参数：

| 参数 | 说明 |
|---|---|
| `--run-id` | 必填。run 目录名 = `rjob_logs/<run_id>/` |
| `--benchmark` | `mock` / `osworld1` / `osworld2` / `osworld21` |
| `--tasks` | 逗号分隔 task id（覆盖 manifest） |
| `--limit N` | 取清单前 N 题 |
| `--shard I/N` | 把（limit 后的）清单**交错**切成 N 片，本 run 跑第 I 片（并发用） |
| `--slow-model NAME` | 慢模型（大脑）API 名，取自 `harness.slow_model.apis`；缺省用 `default` |
| `--fast-model NAME` | 快模型（小脑）API 名，取自 `harness.fast_model.apis`；缺省用 `default` |
| `--route-policy` / `--allow-cli` | 路由策略与 CLI 开关 |
| `--execution-mode` / `--stage-max-actions` | step/stage 模式与阶段动作上限 |
| `--max-steps` / `--max-wall` | 单题预算（步数 / 墙钟秒） |
| `--slow-reasoning` | `off` / `low`（默认）/ `medium` / `high` |
| `--slow-max-tokens` | 慢模型单次响应上限 |
| `--export-paths` | 逗号分隔的 VM 内交付物路径（finish 时导出） |
| `--poll` / `--timeout` | 轮询间隔与总超时 |
| `--keep` | 结束后**保留** rjob（不 stop 不 delete），供事后排查；默认跑完即删 |
| `--delete` | 已默认开启，保留此参数仅为兼容旧脚本 |
| `--no-sync` | 跳过 rsync（复用已有部署副本） |

单独盯一个已提交的 job：

```bash
tandem-eval rjob watch tandem-<run-id> --run-id <run-id> --config infra/config.json
```

## 4. worker 侧入口

`infra/worker.sh <job spec 绝对路径>` 由 rjob worker 调用：

- **mock 基准**：直接用系统 `python3` 跑 `tandem_eval entry`，不碰共享资源与 docker；
- **真实基准**：
  1. source 共享 `env.sh`（`OSWORLD_PYTHON` / `EVAL_TOOLS` / `LD_LIBRARY_PATH` 等）；
  2. 把 config 里的 registry 凭据写入 `~/.docker/config.json`（`chmod 600`）；
  3. 覆盖平台预注入的代理（真机上 `httpproxy-headless-dev-test` 不可解析），
     固定为 `httpproxy-headless.kubebrain.svc.pjlab.local:3128`，并设置 `NO_PROXY`；
  4. 起 `dockerd`（独立 data-root/exec-root，指向该代理），拉 `inner_image`，
     打 tag 为 `happysixd/osworld-docker:latest`；
  5. 用 `$OSWORLD_PYTHON` 跑 `tandem_eval entry`（Pillow / easyocr 已在冻结 venv 内），
     跑前先做一次依赖自检。

worker 侧 `tandem_eval entry --spec <spec>` 是实际执行体：起 VM → 跑任务 → 录事件 → 评分 →
写 run 目录。环境生命周期、预算强制、落盘都在这一层，harness 只管决策。

## 5. 共享资源（只读）

路径：`/mnt/shared-storage-user/evobox-share-gpfs2/xuyuanxun/shared_resources/sa_cua`

| 内容 | 位置 |
|---|---|
| `env.sh` | 共享环境变量（`OSWORLD_PYTHON` / `EVAL_TOOLS` / `LD_LIBRARY_PATH`） |
| 基准 | `benchmarks/osworld1`、`benchmarks/osworld2` |
| 任务索引 | `benchmarks/osworld2/evaluation_examples/test_v2.json`（形状 `{"tasks": [...]}`） |
| 任务类 | `benchmarks/osworld2/evaluation_examples/task_class/task_NNN.py` |
| VM 镜像 | `vm/osworld1/Ubuntu.qcow2`、`vm/osworld2/osworld-v2-ubuntu-x86.qcow2` |
| 排序服务活地址 | `services/qwen_jev.json`、`services/kev.json` |

纪律：

- 共享 venv **冻结**——不装包、不升级；本仓库代码经 rsync 部署副本 + `PYTHONPATH` 挂载即用
  （纯 Python、零新增依赖、Python 3.12 兼容）；
- OSW2 任务资产经 `OSWORLD_FILE_BASE_URL` 指向本地 `asset_root`，**必须在任务模块 import 前设置**
  （见 `osworld.py` 的 `_bootstrap_runtime`），否则资产 URL 会在 import 期被固化；
- 每个 run 的 `code_version.json` 记录 git describe、protocol_version、镜像 digest、run spec（secrets 脱敏）。

## 6. 排序服务地址发现

JEV / Kev 部署在闲时 GPU，**地址随重新部署变化**。因此：

- 配置里快模型条目的 `endpoint` **留空** = worker 侧建 session 时从同条目的
  `endpoint_file`（`services/qwen_jev.json` / `services/kev.json`）读取最新地址；
- 发现文件带 `ready` / `status` 就绪位检查，并有双视角符号链接兜底；
- 固定地址时直接写配置值，不走发现。

JEV 的 `/rank` 要求 `image_path` 是**服务宿主机可读的文件路径**。截图本体在 adpc，
因此由 harness 暂存到 evobox 中转目录（`harness.fast_model.image_stage.write` → `.read`），
payload 带排序服务宿主机视角路径。暂存按内容哈希命名去重（`step_00_<sha16>.png`），断点无害。

## 6.5 OSWorld 2.1 的特别约定

OSW2.1 走**独立环境**（独立 runtime / VM / 资产 / venv / env.sh），与 OSW1、OSW2 不共用：

- **共享资源**：`<sa_cua>/osworld21/`（`README_CN.md` 是环境侧的权威说明）。
- **挂载视角只有一个**：平台不允许同一 source 挂两次，而 OSW2.1 的 `env.sh` 与
  `prepared/runtime/` 软链层写的是 **canonical 路径** `/mnt/shared-storage-gpfs2/...`，
  所以 evobox 卷在 worker 里统一挂 canonical 视角，配置里所有 evobox 路径也随之统一
  （旧环境的 `env.sh` 是自相对的，两种视角都行，不受影响）。
  - worker：`gpfs://gpfs2/evobox-share-gpfs2` → `/mnt/shared-storage-gpfs2/evobox-share-gpfs2`
  - 开发机：默认只有 user 视角 `/mnt/shared-storage-user/evobox-share-gpfs2`，
    想在开发机直接读配置里的路径，建一个软链即可（只影响开发机）：
    `ln -s /mnt/shared-storage-user/evobox-share-gpfs2 /mnt/shared-storage-gpfs2/evobox-share-gpfs2`
- **env.sh**：worker 用 `eval.benchmarks.osworld21.shared_env` 指定的独立 env.sh
  （它再 source canonical 的 `osworld21/env.sh`），**不与旧环境 env.sh 混用**。
- **镜像**：直接拉发布清单固定的运行镜像（digest），**不覆盖 `latest`**，
  也不复用 `eval.inner_image`；OSW2.1 的 runtime 自己按固定版本引用。
  固定版本见 `osworld21/runtime/benchmark_releases/osworld-v2.1.json` 与
  `desktop_env/providers/docker/provider.py`。
- **依赖自检**：OSW2.1 的独立 venv 按官方 `uv.lock` 安装，**没有 easyocr**，
  worker 只检查 Pillow；旧环境仍检查 `easyocr + PIL`。
- **题目清单**：`tandem_eval/manifest.py` 的 `OSWORLD21_TASK_IDS` 覆盖官方001–108题。
  清单不等于环境验收报告；API、VM或评分异常仍记为 `failed_unscored`。
- **共享环境**：通用VM为 `vm/unified-20261022-complete.qcow2`，经 `prepared/runtime`
  接入环境准备适配。依赖缓存、网站DNS/证书和控制器传输适配在共享环境侧维护，
  不改变Tandem规划、候选、验证或评分逻辑。worker只读基盘与冻结venv，每题使用隔离VM。
- **网站与权限**：从环境侧 `services/web/endpoint.json` 与 `services/gitlab/endpoint.json`
  发现内网地址，不依赖开发机的localhost。使用者需有共享盘、内网和镜像仓库权限；
  026、041通过配置模板中的 `gitlab.private_token_file` 读取共享测试管理员token。
  该文件位于共享环境 `shared_credentials/gitlab-test-admin.token`，按所有者授权以644权限供集群用户读取；
  仅用于这套独立测试GitLab，不包含GitHub、模型API或镜像仓库凭据。token轮换时由环境维护者更新该文件。
  模型API及其他凭据仍使用各自获授权的配置，置于已gitignore的 `infra/config.json`；
  传给worker的job spec保持600权限，所有token均不提交Git或写入公开结果。
  替代judge/模拟器的身份另记于 `code_version.json.resources`，不混作原生GPT配置。
- **卷参数**：`volume_size` 只是传给官方接口的卷参数，**不代表根盘真有这么大**
  （实测根盘约 49GB、可用约 21GB）。

## 7. 批量运行

```bash
bash scripts/run_osw2_20x5.sh    # 20 题、5 并发（切片 → 提交 → 等收敛 → 汇总）
```

脚本把任务清单交错切成 5 片，每片一个 rjob。并发注意：部署副本 rsync 用 `--delete`，
`job_specs/` 已排除在同步之外——否则并发 run 会删掉彼此在跑的 spec。

## 8. 已知坑

| 现象 | 根因 | 处理 |
|---|---|---|
| **大批量任务在 `fetch_task` 阶段即死**：wall=0.001s、0 次 LLM 调用、0 帧、异常 `ValueError` | `desktop_env/controllers/website.py` 顶层 `raise ValueError("WEBSITE_HOST_SUFFIX must be set in environment variables")`。import 该模块的 9 个任务全部在 import 期死，其余 11 个全过——**完美相关** | 在 worker 环境注入 `WEBSITE_HOST_SUFFIX`，或提供 `.env`。**不是模型能力问题**，但会让准确率被严重低估 |
| episode 只有 `exception_type`，没有异常消息 | `recorder.py` 只记异常类型不记正文 | 诊断缺口；已知问题，暂未修 |
| OSW2 索引里的 domain 被标成 `tasks` | `test_v2.json` 形状是 `{"tasks": ["001", ...]}`（顶层只有 `tasks` 一个键，值是 task id 列表），`load_index` 把顶层键当 domain | 已知问题，暂未修 |
| 3 题 `stop_reason=done` 却 0 分 | verifier 的完成判定偏乐观 | 与 harness 能力分开评估，别当成"模型没做对" |
| 输入 token 是输出的 15–25 倍 | a11y 树整段进 prompt | 成本优化的主要空间 |

## 9. 排障速查

| 想看 | 去哪 |
|---|---|
| 这次 run 整体结果 | `rjob_logs/<run_id>/summary.json` |
| 进度 / 卡在哪一题 | `rjob_logs/<run_id>/heartbeat.json` |
| worker 侧日志（LLM 审计 + 事件流） | `rjob_logs/<run_id>/worker/entry.log` |
| 平台日志 | `rjob_logs/<run_id>/platform.tail.log`、`platform.status.txt` |
| 某题的完整决策链 | `rjob_logs/<run_id>/episodes/<task_id>/events.jsonl` |
| 某题的全部 LLM 请求/响应 | `.../llm_calls.jsonl` |
| 这次到底跑了什么版本 | `rjob_logs/<run_id>/code_version.json`（git describe + 镜像 digest + run spec） |
| 想看轨迹回放 | `python demo/build_site.py --runs-root rjob_logs --out demo/site --benchmark osworld2` |

worker 侧各段日志：

| 文件 | 内容 |
|---|---|
| `worker/entry.log` | entry 主日志 |
| `worker/dockerd.log` / `docker-info.txt` / `docker-pull.log` | 容器运行时准备 |
| `worker/deps-check.log` | 冻结 venv 依赖自检（easyocr / PIL） |

## 10. 开发闭环

```
改代码 → ruff check src tests demo → pytest →（若触碰协议）tandem check → 提交
```

真机 smoke：

- `--benchmark mock` 走 mock 链路（不起 VM、不碰共享资源），可本地跑；
- 真实基准走 OSW1 / OSW2 / OSW2.1，按本文 §3–§7。

## 11. 浏览器 CDP 语义快照

- `tandem_eval.browser` 在 VM 内通过 loopback 依次探测 `1337` / `9222`，只调用
  `GET /json/list` 与 `Runtime.evaluate`，不重启 Chrome、不激活 tab、不执行页面 mutation。
  多 tab 时选择 `document.visibilityState=visible` 的页面。
- 任务必须已由环境以远程调试参数启动 Chrome；Task 008 的 setup 已显式启用 `1337`。
  其他任务未启用、端口被拒绝或快照异常时只记录诊断并回退 a11y，不让整题失败。
- 快照经协议 `browser_snapshot` 文件引用进入 harness；episode 正本保存在
  `episodes/<task>/browser/step_XX.json`。密码、隐藏和文件输入元素不进入快照。
- 2026-10-29 Task 008 真机 probe 已验证 `1337`、WebSocket 和 DOM 序列化可用，
  无需额外权限。首次有效快照含 4 个元素；9222 被其他环境组件占用时不影响 1337 采集。

## 12. JEV 断线恢复

- 排序请求发生连接超时、瞬态 HTTP 408/429/500/502/503/504 或响应 JSON 错误时，
  在 1200 秒总窗口内重试；非瞬态 HTTP 错误立即失败，不盲目重试。
- 健康检查超时为 30 秒；`Retry-After` 优先于默认 15 秒等待。恢复事件记录
  `attempt`、`endpoint`、`error_type`、`remaining_seconds` 和 `retry_delay_seconds`。
- worker 建 session 时注入 endpoint provider。恢复过程中会重新读取 `endpoint_file`，
  JEV 重新部署导致地址变化时记录 `fast_model_endpoint_changed` 并切换地址。
  发现文件未 ready 时保留当前地址继续有界恢复，不会直接放弃整题。

## 13. 命名实验

统一使用仓库内 `scripts/run_experiment.sh`，不要直接散落调用 `tandem-eval rjob run`。
每次实验必须指定唯一名称（小写字母、数字、连字符），所有结果归档到
`rjob_logs/<experiment>/`：

- `manifest.json`：tasks、并发、模型、预算、watch timeout、git describe；
- `batch.log`：批次输出；
- `drivers/<task>.driver.log`：单个 rjob 生命周期日志；
- `<experiment>-<task>/`：worker、模型调用、轨迹、截图和评分结果。

脚本只同步一次代码，然后按指定并发提交。worker 的 adpc 挂载只读时，结果先落
evobox 暂存，单题完成后立即 rsync 到实验目录。`--dry-run` 只生成 manifest 和 batch log，
不提交 rjob。
