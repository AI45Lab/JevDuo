"""Small authoring API for NorthCUA-owned tools.

Normal tool authors should use this layer instead of instantiating every
projection shape directly. ``ToolContract`` remains the canonical IR; this
module keeps authoring concise while preserving one metadata source.
"""

from __future__ import annotations

import inspect
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, TypeVar, get_type_hints

from pydantic import BaseModel

from northcua_protocol.capabilities import Capability, JsonObject, ToolRiskLevel
from northcua_runtime_shared.tool_contracts.bindings import DirectHandlerBinding, ToolBinding
from northcua_runtime_shared.tool_contracts.contracts import (
    ToolAudience,
    ToolContract,
    ToolContractKind,
    ToolPlacement,
    ToolVisibility,
)

_HandlerT = TypeVar("_HandlerT", bound=Callable[..., Any])


@dataclass(frozen=True, slots=True)
class ToolDefinition:
    """Owned tool contract with an optional in-process handler."""

    contract: ToolContract
    handler: Callable[..., Any] | None = None

    @property
    def tool_id(self) -> str:
        """Return the canonical tool id."""

        return self.contract.tool_id


@dataclass(frozen=True, slots=True)
class ToolSet:
    """Immutable owned tool collection for one server/domain catalog."""

    name: str
    tools: tuple[ToolDefinition, ...]

    @property
    def contracts(self) -> tuple[ToolContract, ...]:
        """Return contracts in deterministic declaration order."""

        return tuple(tool.contract for tool in self.tools)

    @property
    def handlers(self) -> dict[str, Callable[..., Any]]:
        """Return declared handlers keyed by contract tool id."""

        return {tool.tool_id: tool.handler for tool in self.tools if tool.handler is not None}


def define_tool(
    *,
    tool_id: str,
    public_name: str = "",
    version: str = "1",
    description: str = "",
    namespace: str = "",
    agent_kind: str = "",
    kind: ToolContractKind = ToolContractKind.TOOL,
    audience: ToolAudience = ToolAudience.AGENT,
    placement: ToolPlacement = ToolPlacement.MCP_SERVER,
    visibility: ToolVisibility = ToolVisibility.MODEL_VISIBLE,
    capability: Capability = Capability.DESKTOP_INPUT,
    params_model: type[BaseModel] | None = None,
    json_schema: JsonObject | None = None,
    result_model: type[BaseModel] | None = None,
    timeout_seconds: float = 10.0,
    side_effect: bool = True,
    readonly: bool | None = None,
    requires_focus: bool = False,
    risk_level: ToolRiskLevel = ToolRiskLevel.LOW,
    max_payload_bytes: int = 1_000_000,
    binding: ToolBinding | None = None,
    bindings: Sequence[ToolBinding] = (),
    profiles: Sequence[str] = (),
    aliases: Sequence[str] = (),
    source: str = "",
    imported: bool = False,
    audit_tags: Sequence[str] = (),
    resource_requirements: Sequence[str] = (),
    session_requirements: Sequence[str] = (),
    lease_requirements: Sequence[str] = (),
    changelog: str = "",
) -> ToolContract:
    """Define one owned tool contract.

    Args mirror ``ToolContract`` intentionally, but the function is the public
    authoring entry so future validation/defaults can live in one place.
    """

    if params_model is not None and json_schema is not None:
        raise ValueError("define_tool accepts either params_model or json_schema, not both.")
    return ToolContract(
        tool_id=tool_id,
        public_name=public_name,
        version=version,
        description=description,
        namespace=namespace,
        agent_kind=agent_kind,
        kind=kind,
        audience=audience,
        placement=placement,
        visibility=visibility,
        capability=capability,
        params_model=params_model,
        json_schema=json_schema,
        result_model=result_model,
        timeout_seconds=timeout_seconds,
        side_effect=side_effect,
        readonly=readonly,
        requires_focus=requires_focus,
        risk_level=risk_level,
        max_payload_bytes=max_payload_bytes,
        binding=binding,
        bindings=tuple(bindings),
        profiles=tuple(profiles),
        aliases=tuple(aliases),
        source=source,
        imported=imported,
        audit_tags=tuple(audit_tags),
        resource_requirements=tuple(resource_requirements),
        session_requirements=tuple(session_requirements),
        lease_requirements=tuple(lease_requirements),
        changelog=changelog,
    )


