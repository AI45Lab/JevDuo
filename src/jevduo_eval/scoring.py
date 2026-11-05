"""评分与 failed_unscored 纪律（API/VM 异常 ≠ 0 分）。official_score 语义与旧仓库一致。"""

from __future__ import annotations

from typing import Any


def official_score(value: Any) -> float:
    return float(value["score"]) if isinstance(value, dict) else float(value)


def episode_status(*, scored: bool, exception: BaseException | None) -> str:
    """终态分类：scored / failed_unscored。评分失败与执行异常都不计入 0 分。"""
    if exception is not None or not scored:
        return "failed_unscored"
    return "scored"
