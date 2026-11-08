"""Canonical tool contract model shared across NorthCUA runtimes."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from enum import Enum

from pydantic import BaseModel

from northcua_protocol.capabilities import Capability, JsonObject, ToolDescriptor, ToolRiskLevel
from northcua_runtime_shared.tool_contracts.bindings import ToolBinding, binding_summary
from northcua_runtime_shared.tool_contracts.projection import (
    ProviderSchemaProjection,
    provider_schema_projection,
    provider_safe_parameters_schema,
    schema_from_model,
    tool_descriptor_from_contract,
)


class ToolVisibility(str, Enum):
    """Visibility of a tool in model/provider-facing surfaces."""

    MODEL_VISIBLE = "model_visible"
    RUNTIME_ONLY = "runtime_only"
    INTERNAL_ONLY = "internal_only"
    OPERATOR_ONLY = "operator_only"

    @property
    def model_visible(self) -> bool:
        """Return whether this visibility should be shown to the model."""

        return self == ToolVisibility.MODEL_VISIBLE


class ToolContractKind(str, Enum):
    """High-level contract kind."""

    TOOL = "tool"
    PROBE = "probe"
    ACTION = "action"
    ARTIFACT = "artifact"
    FINALIZER = "finalizer"
    SANDBOX_PROVIDER = "sandbox_provider"
    SKILL_ASSET = "skill_asset"
    EXTERNAL = "external"


class ToolAudience(str, Enum):
    """The subsystem/audience a tool contract is intended for."""

    AGENT = "agent"
    RUNTIME = "runtime"
    EVALUATOR = "evaluator"
    ENV_BUILDER = "env_builder"
    OPERATOR = "operator"
    EXTERNAL = "external"


class ToolPlacement(str, Enum):
    """Where a tool may be placed/executed after profile resolution."""

    MCP_SERVER = "mcp_server"
    LOCAL_HANDLER = "local_handler"
    DEVICE_COMMAND = "device_command"
    REMOTE_PROXY = "remote_proxy"
    CLIENT_PROBE = "client_probe"
    CLIENT_MUTATING = "client_mutating"
    EVALUATOR_ARTIFACT = "evaluator_artifact"
    FINALIZER = "finalizer"
    SANDBOX_PROVIDER = "sandbox_provider"
    HOST_PROVIDER = "host_provider"
    EXTERNAL_MCP = "external_mcp"


class ProviderSchemaMode(str, Enum):
    """Provider-facing JSON Schema projection mode."""

    COMPATIBLE = "compatible"
    UNION = "union"


@dataclass(frozen=True, slots=True)
class ToolContract:
    """Single source of truth for one executable or probe tool.

    The contract owns protocol metadata and parameter schema. MCP descriptors,
    AgentKit specs, remote direct tools, and provider-facing schemas must be
    projected from this object rather than re-declared.
    """

    tool_id: str
    public_name: str = ""
    version: str = "1"
    description: str = ""
    namespace: str = ""
    agent_kind: str = ""
    kind: ToolContractKind = ToolContractKind.TOOL
    audience: ToolAudience = ToolAudience.AGENT
    placement: ToolPlacement = ToolPlacement.MCP_SERVER
    visibility: ToolVisibility = ToolVisibility.MODEL_VISIBLE
    capability: Capability = Capability.DESKTOP_INPUT
    params_model: type[BaseModel] | None = None
    json_schema: JsonObject | None = None
    result_model: type[BaseModel] | None = None
    timeout_seconds: float = 10.0
    side_effect: bool = True
    readonly: bool | None = None
    requires_focus: bool = False
    risk_level: ToolRiskLevel = ToolRiskLevel.LOW
    max_payload_bytes: int = 1_000_000
    binding: ToolBinding | None = None
    bindings: tuple[ToolBinding, ...] = ()
    profiles: tuple[str, ...] = ()
    aliases: tuple[str, ...] = ()
    source: str = ""
    imported: bool = False
    audit_tags: tuple[str, ...] = ()
    resource_requirements: tuple[str, ...] = ()
    session_requirements: tuple[str, ...] = ()
    lease_requirements: tuple[str, ...] = ()
    changelog: str = ""
    provider_schema_mode: ProviderSchemaMode = ProviderSchemaMode.COMPATIBLE

    @property
    def contract_id(self) -> str:
        """Return the stable contract identity."""

        return self.tool_id

    @property
    def provider_name(self) -> str:
        """Return the model/provider-facing tool name."""

        return self.public_name or self.tool_id

    @property
    def model_visible(self) -> bool:
        """Return whether this contract belongs in provider-facing surfaces."""

        return self.visibility.model_visible

    @property
    def callable_by_runtime(self) -> bool:
        """Return whether runtime dispatch may call this contract."""

        return self.visibility != ToolVisibility.INTERNAL_ONLY

    @property
    def is_readonly(self) -> bool:
        """Return the contract readonly policy."""

        if self.readonly is not None:
            return self.readonly
        return not self.side_effect

    @property
    def all_bindings(self) -> tuple[ToolBinding, ...]:
        """Return every declared execution binding."""

        if self.binding is None:
            return self.bindings
        return (self.binding, *self.bindings)

    @property
    def server_name(self) -> str:
        """Return the owning MCP server name when present."""

        for binding in self.all_bindings:
            server_name = str(getattr(binding, "server_name", "") or "")
            if server_name:
                return server_name
        return ""

    @property
    def registration_hook(self) -> str:
        """Return the registration hook when present."""

        for binding in self.all_bindings:
            registration_hook = str(getattr(binding, "registration_hook", "") or "")
            if registration_hook:
                return registration_hook
        return ""

    @property
    def parameters_schema(self) -> JsonObject:
        """Return the protocol parameter schema for this contract."""

        if self.json_schema is not None:
            return deepcopy(self.json_schema)
        return schema_from_model(self.params_model)

    @property
    def provider_parameters_schema(self) -> JsonObject:
        """Return provider-safe parameter schema for model-visible tools."""

        return provider_safe_parameters_schema(
            self.parameters_schema,
            preserve_unions=self.provider_schema_mode == ProviderSchemaMode.UNION,
        )

    @property
    def provider_strict(self) -> bool:
        """Return whether OpenAI strict function calling is safe for this contract."""

        return self.provider_schema_mode != ProviderSchemaMode.UNION

    @property
    def provider_projection(self) -> ProviderSchemaProjection:
        """Return provider-safe schema plus explicit lossiness metadata."""

        return provider_schema_projection(
            self.parameters_schema,
            preserve_unions=self.provider_schema_mode == ProviderSchemaMode.UNION,
        )

    @property
    def lossiness_manifest(self) -> tuple[str, ...]:
        """Return schema paths removed by provider projection."""

        return self.provider_projection.lossiness_manifest

    def to_descriptor(self) -> ToolDescriptor:
        """Project this contract to a protocol descriptor."""

        return tool_descriptor_from_contract(self)

    def to_summary(self) -> JsonObject:
        """Return a stable JSON summary used for snapshots and fingerprints."""

        summary: JsonObject = {
            "tool_id": self.tool_id,
            "contract_id": self.contract_id,
            "public_name": self.provider_name,
            "version": self.version,
            "description": self.description,
            "namespace": self.namespace,
            "agent_kind": self.agent_kind,
            "kind": self.kind.value,
            "audience": self.audience.value,
            "placement": self.placement.value,
            "visibility": self.visibility.value,
            "capability": self.capability.value,
            "schema": self.parameters_schema,
            "provider_schema": self.provider_parameters_schema,
            "provider_lossiness_manifest": list(self.lossiness_manifest),
            "timeout_seconds": self.timeout_seconds,
            "side_effect": self.side_effect,
            "readonly": self.is_readonly,
            "requires_focus": self.requires_focus,
            "risk_level": self.risk_level.value,
            "max_payload_bytes": self.max_payload_bytes,
            "server_name": self.server_name,
            "registration_hook": self.registration_hook,
            "profiles": list(self.profiles),
            "aliases": list(self.aliases),
            "source": self.source,
            "imported": self.imported,
            "audit_tags": list(self.audit_tags),
            "resource_requirements": list(self.resource_requirements),
            "session_requirements": list(self.session_requirements),
            "lease_requirements": list(self.lease_requirements),
            "changelog": self.changelog,
        }
        if self.params_model is not None:
            summary["params_model"] = f"{self.params_model.__module__}:{self.params_model.__qualname__}"
        if self.result_model is not None:
            summary["result_model"] = f"{self.result_model.__module__}:{self.result_model.__qualname__}"
        if self.provider_schema_mode != ProviderSchemaMode.COMPATIBLE:
            summary["provider_schema_mode"] = self.provider_schema_mode.value
            summary["provider_strict"] = self.provider_strict
        bindings = self.all_bindings
        if bindings:
            summary["bindings"] = [binding_summary(binding) for binding in bindings]
        return summary


__all__ = [
    "ProviderSchemaMode",
    "ToolAudience",
    "ToolContract",
    "ToolContractKind",
    "ToolPlacement",
    "ToolVisibility",
]
