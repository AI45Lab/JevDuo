"""Application-level task result contract for NorthCUA CLI rendering."""

from __future__ import annotations

from dataclasses import dataclass, field

from northcua.core.enums import TaskStatus
from northcua.core.models import StepLogRecord

__all__ = ["TaskRunResult"]


@dataclass(slots=True, frozen=True)
class TaskRunResult:
    """Structured result of one task loop execution.

    Attributes:
        steps: Step logs produced by the task loop.
        terminal_status: Terminal status when reached.
        hit_step_limit: Whether the task stopped at the configured step limit.
    """

    steps: list[StepLogRecord] = field(default_factory=list)
    terminal_status: TaskStatus | None = None
    hit_step_limit: bool = False
