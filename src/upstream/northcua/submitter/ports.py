"""Small controller transport port shared by single-task execution modes."""

from __future__ import annotations

from typing import Protocol

from northcua_protocol.capabilities import Capability
from northcua_protocol.payloads import TaskAccepted, TaskCancel, TaskFinished, TaskStatusSnapshot


class ControllerClientPort(Protocol):
    """Controller operations required by a single NorthCUA task."""

    async def connect(self) -> None: ...

    async def close(self) -> None: ...

    async def submit_task(
        self,
        *,
        agent_kind: str,
        request: str,
        token: str,
        target_device_id: str,
        required_capabilities: set[Capability],
        metadata: dict[str, object],
        task_id: str,
        trace_id: str,
    ) -> TaskAccepted: ...

    async def wait_for_finished(self, task_id: str, *, timeout_seconds: float | None = None) -> TaskFinished: ...

    async def task_statuses(self, task_ids: list[str], *, token: str = "") -> list[TaskStatusSnapshot]: ...

    async def cancel_task(self, cancel: TaskCancel) -> None: ...


__all__ = ["ControllerClientPort"]
