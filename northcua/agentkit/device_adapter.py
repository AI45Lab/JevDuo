"""Generic MCP-backed device runtime adapter for AgentKit tools."""

from __future__ import annotations

import inspect
import logging
from dataclasses import replace
from pathlib import Path
from typing import Protocol, cast

from pydantic import BaseModel, ValidationError

from northcua.agentkit.device_artifacts import AgentKitCommandArtifactExtractor
from northcua.agentkit.imports import resolve_dotted
from northcua.agentkit.registry import load_all_agent_specs
from northcua.agentkit.session_context import AGENTKIT_SETTINGS_PARAM
from northcua.agentkit.spec import ProbeUseRef, SessionAdapter, ToolUseRef
from northcua.runtime.mcp.invocation_context import MCPInvocationContext, use_mcp_invocation_context
from northcua.runtime.mcp.registry import MCPFactoryContext, NorthCUAMCPRegistry
from northcua.runtime.mcp.result_adapter import normalize_tool_result
from northcua_protocol.capabilities import Capability
from northcua_protocol.capabilities import JsonObject as ProtocolJsonObject
from northcua_protocol.capabilities import PlatformKind, ToolDescriptor, ToolRiskLevel
from northcua_protocol.payloads import CommandRequest, CommandResult
from northcua_protocol.runtime_sidecars import attach_runtime_sidecar
from northcua_protocol.state import CommandState
from northcua_runtime_shared import RuntimeHealth
from northcua_runtime_shared.tool_contracts import (
    DirectHandlerBinding,
    McpBinding,
    ToolAudience,
    ToolContract,
    ToolContractKind,
    ToolDispatcher,
    ToolDispatchPolicy,
    ToolPlacement,
    ToolVisibility,
)

logger = logging.getLogger(__name__)


class _McpServer(Protocol):
    async def call_tool(self, tool_name: str, parameters: dict[str, object]) -> object:
        """Invoke one registered MCP tool.

        Args:
            tool_name: Runtime tool name registered by the MCP server.
            parameters: JSON-like parameters supplied by the controller.

        Returns:
            Raw MCP result before protocol normalization.
        """


