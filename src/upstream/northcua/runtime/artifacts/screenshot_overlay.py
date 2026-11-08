"""Shared screenshot persistence and overlay rendering helpers."""

from __future__ import annotations

import base64
import io
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path

from PIL import Image

from northcua.agentkernel.actions.protocol import ActionCommandInfo
from northcua.agentkernel.overlay.history_overlay import HistoryActionOverlayPlanner, HistoryActionOverlayRenderer
from northcua.agentkernel.overlay.set_of_mark import (
    SetOfMarkOverlayMark,
    SetOfMarkOverlayRenderer,
    SetOfMarkOverlaySpec,
)
from northcua.core.json_types import JsonObject


class ScreenshotOverlayKind(str, Enum):
    """Supported overlay/enhancement processor kinds."""

    ACTION = "action"
    MARKS = "marks"


@dataclass(slots=True, frozen=True)
class ScreenshotOverlayRequest:
    """One screenshot enhancement request.

    Attributes:
        kind: Overlay processor kind to dispatch.
        source_image_path: Existing screenshot file path.
        output_path: Destination overlay image file path.
        action: Optional normalized action used by action overlays.
        marks: Optional screenshot marks used by Set-of-Mark overlays.
        metadata: Small JSON metadata copied to the produced artifact.
    """

    kind: ScreenshotOverlayKind
    source_image_path: str
    output_path: str
    action: ActionCommandInfo | None = None
    marks: list[SetOfMarkOverlayMark] = field(default_factory=list)
    metadata: JsonObject = field(default_factory=dict)


@dataclass(slots=True, frozen=True)
class ScreenshotOverlayArtifact:
    """One persisted screenshot enhancement artifact.

    Attributes:
        output_path: Filesystem path for the rendered overlay image.
        kind: Overlay processor kind that produced the artifact.
        metadata: Small JSON metadata describing overlay planning output.
    """

    output_path: str
    kind: ScreenshotOverlayKind
    metadata: JsonObject = field(default_factory=dict)


class BaseScreenshotOverlayProcessor(ABC):
    """Abstract processor for one screenshot enhancement family."""

    kind: ScreenshotOverlayKind

    def supports(self, request: ScreenshotOverlayRequest) -> bool:
        """Return whether the processor can handle the request.

        Args:
            request: Screenshot overlay request to inspect.

        Returns:
            True when the request kind matches this processor.
        """

        return request.kind == self.kind

    @abstractmethod
    def process(self, request: ScreenshotOverlayRequest) -> ScreenshotOverlayArtifact | None:
        """Process one screenshot enhancement request.

        Args:
            request: Screenshot overlay request to process.

        Returns:
            Persisted overlay artifact, or None when no overlay is produced.
        """


class ActionScreenshotOverlayProcessor(BaseScreenshotOverlayProcessor):
    """Render action-driven overlays on top of a persisted screenshot."""

    kind = ScreenshotOverlayKind.ACTION

    def __init__(
        self,
        *,
        badges_enabled: bool = True,
        max_badge_text: int = 32,
        planner: HistoryActionOverlayPlanner | None = None,
        renderer: HistoryActionOverlayRenderer | None = None,
    ) -> None:
        """Initialize the action overlay processor.

        Args:
            badges_enabled: Whether text badges should be rendered.
            max_badge_text: Maximum badge text length.
            planner: Optional overlay planner override.
            renderer: Optional overlay renderer override.
        """

        self.planner = planner or HistoryActionOverlayPlanner(
            badges_enabled=badges_enabled,
            max_badge_text=max_badge_text,
        )
        self.renderer = renderer or HistoryActionOverlayRenderer()

    def process(self, request: ScreenshotOverlayRequest) -> ScreenshotOverlayArtifact | None:
        """Render one normalized action overlay request.

        Args:
            request: Action overlay request to render.

        Returns:
            Persisted action overlay artifact, or None when unsupported.
        """

        if request.action is None:
            return None
        spec = self.planner.plan(request.action)
        if spec is None:
            return None
        artifact = self.renderer.render(
            source_image_path=request.source_image_path,
            spec=spec,
            output_path=request.output_path,
        )
        return ScreenshotOverlayArtifact(
            output_path=artifact.output_path,
            kind=self.kind,
            metadata={
                "overlay_spec": artifact.spec.to_dict(),
                **dict(request.metadata),
            },
        )


