"""NorthCUA application-side configuration models."""

from __future__ import annotations

from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

from northcua.agentkernel.context.domain.config import ContextManagementConfig

from .config_base import (
    ArtifactConfig,
    CoordinatePolicyConfig,
    ExecutionConfig,
    PromptPolicyConfig,
    WebSocketServerConfig,
    _default_desktop_coordinate_policy,
)
from .enums import (
    AccessibilityDiffMode,
    AccessibilityScope,
    EvaluationMode,
    EvaluationSchedule,
    EvaluationSelectorStrategy,
    EvaluationToolMode,
    EvaluationToolPlacement,
)
from .errors import ConfigValidationError
from .json_types import JsonObject

_EVALUATOR_KIND_PATTERN = r"^[A-Za-z_][A-Za-z0-9_.-]*$"


class DesktopPolicyConfig(BaseModel):
    """Desktop-native behavior policy.

    Fields:
        Defines backend selection, screen capture scope, coordinates, and OSWorld
        remote-desktop settings.
    """

    model_config = ConfigDict(extra="forbid")

    backend: Literal["native", "osworld"] = "native"
    primary_screen_only: bool = True
    coordinate_policy: CoordinatePolicyConfig = Field(default_factory=_default_desktop_coordinate_policy)
    osworld: "OSWorldDesktopConfig" = Field(default_factory=lambda: OSWorldDesktopConfig())


class OSWorldDesktopConfig(BaseModel):
    """OSWorld remote desktop backend configuration.

    Fields:
        vm_ip: OSWorld VM HTTP service host.
        server_port: OSWorld VM HTTP service port.
        command_timeout_seconds: Timeout for remote Python/action calls.
        screenshot_timeout_seconds: Timeout for screenshot fetches.
        retry_times: Number of HTTP retry attempts.
        retry_interval_seconds: Delay between retry attempts.
        enable_accessibility: Whether to expose OSWorld accessibility capture.
    """

    vm_ip: str = "127.0.0.1"
    server_port: int = 5000
    command_timeout_seconds: float = 120.0
    screenshot_timeout_seconds: float = 10.0
    retry_times: int = 3
    retry_interval_seconds: float = 5.0
    enable_accessibility: bool = True


class WebPostActionSettleConfig(BaseModel):
    """Post-action browser settling policy for web-agent observations.

    Fields:
        Controls debounce, maximum wait, and the number of consecutive stable
        snapshot/bbox samples required before the post-action SoM observation
        is considered aligned with the page.
    """

    enabled: bool = True
    profile: Literal["fast", "balanced", "conservative"] = "balanced"
    debounce_ms: int = Field(default=150, ge=0)
    max_wait_ms: int = Field(default=3000, ge=0)
    sample_interval_ms: int = Field(default=150, ge=0)
    snapshot_stable_samples: int = Field(default=2, ge=1)
    bbox_stable_samples: int = Field(default=2, ge=1)
    bbox_candidate_limit: int = Field(default=60, ge=1)
    load_wait_timeout_ms: int = Field(default=0, ge=0)
    final_bbox_verify: bool = True


class WebAgentConfig(BaseModel):
    """Generic web-agent runtime defaults.

    Fields:
        Defines browser visibility, step budget, command/screenshot timeouts,
        snapshot policy, screenshot policy, and post-action waits. The web
        action backend is Playwright-only.
    """

    model_config = ConfigDict(extra="forbid")

    playwright_browser: Literal["chromium", "firefox", "webkit"] = "chromium"
    browser_channel: Literal["chrome", "msedge"] | None = None
    browser_executable_path: str | None = None
    headed: bool = False
    max_steps: int = Field(default=20, ge=1)
    command_timeout_seconds: float = Field(default=30.0, gt=0)
    command_retry_attempts: int = Field(default=2, ge=1)
    command_retry_backoff_seconds: float = Field(default=0.25, ge=0)
    action_preflight_enabled: bool = True
    action_preflight_timeout_ms: int = Field(default=750, ge=0)
    action_preflight_hit_test: bool = True
    screenshot_timeout_seconds: float = Field(default=4.0, gt=0)
    snapshot_interactive: bool = True
    snapshot_compact: bool = True
    snapshot_depth: int = Field(default=6, ge=0)
    snapshot_max_refs: int | None = Field(default=None, ge=1)
    snapshot_max_visited: int | None = Field(default=None, ge=1)
    snapshot_eval_timeout_seconds: float = Field(default=8.0, gt=0)
    snapshot_fallback_max_refs: int = Field(default=512, ge=1)
    snapshot_visible_text_max_chars: int = Field(default=12_000, ge=0)
    post_action_wait_ms: int = Field(default=500, ge=0)
    prompt_history_strategy: Literal["desktop", "append"] = "desktop"
    allowed_url_schemes: tuple[str, ...] = ("http", "https", "about")
    allow_file_urls: bool = False
    allow_localhost_urls: bool = True
    allow_private_network_urls: bool = False
    max_screenshot_bytes: int = Field(default=10_000_000, ge=1)
    initial_observation_mode: Literal["none", "snapshot", "som"] = "som"
    post_action_observation_mode: Literal["none", "snapshot", "som"] = "som"
    post_action_observation_timeout_seconds: float = Field(default=15.0, gt=0)
    readonly_post_observation: bool = False
    persist_pre_action_observation: bool = True
    persist_post_action_observation: bool = True
    som_max_marks: int = Field(default=150, ge=1)
    som_max_box_lookup: int = Field(default=512, ge=1)
    som_min_bbox_css_px: float = Field(default=3.0, ge=0)
    som_empty_ref_retry_attempts: int = Field(default=1, ge=0)
    post_action_settle: WebPostActionSettleConfig = Field(default_factory=WebPostActionSettleConfig)