def owned_tool(
    *,
    tool_id: str | None = None,
    public_name: str = "",
    version: str = "1",
    description: str = "",
    namespace: str = "",
    agent_kind: str = "",
    kind: ToolContractKind = ToolContractKind.TOOL,
    audience: ToolAudience = ToolAudience.AGENT,
    placement: ToolPlacement = ToolPlacement.LOCAL_HANDLER,
    visibility: ToolVisibility = ToolVisibility.MODEL_VISIBLE,
    capability: Capability = Capability.DESKTOP_INPUT,
    params_model: type[BaseModel] | None = None,
    json_schema: JsonObject | None = None,
    result_model: type[BaseModel] | None = None,
    timeout_seconds: float = 10.0,
    side_effect: bool = True,
    readonly: bool | None = None,
    requires_focus: bool = False,
    risk_level: ToolRiskLevel = ToolRiskLevel.LOW,
    max_payload_bytes: int = 1_000_000,
    binding: ToolBinding | None = None,
    bindings: Sequence[ToolBinding] = (),
    profiles: Sequence[str] = (),
    aliases: Sequence[str] = (),
    source: str = "",
    imported: bool = False,
    audit_tags: Sequence[str] = (),
    resource_requirements: Sequence[str] = (),
    session_requirements: Sequence[str] = (),
    lease_requirements: Sequence[str] = (),
    changelog: str = "",
) -> Callable[[_HandlerT], _HandlerT]:
    """Decorate an owned local handler with its canonical contract.

    The decorator is syntax sugar only: it attaches a ``ToolDefinition`` and
    ``ToolContract`` to the handler for registries/adapters to consume.
    """

    def _decorate(handler: _HandlerT) -> _HandlerT:
        contract_tool_id = tool_id or handler.__name__
        handler_source = source or _handler_path(handler)
        resolved_params_model = params_model or _infer_single_params_model(handler)
        resolved_binding = binding
        if resolved_binding is None and not bindings:
            resolved_binding = DirectHandlerBinding(handler_path=handler_source)
        contract = define_tool(
            tool_id=contract_tool_id,
            public_name=public_name,
            version=version,
            description=description or _doc_summary(handler),
            namespace=namespace,
            agent_kind=agent_kind,
            kind=kind,
            audience=audience,
            placement=placement,
            visibility=visibility,
            capability=capability,
            params_model=resolved_params_model,
            json_schema=json_schema,
            result_model=result_model,
            timeout_seconds=timeout_seconds,
            side_effect=side_effect,
            readonly=readonly,
            requires_focus=requires_focus,
            risk_level=risk_level,
            max_payload_bytes=max_payload_bytes,
            binding=resolved_binding,
            bindings=bindings,
            profiles=profiles,
            aliases=aliases,
            source=handler_source,
            imported=imported,
            audit_tags=audit_tags,
            resource_requirements=resource_requirements,
            session_requirements=session_requirements,
            lease_requirements=lease_requirements,
            changelog=changelog,
        )
        definition = ToolDefinition(contract=contract, handler=handler)
        setattr(handler, "__northcua_tool_contract__", contract)
        setattr(handler, "__northcua_tool_definition__", definition)
        return handler

    return _decorate


def toolset(name: str, tools: Iterable[ToolDefinition | ToolContract | Callable[..., Any]]) -> ToolSet:
    """Create an immutable owned toolset from definitions, contracts, or decorated handlers."""

    definitions: list[ToolDefinition] = []
    for item in tools:
        if isinstance(item, ToolDefinition):
            definitions.append(item)
            continue
        if isinstance(item, ToolContract):
            definitions.append(ToolDefinition(contract=item))
            continue
        definition = getattr(item, "__northcua_tool_definition__", None)
        if isinstance(definition, ToolDefinition):
            definitions.append(definition)
            continue
        raise TypeError(f"Toolset {name!r} contains an item without an owned ToolContract: {item!r}")
    _validate_toolset(name, definitions)
    return ToolSet(name=name, tools=tuple(definitions))


def _validate_toolset(name: str, definitions: Sequence[ToolDefinition]) -> None:
    seen_ids: set[str] = set()
    seen_public_names: dict[str, str] = {}
    for definition in definitions:
        contract = definition.contract
        if contract.tool_id in seen_ids:
            raise ValueError(f"Toolset {name!r} declares duplicate tool_id {contract.tool_id!r}")
        seen_ids.add(contract.tool_id)
        public_name = contract.provider_name
        previous_tool_id = seen_public_names.get(public_name)
        if previous_tool_id is not None:
            raise ValueError(
                f"Toolset {name!r} declares duplicate public tool name {public_name!r} "
                f"for {previous_tool_id!r} and {contract.tool_id!r}"
            )
        seen_public_names[public_name] = contract.tool_id


def _infer_single_params_model(handler: Callable[..., Any]) -> type[BaseModel] | None:
    signature = inspect.signature(handler)
    parameters = [
        parameter
        for parameter in signature.parameters.values()
        if parameter.kind
        in {
            inspect.Parameter.POSITIONAL_ONLY,
            inspect.Parameter.POSITIONAL_OR_KEYWORD,
            inspect.Parameter.KEYWORD_ONLY,
        }
    ]
    if not parameters:
        return None
    if len(parameters) != 1:
        raise ValueError(
            f"Owned tool handler {_handler_path(handler)!r} must provide params_model/json_schema "
            "or accept one pydantic BaseModel parameter."
        )
    annotation = _resolved_annotation(handler, parameters[0].name)
    if isinstance(annotation, type) and issubclass(annotation, BaseModel):
        return annotation
    raise ValueError(
        f"Owned tool handler {_handler_path(handler)!r} must provide params_model/json_schema "
        "or annotate its single parameter with a pydantic BaseModel."
    )


def _handler_path(handler: Callable[..., Any]) -> str:
    return f"{handler.__module__}:{handler.__qualname__}"


def _resolved_annotation(handler: Callable[..., Any], parameter_name: str) -> object:
    try:
        return get_type_hints(handler).get(parameter_name, inspect.Parameter.empty)
    except (NameError, TypeError):
        signature = inspect.signature(handler)
        return signature.parameters[parameter_name].annotation


def _doc_summary(handler: Callable[..., Any]) -> str:
    doc = inspect.getdoc(handler) or ""
    return doc.splitlines()[0] if doc else ""


__all__ = ["ToolDefinition", "ToolSet", "define_tool", "owned_tool", "toolset"]
