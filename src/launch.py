"""启动 agent 子进程，把它的终态映射成 OSWorld 动作。

子进程用仓库 venv（上游源码 + NexAU），父进程用冻结的 OSWORLD_PYTHON；
两者只通过 VM 的 HTTP 端口与 result.json 交互。
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
AGENT_PYTHON = ROOT / "venv" / "bin" / "python"
TASK_RUNNER = ROOT / "src" / "task_run.py"
API_KEY_ENV = "JEVDUO_API_KEY"
_DROP_ENV = {"PYTHONPATH", "PYTHONHOME", "LD_LIBRARY_PATH", "VIRTUAL_ENV"}


def agent_env(api_key: str, *, direct_api: bool) -> dict[str, str]:
    """agent 子进程环境：去掉共享 venv 的干扰变量；direct_api 时再去掉全部代理变量。"""
    env = {
        key: value for key, value in os.environ.items()
        if key not in _DROP_ENV and (not direct_api or not key.lower().endswith("_proxy"))
    }
    env["PATH"] = str(AGENT_PYTHON.parent) + os.pathsep + env.get("PATH", "")
    env[API_KEY_ENV] = api_key
    env["PYTHONUNBUFFERED"] = "1"
    env["PYTHONPATH"] = os.pathsep.join((str(ROOT / "src"), str(ROOT / "src/upstream")))
    return env


def run_agent(*, query: str, session_id: str, app_config: Path, log_dir: Path,
              result_path: Path, stdout_path: Path, max_steps: int, timeout_seconds: float,
              api_key: str, direct_api: bool, local_gui: dict | None = None) -> dict:
    """跑一题；超时终止整个进程组。返回 task_run 写的 JSON + returncode/timed_out。"""
    command = [
        str(AGENT_PYTHON), str(TASK_RUNNER),
        "--app-config", str(app_config), "--log-dir", str(log_dir),
        "--session-id", session_id, "--max-steps", str(max_steps),
        "--result", str(result_path), f"--query={query}",
    ]
    if local_gui and local_gui.get("backend") == "jev":
        import time

        command.extend(("--gui-config", json.dumps(local_gui),
                        "--deadline-at", str(time.time() + timeout_seconds)))
    stdout_path.parent.mkdir(parents=True, exist_ok=True)
    timed_out = False
    with stdout_path.open("w", encoding="utf-8") as log:
        process = subprocess.Popen(command, stdout=log, stderr=subprocess.STDOUT,
                                   env=agent_env(api_key, direct_api=direct_api),
                                   start_new_session=True)
        try:
            process.wait(timeout=timeout_seconds)
        except subprocess.TimeoutExpired:
            timed_out = True
            os.killpg(process.pid, signal.SIGTERM)
            try:
                process.wait(timeout=20)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGKILL)
                process.wait()
    info: dict = {}
    if result_path.is_file():
        info = json.loads(result_path.read_text(encoding="utf-8"))
    info["returncode"] = process.returncode
    info["timed_out"] = timed_out
    return info


def classify(info: dict) -> tuple[str | None, str]:
    """agent 终态 → (发给 VM 的动作 DONE/FAIL/None, stop_reason)。

    异常返回 ("error", 原因)，由调用方记为 failed_unscored；
    步数/墙钟耗尽不发终态动作，直接评分。
    """
    if info.get("timed_out"):
        return None, "wall_time_limit"
    if info.get("error") or info.get("returncode") != 0 or "step_count" not in info:
        return "error", str(info.get("error") or "agent 子进程异常退出或未写结果")
    if info.get("hit_step_limit"):
        return None, "max_steps"
    status = info.get("terminal_status")
    decision = info.get("terminal_decision") or {}
    reason = str(decision.get("reason") or "")
    if status == "finish":
        return "DONE", "done"
    if status == "fail":
        if reason == "max_steps":
            return None, "max_steps"
        return "FAIL", reason or "fail"
    if status == "error":
        return "error", str(decision.get("final_answer") or "agent 终态为 error")
    return None, "no_terminal"