class AgentKitSkillConfig(BaseModel):
    """Per-agent skill overrides for the AgentKit/NexAU bridge.

    Fields:
        enabled: Whether AgentKit merges spec/config skill paths into the
            loaded NexAU agent config.
        paths: Folder-based skill paths. Relative paths resolve against the
            app config directory.
        asset_tools_enabled: Whether read-only skill asset tools are exposed
            to this agent's model tool surface.
        asset_read_max_bytes: Maximum bytes returned by one asset read.
        allowed_file_extensions: File suffixes that may be returned as text;
            empty string means extensionless files.
        allow_binary_summary: Whether binary/disallowed files may return only
            checksum and size metadata.
        allow_missing: Whether missing configured skill paths are skipped.
    """

    enabled: bool = True
    paths: list[str] = Field(default_factory=list)
    asset_tools_enabled: bool = False
    asset_read_max_bytes: int = Field(default=64 * 1024, gt=0)
    allowed_file_extensions: list[str] = Field(default_factory=list)
    allow_binary_summary: bool = True
    allow_missing: bool = False


class AgentKitAgentConfig(BaseModel):
    """Generic per-agent low-code runtime config.

    Fields:
        agent_config_path: Optional NexAU agent config override for this agent.
        settings: Agent-specific settings payload preserved for the agent runtime.
        evaluator: Optional evaluator override payload for this agent kind.
        skills: AgentKit/NexAU skill path and asset-tool overrides.
        enabled: Reserved deployment toggle for AgentKit-aware surfaces.
        cli: Reserved generated CLI metadata overrides.
    """

    agent_config_path: str | None = None
    settings: JsonObject = Field(default_factory=dict)
    evaluator: JsonObject = Field(default_factory=dict)
    skills: AgentKitSkillConfig = Field(default_factory=AgentKitSkillConfig)
    enabled: bool = True
    cli: JsonObject = Field(default_factory=dict)


class ProbeToolCallConfig(BaseModel):
    """One internal runtime probe tool call.

    Fields:
        server: Runtime server name.
        tool: Runtime tool name.
        arguments: Tool arguments.
        adapter: Adapter handling the tool call.
    """

    server: str
    tool: str
    arguments: JsonObject = Field(default_factory=dict)
    adapter: str


class ProbeChainConfig(BaseModel):
    """Named sequence of internal runtime probe calls.

    Fields:
        steps: Ordered internal probe tool calls.
    """

    steps: list[ProbeToolCallConfig] = Field(default_factory=list)


class ObservationConfig(BaseModel):
    """Observation/state collection configuration.

    Fields:
        a11y: Accessibility observation policy.
    """

    model_config = ConfigDict(extra="forbid")

    a11y: "AccessibilityObservationConfig" = Field(default_factory=lambda: AccessibilityObservationConfig())


class AccessibilityObservationConfig(BaseModel):
    """Desktop accessibility collection and persistence policy.

    Fields:
        Defines accessibility tools, scope, diffing, capture limits, raw artifact persistence, timeout, and fail-open mode.
    """

    enabled: bool = False
    server_name: str = "DesktopNativeA11yExecutor"
    capture_tool_name: str = "capture_accessibility_tree"
    capabilities_tool_name: str = "get_accessibility_capabilities"
    scope: AccessibilityScope = AccessibilityScope.DESKTOP
    diff_mode: AccessibilityDiffMode = AccessibilityDiffMode.BASIC
    max_depth: int = 50
    max_children_per_node: int = 256
    max_nodes: int = 5000
    include_raw_payload: bool = False
    persist_raw_artifact: bool = False
    capture_timeout_seconds: float = 3.0
    fail_open: bool = True


