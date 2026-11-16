"""A worker-owned wall deadline, including setup, acting and official scoring."""

from __future__ import annotations

import signal


class TaskWallTimeLimit(BaseException):
    """Escape evaluator `except Exception` blocks at the worker deadline."""


def arm(seconds: float):
    def expired(_signum, _frame):
        raise TaskWallTimeLimit("task_wall_time_limit")

    previous = signal.signal(signal.SIGALRM, expired)
    signal.setitimer(signal.ITIMER_REAL, seconds)

    def cancel():
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous)

    return cancel
