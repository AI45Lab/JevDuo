"""Typed Web artifact IR used by trace replay and evaluators."""

from __future__ import annotations

from enum import Enum

from pydantic import BaseModel, Field

from northcua.core.json_types import JsonObject


class WebObservationArtifactRole(str, Enum):
    """Stable roles for Web current-observation artifacts."""

    CURRENT = "current"
    TERMINAL_VERIFICATION = "round_end_verify"


class WebToolArtifactImageKind(str, Enum):
    """Image variants that may be attached to a Web tool result."""

    RAW = "raw"
    ACTION_OVERLAY = "action_overlay"
    SOM = "som"
    NONE = "none"


class WebToolArtifactAttributionSource(str, Enum):
    """How a tool-result image was attributed to a Web action."""

    EXPLICIT_PATH = "explicit_path"
    IMAGE_HASH = "image_hash"
    NONE = "none"


class WebTerminalVerificationStatus(str, Enum):
    """Outcome of the terminal current-observation verification hook."""

    CAPTURED = "captured"
    FAILED = "failed"
    SKIPPED = "skipped"


class WebObservationArtifact(BaseModel):
    """Current page artifact aligned with desktop step observation semantics."""

    role: WebObservationArtifactRole = WebObservationArtifactRole.CURRENT
    screenshot_path: str = ""
    annotated_screenshot_path: str = ""
    dom_snapshot_path: str = ""
    dom_snapshot_json_path: str = ""
    page_url: str = ""
    page_title: str = ""
    observation_id: str = ""
    settled: bool = True
    settle_reason: str = "stable"
    ref_count: int = 0
    viewport: JsonObject = Field(default_factory=dict)
    metadata: JsonObject = Field(default_factory=dict)


class WebToolResultArtifact(BaseModel):
    """Tool-result visual evidence for one Web action."""

    tool_name: str = ""
    tool_call_id: str = ""
    action: str = ""
    status: str = ""
    is_error: bool = False
    text: str = ""
    selected_image_kind: WebToolArtifactImageKind = WebToolArtifactImageKind.NONE
    raw_image_path: str = ""
    action_overlay_image_path: str = ""
    som_image_path: str = ""
    selected_image_path: str = ""
    post_action_observation_summary: str = ""
    page_url: str = ""
    page_title: str = ""
    som_metadata: JsonObject = Field(default_factory=dict)
    metadata: JsonObject = Field(default_factory=dict)


class WebToolResultArtifactHint(BaseModel):
    """Stable image attribution hint for one Web action result."""

    action_index: int
    tool_name: str = ""
    tool_call_id: str = ""
    source: WebToolArtifactAttributionSource = WebToolArtifactAttributionSource.NONE
    selected_image_kind: WebToolArtifactImageKind = WebToolArtifactImageKind.NONE
    raw_image_path: str = ""
    action_overlay_image_path: str = ""
    som_image_path: str = ""
    selected_image_path: str = ""
    metadata: JsonObject = Field(default_factory=dict)


class WebToolImageArtifactEvent(BaseModel):
    """Step-local event emitted when a Web tool materializes an image artifact."""

    sequence: int
    invocation_id: int
    action: str = ""
    raw_image_path: str = ""
    action_overlay_image_path: str = ""
    som_image_path: str = ""
    selected_image_path: str = ""
    selected_image_kind: WebToolArtifactImageKind = WebToolArtifactImageKind.NONE
    source: WebToolArtifactAttributionSource = WebToolArtifactAttributionSource.EXPLICIT_PATH
    metadata: JsonObject = Field(default_factory=dict)


class WebToolInvocationArtifactEvent(BaseModel):
    """Step-local event emitted for every Web tool invocation.

    Image artifacts are optional for a tool call. Keeping an invocation ledger
    lets replay/evaluator attribution bind images to the exact call that
    produced them without falling back to same-action ordering guesses.
    """

    sequence: int
    invocation_id: int
    action: str = ""
    metadata: JsonObject = Field(default_factory=dict)


class WebTerminalVerificationRecord(BaseModel):
    """Terminal verification metadata for the final Web step."""

    status: WebTerminalVerificationStatus
    reason: str = ""
    source: str = ""
    runtime_mode: str = ""
    previous_screenshot_path: str = ""
    screenshot_path: str = ""
    previous_dom_snapshot_path: str = ""
    dom_snapshot_path: str = ""
    previous_dom_snapshot_json_path: str = ""
    dom_snapshot_json_path: str = ""
    page_url: str = ""
    page_title: str = ""
    error: str = ""
    metadata: JsonObject = Field(default_factory=dict)