class EvaluationBundleConfig(BaseModel):
    """Bundle-building knobs for the round evaluator.

    Fields:
        Defines prior summaries, accessibility, runtime event, and reasoning inclusion for evaluator bundles.
    """

    include_prior_round_summaries: int = 3
    include_a11y: bool = True
    include_runtime_events: bool = True
    include_llm_reasoning_summary: bool = True


class EvaluationSelectorConfig(BaseModel):
    """Evidence selection policy for one round.

    Fields:
        strategy: Evidence selection strategy.
        max_images: Maximum images selected.
        max_non_image_refs: Maximum accessibility/runtime refs retained after
            bundle construction.
        top_k_per_criterion: Top evidence count per criterion.
    """

    strategy: EvaluationSelectorStrategy = EvaluationSelectorStrategy.ALL_OR_TOPK
    max_images: int = Field(default=8, ge=0)
    max_non_image_refs: int = Field(default=16, ge=0)
    top_k_per_criterion: int = Field(default=2, ge=0)


class EvaluationJudgeConfig(BaseModel):
    """Judge aggregation and confidence thresholds.

    Fields:
        Defines vote count, success threshold, uncertainty threshold, and evidence-reference requirements.
    """

    self_consistency_votes: int = Field(default=1, ge=1)
    success_threshold: float = Field(default=0.8, ge=0.0, le=1.0)
    uncertainty_threshold: float = Field(default=0.45, ge=0.0, le=1.0)
    require_evidence_refs: bool = True


class EvaluationToolConfig(BaseModel):
    """Tool policy for the evaluator.

    Fields:
        enabled: Whether evaluator tools are enabled.
        mode: Evaluator tool mode.
        allowed_placements: Tool placements the evaluator may expose.
        allow_client_live_probe: Whether client-side live probes may run.
        allow_client_mutating_tools: Whether client-side mutating tools may run.
        client_probe_tools: Standard client probe tool names to expose.
        client_gui_tools: Standard client GUI tool names to expose.
    """

    enabled: bool = True
    mode: EvaluationToolMode = EvaluationToolMode.DETERMINISTIC_ONLY
    allowed_placements: list[EvaluationToolPlacement] = Field(
        default_factory=lambda: [
            EvaluationToolPlacement.SERVER_ARTIFACT,
            EvaluationToolPlacement.CLIENT_PROBE,
            EvaluationToolPlacement.FINALIZER,
        ]
    )
    allow_client_live_probe: bool = False
    allow_client_mutating_tools: bool = False
    client_probe_tools: list[str] = Field(default_factory=list)
    client_gui_tools: list[str] = Field(default_factory=list)


class EvaluationEvidenceConfig(BaseModel):
    """Default evidence policy exposed to AgentKit evaluator specs.

    Fields:
        screenshot_max: Maximum selected image evidence refs.
        include_initial_screenshot: Whether the earliest screenshot is pinned.
        include_last_screenshot: Whether the latest screenshot is pinned.
        include_terminal_verification: Whether verification screenshots are pinned.
    """

    screenshot_max: int = Field(default=4, ge=0)
    include_initial_screenshot: bool = True
    include_last_screenshot: bool = True
    include_terminal_verification: bool = True


class EvaluationOutputConfig(BaseModel):
    """Artifact/log names for evaluator outputs.

    Fields:
        round_log_name: Round evaluation JSONL filename.
        session_rollup_name: Session evaluation rollup filename.
    """

    round_log_name: str = "round_evaluations.jsonl"
    session_rollup_name: str = "session_evaluation.json"


class EvaluationPromptBudgetConfig(BaseModel):
    """Independent prompt budget for terminal evaluator projection."""

    target_tokens: int = Field(default=60_000, ge=1_024)
    hard_tokens: int = Field(default=75_000, ge=1_024)
    final_full_steps: int = Field(default=20, ge=1)
    chars_per_token: float = Field(default=4.0, gt=0.0)

    @model_validator(mode="after")
    def _validate_budget_order(self) -> "EvaluationPromptBudgetConfig":
        if self.hard_tokens < self.target_tokens:
            raise ValueError("evaluation prompt hard_tokens must be >= target_tokens")
        return self