class AgentKitMCPRuntimeAdapter:
    """Expose AgentKit-declared MCP tools through the device protocol."""

    def __init__(
        self,
        *,
        agent_kinds: set[str] | None = None,
        platforms: set[PlatformKind] | None = None,
        artifact_root: str | Path = "logs/device-agentkit",
        max_allowed_risk: ToolRiskLevel | None = ToolRiskLevel.HIGH,
    ) -> None:
        self.agent_kinds = set(agent_kinds) if agent_kinds is not None else None
        self.platforms = set(platforms) if platforms is not None else None
        self.artifact_root = Path(artifact_root)
        self.max_allowed_risk = max_allowed_risk
        enabled_specs = tuple(load_all_agent_specs().enabled())
        candidates = [
            (spec.kind, item)
            for spec in enabled_specs
            if self.agent_kinds is None or spec.kind in self.agent_kinds
            if self.platforms is None or spec.runtime.platform in self.platforms
            for item in spec.all_device_tools()
            if item.server_name or item.handler is not None
        ]
        name_counts: dict[str, int] = {}
        for _, item in candidates:
            name_counts[item.tool_id] = name_counts.get(item.tool_id, 0) + 1
        self._tool_specs: dict[str, ToolUseRef | ProbeUseRef] = {}
        self._tool_contracts: dict[str, ToolContract] = {}
        self._tool_names: dict[str, str] = {}
        self._tool_agent_kinds: dict[str, str] = {}
        self._session_adapters: dict[tuple[str, str], SessionAdapter] = {}
        self._session_close_tool_ids: set[str] = set()
        self._artifact_extractor = AgentKitCommandArtifactExtractor()
        selected_agent_kinds = {agent_kind for agent_kind, _ in candidates}
        for agent_kind, item in candidates:
            prefixed_tool_id = f"{agent_kind}.{item.tool_id}"
            self._add_tool_spec(prefixed_tool_id, item, agent_kind=agent_kind)
            if name_counts[item.tool_id] == 1:
                self._add_tool_spec(item.tool_id, item, agent_kind=agent_kind)
        for spec in enabled_specs:
            if spec.kind not in selected_agent_kinds or spec.session.adapter_cls is None:
                continue
            close_tool_name = spec.session.close_tool_name.strip()
            if not close_tool_name:
                continue
            close_tool_id = f"{spec.kind}.{close_tool_name}"
            self._add_tool_spec(
                close_tool_id,
                _session_close_tool_spec(
                    agent_kind=spec.kind,
                    close_tool_name=close_tool_name,
                    capability=next(iter(spec.runtime.capabilities), Capability.DESKTOP_INPUT),
                ),
                agent_kind=spec.kind,
            )
            self._session_close_tool_ids.add(close_tool_id)
        self._servers_by_name: dict[str, _McpServer] = {}

    @property
    def capabilities(self) -> set[Capability]:
        """Return capabilities exposed by the selected AgentKit tools.

        Returns:
            Capability set advertised to the controller for device matching.
        """

        return {contract.capability for contract in self._tool_contracts.values()}

    @property
    def tool_contracts(self) -> list[ToolContract]:
        """Return contract-backed controller tool surface for selected AgentKit tools."""

        return [contract for _, contract in sorted(self._tool_contracts.items())]

    @property
    def tools(self) -> list[ToolDescriptor]:
        """Return controller tool descriptors for selected AgentKit tools.

        Returns:
            Protocol descriptors for every selected tool id, including
            prefixed aliases used to avoid cross-agent collisions.
        """

        return [contract.to_descriptor() for contract in self.tool_contracts]

    async def healthcheck(self) -> RuntimeHealth:
        """Check whether every advertised MCP server can be built.

        Returns:
            Runtime health with per-capability status for controller leases.
        """

        try:
            for tool_id, spec in self._tool_specs.items():
                self._check_tool_buildable(tool_id, spec)
        except Exception as exc:  # pragma: no cover - runtime/environment boundary
            logger.warning("AgentKit device healthcheck failed", exc_info=True)
            return RuntimeHealth(
                healthy=False, message=str(exc), capability_health={cap: False for cap in self.capabilities}
            )
        return RuntimeHealth(healthy=True, capability_health={cap: True for cap in self.capabilities})

    def _check_tool_buildable(self, tool_id: str, spec: ToolUseRef | ProbeUseRef) -> None:
        if tool_id in self._session_close_tool_ids:
            return
        _ensure_registration(spec.registration_hook)
        if spec.server_name:
            self._server(spec.server_name)
        elif spec.handler is None:
            raise RuntimeError(f"AgentKit tool has no handler or MCP server: {spec.tool_id}")

    async def execute(self, command: CommandRequest) -> CommandResult:
        """Execute one AgentKit MCP tool command.

        Args:
            command: Controller command containing tool id, session id, and
                JSON-like parameters.

        Returns:
            Protocol command result with normalized payload and extracted
            artifacts.
        """

        spec = self._tool_specs.get(command.tool_id)
        if spec is None:
            return _failure(command, f"unknown AgentKit tool_id: {command.tool_id}")
        runtime_tool_name = self._tool_names.get(command.tool_id, spec.tool_id)
        raw_parameters = dict(command.parameters)
        raw_parameters.pop(AGENTKIT_SETTINGS_PARAM, None)
        try:
            if command.tool_id in self._session_close_tool_ids:
                self._validated_parameters(command.tool_id, raw_parameters)
                return await self._close_session(command, self._tool_agent_kinds.get(command.tool_id, ""))
            _ensure_registration(spec.registration_hook)
            raw_parameters = self._validated_parameters(command.tool_id, raw_parameters)
            self._ensure_session(command, self._tool_agent_kinds.get(command.tool_id, ""), spec)
            if spec.server_name:
                server = self._server(spec.server_name)
                with use_mcp_invocation_context(MCPInvocationContext(session_id=command.session_id)):
                    raw = await server.call_tool(runtime_tool_name, raw_parameters)
            else:
                with use_mcp_invocation_context(MCPInvocationContext(session_id=command.session_id)):
                    raw = await _call_declared_handler(spec, raw_parameters)
            normalized = normalize_tool_result(raw)
            normalized, artifacts = self._artifact_extractor.extract(command.command_id, normalized)
            normalized = _attach_agent_runtime_sidecar(
                agent_kind=self._tool_agent_kinds.get(command.tool_id, ""),
                command=command,
                payload=normalized,
            )
            return CommandResult(
                command_id=command.command_id,
                session_id=command.session_id,
                lease_id=command.lease_id,
                device_id=command.device_id,
                lease_generation=command.lease_generation,
                status=CommandState.SUCCEEDED,
                result=normalized if isinstance(normalized, (dict, str)) or normalized is None else str(normalized),
                artifacts=artifacts,
            )
        except ValidationError as exc:
            web_tool_error = _model_visible_web_validation_error(
                command, spec, self._tool_agent_kinds.get(command.tool_id, ""), exc
            )
            if web_tool_error is not None:
                return web_tool_error
            logger.warning("AgentKit MCP command validation failed", extra={"tool_id": command.tool_id}, exc_info=True)
            return _failure(command, str(exc))
        except Exception as exc:  # pragma: no cover - runtime boundary
            logger.warning("AgentKit MCP command failed", extra={"tool_id": command.tool_id}, exc_info=True)
            return _failure(command, str(exc))

    def _validated_parameters(self, tool_id: str, parameters: dict[str, object]) -> dict[str, object]:
        contract = self._tool_contracts[tool_id]
        envelope = ToolDispatcher().prepare(
            contract,
            parameters,
            policy=ToolDispatchPolicy(max_allowed_risk=self.max_allowed_risk),
        )
        validated = envelope.validated_parameters
        if isinstance(validated, BaseModel):
            return dict(validated.model_dump(mode="json"))
        return dict(validated)

    def _add_tool_spec(self, tool_id: str, spec: ToolUseRef | ProbeUseRef, *, agent_kind: str) -> None:
        if tool_id in self._tool_specs:
            raise ValueError(f"Duplicate AgentKit device tool_id: {tool_id}")
        contract = _device_contract_for_spec(tool_id=tool_id, spec=spec, agent_kind=agent_kind)
        self._tool_specs[tool_id] = spec
        self._tool_contracts[tool_id] = contract
        self._tool_names[tool_id] = spec.tool_id
        self._tool_agent_kinds[tool_id] = agent_kind

    def _server(self, server_name: str) -> _McpServer:
        server = self._servers_by_name.get(server_name)
        if server is None:
            server = cast(
                _McpServer,
                NorthCUAMCPRegistry.build(
                    server_name,
                    context=MCPFactoryContext(process_name=f"northcua-device:{server_name}"),
                ),
            )
            self._servers_by_name[server_name] = server
        return server

    def _ensure_session(self, command: CommandRequest, agent_kind: str, spec: ToolUseRef | ProbeUseRef) -> None:
        del spec
        if not agent_kind:
            return
        agent_spec = load_all_agent_specs().maybe_get(agent_kind)
        if agent_spec is None or agent_spec.session.adapter_cls is None:
            return
        adapter = self._session_adapter(agent_kind, command=command)
        if adapter is None:
            return
        adapter.ensure_device_session(command=command, artifact_root=self.artifact_root)

    def _session_adapter(self, agent_kind: str, *, command: CommandRequest) -> SessionAdapter | None:
        agent_spec = load_all_agent_specs().maybe_get(agent_kind)
        if agent_spec is None or agent_spec.session.adapter_cls is None:
            return None
        key = (agent_kind, command.session_id)
        adapter = self._session_adapters.get(key)
        if adapter is None:
            raw_settings = command.parameters.get(AGENTKIT_SETTINGS_PARAM)
            adapter_cls = cast(type[SessionAdapter], resolve_dotted(agent_spec.session.adapter_cls))
            adapter = cast(
                SessionAdapter,
                adapter_cls(
                    agent_kind=agent_kind,
                    session_id=command.session_id,
                    settings=raw_settings if isinstance(raw_settings, dict) else None,
                    artifact_root=self.artifact_root,
                ),
            )
            self._session_adapters[key] = adapter
        return adapter

    async def _close_session(self, command: CommandRequest, agent_kind: str) -> CommandResult:
        try:
            adapter = self._session_adapter(agent_kind, command=command)
            result: object = {"status": "ok", "text": f"closed {agent_kind or 'agent'} session"}
            if adapter is not None:
                result = await adapter.close_device_session(command=command, artifact_root=self.artifact_root)
            if isinstance(result, dict):
                protocol_result: ProtocolJsonObject | str | None = cast(
                    ProtocolJsonObject,
                    {str(key): value for key, value in result.items()},
                )
            elif isinstance(result, str) or result is None:
                protocol_result = result
            else:
                protocol_result = str(result)
            return CommandResult(
                command_id=command.command_id,
                session_id=command.session_id,
                lease_id=command.lease_id,
                device_id=command.device_id,
                lease_generation=command.lease_generation,
                status=CommandState.SUCCEEDED,
                result=protocol_result,
            )
        finally:
            self._session_adapters.pop((agent_kind, command.session_id), None)


