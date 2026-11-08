"""Typed models for Set-of-Mark screenshot overlays."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field

from northcua.agentkernel.json_types import JsonObject


@dataclass(frozen=True)
class SetOfMarkBox:
    """One rectangle in either CSS or screenshot-pixel coordinates."""

    x: float
    y: float
    width: float
    height: float

    @property
    def right(self) -> float:
        return self.x + self.width

    @property
    def bottom(self) -> float:
        return self.y + self.height

    def to_dict(self) -> JsonObject:
        return asdict(self)


@dataclass(frozen=True)
class SetOfMarkOverlayMark:
    """One visual mark rendered on an annotated screenshot."""

    ref: str
    label: str
    role: str
    name: str
    bbox_css: SetOfMarkBox
    bbox_image: SetOfMarkBox
    visible: bool = True
    enabled: bool = True

    def to_dict(self) -> JsonObject:
        return asdict(self)


@dataclass(frozen=True)
class SetOfMarkOverlaySpec:
    """Complete Set-of-Mark overlay request."""

    marks: list[SetOfMarkOverlayMark]
    coordinate_frame: str = "viewport_css"
    image_coordinate_frame: str = "screenshot_pixels"
    truncated: bool = False
    omitted_count: int = 0
    max_marks: int = 60
    metadata: JsonObject = field(default_factory=dict)

    def to_dict(self) -> JsonObject:
        return asdict(self)


@dataclass(frozen=True)
class SetOfMarkOverlayArtifact:
    """Rendered Set-of-Mark overlay artifact."""

    output_path: str
    spec: SetOfMarkOverlaySpec
