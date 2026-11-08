"""Bootstrap loader for NorthCUA's dual-config setup."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import cast

from northcua.agentinfra.runtime.protocol import AgentKind

from .nexau_support import rethrow_missing_nexau

try:
    from nexau.archs.main_sub.config import AgentConfig
    from nexau.archs.main_sub.utils import load_yaml_with_vars

    from northcua.agentinfra.nexau_compat.adapters.providers import NexAUProviderAdapter
    from northcua.agentinfra.providers.llm_fallback import attach_llm_fallbacks, parse_llm_fallbacks
except ModuleNotFoundError as exc:  # pragma: no cover - exercised by runtime startup
    rethrow_missing_nexau(exc)

from northcua.agentinfra.nexau_compat.adapters.config import NexAUConfigAdapter
from northcua.runtime.mcp.builtin_servers import ensure_builtin_mcp_servers_registered
from northcua.runtime.mcp.server_config import visible_nexau_mcp_server_configs

from .config import NorthCUAAppConfig
from .json_types import JsonObject

logger = logging.getLogger(__name__)


@dataclass(slots=True, frozen=True)
class NorthCUABootstrapConfig:
    """Combined runtime config for application bootstrapping.

    Attributes:
        app: NorthCUA application config.
        agent: NexAU agent config.
        app_config_path: Resolved app config path.
        agent_config_path: Resolved agent config path.
    """

    app: NorthCUAAppConfig
    agent: AgentConfig
    app_config_path: Path
    agent_config_path: Path


class NorthCUAConfigLoader:
    """Load and combine NorthCUA app config with NexAU agent config."""

    _DESKTOP_OWNED_AGENT_FIELDS: tuple[str, ...] = (
        "system_prompt",
        "system_prompt_type",
        "system_prompt_suffix",
    )

    @staticmethod
    def from_app_config(
        app_config_path: str | Path,
        *,
        agent_config_path_override: str | Path | None = None,
        agent_kind: AgentKind | str = AgentKind.DESKTOP,
    ) -> NorthCUABootstrapConfig:
        """Load `app.yaml` then resolve and load the referenced NexAU `agent.yaml`.

        Args:
            app_config_path: Path to the NorthCUA app config.
            agent_config_path_override: Optional explicit NexAU agent config path.
            agent_kind: Agent kind used to select the default agent config.

        Returns:
            Combined bootstrap config with resolved paths.
        """

        app_path = Path(app_config_path).resolve()
        app = NorthCUAAppConfig.from_yaml(app_path)
        summary_model = app.context_management.summary.model
        if summary_model.agent_config_path:
            summary_path = Path(summary_model.agent_config_path).expanduser()
            if not summary_path.is_absolute():
                summary_path = app_path.parent / summary_path
            summary_model.agent_config_path = str(summary_path.resolve())
        agent_kind_value = getattr(agent_kind, "value", str(agent_kind))
        if agent_config_path_override is not None:
            configured_agent_path = Path(agent_config_path_override)
        elif agent_kind_value in app.agents and app.agents[agent_kind_value].agent_config_path:
            configured_agent_path = Path(str(app.agents[agent_kind_value].agent_config_path))
        else:
            configured_agent_path = Path(app.agent_config_path)
        agent_path = (
            configured_agent_path if configured_agent_path.is_absolute() else (app_path.parent / configured_agent_path)
        )
        agent_path = agent_path.resolve()
        NorthCUAConfigLoader._validate_agentkit_settings(app, agent_kind_value)
        app.agent_mcp_servers = NorthCUAConfigLoader._load_raw_mcp_servers(agent_path)
        agent = NorthCUAConfigLoader._load_agent_config(
            agent_path,
            app=app,
            app_config_path=app_path,
            agent_kind=agent_kind_value,
            strip_desktop_owned_prompt=agent_kind_value == AgentKind.DESKTOP.value,
        )
        return NorthCUABootstrapConfig(
            app=app,
            agent=agent,
            app_config_path=app_path,
            agent_config_path=agent_path,
        )

    @staticmethod
    def _validate_agentkit_settings(app: NorthCUAAppConfig, agent_kind: str) -> None:
        """Validate registered low-code agent settings when a spec is available."""

        try:
            from northcua.agentkit.registry import load_all_agent_specs
        except Exception:  # pragma: no cover - AgentKit is optional for minimal config tooling
            logger.debug("AgentKit registry unavailable while validating config settings", exc_info=True)
            return
        spec = load_all_agent_specs().maybe_get(agent_kind)
        if spec is None or agent_kind not in app.agents:
            return
        spec.validate_settings(app.agents[agent_kind].settings)

    @staticmethod
    def _load_raw_mcp_servers(agent_config_path: Path) -> list[JsonObject]:
        """Load the raw MCP server configs, including NorthCUA metadata."""

        raw_config = load_yaml_with_vars(agent_config_path)
        if not isinstance(raw_config, dict):
            return []
        raw_servers = raw_config.get("mcp_servers")
        if not isinstance(raw_servers, list):
            return []
        return [dict(server) for server in raw_servers if isinstance(server, dict)]

    @staticmethod
    def _load_agent_config(
        agent_config_path: Path,
        *,
        app: NorthCUAAppConfig,
        app_config_path: Path,
        agent_kind: str,
        strip_desktop_owned_prompt: bool,
    ) -> AgentConfig:
        """Load an AgentConfig while normalizing NorthCUA-owned config fields.

        Desktop prompt ownership lives in NorthCUA's prompt envelope builder.
        Keeping a desktop ``system_prompt`` in NexAU agent config causes
        duplicate provider instructions and can leak raw ``file://...`` URIs
        into provider instructions payloads.
        """

        raw_config = load_yaml_with_vars(agent_config_path)
        if not isinstance(raw_config, dict) or not raw_config:
            raise ValueError(f"Empty or invalid agent configuration: {agent_config_path}")

        normalized = dict(raw_config)
        raw_llm_fallbacks = normalized.pop("llm_fallbacks", None)
        NorthCUAConfigLoader._normalize_llm_runtime_fields(normalized)
        NorthCUAConfigLoader._merge_agentkit_skills(
            normalized,
            app=app,
            app_config_path=app_config_path,
            agent_kind=agent_kind,
            agent_config_path=agent_config_path,
        )
        if strip_desktop_owned_prompt:
            for field_name in NorthCUAConfigLoader._DESKTOP_OWNED_AGENT_FIELDS:
                normalized.pop(field_name, None)
        raw_mcp_servers = normalized.get("mcp_servers")
        if isinstance(raw_mcp_servers, list):
            ensure_builtin_mcp_servers_registered()
            normalized["mcp_servers"] = visible_nexau_mcp_server_configs(
                [dict(server) for server in raw_mcp_servers if isinstance(server, dict)]
            )

        validated = NexAUConfigAdapter.normalize(cast(JsonObject, normalized))
        builder = NexAUConfigAdapter.builder(validated, agent_config_path.parent)
        agent_config = (
            builder.build_core_properties()
            .build_llm_config()
            .build_mcp_servers()
            .build_hooks()
            .build_tracers()
            .build_tools()
            .build_sub_agents()
            .build_skills()
            .build_system_prompt_path()
            .build_sandbox()
            .get_agent_config()
        )
        compatibility_llm_config = NexAUProviderAdapter().prepare_llm_config(agent_config.llm_config)
        agent_config = agent_config.model_copy(update={"llm_config": compatibility_llm_config})
        fallbacks = parse_llm_fallbacks(raw_llm_fallbacks, primary_llm_config=agent_config.llm_config)
        return attach_llm_fallbacks(agent_config, fallbacks)

    @staticmethod
    def _merge_agentkit_skills(
        normalized: JsonObject,
        *,
        app: NorthCUAAppConfig,
        app_config_path: Path,
        agent_kind: str,
        agent_config_path: Path,
    ) -> None:
        """Apply registered AgentKit skill defaults to a raw NexAU config."""

        try:
            from northcua.agentkit.registry import load_all_agent_specs
            from northcua.agentkit.skills import AgentKitSkillResolver
        except Exception:  # pragma: no cover - AgentKit is optional for minimal config tooling
            logger.debug("AgentKit skill resolver unavailable while loading agent config", exc_info=True)
            return
        registry = load_all_agent_specs()
        spec = registry.maybe_get(agent_kind)
        AgentKitSkillResolver.apply_to_raw_agent_config(
            normalized,
            spec=spec,
            app_agent_config=app.agents.get(agent_kind),
            app_config_path=app_config_path,
            agent_config_path=agent_config_path,
        )

    @staticmethod
    def _normalize_llm_runtime_fields(agent_payload: JsonObject) -> None:
        """Move runtime-only LLM budget fields out of provider payload config.

        Older NorthCUA examples placed ``max_context_tokens`` under
        ``llm_config``. NexAU treats unknown LLMConfig keys as provider
        ``extra_params`` and forwards them to OpenAI Responses, where
        ``max_context_tokens`` is not a valid request parameter. The field is
        a NexAU AgentConfig runtime budget, so preserve its meaning by lifting
        it to the agent root before AgentConfigBuilder constructs LLMConfig.
        """

        llm_config = agent_payload.get("llm_config")
        if not isinstance(llm_config, dict):
            return
        max_context_tokens = llm_config.pop("max_context_tokens", None)
        if max_context_tokens is not None and "max_context_tokens" not in agent_payload:
            agent_payload["max_context_tokens"] = max_context_tokens
