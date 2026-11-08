"""Device-side websocket command execution routed through registered MCP servers."""

from __future__ import annotations

import logging

from pydantic import BaseModel

try:
    from typing import Protocol, TypeAlias, cast
except ImportError:  # pragma: no cover - compatibility for older ad-hoc test interpreters
    from typing_extensions import Protocol, TypeAlias, cast

from northcua.app.logging import log_runtime_event
from northcua.core.coordinates import CoordinatePolicy, CoordinateTarget, resolve_coordinate_policy
from northcua.core.enums import CoordinateSpace, ResultStatus
from northcua.core.models import CommandExecutionResultRecord
from northcua.core.protocol import CommandRequestPayload, CommandResultPayload
from northcua.runtime.mcp.agent_extensions import run_agent_mcp_registration_hook
from northcua.runtime.mcp.builtin_servers import ensure_builtin_mcp_servers_registered
from northcua.runtime.mcp.desktop_native.state import DesktopNativeServerState
from northcua.runtime.mcp.invocation_context import MCPInvocationContext, use_mcp_invocation_context
from northcua.runtime.mcp.registry import MCPFactoryContext, NorthCUAMCPRegistry
from northcua.runtime.mcp.result_adapter import normalize_tool_result
from northcua_protocol.capabilities import ToolRiskLevel
from northcua_runtime_shared.tool_contracts import ToolContract, ToolDispatcher, ToolDispatchPolicy

logger = logging.getLogger(__name__)

JsonValue: TypeAlias = object
CommandParameters: TypeAlias = dict[str, JsonValue]


class _McpServer(Protocol):
    async def call_tool(self, tool_name: str, parameters: dict[str, object]) -> object: ...


