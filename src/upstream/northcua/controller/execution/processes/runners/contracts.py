"""Contracts shared by process-worker runner strategies."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Protocol

from northcua.controller.execution.executor_protocol import ControllerExecutionContext
from northcua.controller.execution.processes.contracts import WorkerRunRequest
from northcua.controller.execution.processes.transport import CommandTransport
from northcua.controller.execution.processes.worker_client import (
    WorkerCommandTransport,
    WorkerHeartbeatStateReporter,
    WorkerIPCClient,
)
from northcua_protocol.payloads import TaskFinished

CommandTransportFactory = Callable[[ControllerExecutionContext, Mapping[str, object]], CommandTransport]


class ProcessWorkerPhaseReporter(WorkerHeartbeatStateReporter, Protocol):
    """Minimal lifecycle surface used by process-runner factories."""

    async def set_phase(self, phase: str) -> None:
        """Publish the current worker phase."""

    def set_phase_from_thread(self, phase: str) -> None:
        """Publish a phase from synchronous bootstrap code."""


class ProcessWorkerRunner(Protocol):
    """Executable runner created inside a controller worker process."""

    async def run(self, context: ControllerExecutionContext) -> TaskFinished:
        """Execute one lease-scoped controller task."""

        ...


@dataclass(slots=True, frozen=True)
class ProcessWorkerRunnerBuildContext:
    """Dependencies required to construct one process-local runner."""

    request: WorkerRunRequest
    ipc: WorkerIPCClient
    phase_reporter: ProcessWorkerPhaseReporter

    def command_transport_factory(self) -> CommandTransportFactory:
        """Build the shared lease-scoped IPC transport factory."""

        request = self.request
        ipc = self.ipc
        reporter = self.phase_reporter

        def build(
            context: ControllerExecutionContext,
            metadata_overrides: Mapping[str, object],
        ) -> CommandTransport:
            del context
            return WorkerCommandTransport(
                ipc=ipc,
                lease=request.lease,
                metadata_overrides=metadata_overrides,
                command_response_timeout_seconds=request.command_response_timeout_seconds,
                heartbeat_state_reporter=reporter,
            )

        return build


class ProcessWorkerRunnerFactory(Protocol):
    """Strategy that imports and constructs one agent-kind runner."""

    @property
    def agent_kind(self) -> str:
        """Return the protocol agent kind handled by the factory."""

        ...

    async def build(self, context: ProcessWorkerRunnerBuildContext) -> ProcessWorkerRunner:
        """Construct the process-local runner for one request."""

        ...
