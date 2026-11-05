"""停止条件。墙钟/步数预算的强制执行在 jevduo_eval.runner（M2）；
本模块提供 harness 侧的判定与 no_action 上限常量（与旧仓库行为一致）。"""

from __future__ import annotations

from ..protocol.models import Budget

NO_ACTION_LIMIT = 3


def budget_exhausted(budget: Budget) -> str | None:
    """预算耗尽时返回停止原因，否则 None。"""
    if budget.steps_remaining <= 0:
        return "max_steps"
    if budget.wall_seconds_remaining <= 0:
        return "wall_time_limit"
    return None
