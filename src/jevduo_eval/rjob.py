"""rjob 生命周期客户端（开发机侧编排；worker 侧见 entry.py）。

覆盖：sync_repo（rsync 干净源码到部署副本）→ build_job_spec（写入部署副本）→ submit
（--mount 挂载共享存储）→ watch（轮询 rjob get 至终态，超时 watchdog）→ fetch（平台日志
+ 结果目录归档到开发机）→ cleanup（stop 释放资源 + delete 永久清理）。

**收尾默认删 rjob**：无论 job 成功、失败、超时还是中途抛异常，跑完都不在集群上留残留。
要保留现场（事后查 `rjob logs` / `rjob get`）用 `--keep`。

双视角路径（与旧仓库 infra/envs 同构）：dev 档案是开发机挂载视角（rsync/取结果用），
rjob 档案是 worker 容器视角（submit 命令行与 job spec 内路径用），同一存储两个挂载点。
"""

from __future__ import annotations

import subprocess
import time
from pathlib import Path

# 以下仍从本模块导出（对外兼容旧导入路径）
from .config_schema import ConfigError, load_config  # noqa: F401  （对外兼容旧导入路径）
from .rjob_spec import build_job_spec, build_submit_args, job_name  # noqa: F401
from .rjob_status import TERMINAL_STATES, parse_status  # noqa: F401

# job_specs/ 必须排除：--delete 会把"目标多出而源没有"的文件删掉，而每个 run 的 spec
# 写在部署副本里 —— 不排除的话，后一个 run 的同步会删掉前一个还在排队/运行的 spec
RSYNC_EXCLUDES = [".git/", ".venv/", "__pycache__/", ".pytest_cache/", ".ruff_cache/",
                  "rjob_logs/", "job_specs/"]
#: rjob stop 是异步的：刚 stop 完立刻 delete 可能因"仍在终止中"被平台拒绝，故 delete 带重试
DELETE_RETRIES = 3
DELETE_RETRY_SECONDS = 5.0


class RjobError(RuntimeError):
    pass


class RjobClient:
    """rjob CLI 薄封装；command_runner 可注入（测试），默认 subprocess。"""

    def __init__(self, command_runner=None):
        self._run = command_runner or _subprocess_run

    def _rjob(self, *args: str) -> str:
        code, stdout = self._run(["rjob", *args])
        if code != 0:
            raise RjobError(f"rjob {' '.join(args)} 退出码 {code}:\n{stdout[-2000:]}")
        return stdout

    def submit(self, build_args: list[str]) -> str:
        return self._rjob("submit", *build_args)

    def get(self, name: str) -> str:
        return self._rjob("get", name)

    def logs(self, name: str, *, tail_lines: int = 500) -> str:
        return self._rjob("logs", "job", name, "-n", str(tail_lines))

    def stop(self, name: str) -> str:
        return self._rjob("stop", name)

    def delete(self, name: str) -> str:
        return self._rjob("delete", name)


def _subprocess_run(argv: list[str]) -> tuple[int, str]:
    import os

    env = {key: value for key, value in os.environ.items() if key != "KUBEBRAIN_QUOTA_GROUP"}
    result = subprocess.run(argv, capture_output=True, text=True, env=env, timeout=600)
    return result.returncode, (result.stdout + result.stderr)


def sync_repo(repo_root: Path, deploy_root_dev: Path) -> None:
    deploy_root_dev.mkdir(parents=True, exist_ok=True)
    argv = ["rsync", "-a", "--delete"]
    for exclude in RSYNC_EXCLUDES:
        argv += ["--exclude", exclude]
    argv += [str(repo_root) + "/", str(deploy_root_dev) + "/"]
    code, stdout = _subprocess_run(argv)
    if code != 0:
        raise RjobError(f"rsync 失败:\n{stdout[-2000:]}")




def watch(client: RjobClient, name: str, *, poll_seconds: float = 60,
          timeout_seconds: float | None = None, sleep=time.sleep) -> str:
    """轮询至终态；超时抛 TimeoutError（调用方决定是否 stop 收尾）。"""
    started = time.monotonic()
    while True:
        status = parse_status(client.get(name))
        if status in TERMINAL_STATES:
            return status
        if timeout_seconds is not None and time.monotonic() - started >= timeout_seconds:
            raise TimeoutError(f"rjob {name} 超过 {timeout_seconds}s 仍未终态（当前 {status}）")
        sleep(poll_seconds)


