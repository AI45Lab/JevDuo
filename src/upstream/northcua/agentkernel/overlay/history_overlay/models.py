"""Typed overlay models for history action visualization."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field

from northcua.agentkernel.json_types import JsonObject


@dataclass(frozen=True)
class OverlayPoint:
    """One integer pixel coordinate used in an overlay specification.

    Attributes:
        x: Horizontal screenshot coordinate.
        y: Vertical screenshot coordinate.
    """

    x: int
    y: int


@dataclass
class HistoryActionOverlaySpec:
    """Normalized visual overlay specification for one executed action.

    Attributes:
        action_function: Original action function name.
        render_mode: Renderer mode used to draw the overlay.
        badge_text: Optional badge text for non-spatial actions.
        primary_point: Main screenshot coordinate for point-based overlays.
        secondary_point: Secondary coordinate for drag-like overlays.
        path_points: Polyline points for drag-like overlays.
        delta_x: Horizontal scroll delta.
        delta_y: Vertical scroll delta.
        variant: Optional render variant, such as click type.
        metadata: JSON-serializable debug metadata.
    """

    action_function: str
    render_mode: str
    badge_text: str = ""
    primary_point: OverlayPoint | None = None
    secondary_point: OverlayPoint | None = None
    path_points: list[OverlayPoint] = field(default_factory=list)
    delta_x: int = 0
    delta_y: int = 0
    variant: str = ""
    metadata: JsonObject = field(default_factory=dict)

    def to_dict(self) -> JsonObject:
        """Convert the specification into a JSON-serializable dictionary.

        Returns:
            Dictionary representation suitable for trace metadata.
        """

        return asdict(self)


@dataclass
class HistoryActionOverlayArtifact:
    """One rendered overlay artifact.

    Attributes:
        output_path: Local path to the rendered overlay image.
        spec: Overlay specification used to render the artifact.
    """

    output_path: str
    spec: HistoryActionOverlaySpec