class NorthCUADeviceCommandExecutor:
    """Execute websocket command requests through the same local MCP servers as NorthCUA."""

    def __init__(
        self,
        *,
        allowed_server_names: set[str] | None = None,
        max_allowed_risk: ToolRiskLevel | None = ToolRiskLevel.HIGH,
    ) -> None:
        ensure_builtin_mcp_servers_registered()
        self._allowed_server_names = allowed_server_names
        self._max_allowed_risk = max_allowed_risk
        self._context = MCPFactoryContext(process_name="northcua-device")
        self._servers_by_registry_name: dict[str, _McpServer] = {
            name: cast(_McpServer, NorthCUAMCPRegistry.build(name, context=self._context))
            for name in NorthCUAMCPRegistry.names()
            if allowed_server_names is None or name in allowed_server_names
            if NorthCUAMCPRegistry.tool_contracts(name)
        }
        self.data_server = self._servers_by_registry_name.get("DesktopNativeDataExecutor")
        self.action_server = self._servers_by_registry_name.get("DesktopNativeActionExecutor")
        self.cli_server = self._servers_by_registry_name.get("CommandLineExecutor")
        self.browser_server = self._servers_by_registry_name.get("PlaywrightBrowserExecutor")
        self._contract_by_server_tool: dict[tuple[str, str], ToolContract] = {}
        self._mcp_tool_name_by_server_tool: dict[tuple[str, str], str] = {}
        self._server_name_by_tool_name: dict[str, str] = {}
        self._ambiguous_tool_names: set[str] = set()
        self._index_registered_tool_contracts()

    async def execute_command_request(self, payload: CommandRequestPayload) -> CommandResultPayload:
        """Execute all commands in one request and return typed command results.

        Args:
            payload: Batched command request received from the device channel.

        Returns:
            Batched command result payload preserving each command call ID.
        """

        self._apply_payload_coordinate_policy(payload)
        log_runtime_event(
            logger,
            "device_executor_batch_started",
            level=logging.DEBUG,
            session_id=payload.session_id,
            command_count=len(payload.commands),
            tool_names=[command.tool_name for command in payload.commands],
        )
        results = [
            await self._execute_command(
                command.call_id,
                command.tool_name,
                command.parameters,
                server_name=command.server_name,
                registration_hook=command.registration_hook,
                invocation_context=MCPInvocationContext(
                    session_id=payload.session_id,
                    task_name=str(command.tool_type or ""),
                ),
            )
            for command in payload.commands
        ]
        overall_status = (
            ResultStatus.FAILURE
            if any(result.status == ResultStatus.FAILURE for result in results)
            else ResultStatus.SUCCESS
        )
        log_runtime_event(
            logger,
            "device_executor_batch_completed",
            level=logging.DEBUG,
            session_id=payload.session_id,
            command_count=len(payload.commands),
            overall_status=overall_status,
            failure_count=sum(1 for result in results if result.status == ResultStatus.FAILURE),
        )
        return CommandResultPayload(
            session_id=payload.session_id,
            status=overall_status,
            results=results,
        )

    @staticmethod
    def _apply_payload_coordinate_policy(payload: CommandRequestPayload) -> None:
        """Apply command-batch coordinate metadata to desktop MCP state."""

        policy = resolve_coordinate_policy(
            CoordinateTarget.DESKTOP,
            context_values={str(key): str(value) for key, value in payload.metadata.items()},
            fallback_policy=CoordinatePolicy(input_space=CoordinateSpace.IMAGE_PIXELS),
        )
        DesktopNativeServerState().set_coordinate_policy(policy)

    async def _execute_command(
        self,
        call_id: str,
        tool_name: str,
        parameters: CommandParameters,
        *,
        server_name: str = "",
        registration_hook: str = "",
        invocation_context: MCPInvocationContext = MCPInvocationContext(),
    ) -> CommandExecutionResultRecord:
        try:
            server = await self._resolve_server(
                tool_name,
                server_name=server_name,
                registration_hook=registration_hook,
            )
            registry_server_name = self._registry_server_name_for_tool(tool_name, server_name=server_name)
            dispatch_tool_name = self._dispatch_tool_name_for_tool(tool_name, server_name=registry_server_name)
            parameters = self._validated_parameters(
                tool_name,
                parameters,
                server_name=registry_server_name,
            )
            with use_mcp_invocation_context(invocation_context):
                tool_result = await server.call_tool(dispatch_tool_name, parameters)
            result = normalize_tool_result(tool_result)
            log_runtime_event(
                logger,
                "device_executor_command_succeeded",
                level=logging.DEBUG,
                call_id=call_id,
                tool_name=tool_name,
                server_name=server_name or None,
                dispatch_tool_name=dispatch_tool_name,
                server_type=type(server).__name__,
                parameter_keys=sorted(parameters.keys()),
                result_type=type(result).__name__,
            )
            return CommandExecutionResultRecord(
                call_id=call_id,
                status=ResultStatus.SUCCESS,
                result=result,
            )
        except Exception as exc:  # pragma: no cover - defensive boundary
            logger.exception("NorthCUA device command failed for %s", tool_name)
            log_runtime_event(
                logger,
                "device_executor_command_failed",
                level=logging.ERROR,
                call_id=call_id,
                tool_name=tool_name,
                server_name=server_name or None,
                error=str(exc),
                parameter_keys=sorted(parameters.keys()),
            )
            return CommandExecutionResultRecord(
                call_id=call_id,
                status=ResultStatus.FAILURE,
                error=str(exc),
            )

    async def _resolve_server(
        self,
        tool_name: str,
        *,
        server_name: str = "",
        registration_hook: str = "",
    ) -> _McpServer:
        """Resolve the owning MCP server from contract metadata."""

        if server_name:
            return self._server_by_name(server_name, registration_hook=registration_hook)
        registry_server_name = self._registry_server_name_for_tool(tool_name)
        return self._server_by_name(registry_server_name, registration_hook=registration_hook)

    def _server_by_name(self, server_name: str, *, registration_hook: str = "") -> _McpServer:
        server = self._servers_by_registry_name.get(server_name)
        if server is not None:
            return server
        if registration_hook:
            run_agent_mcp_registration_hook(registration_hook)
        if self._allowed_server_names is not None and server_name not in self._allowed_server_names:
            raise ValueError(f"Device-side MCP server is not allowed: {server_name}")
        if NorthCUAMCPRegistry.is_registered(server_name):
            if not NorthCUAMCPRegistry.tool_contracts(server_name):
                raise ValueError(
                    f"Device-side MCP server {server_name!r} has no ToolContract metadata; "
                    "descriptor-only MCP execution is disabled"
                )
            server = cast(_McpServer, NorthCUAMCPRegistry.build(server_name, context=self._context))
            self._servers_by_registry_name[server_name] = server
            self._index_registered_tool_contracts()
            return server
        raise ValueError(f"Unknown device-side MCP server: {server_name}")

    def _index_registered_tool_contracts(self) -> None:
        self._ensure_tool_index_state()
        self._contract_by_server_tool.clear()
        self._mcp_tool_name_by_server_tool.clear()
        self._server_name_by_tool_name.clear()
        self._ambiguous_tool_names.clear()
        for server_name in self._servers_by_registry_name:
            for contract in NorthCUAMCPRegistry.tool_contracts(server_name):
                dispatch_tool_name = contract.provider_name
                for tool_name in _contract_runtime_names(contract):
                    key = (server_name, tool_name)
                    previous_contract = self._contract_by_server_tool.get(key)
                    if previous_contract is not None and previous_contract.tool_id != contract.tool_id:
                        raise ValueError(
                            f"Duplicate device-side runtime tool name {tool_name!r} on MCP server {server_name!r}: "
                            f"{previous_contract.tool_id!r} vs {contract.tool_id!r}"
                        )
                    self._contract_by_server_tool[key] = contract
                    self._mcp_tool_name_by_server_tool[key] = dispatch_tool_name
                    previous_server_name = self._server_name_by_tool_name.get(tool_name)
                    if previous_server_name is not None and previous_server_name != server_name:
                        self._ambiguous_tool_names.add(tool_name)
                    else:
                        self._server_name_by_tool_name[tool_name] = server_name

    def _registry_server_name_for_tool(self, tool_name: str, *, server_name: str = "") -> str:
        self._ensure_tool_index_state()
        if not self._contract_by_server_tool:
            self._index_registered_tool_contracts()
        if server_name:
            key = (server_name, tool_name)
            if key not in self._contract_by_server_tool:
                raise ValueError(f"Tool {tool_name!r} is not registered on MCP server {server_name!r}")
            return server_name
        if tool_name in self._ambiguous_tool_names:
            raise ValueError(f"Ambiguous device-side tool {tool_name!r}; server_name is required")
        resolved = self._server_name_by_tool_name.get(tool_name)
        if resolved is None:
            raise ValueError(f"Unknown device-side tool: {tool_name}")
        return resolved

    def _dispatch_tool_name_for_tool(self, tool_name: str, *, server_name: str) -> str:
        self._ensure_tool_index_state()
        key = (server_name, tool_name)
        dispatch_tool_name = self._mcp_tool_name_by_server_tool.get(key)
        if dispatch_tool_name is None:
            raise ValueError(f"Tool {tool_name!r} is not registered on MCP server {server_name!r}")
        return dispatch_tool_name

    def _validated_parameters(
        self,
        tool_name: str,
        parameters: CommandParameters,
        *,
        server_name: str,
    ) -> CommandParameters:
        self._ensure_tool_index_state()
        key = (server_name, tool_name)
        contract = self._contract_by_server_tool.get(key)
        if contract is not None:
            envelope = ToolDispatcher().prepare(
                contract,
                dict(parameters),
                policy=ToolDispatchPolicy(max_allowed_risk=getattr(self, "_max_allowed_risk", ToolRiskLevel.HIGH)),
            )
            validated = envelope.validated_parameters
            if isinstance(validated, BaseModel):
                return dict(validated.model_dump(mode="json"))
            return dict(validated)
        raise ValueError(
            f"Device-side MCP tool {server_name}.{tool_name} lacks ToolContract metadata; "
            "descriptor-only MCP execution is disabled"
        )

    def _ensure_tool_index_state(self) -> None:
        if not hasattr(self, "_contract_by_server_tool"):
            self._contract_by_server_tool = {}
        if not hasattr(self, "_mcp_tool_name_by_server_tool"):
            self._mcp_tool_name_by_server_tool = {}
        if not hasattr(self, "_server_name_by_tool_name"):
            self._server_name_by_tool_name = {}
        if not hasattr(self, "_ambiguous_tool_names"):
            self._ambiguous_tool_names = set()


def _contract_runtime_names(contract: ToolContract) -> tuple[str, ...]:
    names = [contract.provider_name, contract.tool_id, *contract.aliases]
    return tuple(dict.fromkeys(name for name in names if name))
