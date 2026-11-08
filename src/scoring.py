"""评分语义：官方分数归一化 + failed_unscored 纪律（API/VM 异常 ≠ 0 分）。"""

from __future__ import annotations

from typing import Any


def official_score(value: Any) -> float:
    return float(value["score"]) if isinstance(value, dict) else float(value)


def episode_status(*, scored: bool, exception: BaseException | None) -> str:
    """终态分类：scored / failed_unscored。"""
    if exception is not None or not scored:
        return "failed_unscored"
    return "scored"