async def _call_declared_handler(spec: ToolUseRef | ProbeUseRef, parameters: dict[str, object]) -> object:
    handler = spec.handler
    if handler is None:
        raise RuntimeError(f"AgentKit tool has no handler or MCP server: {spec.tool_id}")
    if spec.params_model is not None:
        result = handler(spec.params_model.model_validate(parameters))
    else:
        result = handler(**parameters)
    if inspect.isawaitable(result):
        return await result
    return result


def _ensure_registration(registration_hook: str) -> None:
    if registration_hook:
        hook = resolve_dotted(registration_hook)
        if callable(hook):
            hook()


def _attach_agent_runtime_sidecar(*, agent_kind: str, command: CommandRequest, payload: object) -> object:
    if agent_kind != "web":
        return payload
    sidecar = _web_runtime_sidecar(command.session_id)
    if not sidecar:
        return payload
    return attach_runtime_sidecar(payload, {"web_ref_aliases": sidecar})


def _model_visible_web_validation_error(
    command: CommandRequest,
    spec: ToolUseRef | ProbeUseRef,
    agent_kind: str,
    exc: ValidationError,
) -> CommandResult | None:
    """Return a model-facing Web tool error for schema failures.

    Model-visible Web actions are allowed to fail as recoverable tool results:
    the agent can read the error, correct its arguments, and continue. Treating
    those mistakes as device command failures pollutes controller telemetry and
    bypasses the Web action service's retryable error contract.
    """

    if agent_kind != "web" or not isinstance(spec, ToolUseRef) or not spec.is_model_visible:
        return None
    try:
        from northcua.agents.web.domain.actions import action_from_tool_name
        from northcua.agents.web.mcp.error_classification import build_web_tool_error_payload
    except Exception:  # pragma: no cover - import boundary
        return None
    action = action_from_tool_name(spec.tool_id)
    if action is None:
        return None
    payload = cast(ProtocolJsonObject, build_web_tool_error_payload(action, exc))
    return CommandResult(
        command_id=command.command_id,
        session_id=command.session_id,
        lease_id=command.lease_id,
        device_id=command.device_id,
        lease_generation=command.lease_generation,
        status=CommandState.SUCCEEDED,
        result=payload,
    )


