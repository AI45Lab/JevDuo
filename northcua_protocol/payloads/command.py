"""Command request/result payloads for the v1 controller protocol."""

from __future__ import annotations

from typing import Literal

from pydantic import Field, model_validator

from northcua_protocol.artifacts import ArtifactRef
from northcua_protocol.base import ProtocolModel
from northcua_protocol.capabilities import JsonObject
from northcua_protocol.state import CommandState

AGENTKIT_SETTINGS_PARAM = "__northcua_agentkit_settings"
COMMAND_RUNTIME_SIDE_CHANNEL_KEYS = frozenset({AGENTKIT_SETTINGS_PARAM})


class WebAgentPostActionSettleRuntimeSettings(ProtocolModel):
    """Safe post-action settle settings allowed over the runtime side channel."""

    enabled: bool | None = None
    profile: Literal["fast", "balanced", "conservative"] | None = None
    debounce_ms: int | None = Field(default=None, ge=0)
    max_wait_ms: int | None = Field(default=None, ge=0)
    sample_interval_ms: int | None = Field(default=None, ge=0)
    snapshot_stable_samples: int | None = Field(default=None, ge=1)
    bbox_stable_samples: int | None = Field(default=None, ge=1)
    bbox_candidate_limit: int | None = Field(default=None, ge=1)
    load_wait_timeout_ms: int | None = Field(default=None, ge=0)
    final_bbox_verify: bool | None = None


class WebAgentRefAliasRuntimeSetting(ProtocolModel):
    """One model-visible web ref alias carried to a remote device session."""

    ref: str = Field(min_length=1)
    source_ref: str = Field(min_length=1)


class WebAgentRuntimeSettings(ProtocolModel):
    """Safe Web-agent session settings carried from controller to device.

    This contract intentionally excludes backend selection and executable-path
    fields. A remote command may tune browser/session behavior for the current
    evaluated run, but it must not pick an arbitrary backend or binary on a
    per-command basis.
    """

    playwright_browser: Literal["chromium", "firefox", "webkit"] | None = None
    browser_channel: Literal["chrome", "msedge"] | None = None
    headed: bool | None = None
    max_steps: int | None = Field(default=None, ge=1)
    command_timeout_seconds: float | None = Field(default=None, gt=0)
    command_retry_attempts: int | None = Field(default=None, ge=1)
    command_retry_backoff_seconds: float | None = Field(default=None, ge=0)
    action_preflight_enabled: bool | None = None
    action_preflight_timeout_ms: int | None = Field(default=None, ge=0)
    action_preflight_hit_test: bool | None = None
    screenshot_timeout_seconds: float | None = Field(default=None, gt=0)
    snapshot_interactive: bool | None = None
    snapshot_compact: bool | None = None
    snapshot_depth: int | None = Field(default=None, ge=0)
    snapshot_max_refs: int | None = Field(default=None, ge=1)
    snapshot_max_visited: int | None = Field(default=None, ge=1)
    snapshot_eval_timeout_seconds: float | None = Field(default=None, gt=0)
    snapshot_fallback_max_refs: int | None = Field(default=None, ge=1)
    snapshot_visible_text_max_chars: int | None = Field(default=None, ge=0)
    post_action_wait_ms: int | None = Field(default=None, ge=0)
    prompt_history_strategy: Literal["desktop", "append"] | None = None
    allowed_url_schemes: tuple[str, ...] | list[str] | None = None
    allow_file_urls: bool | None = None
    allow_localhost_urls: bool | None = None
    allow_private_network_urls: bool | None = None
    max_screenshot_bytes: int | None = Field(default=None, ge=1)
    initial_observation_mode: Literal["none", "snapshot", "som"] | None = None
    post_action_observation_mode: Literal["none", "snapshot", "som"] | None = None
    readonly_post_observation: bool | None = None
    persist_pre_action_observation: bool | None = None
    persist_post_action_observation: bool | None = None
    post_action_settle: WebAgentPostActionSettleRuntimeSettings | None = None
    ref_alias_observation_id: str | None = None
    ref_alias_epoch: int | None = Field(default=None, ge=0)
    ref_aliases: list[WebAgentRefAliasRuntimeSetting] | None = None