class EvaluationConfig(BaseModel):
    """Round/session evaluation runtime policy.

    Fields:
        Defines evaluation enablement, schedule, mode, fail-open behavior, budgets, timeouts, bundle, selector, tools,
        judge, and outputs.
    """

    enabled: bool = False
    default_evaluator_kind: str = Field(
        default="desktop_round_completion",
        min_length=1,
        pattern=_EVALUATOR_KIND_PATTERN,
    )
    schedule: EvaluationSchedule = EvaluationSchedule.AFTER_ROUND_ASYNC
    mode: EvaluationMode = EvaluationMode.ARTIFACT_AWARE
    fail_open: bool = True
    protect_agent_terminal: bool = True
    evaluator_only_retry_attempts: int = Field(default=1, ge=0)
    max_steps: int = Field(default=12, ge=1)
    force_finalizer_after_steps: int = Field(default=8, ge=1)
    round_timeout_seconds: float = Field(default=300.0, gt=0)
    drain_timeout_seconds: float = Field(default=330.0, gt=0)
    prompt_budget: EvaluationPromptBudgetConfig = Field(default_factory=EvaluationPromptBudgetConfig)
    evidence: EvaluationEvidenceConfig = Field(default_factory=EvaluationEvidenceConfig)
    bundle: EvaluationBundleConfig = Field(default_factory=EvaluationBundleConfig)
    selector: EvaluationSelectorConfig = Field(default_factory=EvaluationSelectorConfig)
    tools: EvaluationToolConfig = Field(default_factory=EvaluationToolConfig)
    judge: EvaluationJudgeConfig = Field(default_factory=EvaluationJudgeConfig)
    outputs: EvaluationOutputConfig = Field(default_factory=EvaluationOutputConfig)


class NorthCUAAppConfig(BaseModel):
    """NorthCUA-owned runtime configuration.

    Fields:
        Defines agent config paths, MCP servers, artifacts, websocket,
        execution, built-in agent settings, prompt, observation, evaluation,
        and template defaults.
    """

    agent_config_path: str
    web_agent_config_path: str | None = None
    agent_mcp_servers: list[JsonObject] = Field(default_factory=list, exclude=True, repr=False)
    artifacts: ArtifactConfig = Field(default_factory=ArtifactConfig)
    websocket: WebSocketServerConfig = Field(default_factory=WebSocketServerConfig)
    execution: ExecutionConfig = Field(default_factory=ExecutionConfig)
    desktop: DesktopPolicyConfig = Field(default_factory=DesktopPolicyConfig)
    web: WebAgentConfig = Field(default_factory=WebAgentConfig)
    agents: dict[str, AgentKitAgentConfig] = Field(default_factory=dict)
    prompt: PromptPolicyConfig = Field(default_factory=PromptPolicyConfig)
    observation: ObservationConfig = Field(default_factory=ObservationConfig)
    evaluation: EvaluationConfig = Field(default_factory=EvaluationConfig)
    context_management: ContextManagementConfig = Field(default_factory=ContextManagementConfig)

    @model_validator(mode="after")
    def _sync_derived_fields(self) -> "NorthCUAAppConfig":
        self.artifacts.round_evaluation_log_name = self.evaluation.outputs.round_log_name
        self.artifacts.session_evaluation_rollup_name = self.evaluation.outputs.session_rollup_name
        self._sync_builtin_agentkit_entries()
        return self

    def _sync_builtin_agentkit_entries(self) -> None:
        """Mirror legacy built-in config into the generic AgentKit mapping."""

        defaults: dict[str, tuple[str | None, BaseModel]] = {
            "desktop": (self.agent_config_path, self.desktop),
            "web": (self.web_agent_config_path, self.web),
        }
        for kind, (agent_config_path, settings_model) in defaults.items():
            existing = self.agents.get(kind)
            if existing is None:
                self.agents[kind] = AgentKitAgentConfig(
                    agent_config_path=agent_config_path,
                    settings=settings_model.model_dump(mode="json"),
                )
                continue
            if existing.agent_config_path is None and agent_config_path is not None:
                existing.agent_config_path = agent_config_path
            if not existing.settings:
                existing.settings = settings_model.model_dump(mode="json")

    @classmethod
    def from_yaml(cls, path: str | Path) -> "NorthCUAAppConfig":
        """Load app config from a YAML file.

        Args:
            path: YAML config path.

        Returns:
            Validated NorthCUA application config.

        Raises:
            ConfigValidationError: If the YAML root is not a mapping.
        """

        config_path = Path(path)
        data = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
        if not isinstance(data, dict):
            raise ConfigValidationError(f"Expected mapping config in {config_path}")
        return cls.model_validate(data)
