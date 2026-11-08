"""Terminal verification artifact capture for desktop rounds."""

from __future__ import annotations

import json
import logging
import os
import shutil
from collections.abc import Sequence

from northcua.app.logging import log_runtime_event
from northcua.core.config import DesktopPolicyConfig
from northcua.core.enums import ObservationArtifactKind, RuntimeMode, TaskStatus
from northcua.core.json_types import JsonObject
from northcua.core.models import ObservationArtifact, StepLogRecord
from northcua.runtime.artifacts.store import NorthCUAArtifactStore
from northcua.runtime.mcp.desktop_native_server import save_data_url_to_path

from .verification_commands import (
    CommandExecutorProtocol,
    RemoteToolDispatcher,
    capture_local_image_url,
    capture_remote_image_url,
)

logger = logging.getLogger(__name__)
_TERMINAL_TASK_STATUSES = {TaskStatus.FINISH, TaskStatus.FAIL, TaskStatus.ERROR}


class RoundEndVerificationCapture:
    """Capture a clean screenshot when a desktop query reaches a terminal point."""

    def __init__(
        self,
        *,
        desktop_policy: DesktopPolicyConfig,
        artifact_store: NorthCUAArtifactStore,
        command_executor: CommandExecutorProtocol,
    ) -> None:
        """Initialize terminal verification capture.

        Args:
            desktop_policy: Desktop screenshot scope policy.
            artifact_store: Artifact store used for screenshot paths.
            command_executor: Local command executor for in-process captures.
        """

        self.desktop_policy = desktop_policy
        self.artifact_store = artifact_store
        self.command_executor = command_executor

    def set_command_executor(self, command_executor: CommandExecutorProtocol) -> None:
        """Update the command executor used for local verification captures.

        Args:
            command_executor: Local executor used for subsequent captures.
        """

        self.command_executor = command_executor

    async def capture_for_terminal_step(
        self,
        *,
        step_log: StepLogRecord,
        runtime_mode: RuntimeMode,
        remote_tool_dispatcher: RemoteToolDispatcher | None = None,
        previous_steps: Sequence[StepLogRecord] = (),
    ) -> None:
        """Attempt to capture and attach a terminal verification screenshot.

        Verification is a best-effort artifact hook: it should always be
        attempted for terminal desktop steps, but capture failure must not
        change the task result.

        Args:
            step_log: Step log that may receive the screenshot artifact.
            runtime_mode: Local or remote runtime mode.
            remote_tool_dispatcher: Optional remote dispatcher for remote mode.
            previous_steps: Persisted steps from the same task/round used to
        """

        if step_log.status not in _TERMINAL_TASK_STATUSES:
            return
        await self._capture(
            step_log=step_log,
            runtime_mode=runtime_mode,
            remote_tool_dispatcher=remote_tool_dispatcher,
            previous_steps=previous_steps,
            reason="terminal",
        )

    async def capture_for_step_limit(
        self,
        *,
        step_log: StepLogRecord,
        runtime_mode: RuntimeMode,
        remote_tool_dispatcher: RemoteToolDispatcher | None = None,
        previous_steps: Sequence[StepLogRecord] = (),
    ) -> None:
        """Attempt to capture and attach a max-step verification screenshot.

        Step-limit exits intentionally keep the last model step status as-is in
        local CLI runs, but still need a final visual artifact for debugging and
        SFT review.

        Args:
            step_log: Step log that may receive the screenshot artifact.
            runtime_mode: Local or remote runtime mode.
            remote_tool_dispatcher: Optional remote dispatcher for remote mode.
            previous_steps: Persisted steps from the same task/round used to
                tool result of its own.
        """

        await self._capture(
            step_log=step_log,
            runtime_mode=runtime_mode,
            remote_tool_dispatcher=remote_tool_dispatcher,
            previous_steps=previous_steps,
            reason="step_limit",
        )

    async def _capture(
        self,
        *,
        step_log: StepLogRecord,
        runtime_mode: RuntimeMode,
        remote_tool_dispatcher: RemoteToolDispatcher | None,
        previous_steps: Sequence[StepLogRecord],
        reason: str,
    ) -> None:
        """Capture, persist, and attach one verification screenshot."""

        all_screens = not self.desktop_policy.primary_screen_only
        log_runtime_event(
            logger,
            "round_end_verification_capture_started",
            session_id=step_log.session_id,
            session_step=step_log.session_step,
            round_num=step_log.round_num,
            round_step=step_log.round_step,
            runtime_mode=runtime_mode,
            all_screens=all_screens,
            reason=reason,
        )
        try:
            image_url = (
                await self._capture_remote_image_url(
                    step_log=step_log,
                    all_screens=all_screens,
                    remote_tool_dispatcher=remote_tool_dispatcher,
                )
                if runtime_mode == RuntimeMode.REMOTE
                else await self._capture_local_image_url(step_log=step_log, all_screens=all_screens)
            )
            image_path = _persist_image(self.artifact_store, step_log.session_step, image_url)
        except Exception as exc:  # pragma: no cover - defensive artifact boundary
            logger.exception("Round-end verification screenshot capture failed")
            step_log.observation_metadata["round_end_verification_status"] = "failure"
            step_log.observation_metadata["round_end_verification_error"] = str(exc)
            log_runtime_event(
                logger,
                "round_end_verification_capture_failed",
                level=logging.WARNING,
                session_id=step_log.session_id,
                session_step=step_log.session_step,
                round_num=step_log.round_num,
                round_step=step_log.round_step,
                error=str(exc),
                reason=reason,
            )
            return

        previous_clean = step_log.clean_screenshot_path
        metadata = {
            "all_screens": all_screens,
            "previous_clean_screenshot_path": previous_clean,
            "runtime_mode": runtime_mode.value,
            "reason": reason,
            "coordinate_space": "desktop",
        }
        artifact = ObservationArtifact(
            kind=ObservationArtifactKind.IMAGE,
            role="round_end_verify",
            source="mcp:DesktopNativeDataExecutor.capture_desktop_screenshot",
            path=image_path,
            metadata=metadata,
        )
        step_log.observation_artifacts.append(artifact)
        step_log.observation_metadata["round_end_verification_status"] = "success"
        step_log.observation_metadata["round_end_verification_screenshot_path"] = image_path
        step_log.observation_metadata["round_end_verification_previous_clean_screenshot_path"] = previous_clean
        step_log.observation_metadata["round_end_verification_source"] = artifact.source
        step_log.observation_metadata["round_end_verification_coordinate_space"] = metadata["coordinate_space"]
        step_log.clean_screenshot_path = image_path
        log_runtime_event(
            logger,
            "round_end_verification_capture_completed",
            session_id=step_log.session_id,
            session_step=step_log.session_step,
            round_num=step_log.round_num,
            round_step=step_log.round_step,
            screenshot_path=image_path,
            previous_clean_screenshot_path=previous_clean,
            source=artifact.source,
            coordinate_space=metadata["coordinate_space"],
            reason=reason,
        )

    async def _capture_local_image_url(
        self,
        *,
        step_log: StepLogRecord,
        all_screens: bool,
    ) -> str:
        return await capture_local_image_url(
            command_executor=self.command_executor,
            step_log=step_log,
            all_screens=all_screens,
        )

    async def _capture_remote_image_url(
        self,
        *,
        step_log: StepLogRecord,
        all_screens: bool,
        remote_tool_dispatcher: RemoteToolDispatcher | None,
    ) -> str:
        return await capture_remote_image_url(
            step_log=step_log,
            all_screens=all_screens,
            remote_tool_dispatcher=remote_tool_dispatcher,
        )


def _persist_image(artifact_store: NorthCUAArtifactStore, session_step: int, image_url: str) -> str:
    destination = artifact_store.verification_screenshot_path(session_step)
    destination.parent.mkdir(parents=True, exist_ok=True)
    if image_url.startswith("data:image/"):
        save_data_url_to_path(image_url, str(destination))
        return str(destination)
    if image_url and os.path.exists(image_url):
        shutil.copy2(image_url, destination)
        return str(destination)
    raise RuntimeError("Verification capture did not return an image.")
