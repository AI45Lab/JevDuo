"""Tool-result desktop state and initial screenshot coordination."""

from __future__ import annotations

import platform
from collections.abc import Collection
from pathlib import Path

from nexau.archs.main_sub.config import AgentConfig

from northcua.agentkernel.json_types import JsonObject
from northcua.core.config import (
    AccessibilityObservationConfig,
    NorthCUAAppConfig,
    ObservationConfig,
    ProbeChainConfig,
)
from northcua.core.errors import ConfigValidationError
from northcua.core.models import ObservationContext, StepLogRecord
from northcua.runtime.accessibility.persist import AccessibilityArtifactPersister
from northcua.runtime.artifacts.store import NorthCUAArtifactStore
from northcua.runtime.mcp.accessibility_server import (
    A11Y_CAPABILITIES_TOOL_NAME,
    A11Y_CAPTURE_TOOL_NAME,
)
from northcua.runtime.mcp.server_config import parse_mcp_server_config

from .accessibility import AccessibilityObservationAugmentor
from .adapters import BUILT_IN_PROBE_ADAPTER_NAMES, ProbeAdapterRegistry
from .base import ProbeExecutor
from .catalog import built_in_probe_chains, default_accessibility_probe_chain
from .executors import LocalInternalProbeExecutor
from .sources import ProbeChainStateSource, ToolResultStateSource


def local_observation_probe_chains(app_config: NorthCUAAppConfig) -> dict[str, ProbeChainConfig]:
    """Return the probe chains a local Desktop run executes for ``app_config``."""

    probe_chains = built_in_probe_chains(app_config.desktop)
    if app_config.observation.a11y.enabled:
        probe_chains.setdefault(
            "desktop_a11y_default",
            default_accessibility_probe_chain(app_config.observation.a11y),
        )
    return probe_chains


def validate_local_observation_config(app_config: NorthCUAAppConfig, agent_config: AgentConfig) -> None:
    """Apply the local Desktop observation startup checks without creating artifacts.

    ``northcua config validate`` uses this so a configuration that
    ``ObservationCoordinator`` would reject is reported before a run starts.

    Raises:
        ConfigValidationError: If a probe chain references a missing,
            model-visible or incompatible MCP server, or an unknown adapter.
    """

    _validate_observation_configuration(
        observation_config=app_config.observation,
        agent_config=agent_config,
        agent_mcp_servers=list(getattr(app_config, "agent_mcp_servers", []) or []),
        probe_chains=local_observation_probe_chains(app_config),
        adapter_names=BUILT_IN_PROBE_ADAPTER_NAMES,
    )


