"""rjob 状态解析：从 `rjob get` 的输出判断 job 是否进入终态。

平台输出格式不稳定——状态行、关键词、任务计数字典都可能带状态词，
故先剔除计数字典再关键词兜底；确实认不出时返回 unknown（交由上层按「未终态」处理）。
"""

from __future__ import annotations

import re

TERMINAL_STATES = {"succeeded", "failed", "deleted"}

_STATUS_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("deleted", re.compile(r"(?i)\bdeleted\b|已删除")),
    ("succeeded", re.compile(r"(?i)\b(succeeded|succeed)\b|成功")),
    ("failed", re.compile(r"(?i)\b(failed|error)\b|失败")),
    (
        "running",
        re.compile(r"(?i)\b(running|terminating|pending|creating|waiting|starting|inqueue)\b|运行中|等待"),
    ),
]
# rjob get 的任务计数字典（{'active': 0, 'failed': 0, ...}）含状态词字样，须先剔除再关键词匹配
_COUNT_DICT_RE = re.compile(r"['\"]\w+['\"]\s*:\s*\d+")
# 首选：状态行本体，如 "rjob jevduo-x: Succeeded" / "replica jevduo-x-abc: SUCCEED"
_STATUS_LINE_RE = re.compile(r"(?i)\b(?:rjob|replica)\s+\S+:\s*([a-z_]+)")


def parse_status(get_output: str) -> str:
    """解析 rjob get 输出；优先取状态行，剔除计数字典后做关键词兜底；不识别返回 unknown。"""
    line = _STATUS_LINE_RE.search(get_output)
    if line:
        word = line.group(1).lower()
        for status, pattern in _STATUS_PATTERNS:
            if pattern.search(word):
                return status
        return "unknown"
    cleaned = _COUNT_DICT_RE.sub(" ", get_output)
    for status, pattern in _STATUS_PATTERNS:
        if pattern.search(cleaned):
            return status
    return "unknown"
