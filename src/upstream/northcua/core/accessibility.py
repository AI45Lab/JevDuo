"""Core accessibility protocol shared across runtime, MCP, and observation layers."""

from __future__ import annotations

from typing import ClassVar

from pydantic import BaseModel, ConfigDict, Field

from .enums import (
    AccessibilityCaptureStatus,
    AccessibilityDiffMode,
    AccessibilityRawFormat,
    AccessibilityScope,
    AccessibilitySourceKind,
)


class AccessibilityCapabilities(BaseModel):
    """Runtime capability description for the active platform/backend.

    Attributes:
        platform_name: Source platform/backend name.
        source: Accessibility backend source.
        supported_scopes: Capture scopes supported by the backend.
        requires_permission: Whether OS/user permission is required.
        permission_granted: Whether required permission is currently granted.
        raw_formats: Raw payload formats available from the backend.
        message: Human-readable capability note.
    """

    model_config: ClassVar[ConfigDict] = ConfigDict(extra="ignore")

    platform_name: str = ""
    source: AccessibilitySourceKind | None = None
    supported_scopes: list[AccessibilityScope] = Field(default_factory=list)
    requires_permission: bool = False
    permission_granted: bool = True
    raw_formats: list[AccessibilityRawFormat] = Field(default_factory=list)
    message: str = ""


class AccessibilityCaptureRequest(BaseModel):
    """Normalized capture request for one accessibility snapshot.

    Attributes:
        scope: Requested capture scope.
        max_depth: Maximum tree depth to capture.
        max_children_per_node: Maximum children captured for each node.
        max_nodes: Maximum total nodes to capture.
        include_raw_payload: Whether to include raw backend payload text.
        capture_timeout_seconds: Capture timeout in seconds.
    """

    model_config: ClassVar[ConfigDict] = ConfigDict(extra="ignore")

    scope: AccessibilityScope = AccessibilityScope.DESKTOP
    max_depth: int = 50
    max_children_per_node: int = 256
    max_nodes: int = 5000
    include_raw_payload: bool = False
    capture_timeout_seconds: float = 3.0


class AccessibilityBounds(BaseModel):
    """Normalized screen-space bounds.

    Attributes:
        x: Left coordinate in screen pixels.
        y: Top coordinate in screen pixels.
        width: Bounds width in pixels.
        height: Bounds height in pixels.
    """

    model_config: ClassVar[ConfigDict] = ConfigDict(extra="ignore")

    x: int = 0
    y: int = 0
    width: int = 0
    height: int = 0

    @classmethod
    def from_rect(
        cls, left: float | int, top: float | int, right: float | int, bottom: float | int
    ) -> "AccessibilityBounds":
        """Create bounds from a left/top/right/bottom rectangle.

        Args:
            left: Left coordinate.
            top: Top coordinate.
            right: Right coordinate.
            bottom: Bottom coordinate.

        Returns:
            Normalized accessibility bounds.
        """

        x = int(left)
        y = int(top)
        width = max(int(right) - x, 0)
        height = max(int(bottom) - y, 0)
        return cls(x=x, y=y, width=width, height=height)


class AccessibilityWindowRef(BaseModel):
    """Window-level reference associated with one capture.

    Attributes:
        app_name: Application name.
        title: Window title.
        pid: Process identifier.
        bounds: Optional window bounds.
    """

    model_config: ClassVar[ConfigDict] = ConfigDict(extra="ignore")

    app_name: str = ""
    title: str = ""
    pid: int = 0
    bounds: AccessibilityBounds | None = None


class AccessibilityNode(BaseModel):
    """Normalized accessibility node.

    Attributes:
        node_id: Stable node identifier within the snapshot.
        role: Accessibility role.
        name: Accessible name.
        description: Accessible description.
        value: Current accessible value.
        text: Visible or extracted text.
        bounds: Optional node bounds.
        states: Accessibility states.
        actions: Supported accessibility actions.
        attributes: Additional string attributes.
        children: Child accessibility nodes.
    """

    model_config: ClassVar[ConfigDict] = ConfigDict(extra="ignore")

    node_id: str
    role: str
    name: str = ""
    description: str = ""
    value: str = ""
    text: str = ""
    bounds: AccessibilityBounds | None = None
    states: list[str] = Field(default_factory=list)
    actions: list[str] = Field(default_factory=list)
    attributes: dict[str, str] = Field(default_factory=dict)
    children: list["AccessibilityNode"] = Field(default_factory=list)


class AccessibilityCaptureStats(BaseModel):
    """Capture statistics recorded for one snapshot.

    Attributes:
        node_count: Number of nodes captured.
        max_depth: Deepest captured tree depth.
        truncated: Whether capture limits truncated the tree.
        capture_ms: Capture duration in milliseconds.
    """

    model_config: ClassVar[ConfigDict] = ConfigDict(extra="ignore")

    node_count: int = 0
    max_depth: int = 0
    truncated: bool = False
    capture_ms: float = 0.0