class ObservationCoordinator:
    """Collect tool-returned desktop state with an initial screenshot fallback."""

    def __init__(
        self,
        *,
        app_config: NorthCUAAppConfig,
        agent_config: AgentConfig,
        artifact_store: NorthCUAArtifactStore,
        probe_executor: ProbeExecutor,
        agent_mcp_servers: list[JsonObject] | None = None,
        platform_name: str | None = None,
    ) -> None:
        self.app_config = app_config
        self.agent_config = agent_config
        self.artifact_store = artifact_store
        self.probe_executor = probe_executor
        self.agent_mcp_servers = list(agent_mcp_servers or getattr(app_config, "agent_mcp_servers", []) or [])
        self.platform_name = platform_name or (
            "osworld" if app_config.desktop.backend == "osworld" else platform.system().lower()
        )
        self.home_directory = "unavailable" if self.platform_name in {"remote", "osworld"} else str(Path.home())
        self.adapter_registry = ProbeAdapterRegistry(
            artifact_store,
            persist_a11y_raw_artifact=app_config.observation.a11y.persist_raw_artifact,
        )
        self.probe_chains = local_observation_probe_chains(app_config)
        _validate_observation_configuration(
            observation_config=app_config.observation,
            agent_config=agent_config,
            agent_mcp_servers=self.agent_mcp_servers,
            probe_chains=self.probe_chains,
            adapter_names=self.adapter_registry.names,
        )
        self.tool_result_source = ToolResultStateSource()
        self.initial_screenshot_source = self._probe_source("desktop_snapshot_default")
        self.accessibility_augmentor = self._build_accessibility_augmentor(app_config.observation.a11y)

    @classmethod
    def for_local(
        cls,
        *,
        app_config: NorthCUAAppConfig,
        agent_config: AgentConfig,
        artifact_store: NorthCUAArtifactStore,
    ) -> "ObservationCoordinator":
        """Create a local observation coordinator using in-process MCP servers.

        Args:
            app_config: Application config that owns accessibility settings and
                hidden MCP server definitions.
            agent_config: NexAU agent config used to validate model-visible
                versus internal-only probe servers.
            artifact_store: Store used by probe adapters for observation
                screenshots and accessibility artifacts.

        Returns:
            Coordinator wired to the local internal probe executor.
        """

        return cls(
            app_config=app_config,
            agent_config=agent_config,
            artifact_store=artifact_store,
            probe_executor=LocalInternalProbeExecutor(),
            agent_mcp_servers=list(getattr(app_config, "agent_mcp_servers", []) or []),
        )

    async def collect(self, session_step: int, previous_steps: list[StepLogRecord]) -> ObservationContext:
        """Reuse a clean tool-result image, capturing state only when none is available.

        Args:
            session_step: Monotonic session step being prepared for prompting.
            previous_steps: Persisted prior steps used by history-aware
                state selection and accessibility diffing.

        Returns:
            Prompt-facing observation context with current-state artifacts,
            metadata, and optional accessibility records.
        """

        state = await self.tool_result_source.collect(session_step=session_step, previous_steps=previous_steps)
        if not state.current_clean_screenshot_url:
            state = await self.initial_screenshot_source.collect(
                session_step=session_step, previous_steps=previous_steps
            )
        observation = ObservationContext(
            platform_name=self.platform_name,
            current_artifacts=state.artifacts,
            current_clean_screenshot_path=state.current_clean_screenshot_path,
            current_clean_screenshot_url=state.current_clean_screenshot_url,
            selected_history_image_path=state.selected_history_image_path,
            selected_history_image_url=state.selected_history_image_url,
            observation_metadata=state.metadata,
        )
        if self.accessibility_augmentor is not None:
            observation = await self.accessibility_augmentor.augment(
                observation_context=observation,
                session_step=session_step,
                previous_steps=previous_steps,
            )
        observation.observation_metadata.setdefault("desktop_environment", self.platform_name)
        observation.observation_metadata.setdefault("home_directory", self.home_directory)
        return observation

    def _probe_source(self, chain_name: str) -> ProbeChainStateSource:
        chain = self.probe_chains.get(chain_name)
        if chain is None:
            raise ConfigValidationError(f"Unknown observation probe chain: {chain_name}")
        return ProbeChainStateSource(
            chain_name=chain_name,
            chain=chain,
            probe_executor=self.probe_executor,
            adapter_registry=self.adapter_registry,
        )

    def _build_accessibility_augmentor(
        self,
        accessibility_config: AccessibilityObservationConfig,
    ) -> AccessibilityObservationAugmentor | None:
        if not accessibility_config.enabled:
            return None
        return AccessibilityObservationAugmentor(
            config=accessibility_config,
            probe_source=self._probe_source("desktop_a11y_default"),
            probe_executor=self.probe_executor,
            artifact_persister=AccessibilityArtifactPersister(self.artifact_store),
            cache_key=self.artifact_store.root.name,
        )


def _validate_observation_configuration(
    *,
    observation_config: ObservationConfig,
    agent_config: AgentConfig,
    agent_mcp_servers: list[JsonObject],
    probe_chains: dict[str, ProbeChainConfig],
    adapter_names: Collection[str],
) -> None:
    server_configs: dict[str, JsonObject] = {}
    for raw_server in agent_mcp_servers:
        if not isinstance(raw_server, dict):
            continue
        parsed = parse_mcp_server_config(dict(raw_server))
        server_configs[parsed.name] = {
            **parsed.to_nexau_config(),
            "name": parsed.name,
            "model_visible": parsed.model_visible,
        }

    for server in list(agent_config.mcp_servers or []):
        if isinstance(server, dict):
            server_configs.setdefault(
                str(server.get("name") or ""),
                {**dict(server), "model_visible": True},
            )
    _validate_accessibility_tool_names(observation_config.a11y)
    for chain_name, chain in probe_chains.items():
        _validate_chain(chain_name, chain, server_configs, adapter_names)


def _validate_chain(
    chain_name: str,
    chain: ProbeChainConfig,
    server_configs: dict[str, JsonObject],
    adapter_names: Collection[str],
) -> None:
    for step in chain.steps:
        server_config = server_configs.get(step.server)
        if server_config is None:
            raise ConfigValidationError(
                f"Observation probe chain `{chain_name}` references unknown MCP server `{step.server}`."
            )
        if bool(server_config.get("model_visible", True)):
            raise ConfigValidationError(
                f"Observation probe chain `{chain_name}` must use internal-only MCP server `{step.server}`."
            )
        if step.adapter not in adapter_names:
            raise ConfigValidationError(
                f"Observation probe chain `{chain_name}` references unknown adapter `{step.adapter}`."
            )


def _validate_accessibility_tool_names(accessibility_config: AccessibilityObservationConfig) -> None:
    if not accessibility_config.enabled:
        return
    if accessibility_config.server_name != "DesktopNativeA11yExecutor":
        return
    if accessibility_config.capture_tool_name != A11Y_CAPTURE_TOOL_NAME:
        raise ConfigValidationError(
            "Accessibility capture tool name does not match the built-in DesktopNativeA11yExecutor contract."
        )
    if accessibility_config.capabilities_tool_name != A11Y_CAPABILITIES_TOOL_NAME:
        raise ConfigValidationError(
            "Accessibility capabilities tool name does not match the built-in DesktopNativeA11yExecutor contract."
        )