def sanitize_agentkit_settings_side_channel(value: object) -> JsonObject:
    """Validate and normalize AgentKit runtime settings.

    The wire side channel accepts a flat Web-agent settings object. The
    normalized value is the safe subset consumed by device-side session
    adapters.
    """

    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ValueError(f"{AGENTKIT_SETTINGS_PARAM} must be an object")
    try:
        settings = WebAgentRuntimeSettings.model_validate(value)
    except Exception as exc:
        raise ValueError(f"invalid {AGENTKIT_SETTINGS_PARAM}: {exc}") from exc
    return settings.model_dump(mode="json", exclude_none=True)


def sanitize_command_runtime_side_channels(parameters: JsonObject) -> JsonObject:
    """Return validated runtime-only side-channel parameters."""

    side_channels: JsonObject = {}
    if AGENTKIT_SETTINGS_PARAM in parameters:
        side_channels[AGENTKIT_SETTINGS_PARAM] = sanitize_agentkit_settings_side_channel(
            parameters[AGENTKIT_SETTINGS_PARAM]
        )
    return side_channels


class CommandRequest(ProtocolModel):
    """Controller -> device executable command.

    Attributes:
        command_id: Stable command id used for idempotency.
        idempotency_key: Request hash/key controlled by the controller.
        session_id: Session that owns the command.
        lease_id: Lease authorizing the command.
        device_id: Target device id.
        tool_id: Protocol-facing tool id from the advertised surface.
        parameters: JSON object passed to the runtime adapter.
        metadata: Runtime metadata inherited from the session lease.
        timeout_seconds: Per-command timeout budget.
        attempt: Retry attempt number.
        lease_generation: Lease generation expected by the device.
        state: Initial command lifecycle state.
    """

    command_id: str
    idempotency_key: str
    session_id: str
    lease_id: str
    device_id: str
    tool_id: str
    parameters: JsonObject = Field(default_factory=dict)
    metadata: JsonObject = Field(default_factory=dict)
    timeout_seconds: float = 10.0
    attempt: int = 1
    lease_generation: int = 1
    state: CommandState = CommandState.SENT


class CommandResult(ProtocolModel):
    """Device -> controller command result.

    Attributes:
        command_id: Command id being answered.
        session_id: Session that owns the command.
        lease_id: Lease used for authority checks.
        device_id: Reporting device id.
        lease_generation: Lease generation echoed from the request.
        status: Terminal command state.
        result: Small structured command output.
        artifacts: Artifact refs for screenshots, trees, or larger payloads.
        error: Optional failure reason.
    """

    command_id: str
    session_id: str
    lease_id: str
    device_id: str
    lease_generation: int = 1
    status: CommandState
    result: JsonObject | str | None = None
    artifacts: list[ArtifactRef] = Field(default_factory=list)
    error: str | None = None

    @model_validator(mode="after")
    def _validate_terminal_status(self) -> "CommandResult":
        """Ensure device results carry a terminal command state."""

        if self.status not in {
            CommandState.SUCCEEDED,
            CommandState.FAILED,
            CommandState.TIMED_OUT,
            CommandState.CANCELLED,
        }:
            raise ValueError("CommandResult.status must be terminal")
        return self


class CommandCancel(ProtocolModel):
    """Controller -> device command cancellation request.

    Attributes:
        command_id: Command to cancel.
        session_id: Session that owns the command.
        lease_id: Lease used for authority checks.
        device_id: Target device id.
        reason: Human-readable cancellation reason.
    """

    command_id: str
    session_id: str
    lease_id: str
    device_id: str
    reason: str = ""


__all__ = [
    "AGENTKIT_SETTINGS_PARAM",
    "COMMAND_RUNTIME_SIDE_CHANNEL_KEYS",
    "CommandCancel",
    "CommandRequest",
    "CommandResult",
    "WebAgentRuntimeSettings",
    "WebAgentRefAliasRuntimeSetting",
    "WebAgentPostActionSettleRuntimeSettings",
    "sanitize_agentkit_settings_side_channel",
    "sanitize_command_runtime_side_channels",
]
