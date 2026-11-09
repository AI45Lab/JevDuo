"""rjob CLI 薄封装：submit / get / logs / stop / delete + 状态解析。

只依赖 subprocess；可注入 command_runner 便于测试。
"""

from __future__ import annotations

import os
import re
import subprocess
import time

TERMINAL_STATES = {"succeeded", "failed", "deleted"}

# rjob get 的任务计数字典（{'active': 0, ...}）含状态词，匹配前先剔除
_COUNT_DICT_RE = re.compile(r"['\"]\w+['\"]\s*:\s*\d+")
_STATUS_LINE_RE = re.compile(r"(?i)\b(?:rjob|replica)\s+\S+:\s*([a-z_]+)")
_PATTERNS = [
    ("deleted", re.compile(r"(?i)\bdeleted\b|已删除")),
    ("succeeded", re.compile(r"(?i)\b(succeeded|succeed)\b|成功")),
    ("failed", re.compile(r"(?i)\b(failed|error)\b|失败")),
    ("running", re.compile(
        r"(?i)\b(running|terminating|pending|creating|waiting|starting|inqueue)\b|运行中|等待")),
]

DELETE_RETRIES = 3
DELETE_RETRY_SECONDS = 5.0


class RjobError(RuntimeError):
    pass


class Client:
    """rjob 命令封装；command_runner(argv) -> (returncode, stdout+stderr)。"""

    def __init__(self, command_runner=None):
        self._run = command_runner or _run_subprocess

    def _call(self, *args: str) -> str:
        code, output = self._run(["rjob", *args])
        if code != 0:
            raise RjobError(f"rjob {' '.join(args)} 退出码 {code}:\n{output[-2000:]}")
        return output

    def submit(self, argv: list[str]) -> str:
        return self._call("submit", *argv)

    def get(self, name: str) -> str:
        return self._call("get", name)

    def logs(self, name: str, tail_lines: int = 500) -> str:
        return self._call("logs", "job", name, "-n", str(tail_lines))

    def stop(self, name: str) -> str:
        return self._call("stop", name)

    def delete(self, name: str) -> str:
        return self._call("delete", name)


def _run_subprocess(argv: list[str]) -> tuple[int, str]:
    env = {key: value for key, value in os.environ.items() if key != "KUBEBRAIN_QUOTA_GROUP"}
    result = subprocess.run(argv, capture_output=True, text=True, env=env, timeout=600)
    return result.returncode, result.stdout + result.stderr


def parse_status(output: str) -> str:
    line = _STATUS_LINE_RE.search(output)
    if line:
        word = line.group(1).lower()
        for status, pattern in _PATTERNS:
            if pattern.search(word):
                return status
        return "unknown"
    cleaned = _COUNT_DICT_RE.sub(" ", output)
    for status, pattern in _PATTERNS:
        if pattern.search(cleaned):
            return status
    return "unknown"


def job_name(run_id: str, job_id: int) -> str:
    return f"jevduo-{run_id}-{job_id:02d}"


def watch(client: Client, name: str, *, poll_seconds: float = 60,
          timeout_seconds: float | None = None, sleep=time.sleep) -> str:
    started = time.monotonic()
    while True:
        status = parse_status(client.get(name))
        if status in TERMINAL_STATES:
            return status
        if timeout_seconds is not None and time.monotonic() - started >= timeout_seconds:
            raise TimeoutError(f"rjob {name} 超过 {timeout_seconds}s 仍未终态（当前 {status}）")
        sleep(poll_seconds)


def cleanup(client: Client, name: str, *, keep: bool = False, sleep=time.sleep) -> dict:
    """收尾：stop 释放资源 → delete 清理。keep=True 原样保留。"""
    if keep:
        return {"stopped": False, "deleted": False, "kept": True}
    stopped = False
    try:
        client.stop(name)
        stopped = True
    except RjobError:
        pass
    deleted = False
    for attempt in range(DELETE_RETRIES):
        try:
            client.delete(name)
            deleted = True
            break
        except RjobError:
            if attempt < DELETE_RETRIES - 1:
                sleep(DELETE_RETRY_SECONDS)
    return {"stopped": stopped, "deleted": deleted}
