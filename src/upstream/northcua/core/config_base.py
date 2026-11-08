"""Foundational NorthCUA application configuration models."""

from __future__ import annotations

from typing import Literal

from pydantic import AliasChoices, BaseModel, ConfigDict, Field, SecretStr, model_validator

from .enums import CoordinateSpace, RuntimeMode
from .json_types import JsonObject

CONTEXT_SUMMARY_TRACE_LOG_NAME = "context_summary_llm_trace.jsonl"
CONTEXT_COMPACTION_LOG_NAME = "context_compaction.jsonl"
CONTEXT_CHECKPOINT_DIR_NAME = "context"


class PromptPolicyConfig(BaseModel):
    """Prompt/image window policy knobs.

    Fields:
        Controls history size, screenshot inclusion and summary language.
    """

    model_config = ConfigDict(extra="forbid")

    history_window_rounds: int = 8
    # Total prompt image budget, including Web Set-of-Mark images.
    screenshot_window_images: int = 3
    # Web refs are latest-only for action safety: current observations may
    # expose refs; >=1 means only the latest retained observation keeps
    # actionable refs, and older retained observations are expired.
    ref_snapshot_window_observations: int = 1
    history_action_trace_enabled: bool = True
    history_action_trace_include_initial: bool = True
    history_action_trace_badges: bool = True
    history_action_trace_max_badge_text: int = 32
    summary_language_matches_query: bool = True
    include_initial_observation_image: bool = True
    include_subsequent_observation_images: bool = True


class ArtifactConfig(BaseModel):
    """Disk layout for traces and screenshots.

    Fields:
        Defines artifact root and log filenames for runtime, LLM, step, and evaluation records.
    """

    root_dir: str = "logs"
    step_log_name: str = "steps.jsonl"
    llm_trace_log_name: str = "llm_trace.jsonl"
    evaluation_log_name: str = "evaluation.jsonl"
    round_evaluation_log_name: str = "round_evaluations.jsonl"
    session_evaluation_rollup_name: str = "session_evaluation.json"
    runtime_log_name: str = "runtime.jsonl"
    eval_runtime_log_name: str = "eval_runtime.jsonl"


class WebSocketServerConfig(BaseModel):
    """Long-lived websocket transport settings.

    Fields:
        Defines websocket host, port, runtime mode, heartbeat cadence, and command polling timeouts.
    """

    host: str = "127.0.0.1"
    port: int = 8765
    runtime_mode: RuntimeMode = RuntimeMode.LOCAL
    heartbeat_interval_seconds: float = 10.0
    command_result_timeout_seconds: float = 60.0
    command_poll_interval_seconds: float = 0.1


class ExecutionConfig(BaseModel):
    """NorthCUA execution/session defaults around NexAU runtime.

    Fields:
        Defines user/session backend, tracer flags, Langfuse metadata, and per-round iteration limits.
    """

    user_id: str = "northcua_user"
    session_backend: str = "memory"
    enable_in_memory_tracer: bool = True
    enable_langfuse_tracer: bool = False
    langfuse_public_key: SecretStr | None = None
    langfuse_secret_key: SecretStr | None = None
    langfuse_host: str | None = Field(
        default=None,
        validation_alias=AliasChoices("langfuse_host", "langfuse_base_url"),
    )
    langfuse_tags: list[str] = Field(default_factory=list)
    langfuse_metadata: JsonObject = Field(default_factory=dict)
    experiment_id: str = ""
    experiment_tags: list[str] = Field(default_factory=list)
    max_agent_iterations_per_round: int = 8
    no_tool_call_attempts: Literal[1, 3] = 1
    # Provider-response recovery is separate from the text-only retry policy.
    model_response_recovery: bool = False

    @model_validator(mode="after")
    def reject_combined_response_retry_policies(self) -> ExecutionConfig:
        if self.model_response_recovery and self.no_tool_call_attempts != 1:
            raise ValueError("model_response_recovery and no_tool_call_attempts=3 cannot be combined")
        return self


class CoordinatePolicyConfig(BaseModel):
    """Configured model-facing coordinate interpretation policy.

    Fields:
        input_space: Coordinate space expected from model/tool input.
        clamp_to_bounds: Whether mapped coordinates should be clamped.
    """

    input_space: CoordinateSpace
    clamp_to_bounds: bool = False


def _default_desktop_coordinate_policy() -> CoordinatePolicyConfig:
    """Return the default desktop coordinate policy."""

    return CoordinatePolicyConfig(input_space=CoordinateSpace.IMAGE_PIXELS)
