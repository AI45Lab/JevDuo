"""Desktop observation compatibility facade and snapshot conversion helpers."""

from __future__ import annotations

from typing import Protocol

from PIL import Image

from northcua.core.enums import ObservationArtifactKind
from northcua.core.models import (
    DesktopStateSnapshot,
    ObservationArtifact,
    ObservationContext,
    StepLogRecord,
)


def _image_metadata(path: str, metadata: dict[str, object]) -> dict[str, object]:
    """Fill missing screenshot dimensions from the already persisted image."""

    resolved = dict(metadata)
    width = resolved.get("width")
    height = resolved.get("height")
    if isinstance(width, int) and width > 0 and isinstance(height, int) and height > 0:
        return resolved
    try:
        with Image.open(path) as image:
            resolved.update({"width": image.width, "height": image.height})
    except (FileNotFoundError, OSError):
        pass
    return resolved


class DesktopObservationCoordinatorProtocol(Protocol):
    """Agent-owned coordinator contract consumed by the desktop runtime facade."""

    async def collect(self, session_step: int, previous_steps: list[StepLogRecord]) -> ObservationContext:
        """Collect one normalized desktop observation context.

        Args:
            session_step: Current step number.
            previous_steps: Prior persisted steps for history-aware observation.

        Returns:
            Normalized observation context.
        """


class DesktopObservationService:
    """Collect desktop state while preserving the legacy snapshot-facing API."""

    def __init__(self, *, coordinator: DesktopObservationCoordinatorProtocol) -> None:
        self._coordinator = coordinator

    async def collect(self, session_step: int, previous_steps: list[StepLogRecord]) -> DesktopStateSnapshot:
        """Project the single tool-result observation path into a desktop snapshot."""

        return self.snapshot_from_context(await self.collect_context(session_step, previous_steps))

    async def collect_context(self, session_step: int, previous_steps: list[StepLogRecord]) -> ObservationContext:
        """Collect the latest tool-returned state, with an initial screenshot fallback."""

        return await self._coordinator.collect(session_step, previous_steps)

    @staticmethod
    def snapshot_from_context(observation_context: ObservationContext) -> DesktopStateSnapshot:
        """Project a normalized observation context back into the legacy snapshot shape.

        Args:
            observation_context: Normalized context from the observation coordinator.

        Returns:
            Legacy desktop state snapshot with equivalent fields.
        """

        return DesktopStateSnapshot(
            platform_name=observation_context.platform_name,
            current_window=observation_context.current_window,
            current_controls=list(observation_context.current_controls),
            clean_screenshot_path=observation_context.current_clean_screenshot_path,
            clean_screenshot_url=observation_context.current_clean_screenshot_url,
            selected_history_image_path=observation_context.selected_history_image_path,
            selected_history_image_url=observation_context.selected_history_image_url,
            screenshot_metadata=_image_metadata(
                observation_context.current_clean_screenshot_path,
                dict(observation_context.observation_metadata),
            ),
        )

    @staticmethod
    def context_from_snapshot(snapshot: DesktopStateSnapshot) -> ObservationContext:
        """Build a normalized observation context from a legacy desktop snapshot.

        Args:
            snapshot: Legacy desktop state snapshot.

        Returns:
            Normalized observation context projected from the snapshot fields.
        """

        artifacts: list[ObservationArtifact] = []
        if snapshot.clean_screenshot_path or snapshot.clean_screenshot_url:
            artifact_url = snapshot.clean_screenshot_url if not snapshot.clean_screenshot_path else ""
            artifacts.append(
                ObservationArtifact(
                    kind=ObservationArtifactKind.IMAGE,
                    role="current_clean",
                    source="legacy_snapshot",
                    path=snapshot.clean_screenshot_path,
                    url=artifact_url,
                    metadata=dict(snapshot.screenshot_metadata),
                )
            )
        return ObservationContext(
            platform_name=snapshot.platform_name,
            current_window=snapshot.current_window,
            current_controls=list(snapshot.current_controls),
            current_artifacts=artifacts,
            current_clean_screenshot_path=snapshot.clean_screenshot_path,
            current_clean_screenshot_url=snapshot.clean_screenshot_url,
            selected_history_image_path=snapshot.selected_history_image_path,
            selected_history_image_url=snapshot.selected_history_image_url,
            observation_metadata=dict(snapshot.screenshot_metadata),
        )