def _web_runtime_sidecar(session_id: str) -> ProtocolJsonObject:
    try:
        from northcua.agents.web.session import WebAgentSessionRegistry
    except Exception:
        return {}
    try:
        aliases = WebAgentSessionRegistry().get_registered_session(session_id).current_ref_aliases
    except LookupError:
        return {}
    if not aliases.aliases and not aliases.observation_id:
        return {}
    return {
        "observation_id": aliases.observation_id,
        "alias_epoch": aliases.alias_epoch,
        "aliases": [alias.model_dump(mode="json") for alias in aliases.aliases],
    }


def _session_close_tool_spec(*, agent_kind: str, close_tool_name: str, capability: Capability) -> ToolUseRef:
    contract = ToolContract(
        tool_id=close_tool_name,
        public_name=close_tool_name,
        description=f"Close and unregister a device-side {agent_kind} session.",
        namespace="agentkit.session",
        agent_kind=agent_kind,
        kind=ToolContractKind.TOOL,
        audience=ToolAudience.RUNTIME,
        placement=ToolPlacement.DEVICE_COMMAND,
        visibility=ToolVisibility.RUNTIME_ONLY,
        capability=capability,
        json_schema={"type": "object", "properties": {}, "additionalProperties": False},
        timeout_seconds=5.0,
        side_effect=True,
        readonly=False,
        risk_level=ToolRiskLevel.LOW,
        binding=DirectHandlerBinding(
            handler_path="northcua.agentkit.device_adapter:AgentKitMCPRuntimeAdapter._close_session"
        ),
        source="northcua.agentkit.device_adapter",
    )
    return ToolUseRef.from_contract(contract, model_visible=False)


def _device_contract_for_spec(*, tool_id: str, spec: ToolUseRef | ProbeUseRef, agent_kind: str) -> ToolContract:
    contract = spec.contract
    if contract.tool_id != tool_id:
        return replace(
            contract,
            tool_id=tool_id,
            public_name=tool_id,
            agent_kind=contract.agent_kind or agent_kind,
            aliases=tuple(dict.fromkeys((contract.tool_id, *contract.aliases))),
        )
    return contract


def _handler_path(handler: object) -> str:
    if handler is None:
        return ""
    module = getattr(handler, "__module__", "")
    qualname = getattr(handler, "__qualname__", "")
    return f"{module}:{qualname}" if module and qualname else ""


def _failure(command: CommandRequest, error: str) -> CommandResult:
    return CommandResult(
        command_id=command.command_id,
        session_id=command.session_id,
        lease_id=command.lease_id,
        device_id=command.device_id,
        lease_generation=command.lease_generation,
        status=CommandState.FAILED,
        error=error,
    )