class AccessibilitySummary(BaseModel):
    """Prompt-independent human/debug summary for one snapshot.

    Attributes:
        root_role: Root node role.
        root_name: Root node name.
        focused_role: Focused node role.
        focused_name: Focused node name.
        focused_path: Path to the focused node.
        actionable_count: Number of actionable nodes.
    """

    model_config: ClassVar[ConfigDict] = ConfigDict(extra="ignore")

    root_role: str = ""
    root_name: str = ""
    focused_role: str = ""
    focused_name: str = ""
    focused_path: list[str] = Field(default_factory=list)
    actionable_count: int = 0


class AccessibilitySnapshot(BaseModel):
    """Structured desktop accessibility snapshot.

    Attributes:
        status: Capture status.
        platform_name: Source platform/backend name.
        source: Accessibility backend source.
        scope: Capture scope.
        window: Optional captured window reference.
        tree: Optional accessibility tree root.
        stats: Capture statistics.
        summary: Optional snapshot summary.
        raw_format: Raw payload format.
        raw_payload: Raw backend payload text.
        error: Capture error message.
    """

    model_config: ClassVar[ConfigDict] = ConfigDict(extra="ignore")

    status: AccessibilityCaptureStatus
    platform_name: str = ""
    source: AccessibilitySourceKind | None = None
    scope: AccessibilityScope = AccessibilityScope.DESKTOP
    window: AccessibilityWindowRef | None = None
    tree: AccessibilityNode | None = None
    stats: AccessibilityCaptureStats = Field(default_factory=AccessibilityCaptureStats)
    summary: AccessibilitySummary | None = None
    raw_format: AccessibilityRawFormat = AccessibilityRawFormat.NONE
    raw_payload: str = ""
    error: str = ""

    @classmethod
    def failed(
        cls,
        *,
        status: AccessibilityCaptureStatus,
        platform_name: str,
        source: AccessibilitySourceKind | None,
        scope: AccessibilityScope,
        error: str,
        capture_ms: float = 0.0,
    ) -> "AccessibilitySnapshot":
        """Build a structured failed/empty snapshot.

        Args:
            status: Failure or empty capture status.
            platform_name: Source platform/backend name.
            source: Accessibility backend source.
            scope: Capture scope.
            error: Capture error message.
            capture_ms: Capture duration in milliseconds.

        Returns:
            Failed accessibility snapshot.
        """

        return cls(
            status=status,
            platform_name=platform_name,
            source=source,
            scope=scope,
            stats=AccessibilityCaptureStats(capture_ms=capture_ms),
            error=error,
        )


class AccessibilityDiffSummary(BaseModel):
    """Basic structural diff between two consecutive snapshots.

    Attributes:
        added_nodes: Number of nodes added.
        removed_nodes: Number of nodes removed.
        changed_nodes: Number of nodes changed.
        focus_changed: Whether focused node path changed.
        previous_focused_path: Focus path from the prior snapshot.
        current_focused_path: Focus path from the current snapshot.
    """

    model_config: ClassVar[ConfigDict] = ConfigDict(extra="ignore")

    added_nodes: int = 0
    removed_nodes: int = 0
    changed_nodes: int = 0
    focus_changed: bool = False
    previous_focused_path: list[str] = Field(default_factory=list)
    current_focused_path: list[str] = Field(default_factory=list)


class AccessibilityStepRecord(BaseModel):
    """Compact step log record for one accessibility capture.

    Attributes:
        status: Capture status.
        source: Accessibility backend source.
        scope: Capture scope.
        artifact_path: Path to the structured accessibility artifact.
        raw_artifact_path: Path to the raw accessibility artifact.
        node_count: Number of nodes captured.
        max_depth: Deepest captured tree depth.
        truncated: Whether capture limits truncated the tree.
        root_role: Root node role.
        root_name: Root node name.
        focused_role: Focused node role.
        focused_name: Focused node name.
        focused_path: Path to the focused node.
        actionable_count: Number of actionable nodes.
        capture_ms: Capture duration in milliseconds.
        diff_mode: Diff mode used for this record.
        diff: Optional diff from the prior capture.
        error: Capture error message.
    """

    model_config: ClassVar[ConfigDict] = ConfigDict(extra="ignore")

    status: AccessibilityCaptureStatus
    source: AccessibilitySourceKind | None = None
    scope: AccessibilityScope = AccessibilityScope.DESKTOP
    artifact_path: str = ""
    raw_artifact_path: str = ""
    node_count: int = 0
    max_depth: int = 0
    truncated: bool = False
    root_role: str = ""
    root_name: str = ""
    focused_role: str = ""
    focused_name: str = ""
    focused_path: list[str] = Field(default_factory=list)
    actionable_count: int = 0
    capture_ms: float = 0.0
    diff_mode: AccessibilityDiffMode = AccessibilityDiffMode.NONE
    diff: AccessibilityDiffSummary | None = None
    error: str = ""