class SetOfMarkScreenshotOverlayProcessor(BaseScreenshotOverlayProcessor):
    """Render web Set-of-Mark overlays on top of a persisted screenshot."""

    kind = ScreenshotOverlayKind.MARKS

    def __init__(self, *, renderer: SetOfMarkOverlayRenderer | None = None) -> None:
        self.renderer = renderer or SetOfMarkOverlayRenderer()

    def process(self, request: ScreenshotOverlayRequest) -> ScreenshotOverlayArtifact | None:
        """Render one Set-of-Mark overlay request."""

        if not request.marks:
            return None
        spec = SetOfMarkOverlaySpec(
            marks=list(request.marks),
            truncated=bool(request.metadata.get("truncated", False)),
            omitted_count=int(request.metadata.get("omitted_count", 0)),
            max_marks=int(request.metadata.get("max_marks", len(request.marks))),
            metadata=dict(request.metadata),
        )
        artifact = self.renderer.render(
            source_image_path=request.source_image_path,
            spec=spec,
            output_path=request.output_path,
        )
        return ScreenshotOverlayArtifact(
            output_path=artifact.output_path,
            kind=self.kind,
            metadata={
                "overlay_spec": artifact.spec.to_dict(),
                **dict(request.metadata),
            },
        )


class ScreenshotOverlayService:
    """Shared orchestrator for screenshot persistence and overlay rendering."""

    def __init__(
        self,
        *,
        processors: list[BaseScreenshotOverlayProcessor] | None = None,
    ) -> None:
        """Initialize the overlay service with available processors.

        Args:
            processors: Optional processor list; defaults to action overlays.
        """

        active_processors = processors or [ActionScreenshotOverlayProcessor(), SetOfMarkScreenshotOverlayProcessor()]
        self._processors: dict[ScreenshotOverlayKind, BaseScreenshotOverlayProcessor] = {
            processor.kind: processor for processor in active_processors
        }

    def render(self, request: ScreenshotOverlayRequest) -> ScreenshotOverlayArtifact | None:
        """Dispatch one request to the matching processor.

        Args:
            request: Screenshot overlay request to render.

        Returns:
            Persisted overlay artifact, or None when no overlay is produced.

        Raises:
            ValueError: If no processor is registered for the request kind.
        """

        processor = self._processors.get(request.kind)
        if processor is None:
            raise ValueError(f"No screenshot overlay processor registered for {request.kind.value!r}")
        return processor.process(request)

    def render_action_overlay(
        self,
        *,
        source_image_path: str,
        output_path: str,
        action: ActionCommandInfo,
        metadata: JsonObject | None = None,
    ) -> ScreenshotOverlayArtifact | None:
        """Render one action overlay request.

        Args:
            source_image_path: Existing screenshot path.
            output_path: Destination overlay image path.
            action: Action metadata used to plan the overlay.
            metadata: Optional artifact metadata to preserve.

        Returns:
            Persisted action overlay artifact, or None when no overlay is produced.
        """

        return self.render(
            ScreenshotOverlayRequest(
                kind=ScreenshotOverlayKind.ACTION,
                source_image_path=source_image_path,
                output_path=output_path,
                action=action,
                metadata=dict(metadata or {}),
            )
        )

    def render_set_of_mark_overlay(
        self,
        *,
        source_image_path: str,
        output_path: str,
        marks: list[SetOfMarkOverlayMark],
        metadata: JsonObject | None = None,
    ) -> ScreenshotOverlayArtifact | None:
        """Render one Set-of-Mark overlay request."""

        return self.render(
            ScreenshotOverlayRequest(
                kind=ScreenshotOverlayKind.MARKS,
                source_image_path=source_image_path,
                output_path=output_path,
                marks=list(marks),
                metadata=dict(metadata or {}),
            )
        )


def persist_data_url_image(image_url: str, destination: str | Path) -> None:
    """Persist a data URL image using the destination file format.

    Args:
        image_url: Image data URL to persist.
        destination: Destination filesystem path.

    Raises:
        ValueError: If the input is not an image data URL.
    """

    if not image_url.startswith("data:image/"):
        raise ValueError("Expected image data URL")
    _, base64_payload = image_url.split(",", 1)
    image_bytes = base64.b64decode(base64_payload)
    output_path = Path(destination)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with Image.open(io.BytesIO(image_bytes)) as image:
        image_format = "PNG" if output_path.suffix.lower() == ".png" else None
        converted = image.convert("RGBA" if image_format == "PNG" else "RGB")
        converted.save(output_path, format=image_format)