def fetch_artifacts(client: RjobClient, name: str, run_root: Path,
                    *, tail_lines: int = 500) -> Path:
    """把平台状态/日志写进 run 目录本身（唯一落盘位置 = rjob_logs/<run_id>）。

    平台命令失败不致命（rjob logs 在日志未聚合时 CLI 自身会崩，见 DEVIATIONS #19）：
    错误文本写入对应文件后继续。
    """
    run_root.mkdir(parents=True, exist_ok=True)
    try:
        (run_root / "platform.status.txt").write_text(client.get(name), encoding="utf-8")
    except RjobError as error:
        (run_root / "platform.status.txt").write_text(f"[rjob get 失败]\n{error}", encoding="utf-8")
    try:
        (run_root / "platform.tail.log").write_text(
            client.logs(name, tail_lines=tail_lines), encoding="utf-8"
        )
    except RjobError as error:
        (run_root / "platform.tail.log").write_text(f"[rjob logs 失败，日志可能未聚合]\n{error}",
                                                    encoding="utf-8")
    return run_root


def cleanup(client: RjobClient, name: str, *, delete: bool = True,
            retries: int = DELETE_RETRIES, sleep=time.sleep) -> dict:
    """任务结束后收尾：stop 释放资源（幂等）→ delete 永久清理。

    **默认删除 rjob**——跑完（无论成功、失败、超时、异常）就不在集群上留残留。
    stop 是异步的：刚 stop 完立刻 delete 可能被平台以"仍在终止中"拒绝，
    故 delete 带重试；仍失败则如实回报 deleted=False，不掩盖主流程结果。

    任一步失败都被吞掉（收尾失败不该盖掉真正的运行结果），但状态如实返回：
    `{"stopped": bool, "deleted": bool}`。
    """
    stopped = False
    try:
        client.stop(name)
        stopped = True
    except RjobError:
        pass  # 已终态时 stop 可能报错，忽略
    if not delete:
        return {"stopped": stopped, "deleted": False}
    deleted = False
    for attempt in range(max(1, retries)):
        try:
            client.delete(name)
            deleted = True
            break
        except RjobError:
            if attempt < retries - 1:
                sleep(DELETE_RETRY_SECONDS)
    return {"stopped": stopped, "deleted": deleted}


def run_job(*, config: dict, repo_root: Path, run_id: str,
            spec_overrides: dict | None = None, client: RjobClient | None = None,
            poll_seconds: float = 60, timeout_seconds: float | None = None,
            keep: bool = False, delete: bool = True, sleep=time.sleep,
            sync: bool = True) -> dict:
    """一站式：sync → spec → submit → watch → fetch → cleanup。

    唯一落盘位置 = log.results_root/<run_id>（= jevduo/rjob_logs/<run_id>）：
    worker 直接写 run 目录（adpc 挂载），平台日志由 fetch 补写进同一目录。

    **收尾默认删 rjob**（delete=True）：submit 之后的任何结局——成功、失败、
    超时、平台报错、中途 Ctrl-C——都会走到 cleanup 把 rjob 从集群上清掉。
    keep=True 则原样保留现场（不 stop 不 delete），供事后查状态/日志。
    """
    spec_overrides = spec_overrides or {}
    client = client or RjobClient()
    if sync:
        sync_repo(repo_root, Path(config["eval"]["deploy_root"]))
    build_job_spec(config=config, run_id=run_id,
                   repo_root=repo_root, spec_overrides=spec_overrides)
    name = job_name(run_id)
    try:
        client.delete(name)  # 防御性清理同名残留（旧脚本同款；不存在时忽略）
    except RjobError:
        pass
    result = {"job": name, "run_id": run_id}
    try:
        # submit 放进 try：提交之后任何异常（含 KeyboardInterrupt）都必须落到 finally
        # 走 cleanup，否则 rjob 会残留在集群上继续占配额。
        client.submit(build_submit_args(config=config, run_id=run_id))
        result["status"] = watch(client, name, poll_seconds=poll_seconds,
                                 timeout_seconds=timeout_seconds, sleep=sleep)
    except TimeoutError as error:
        result["status"], result["error"] = "timeout", str(error)
    except RjobError as error:
        # 平台侧异常（如 rjob get 失败）不该让整轮白跑：记为 error，产物照常归档
        result["status"], result["error"] = "error", str(error)
    finally:
        # fetch 失败不得阻断 cleanup（否则资源泄漏）；错误记入 result 继续
        run_root = Path(config["log"]["results_root"]) / run_id
        result["run_root"] = str(run_root)
        try:
            fetch_artifacts(client, name, run_root)
        except Exception as error:  # 平台日志补写尽力而为：run 数据正本已在该目录
            result["fetch_error"] = f"{type(error).__name__}: {error}"
        if keep:
            result["cleanup"] = {"stopped": False, "deleted": False, "kept": True}
        else:
            # 复用注入的 sleep（生产是 time.sleep，单测是 no-op），delete 重试不额外等
            result["cleanup"] = cleanup(client, name, delete=delete, sleep=sleep)
    return result
