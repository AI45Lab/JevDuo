"""Typed observation contracts for web-agent page state."""

from __future__ import annotations

from pydantic import BaseModel, Field

from northcua.core.json_types import JsonObject


class WebElementRef(BaseModel):
    """One stable snapshot ref exposed by the browser backend for this step.

    Attributes:
        ref_id: Raw element id emitted by the browser substrate.
        role: Accessibility role when available.
        name: Accessible name or label.
        text: Visible text excerpt associated with the element.
        tag: DOM tag name when the substrate exposes it.
        attributes: Additional low-cardinality DOM/accessibility metadata.
    """

    ref_id: str = Field(min_length=1)
    role: str = ""
    name: str = ""
    text: str = ""
    tag: str = ""
    attributes: JsonObject = Field(default_factory=dict)

    @property
    def model_ref(self) -> str:
        """Return the canonical ref spelling expected by web tools.

        Returns:
            Ref id prefixed with ``@`` so prompts and tool calls use one format.
        """

        return self.ref_id if self.ref_id.startswith("@") else f"@{self.ref_id}"


class WebSnapshotObservation(BaseModel):
    """DOM/snapshot observation normalized from browser backend output.

    Attributes:
        url: Current browser page URL.
        title: Current page title.
        origin: Fallback origin or location string when URL is absent.
        snapshot: Compact text snapshot shown to the model by default.
        elements: Interactive element refs parsed from the snapshot payload.
        raw: Original normalized JSON payload for diagnostics.
    """

    url: str = ""
    title: str = ""
    origin: str = ""
    snapshot: str = ""
    elements: list[WebElementRef] = Field(default_factory=list)
    raw: JsonObject = Field(default_factory=dict)

    @property
    def summary(self) -> str:
        """Return a compact human-readable state summary.

        Returns:
            Title and URL suitable for terminal panels and trace summaries.
        """

        title = self.title or "(untitled)"
        url = self.url or self.origin or "(blank)"
        return f"{title} - {url}"


class WebBoundingBox(BaseModel):
    """One rectangle in viewport CSS or screenshot-pixel coordinates."""

    x: float
    y: float
    width: float
    height: float


class WebScreenshotMark(BaseModel):
    """One Set-of-Mark label associated with a screenshot target."""

    ref: str
    label: str
    source_ref: str = ""
    role: str = ""
    name: str = ""
    bbox_css: WebBoundingBox
    bbox_image: WebBoundingBox
    visible: bool = True
    enabled: bool = True


class WebScreenshotAnnotation(BaseModel):
    """Set-of-Mark annotation metadata attached to an annotated screenshot."""

    kind: str = "set_of_mark"
    coordinate_frame: str = "viewport_css"
    image_coordinate_frame: str = "screenshot_pixels"
    marks: list[WebScreenshotMark] = Field(default_factory=list)
    truncated: bool = False
    omitted_count: int = 0
    omitted_reason_counts: dict[str, int] = Field(default_factory=dict)
    marked_count: int = 0
    visible_interactive_count: int = 0
    max_marks: int = 150
    max_box_lookup: int = 512
    source_screenshot_path: str = ""
    overlay_screenshot_path: str = ""


class WebScreenshotObservation(BaseModel):
    """Screenshot artifact metadata.

    Screenshots can come from a file path, a data URL, or both. Prompt policy
    decides whether the image is shown to the model; artifact policy still
    persists it when available.

    Attributes:
        path: Local screenshot path, often produced by the substrate.
        data_url: Inline data URL used for prompt projection when requested.
        width: Optional pixel width.
        height: Optional pixel height.
        annotation: Optional visual annotation metadata for marked screenshots.
        raw: Original substrate payload for diagnostics.
    """

    path: str = ""
    data_url: str = ""
    width: int | None = None
    height: int | None = None
    annotation: WebScreenshotAnnotation | None = None
    raw: JsonObject = Field(default_factory=dict)


class WebObservationPagePayload(BaseModel):
    """Model-visible active page metadata bundled with a fresh observation."""

    url: str = ""
    title: str = ""


class WebObservationSnapshotPayload(BaseModel):
    """Model-visible snapshot text and refs bundled after a web action."""

    text: str = ""
    refs: list[JsonObject] = Field(default_factory=list)
    ref_count: int = 0


class WebObservationScreenshotPayload(BaseModel):
    """Model-visible screenshot/SoM metadata without local or device paths."""

    kind: str = "set_of_mark"
    width: int | None = None
    height: int | None = None
    image_attached: bool = False
    image_omitted_reason: str = ""
    annotation: JsonObject = Field(default_factory=dict)


class WebObservationBundle(BaseModel):
    """Model-visible fresh web observation bundled with tool results.

    The bundle intentionally excludes local, remote, and device screenshot
    paths. Prompt image projection may attach a separate data URL image block,
    while this JSON remains text-safe and stable.
    """

    observation_id: str
    settled: bool = True
    settle_reason: str = "stable"
    page: WebObservationPagePayload = Field(default_factory=WebObservationPagePayload)
    snapshot: WebObservationSnapshotPayload = Field(default_factory=WebObservationSnapshotPayload)
    screenshot: WebObservationScreenshotPayload = Field(default_factory=WebObservationScreenshotPayload)


class WebObservationContext(BaseModel):
    """Prompt-facing page state for a single web-agent step.

    Attributes:
        snapshot: DOM/accessibility snapshot for the active page.
        screenshot: Optional screenshot metadata for visual confirmation.
        recent_action_result: Structured result from the previous web action.
    """

    snapshot: WebSnapshotObservation
    screenshot: WebScreenshotObservation | None = None
    recent_action_result: JsonObject | None = None
    observation_id: str = ""
    settled: bool = True
    settle_reason: str = "stable"

    @property
    def page_url(self) -> str:
        """Return the active page URL.

        Returns:
            Current URL, falling back to the snapshot origin when URL is empty.
        """

        return self.snapshot.url or self.snapshot.origin

    @property
    def page_title(self) -> str:
        """Return the active page title.

        Returns:
            Current page title from the latest snapshot.
        """

        return self.snapshot.title
