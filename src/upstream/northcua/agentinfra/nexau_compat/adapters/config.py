"""Strict, legacy-compatible construction of public NexAU agent configs."""

from __future__ import annotations

import inspect
from collections.abc import Callable, Mapping
from importlib.resources import files
from pathlib import Path
from typing import Any, Literal, cast, overload

from nexau.archs.main_sub.config import AgentConfig, ConfigError
from nexau.archs.main_sub.config.config import AgentConfigBuilder
from nexau.archs.main_sub.config.schema import ConfigError as SchemaConfigError
from nexau.archs.main_sub.config.schema import normalize_agent_config_dict
from nexau.archs.tool import Tool
from nexau.archs.tracer.core import BaseTracer

from .tool import NexAUToolAdapter


class StrictLegacyAgentConfigBuilder(AgentConfigBuilder):
    """Public NexAU builder with private-pin fail-fast semantics.

    Public NexAU added a ``strict`` switch, but its tracer path still skips
    invalid entries even when strict mode is enabled.  This specialization
    closes that gap and routes YAML tools through :class:`NexAUToolAdapter`.
    The resulting object remains the public NexAU ``AgentConfig`` type.
    """

    def __init__(self, config: dict[str, Any], base_path: Path) -> None:
        kwargs = {"strict": True} if "strict" in inspect.signature(AgentConfigBuilder.__init__).parameters else {}
        super().__init__(config, base_path, **kwargs)
        # Private 58d3fa4 was unconditionally strict but did not expose the
        # public diagnostic attribute.  Keeping it present makes the
        # compatibility contract inspectable in both isolated environments.
        self.strict = True

    def _handle_component_error(self, msg: str, error: Exception | None = None) -> None:
        """Always fail closed and retain legacy error wording where stable."""

        raise ConfigError(_legacy_component_error(msg)) from error

    def build_tracers(self) -> StrictLegacyAgentConfigBuilder:
        """Build every tracer or fail immediately, matching private NexAU."""

        tracer_configs = self.config.get("tracers", [])
        if tracer_configs is None:
            tracer_configs = []
        if not isinstance(tracer_configs, list):
            raise ConfigError("'tracers' must be a list")

        resolved_tracers: list[BaseTracer] = []
        for entry in cast(list[object], tracer_configs):
            if entry is None:
                raise ConfigError("Tracer entries cannot be null")
            if isinstance(entry, BaseTracer):
                tracer = entry
            elif isinstance(entry, (str, dict)) or callable(entry):
                try:
                    tracer = self._import_and_instantiate(cast(Any, entry))
                except Exception as exc:
                    raise ConfigError(f"Error loading tracer: {exc}") from exc
            else:
                raise ConfigError(
                    f"Tracer entry must be BaseTracer, string, dict, or callable, got {type(entry)}",
                )
            if not isinstance(tracer, BaseTracer):
                raise ConfigError(
                    f"Tracer must be an instance of BaseTracer, got {type(tracer)}",
                )
            resolved_tracers.append(tracer)

        self.agent_params["tracers"] = resolved_tracers
        return self

    def _load_tool_from_config(self, tool_config: dict[str, Any], base_path: Path) -> Tool:
        """Load a YAML tool through the legacy behavior specialization."""

        name = tool_config.get("name")
        if not isinstance(name, str) or not name:
            raise ConfigError("Tool configuration missing 'name' field")

        raw_yaml_path = tool_config.get("yaml_path")
        if not isinstance(raw_yaml_path, str) or not raw_yaml_path:
            raise ConfigError(f"Tool '{name}' missing 'yaml_path' field")

        lazy = _optional_bool(tool_config.get("lazy", False), field="lazy", tool_name=name)
        defer_loading = _optional_bool(
            tool_config.get("defer_loading"),
            field="defer_loading",
            tool_name=name,
            allow_none=True,
        )
        as_skill = _optional_bool(tool_config.get("as_skill", False), field="as_skill", tool_name=name)
        extra_kwargs = _mapping_value(tool_config.get("extra_kwargs", {}), context=f"Tool '{name}' extra_kwargs")
        conflict_keys = set(extra_kwargs) & {"agent_state", "global_storage", "ctx"}
        if conflict_keys:
            raise ConfigError(
                f"Tool '{name}' extra_kwargs contains reserved keys that cannot be overridden: {sorted(conflict_keys)}",
            )

        binding = tool_config.get("binding")
        if binding is not None and not isinstance(binding, str) and not callable(binding):
            raise ConfigError(f"Tool '{name}' field 'binding' must be a string or callable")

        permissions = _permissions_value(tool_config.get("permissions"), tool_name=name)
        source_id_value = tool_config.get("source_id")
        source_id = source_id_value if isinstance(source_id_value, str) else None
        yaml_path = _resolve_config_path(raw_yaml_path, base_path)

        return NexAUToolAdapter.from_yaml(
            str(yaml_path),
            cast(Callable[..., Any] | str | None, binding),
            as_skill=as_skill,
            extra_kwargs=extra_kwargs,
            lazy=lazy,
            name=name,
            defer_loading=defer_loading,
            permissions=permissions,
            source_id=source_id,
        )


