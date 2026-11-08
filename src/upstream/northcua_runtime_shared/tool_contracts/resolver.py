"""Single entrypoint for resolving NorthCUA tool surfaces from contracts."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any

from northcua_protocol.capabilities import JsonObject, ToolDescriptor
from northcua_runtime_shared.tool_contracts.contracts import ToolContract, ToolVisibility
from northcua_runtime_shared.tool_contracts.fingerprints import fingerprint_json
from northcua_runtime_shared.tool_contracts.profiles import ToolProfile, ToolSurfaceMode


@dataclass(frozen=True, slots=True)
class ToolSurfaceConfig:
    """Optional config filters applied by the resolver.

    Config can remove tools from a profile. It cannot alter contract schema,
    descriptions, routing, or visibility.
    """

    allow_tool_ids: frozenset[str] = frozenset()
    deny_tool_ids: frozenset[str] = frozenset()
    include_visibility: frozenset[ToolVisibility] = frozenset()
    include_imported: bool = False


@dataclass(frozen=True, slots=True)
class ProviderToolProjection:
    """Provider-facing projection of one model-visible tool contract."""

    name: str
    description: str
    input_schema: JsonObject
    contract_id: str
    contract_fingerprint: str
    lossiness_manifest: tuple[str, ...] = ()
    strict: bool = True

    def to_nexau_tool_kwargs(self) -> JsonObject:
        """Return kwargs compatible with ``nexau.archs.tool.Tool``."""

        return {
            "name": self.name,
            "description": self.description,
            "input_schema": self.input_schema,
        }

    def to_openai_function_tool(self) -> JsonObject:
        """Return a generic OpenAI-compatible function-tool payload."""

        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.input_schema,
                **({"strict": False} if not self.strict else {}),
            },
            "metadata": {
                "contract_id": self.contract_id,
                "contract_fingerprint": self.contract_fingerprint,
                "lossiness_manifest": list(self.lossiness_manifest),
            },
        }


@dataclass(frozen=True, slots=True)
class ResolvedToolSurface:
    """Resolved validation, provider, routing, and audit views."""

    profile_name: str
    mode: ToolSurfaceMode
    contracts: tuple[ToolContract, ...]
    validation_descriptors: tuple[ToolDescriptor, ...]
    provider_tools: tuple[ProviderToolProjection, ...]
    route_by_tool_id: Mapping[str, str]
    visibility: Mapping[str, ToolVisibility]
    aliases: Mapping[str, str]
    fingerprints: JsonObject
    lossiness_manifest: Mapping[str, tuple[str, ...]]

    @property
    def model_visible_contracts(self) -> tuple[ToolContract, ...]:
        """Return contracts visible in provider/model surfaces."""

        return tuple(contract for contract in self.contracts if contract.model_visible)


@dataclass(slots=True)
class ToolSurfaceResolver:
    """Resolve a deterministic tool surface from canonical contracts."""

    contracts: tuple[ToolContract, ...] = field(default_factory=tuple)

    def resolve(
        self,
        profile: ToolProfile | None = None,
        *,
        mode: ToolSurfaceMode = ToolSurfaceMode.LOCAL,
        config: ToolSurfaceConfig | Mapping[str, Any] | None = None,
        device_caps: object | None = None,
        evaluator_spec: object | None = None,
        sandbox_caps: object | None = None,
        contracts: Iterable[ToolContract] | None = None,
    ) -> ResolvedToolSurface:
        """Resolve one profile/mode surface.

        ``device_caps``, ``evaluator_spec``, and ``sandbox_caps`` are accepted
        for the shared call signature. Their adapters may pre-filter contracts
        before calling this resolver, but they are not allowed to rewrite schema.
        """

        _ = (device_caps, evaluator_spec, sandbox_caps)
        source_contracts = tuple(contracts) if contracts is not None else self.contracts
        surface_config = _normalize_config(config)
        selected = (
            profile.select(source_contracts)
            if profile is not None
            else tuple(
                contract for contract in source_contracts if not contract.imported or surface_config.include_imported
            )
        )
        selected = tuple(contract for contract in selected if _config_includes(contract, surface_config))
        _ensure_no_tool_id_conflicts(selected, profile_name=profile.name if profile is not None else "")
        _ensure_no_profile_conflicts(selected, profile_name=profile.name if profile is not None else "")
        selected = _dedupe_equivalent_contracts(selected)

        provider_tools = tuple(_provider_projection(contract) for contract in selected if contract.model_visible)
        return ResolvedToolSurface(
            profile_name=profile.name if profile is not None else "",
            mode=mode,
            contracts=selected,
            validation_descriptors=tuple(contract.to_descriptor() for contract in selected),
            provider_tools=provider_tools,
            route_by_tool_id=_route_by_provider_and_tool_id(selected),
            visibility={contract.tool_id: contract.visibility for contract in selected},
            aliases=_aliases_by_public_name(selected),
            fingerprints=_surface_fingerprints(selected, provider_tools),
            lossiness_manifest={
                contract.tool_id: contract.lossiness_manifest for contract in selected if contract.lossiness_manifest
            },
        )


def resolve_tool_surface(
    contracts: Iterable[ToolContract],
    profile: ToolProfile | None = None,
    *,
    mode: ToolSurfaceMode = ToolSurfaceMode.LOCAL,
    config: ToolSurfaceConfig | Mapping[str, Any] | None = None,
) -> ResolvedToolSurface:
    """Convenience function for resolving a one-off contract collection."""

    return ToolSurfaceResolver(tuple(contracts)).resolve(profile, mode=mode, config=config)


def _normalize_config(config: ToolSurfaceConfig | Mapping[str, Any] | None) -> ToolSurfaceConfig:
    if config is None:
        return ToolSurfaceConfig()
    if isinstance(config, ToolSurfaceConfig):
        return config
    return ToolSurfaceConfig(
        allow_tool_ids=frozenset(str(item) for item in config.get("allow_tool_ids", ()) or ()),
        deny_tool_ids=frozenset(str(item) for item in config.get("deny_tool_ids", ()) or ()),
        include_visibility=frozenset(ToolVisibility(str(item)) for item in config.get("include_visibility", ()) or ()),
        include_imported=bool(config.get("include_imported", False)),
    )


def _config_includes(contract: ToolContract, config: ToolSurfaceConfig) -> bool:
    if config.allow_tool_ids and contract.tool_id not in config.allow_tool_ids:
        return False
    if config.deny_tool_ids and contract.tool_id in config.deny_tool_ids:
        return False
    if config.include_visibility and contract.visibility not in config.include_visibility:
        return False
    return True


def _provider_projection(contract: ToolContract) -> ProviderToolProjection:
    contract_fingerprint = fingerprint_json(contract.to_summary())
    projection = contract.provider_projection
    return ProviderToolProjection(
        name=contract.provider_name,
        description=contract.description,
        input_schema=projection.schema,
        contract_id=contract.contract_id,
        contract_fingerprint=contract_fingerprint,
        lossiness_manifest=projection.lossiness_manifest,
        strict=contract.provider_strict,
    )


def _ensure_no_tool_id_conflicts(contracts: tuple[ToolContract, ...], *, profile_name: str) -> None:
    seen: dict[str, str] = {}
    for contract in contracts:
        contract_hash = fingerprint_json(contract.to_summary())
        previous_hash = seen.get(contract.tool_id)
        if previous_hash is not None and previous_hash != contract_hash:
            label = f" in profile {profile_name!r}" if profile_name else ""
            raise ValueError(f"Conflicting tool contract identity for tool_id {contract.tool_id!r}{label}")
        seen[contract.tool_id] = contract_hash


def _ensure_no_profile_conflicts(contracts: tuple[ToolContract, ...], *, profile_name: str) -> None:
    seen: dict[str, str] = {}
    for contract in contracts:
        public_name = contract.provider_name
        contract_hash = fingerprint_json(contract.to_summary())
        previous_hash = seen.get(public_name)
        if previous_hash is not None and previous_hash != contract_hash:
            label = f" in profile {profile_name!r}" if profile_name else ""
            raise ValueError(f"Conflicting tool contract projection for public name {public_name!r}{label}")
        seen[public_name] = contract_hash


def _dedupe_equivalent_contracts(contracts: tuple[ToolContract, ...]) -> tuple[ToolContract, ...]:
    deduped: list[ToolContract] = []
    seen: set[tuple[str, str]] = set()
    for contract in contracts:
        key = (contract.provider_name, fingerprint_json(contract.to_summary()))
        if key in seen:
            continue
        seen.add(key)
        deduped.append(contract)
    return tuple(deduped)


def _aliases_by_public_name(contracts: tuple[ToolContract, ...]) -> dict[str, str]:
    canonical_names = {contract.tool_id: contract.tool_id for contract in contracts}
    provider_names = {contract.provider_name: contract.tool_id for contract in contracts}
    aliases: dict[str, str] = {}
    for contract in contracts:
        for alias in contract.aliases:
            _record_alias(
                aliases,
                alias=alias,
                target=contract.tool_id,
                canonical_names=canonical_names,
                provider_names=provider_names,
            )
        if contract.public_name and contract.public_name != contract.tool_id:
            _record_alias(
                aliases,
                alias=contract.public_name,
                target=contract.tool_id,
                canonical_names=canonical_names,
                provider_names=provider_names,
            )
    return aliases


def _route_by_provider_and_tool_id(contracts: tuple[ToolContract, ...]) -> dict[str, str]:
    routes: dict[str, str] = {}
    for contract in contracts:
        if not contract.server_name:
            continue
        for name in dict.fromkeys((contract.tool_id, contract.provider_name)):
            existing = routes.get(name)
            if existing is not None and existing != contract.server_name:
                raise ValueError(
                    f"Tool route {name!r} maps to both MCP servers {existing!r} and {contract.server_name!r}"
                )
            routes[name] = contract.server_name
    return routes


def _record_alias(
    aliases: dict[str, str],
    *,
    alias: str,
    target: str,
    canonical_names: dict[str, str],
    provider_names: dict[str, str],
) -> None:
    canonical_target = canonical_names.get(alias)
    if canonical_target is not None and canonical_target != target:
        raise ValueError(f"Tool alias {alias!r} conflicts with canonical tool id {canonical_target!r}")
    provider_target = provider_names.get(alias)
    if provider_target is not None and provider_target != target:
        raise ValueError(f"Tool alias {alias!r} conflicts with provider tool name for {provider_target!r}")
    existing = aliases.get(alias)
    if existing is not None and existing != target:
        raise ValueError(f"Tool alias {alias!r} maps to both {existing!r} and {target!r}")
    if alias != target:
        aliases[alias] = target


def _surface_fingerprints(
    contracts: tuple[ToolContract, ...],
    provider_tools: tuple[ProviderToolProjection, ...],
) -> JsonObject:
    contract_summaries = [contract.to_summary() for contract in contracts]
    provider_summaries = [
        {
            "name": tool.name,
            "description": tool.description,
            "input_schema": tool.input_schema,
            "contract_id": tool.contract_id,
            "lossiness_manifest": list(tool.lossiness_manifest),
        }
        for tool in provider_tools
    ]
    route_summaries = [
        {"tool_id": contract.tool_id, "server_name": contract.server_name}
        for contract in contracts
        if contract.server_name
    ]
    return {
        "contract_summary_hash": fingerprint_json(contract_summaries),
        "protocol_schema_hash": fingerprint_json(
            {contract.tool_id: contract.parameters_schema for contract in contracts}
        ),
        "provider_schema_hash": fingerprint_json({tool.name: tool.input_schema for tool in provider_tools}),
        "route_hash": fingerprint_json(route_summaries),
        "final_prompt_surface_hash": fingerprint_json(provider_summaries),
    }


__all__ = [
    "ProviderToolProjection",
    "ResolvedToolSurface",
    "ToolSurfaceConfig",
    "ToolSurfaceResolver",
    "resolve_tool_surface",
]
