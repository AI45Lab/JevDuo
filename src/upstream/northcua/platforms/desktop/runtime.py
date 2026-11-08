"""Desktop platform runtime adapter backed by existing NorthCUA desktop services."""

from __future__ import annotations

from northcua.core.enums import ResultStatus
from northcua.core.models import CommandRequestRecord, ObservationContext, StepLogRecord
from northcua.core.protocol import CommandRequestPayload
from northcua.platforms.protocol import (
    PlatformActionRequest,
    PlatformActionResult,
    PlatformActionStatus,
    PlatformArtifactKind,
    PlatformArtifactRef,
    PlatformArtifactRole,
    PlatformCapability,
    PlatformHealthStatus,
    PlatformKind,
    PlatformObservation,
    PlatformRuntime,
)
from northcua.runtime.desktop.observation import DesktopObservationService
from northcua.runtime.mcp.device_executor import NorthCUADeviceCommandExecutor


class DesktopPlatformRuntime(PlatformRuntime):
    """Desktop runtime adapter for native observation and MCP-backed actions."""

    platform_kind = PlatformKind.DESKTOP

    def __init__(
        self,
        *,
        observation_service: DesktopObservationService | None = None,
        action_executor: NorthCUADeviceCommandExecutor | None = None,
    ) -> None:
        """Initialize the desktop platform runtime.

        Args:
            observation_service: Optional observation service for state capture.
            action_executor: Optional device command executor for actions.
        """

        self._observation_service = observation_service
        self._action_executor = action_executor

    @property
    def observation_service(self) -> DesktopObservationService | None:
        """Return the configured desktop observation service, if any.

        Returns:
            Desktop observation service or None.
        """

        return self._observation_service

    @property
    def action_executor(self) -> NorthCUADeviceCommandExecutor | None:
        """Return the configured desktop action executor, if any.

        Returns:
            Desktop action executor or None.
        """

        return self._action_executor

    async def healthcheck(self) -> PlatformHealthStatus:
        """Return desktop runtime health without probing live state.

        Returns:
            Platform health status with configured capabilities.
        """

        return PlatformHealthStatus(
            platform_kind=self.platform_kind,
            ok=self._observation_service is not None or self._action_executor is not None,
            message="desktop runtime configured" if self._observation_service else "desktop runtime adapter only",
            capabilities=(
                PlatformCapability("screenshot", enabled=self._observation_service is not None),
                PlatformCapability("action", enabled=self._action_executor is not None),
            ),
        )

    async def capture_observation(
        self,
        *,
        session_step: int,
        previous_steps: list[object] | None = None,
    ) -> PlatformObservation:
        """Capture desktop observation through the existing observation service.

        Args:
            session_step: Current step number.
            previous_steps: Previous desktop step records.

        Returns:
            Platform observation wrapping the legacy desktop snapshot.

        Raises:
            RuntimeError: If no observation service was configured.
        """

        if self._observation_service is None:
            raise RuntimeError("DesktopPlatformRuntime has no observation service configured.")
        typed_previous_steps = [step for step in previous_steps or [] if isinstance(step, StepLogRecord)]
        observation_context = await self._observation_service.collect_context(session_step, typed_previous_steps)
        snapshot = self._observation_service.snapshot_from_context(observation_context)
        artifacts: list[PlatformArtifactRef] = []
        for artifact in observation_context.current_artifacts:
            artifacts.append(
                PlatformArtifactRef(
                    kind=PlatformArtifactKind.IMAGE,
                    path=artifact.path,
                    url=artifact.url,
                    role=PlatformArtifactRole.CURRENT_CLEAN if artifact.role == "current_clean" else artifact.role,
                    metadata=dict(artifact.metadata),
                )
            )
        if not artifacts and (snapshot.clean_screenshot_path or snapshot.clean_screenshot_url):
            artifacts.append(
                PlatformArtifactRef(
                    kind=PlatformArtifactKind.IMAGE,
                    path=snapshot.clean_screenshot_path,
                    url=snapshot.clean_screenshot_url if not snapshot.clean_screenshot_path else "",
                    role=PlatformArtifactRole.CURRENT_CLEAN,
                    metadata=dict(snapshot.screenshot_metadata),
                )
            )
        return PlatformObservation(
            platform_kind=self.platform_kind,
            artifacts=tuple(artifacts),
            raw=observation_context,
            metadata={"platform_name": observation_context.platform_name},
        )

    async def execute_action(self, request: PlatformActionRequest) -> PlatformActionResult:
        """Execute a desktop action through the existing command executor.

        Args:
            request: Platform action request.

        Returns:
            Platform action result.

        Raises:
            RuntimeError: If no action executor was configured.
        """

        if self._action_executor is None:
            raise RuntimeError("DesktopPlatformRuntime has no action executor configured.")
        payload = CommandRequestPayload(
            session_id=str(request.metadata.get("session_id", "")),
            round_num=int(request.metadata.get("round_num", 0) or 0),
            round_step=int(request.metadata.get("round_step", 0) or 0),
            commands=[
                CommandRequestRecord(
                    call_id=request.call_id,
                    tool_name=request.name,
                    parameters=dict(request.parameters),
                    server_name=request.server_name,
                )
            ],
        )
        result_payload = await self._action_executor.execute_command_request(payload)
        first_result = result_payload.results[0] if result_payload.results else None
        status = (
            PlatformActionStatus.SUCCESS
            if result_payload.status == ResultStatus.SUCCESS
            else PlatformActionStatus.FAILURE
        )
        return PlatformActionResult(
            call_id=request.call_id,
            status=status,
            result=first_result.result if first_result is not None else None,
            error=first_result.error if first_result is not None else "missing command result",
        )
