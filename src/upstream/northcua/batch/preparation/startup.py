"""Startup command selection for prepared sandboxes."""

from __future__ import annotations

from northcua.batch.contracts import BatchRunConfig, BatchTaskSpec
from northcua.batch.preparation.assignment import PreparedSandboxAssignment
from northcua.batch.preparation.contracts import (
    PreparedSandboxDepth,
    PreparedSandboxEventMessage,
    PreparedSandboxPolicy,
)
from northcua.batch.runner.events import BatchEventWriter
from northcua.sandboxes.contracts import SandboxCommandSpec, SandboxReadinessState
from northcua.sandboxes.guest_capability_evidence import GUEST_CAPABILITY_GATE_LABEL
from northcua.telemetry.contracts import CasePhase, CaseStatus, TelemetryEntityKind


class PreparedSandboxStartupRunner:
    """Select and execute startup commands for prepared sandbox depth modes."""

    def __init__(
        self,
        *,
        config: BatchRunConfig,
        policy: PreparedSandboxPolicy,
        events: BatchEventWriter,
    ) -> None:
        self._config = config
        self._policy = policy
        self._events = events

    def startup_commands_for_depth(self, task: BatchTaskSpec) -> list[SandboxCommandSpec]:
        """Return startup commands selected by the configured depth.

        Args:
            task: Task whose default and per-task startup commands should be
                filtered according to ``PreparedSandboxDepth``.

        Returns:
            Startup commands that should run during background preparation.
        """

        commands = [*self._config.manifest.defaults.startup_commands, *task.startup_commands]
        if self._policy.depth == PreparedSandboxDepth.FULL_INIT:
            return commands
        return [command for command in commands if _is_fixture_setup_command(command)]

    async def run_deferred_startup(
        self,
        *,
        assignment: PreparedSandboxAssignment,
        task: BatchTaskSpec,
    ) -> None:
        """Run startup commands intentionally deferred until assignment.

        Args:
            assignment: Prepared lease assigned to this task attempt.
            task: Batch task whose startup commands should be completed.

        Raises:
            RuntimeError: If deferred startup leaves the device non-ready.
        """

        commands = self._deferred_startup_commands(task)
        if not commands:
            return
        self._events.prepared_event(
            case_id=task.task_id,
            message=PreparedSandboxEventMessage.DEFERRED_STARTUP_STARTED,
            status=CaseStatus.PROVISIONING,
            attempt=assignment.record.attempt,
            details={"attempt": assignment.record.attempt, "command_count": len(commands)},
            sandbox_id=assignment.record.sandbox_id,
            device_id=assignment.record.device_id,
        )
        with self._events.phase_timer(
            case_id=task.task_id,
            phase=CasePhase.PREPARED_SANDBOX,
            status=CaseStatus.PROVISIONING,
            attempt=assignment.record.attempt,
            entity_kind=TelemetryEntityKind.SANDBOX,
            entity_id=assignment.record.sandbox_id,
        ):
            await assignment.lease_context.manager.run_startup_commands(assignment.lease, commands)
            readiness = await assignment.lease_context.manager.wait_for_ready(
                assignment.record.device_id,
                timeout_seconds=self._config.device_readiness_timeout_seconds,
                batch_id=self._config.manifest.batch_id,
                task_id=task.task_id,
                sandbox_id=assignment.record.sandbox_id,
                provider=assignment.record.provider,
                reason="prepared_deferred_startup",
            )
            if readiness != SandboxReadinessState.READY:
                raise RuntimeError(f"prepared device was not ready after deferred startup: {readiness.value}")
        self._events.prepared_event(
            case_id=task.task_id,
            message=PreparedSandboxEventMessage.DEFERRED_STARTUP_FINISHED,
            status=CaseStatus.PROVISIONING,
            attempt=assignment.record.attempt,
            details={"attempt": assignment.record.attempt, "command_count": len(commands)},
            sandbox_id=assignment.record.sandbox_id,
            device_id=assignment.record.device_id,
        )

    def _deferred_startup_commands(self, task: BatchTaskSpec) -> list[SandboxCommandSpec]:
        if self._policy.depth == PreparedSandboxDepth.FULL_INIT:
            return []
        commands = [*self._config.manifest.defaults.startup_commands, *task.startup_commands]
        return [command for command in commands if not _is_fixture_setup_command(command)]


def _is_fixture_setup_command(command: SandboxCommandSpec) -> bool:
    if command.label == GUEST_CAPABILITY_GATE_LABEL:
        return True
    label = command.label.lower()
    text = command.command.lower()
    if label.startswith("open-") or "open_file" in label or "open_url" in label:
        return False
    return any(marker in label or marker in text for marker in ("setup", "extract", "download", "fixture", "assert"))
