"""Shared helpers for bundled AgentKit specs."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

from northcua.runtime.mcp.agent_extensions import run_agent_mcp_registration_hook
from northcua.runtime.mcp.registry import NorthCUAMCPRegistry
from northcua_runtime_shared.tool_contracts import ToolContract

from .spec import AgentCliArgumentSpec, ProbeUseRef, ToolUseRef


class BuiltinAgentCliArguments:
    """Factory for common CLI argument specs shared by built-in agents."""

    def serve_args(self) -> tuple[AgentCliArgumentSpec, ...]:
        """Return common local ``serve`` CLI arguments.

        Returns:
            Argument specs for app config, session id, and artifact root.
        """

        return (
            AgentCliArgumentSpec(("--app-config",), dest="app_config_path", default=None, help="Path to app.yaml"),
            AgentCliArgumentSpec(("--session-id",), dest="session_id", default="", help="Optional session id"),
            AgentCliArgumentSpec(("--log-dir",), dest="log_dir", default=None, help="Artifact root override"),
        )

    def controller_args(self) -> tuple[AgentCliArgumentSpec, ...]:
        """Return common remote submitter CLI arguments.

        Returns:
            Argument specs for controller URL, auth, wait behavior, and target
            device selection.
        """

        return (
            AgentCliArgumentSpec(
                ("--controller",), dest="controller_url", required=True, help="Controller websocket URL"
            ),
            AgentCliArgumentSpec(("--controller-token",), dest="controller_token", default="", help="Submitter token"),
            AgentCliArgumentSpec(
                ("--wait",),
                dest="wait_for_controller_result",
                action="store_true",
                help="Wait for controller TASK_FINISHED",
            ),
            AgentCliArgumentSpec(
                ("--controller-wait-timeout",),
                dest="controller_wait_timeout",
                type=float,
                default=300.0,
                help="Seconds to wait for controller completion",
            ),
            AgentCliArgumentSpec(("--target-device-id",), dest="target_device_id", default="", help="Strict device id"),
            AgentCliArgumentSpec(
                ("--target-capability",),
                dest="target_capabilities",
                action="append",
                default=[],
                help="Required controller capability; repeatable",
            ),
        )


@dataclass(slots=True, frozen=True)
class ToolContractProjector:
    """Project typed tool contracts into AgentKit specs."""

    def tool_specs(
        self, contracts: Iterable[ToolContract], *, model_visible: bool | None = None
    ) -> tuple[ToolUseRef, ...]:
        """Project tool contracts into AgentKit tool specs."""

        specs: list[ToolUseRef] = []
        registered_hooks: set[str] = set()
        for contract in contracts:
            if contract.registration_hook and contract.registration_hook not in registered_hooks:
                run_agent_mcp_registration_hook(contract.registration_hook)
                registered_hooks.add(contract.registration_hook)
            if model_visible is True and not contract.model_visible:
                raise ValueError(
                    f"Cannot force runtime/operator-only tool contract {contract.tool_id!r} to model-visible"
                )
            visible = contract.model_visible if model_visible is None else model_visible
            specs.append(
                ToolUseRef.from_contract(
                    contract,
                    model_visible=visible,
                )
            )
        return tuple(specs)

    def probe_specs(
        self,
        contracts: Iterable[ToolContract],
        *,
        default: bool = True,
        persist: bool = True,
        prompt: bool = True,
    ) -> tuple[ProbeUseRef, ...]:
        """Project probe contracts into AgentKit probe specs."""

        specs: list[ProbeUseRef] = []
        registered_hooks: set[str] = set()
        for contract in contracts:
            if contract.registration_hook and contract.registration_hook not in registered_hooks:
                run_agent_mcp_registration_hook(contract.registration_hook)
                registered_hooks.add(contract.registration_hook)
            specs.append(
                self._probe_spec_from_contract(
                    contract,
                    default=default,
                    persist=persist,
                    prompt=prompt,
                )
            )
        return tuple(specs)

    @staticmethod
    def _probe_spec_from_contract(
        contract: ToolContract,
        *,
        default: bool,
        persist: bool,
        prompt: bool,
    ) -> ProbeUseRef:
        return ProbeUseRef.from_contract(
            contract,
            default=default,
            persist=persist,
            prompt=prompt,
        )


class MCPServerSpecProjector:
    """Project globally registered MCP server contracts into AgentKit specs."""

    def tool_specs(
        self,
        server_name: str,
        *,
        registration_hook: str = "",
        model_visible: bool = True,
    ) -> tuple[ToolUseRef, ...]:
        """Project one MCP server's registered contracts into tool specs.

        Args:
            server_name: Registered NorthCUA MCP server name.
            registration_hook: Optional hook to run before descriptor lookup.
            model_visible: Whether projected tools should be visible to the model.

        Returns:
            Tool specs for contracts registered by the server.
        """

        self._run_registration_hook(registration_hook)
        contracts = NorthCUAMCPRegistry.tool_contracts(server_name)
        if contracts:
            selected_contracts = tuple(
                contract for contract in contracts if contract.model_visible or not model_visible
            )
            return ToolContractProjector().tool_specs(selected_contracts, model_visible=model_visible)
        raise ValueError(f"MCP server {server_name!r} has no ToolContract metadata for AgentKit projection.")

    def probe_specs(
        self,
        server_name: str,
        *,
        registration_hook: str = "",
        default: bool = True,
        persist: bool = True,
        prompt: bool = True,
    ) -> tuple[ProbeUseRef, ...]:
        """Project one MCP server's registered contracts into probe specs.

        Args:
            server_name: Registered NorthCUA MCP server name.
            registration_hook: Optional hook to run before descriptor lookup.
            default: Whether probes run by default.
            persist: Whether probe outputs should be persisted.
            prompt: Whether probe outputs should be projected into prompts.

        Returns:
            Probe specs for contracts registered by the server.
        """

        self._run_registration_hook(registration_hook)
        contracts = NorthCUAMCPRegistry.tool_contracts(server_name)
        if contracts:
            return ToolContractProjector().probe_specs(
                contracts,
                default=default,
                persist=persist,
                prompt=prompt,
            )
        raise ValueError(f"MCP server {server_name!r} has no ToolContract metadata for AgentKit projection.")

    def tool_ids(self, server_name: str, *, registration_hook: str = "") -> tuple[str, ...]:
        """Return protocol tool ids advertised by one MCP server.

        Args:
            server_name: Registered NorthCUA MCP server name.
            registration_hook: Optional hook to run before descriptor lookup.

        Returns:
            Tool ids in registry order.
        """

        self._run_registration_hook(registration_hook)
        contracts = NorthCUAMCPRegistry.tool_contracts(server_name)
        if not contracts:
            raise ValueError(f"MCP server {server_name!r} has no ToolContract metadata for AgentKit projection.")
        return tuple(contract.tool_id for contract in contracts)

    def tool_specs_from_servers(
        self,
        server_names: Iterable[str],
        *,
        registration_hook: str = "",
        model_visible: bool = True,
    ) -> tuple[ToolUseRef, ...]:
        """Project multiple MCP servers into a single deterministic tool tuple.

        Args:
            server_names: Server names to project in caller-provided order.
            registration_hook: Optional hook to run before each descriptor lookup.
            model_visible: Whether projected tools should be visible to the model.

        Returns:
            Flattened tuple of projected tool specs.
        """

        specs: list[ToolUseRef] = []
        for server_name in server_names:
            specs.extend(
                self.tool_specs(
                    server_name,
                    registration_hook=registration_hook,
                    model_visible=model_visible,
                )
            )
        return tuple(specs)

    def tool_specs_from_prefix(
        self,
        server_name_prefix: str,
        *,
        registration_hook: str = "",
        model_visible: bool = True,
    ) -> tuple[ToolUseRef, ...]:
        """Project every registered MCP server with a matching name prefix.

        Args:
            server_name_prefix: Prefix matched against registered server names.
            registration_hook: Optional hook to run before registry lookup.
            model_visible: Whether projected tools should be visible to the model.

        Returns:
            Flattened tuple of projected tool specs for matching servers.
        """

        self._run_registration_hook(registration_hook)
        return self.tool_specs_from_servers(
            (name for name in NorthCUAMCPRegistry.names() if name.startswith(server_name_prefix)),
            registration_hook="",
            model_visible=model_visible,
        )

    def _run_registration_hook(self, registration_hook: str) -> None:
        if registration_hook:
            run_agent_mcp_registration_hook(registration_hook)


_DEFAULT_CLI_ARGUMENTS = BuiltinAgentCliArguments()
_DEFAULT_MCP_PROJECTOR = MCPServerSpecProjector()


def serve_args() -> tuple[AgentCliArgumentSpec, ...]:
    """Return common local ``serve`` CLI arguments.

    Returns:
        Argument specs shared by built-in websocket server commands.
    """

    return _DEFAULT_CLI_ARGUMENTS.serve_args()


def controller_args() -> tuple[AgentCliArgumentSpec, ...]:
    """Return common remote submitter CLI arguments.

    Returns:
        Argument specs for controller URL, auth, wait behavior, and target
        device selection.
    """

    return _DEFAULT_CLI_ARGUMENTS.controller_args()


def tool_specs_from_mcp_server(
    server_name: str,
    *,
    registration_hook: str = "",
    model_visible: bool = True,
) -> tuple[ToolUseRef, ...]:
    """Project one MCP server's registered contracts into tool specs.

    Args:
        server_name: MCP server name to inspect through the global registry.
        registration_hook: Optional dotted hook that lazily registers the
            server before descriptors are read.
        model_visible: Whether projected tools should be exposed to the model.

    Returns:
        AgentKit tool specs mirroring the server descriptors.
    """

    return _DEFAULT_MCP_PROJECTOR.tool_specs(
        server_name,
        registration_hook=registration_hook,
        model_visible=model_visible,
    )


def probe_specs_from_mcp_server(
    server_name: str,
    *,
    registration_hook: str = "",
    default: bool = True,
    persist: bool = True,
    prompt: bool = True,
) -> tuple[ProbeUseRef, ...]:
    """Project one MCP server's registered descriptors into probe specs.

    Args:
        server_name: MCP server name to inspect through the global registry.
        registration_hook: Optional dotted hook that lazily registers the
            server before descriptors are read.
        default: Whether the probe should run in default observation policy.
        persist: Whether probe output should be persisted as artifacts.
        prompt: Whether probe output should be projected into prompts.

    Returns:
        AgentKit probe specs mirroring the server descriptors.
    """

    return _DEFAULT_MCP_PROJECTOR.probe_specs(
        server_name,
        registration_hook=registration_hook,
        default=default,
        persist=persist,
        prompt=prompt,
    )


def tool_ids_from_mcp_server(server_name: str, *, registration_hook: str = "") -> tuple[str, ...]:
    """Return protocol tool ids advertised by one MCP server.

    Args:
        server_name: MCP server to inspect through the registry.
        registration_hook: Optional lazy hook used before reading descriptors.

    Returns:
        Stable tuple of protocol tool ids in descriptor order.
    """

    return _DEFAULT_MCP_PROJECTOR.tool_ids(server_name, registration_hook=registration_hook)


def tool_specs_from_mcp_servers(
    server_names: Iterable[str],
    *,
    registration_hook: str = "",
    model_visible: bool = True,
) -> tuple[ToolUseRef, ...]:
    """Project multiple MCP servers into a single deterministic tool tuple.

    Args:
        server_names: MCP server names to project in caller-provided order.
        registration_hook: Optional shared lazy registration hook.
        model_visible: Whether projected tools should be exposed to the model.

    Returns:
        Concatenated tool specs for all requested servers.
    """

    return _DEFAULT_MCP_PROJECTOR.tool_specs_from_servers(
        server_names,
        registration_hook=registration_hook,
        model_visible=model_visible,
    )


def tool_specs_from_mcp_server_prefix(
    server_name_prefix: str,
    *,
    registration_hook: str = "",
    model_visible: bool = True,
) -> tuple[ToolUseRef, ...]:
    """Project every registered MCP server with a matching name prefix.

    Args:
        server_name_prefix: Prefix used to select registered server names.
        registration_hook: Optional hook invoked before registry lookup.
        model_visible: Whether projected tools should be exposed to the model.

    Returns:
        Concatenated tool specs for matching servers in registry order.
    """

    return _DEFAULT_MCP_PROJECTOR.tool_specs_from_prefix(
        server_name_prefix,
        registration_hook=registration_hook,
        model_visible=model_visible,
    )
