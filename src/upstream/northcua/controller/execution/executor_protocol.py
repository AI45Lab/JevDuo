"""Agent-agnostic task-executor protocol for controller-owned runs."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from northcua_protocol.enums import MessageType
from northcua_protocol.payloads import SessionLeaseGranted, TaskRequest

ControllerProgressSink = Callable[[MessageType, object], Awaitable[None]]


@dataclass(slots=True, frozen=True)
class ControllerExecutionContext:
    """Execution context passed from the controller scheduler to a task runner.

    Attributes:
        request: Accepted task request to execute.
        session_id: Controller session id bound to the lease.
        lease: Device lease granted for this execution.
        blob_store_root: Optional controller blob root for resolving
            controller-owned input artifacts.
        progress_sink: Optional same-submittee websocket sink for task-local
            progress envelopes such as command request/result events.
    """

    request: TaskRequest
    session_id: str
    lease: SessionLeaseGranted
    blob_store_root: Path | None = None
    progress_sink: ControllerProgressSink | None = None


class ControllerTaskExecutor(Protocol):
    """Protocol implemented by controller-owned agent task executors."""

    async def schedule(self, context: ControllerExecutionContext) -> None:
        """Schedule a leased task for background execution.

        Args:
            context: Leased task execution context.
        """

    async def drain(self) -> None:
        """Wait for outstanding background executions before shutdown."""

    async def cancel_task(self, task_id: str, reason: str) -> None:
        """Best-effort cancel for a scheduled background execution.

        Args:
            task_id: Controller task id to cancel.
            reason: Human-readable cancellation reason.
        """
