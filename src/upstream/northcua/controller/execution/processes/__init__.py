"""Process executor support for controller-owned task runners."""

from __future__ import annotations

from northcua.controller.execution.processes.contracts import (
    WorkerJobFailureKind,
    WorkerJobStatus,
)


def __getattr__(name: str) -> object:
    """Lazily expose process executor classes without import cycles."""

    if name == "ProcessTaskExecutorService":
        from northcua.controller.execution.processes.executor import ProcessTaskExecutorService

        return ProcessTaskExecutorService
    raise AttributeError(name)


__all__ = [
    "WorkerJobFailureKind",
    "WorkerJobStatus",
]