class NexAUConfigAdapter:
    """Facade for normalized, strict public NexAU configuration loading."""

    builder_type: type[StrictLegacyAgentConfigBuilder] = StrictLegacyAgentConfigBuilder

    @classmethod
    def normalize(cls, config: Mapping[str, object]) -> dict[str, Any]:
        """Validate a raw mapping with NexAU's typed schema."""

        public_only = sorted(set(config) & {"plugins", "skipped_components", "source_id"})
        if public_only:
            raise ConfigError(f"Unsupported legacy agent config fields: {public_only}")
        raw_tools = config.get("tools", [])
        if isinstance(raw_tools, list):
            for index, raw_tool in enumerate(raw_tools):
                if isinstance(raw_tool, Mapping):
                    unsupported = sorted(set(raw_tool) & {"permissions", "source_id"})
                    if unsupported:
                        raise ConfigError(f"Unsupported legacy tool config fields at index {index}: {unsupported}")

        try:
            return normalize_agent_config_dict(cast(dict[str, Any], dict(config)))
        except SchemaConfigError as exc:
            # Public c98f7fb defines independent ConfigError classes in
            # ``schema`` and ``config``.  NorthCUA exposes one stable type.
            raise ConfigError(str(exc)) from exc

    @classmethod
    def builder(
        cls,
        config: Mapping[str, object],
        base_path: str | Path,
        *,
        normalize: bool = False,
    ) -> StrictLegacyAgentConfigBuilder:
        """Create a strict builder for NorthCUA's explicit build pipeline."""

        config_dict = cls.normalize(config) if normalize else cast(dict[str, Any], dict(config))
        return cls.builder_type(config_dict, Path(base_path))

    @classmethod
    def build(
        cls,
        config: Mapping[str, object],
        base_path: str | Path,
        *,
        normalize: bool = True,
    ) -> AgentConfig:
        """Build a public NexAU ``AgentConfig`` without silent component loss."""

        builder = cls.builder(config, base_path, normalize=normalize)
        return (
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


def _legacy_component_error(message: str) -> str:
    replacements = (
        ("Skipped middleware ", "Error loading middleware "),
        ("Skipped after_model_hook ", "Error loading hook "),
        ("Skipped after_tool_hook ", "Error loading tool hook "),
        ("Skipped before_model_hook ", "Error loading before model hook "),
        ("Skipped before_tool_hook ", "Error loading before tool hook "),
        ("Skipped tool ", "Error loading tool "),
        ("Skipped skill ", "Error loading skill "),
        ("Skipped sub-agent ", "Error loading sub-agent "),
        ("Skipped system prompt file (not found): ", "System prompt file not found: "),
    )
    for public_prefix, legacy_prefix in replacements:
        if message.startswith(public_prefix):
            return f"{legacy_prefix}{message.removeprefix(public_prefix)}"
    return message


def _resolve_config_path(raw_path: str, base_path: Path) -> Path:
    path = Path(raw_path)
    if path.is_absolute():
        return path
    if ":" in raw_path:
        package_name, resource_path = raw_path.split(":", 1)
        return Path(str(files(package_name).joinpath(resource_path)))
    return base_path / path


def _mapping_value(value: object, *, context: str) -> dict[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise ConfigError(f"{context} must be a mapping")
    return {str(key): item for key, item in cast(Mapping[object, Any], value).items()}


@overload
def _optional_bool(
    value: object,
    *,
    field: str,
    tool_name: str,
    allow_none: Literal[False] = False,
) -> bool: ...


@overload
def _optional_bool(
    value: object,
    *,
    field: str,
    tool_name: str,
    allow_none: Literal[True],
) -> bool | None: ...


def _optional_bool(
    value: object,
    *,
    field: str,
    tool_name: str,
    allow_none: bool = False,
) -> bool | None:
    if value is None and allow_none:
        return None
    if not isinstance(value, bool):
        raise ConfigError(f"Tool '{tool_name}' field '{field}' must be a boolean")
    return value


def _permissions_value(value: object, *, tool_name: str) -> dict[str, list[str]] | None:
    if value is None:
        return None
    if not isinstance(value, Mapping):
        raise ConfigError(f"Tool '{tool_name}' field 'permissions' must be a dict")
    permissions: dict[str, list[str]] = {}
    for decision, patterns in cast(Mapping[object, object], value).items():
        if (
            not isinstance(decision, str)
            or not isinstance(patterns, list)
            or not all(isinstance(pattern, str) for pattern in patterns)
        ):
            raise ConfigError(
                f"Tool '{tool_name}' field 'permissions' must map strings to lists of strings",
            )
        permissions[decision] = cast(list[str], patterns)
    return permissions


__all__ = ["NexAUConfigAdapter", "StrictLegacyAgentConfigBuilder"]
