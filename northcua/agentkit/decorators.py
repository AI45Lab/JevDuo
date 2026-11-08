"""Decorators used by low-code NorthCUA agents."""

from __future__ import annotations

from collections.abc import Callable
from typing import ParamSpec, TypeVar

from pydantic import BaseModel

from northcua_protocol.capabilities import Capability, ToolRiskLevel
from northcua_runtime_shared.tool_contracts import (
    DirectHandlerBinding,
    McpBinding,
    ToolContractKind,
    ToolPlacement,
    ToolVisibility,
    define_tool,
)

from .spec import AgentSpec, ProbeUseRef, ToolUseRef

P = ParamSpec("P")
R = TypeVar("R")


def cua_tool(
    name: str,
    *,
    params: type[BaseModel] | None = None,
    description: str = "",
    model_visible: bool = True,
    capability: Capability = Capability.DESKTOP_INPUT,
    server_name: str = "",
    registration_hook: str = "",
    timeout_seconds: float = 10.0,
    side_effect: bool = True,
    requires_focus: bool = False,
    risk_level: ToolRiskLevel = ToolRiskLevel.LOW,
) -> Callable[[Callable[P, R]], Callable[P, R]]:
    """Declare one model/device executable CUA tool.

    Args:
        name: Stable tool id exposed through AgentKit and MCP.
        params: Optional Pydantic model used to validate tool arguments.
        description: Human-readable tool description; falls back to function
            docstring when empty.
        model_visible: Whether the tool should be advertised to the model.
        capability: Runtime capability required to execute the tool.
        server_name: MCP server name when the tool is backed by MCP.
        registration_hook: Dotted hook that lazily registers the MCP server.
        timeout_seconds: Per-command execution timeout.
        side_effect: Whether the tool changes external state.
        requires_focus: Whether the device/window must be focused.
        risk_level: Protocol risk label for controller policy.

    Returns:
        Decorator that attaches a ``ToolUseRef`` to the function.
    """

    def _decorate(func: Callable[P, R]) -> Callable[P, R]:
        resolved_description = description or (func.__doc__ or "").strip()
        binding = (
            McpBinding(server_name=server_name, registration_hook=registration_hook)
            if server_name
            else DirectHandlerBinding(handler_path=f"{func.__module__}:{func.__qualname__}")
        )
        contract = define_tool(
            tool_id=name,
            description=resolved_description,
            kind=ToolContractKind.TOOL,
            placement=ToolPlacement.MCP_SERVER if server_name else ToolPlacement.LOCAL_HANDLER,
            visibility=ToolVisibility.MODEL_VISIBLE if model_visible else ToolVisibility.RUNTIME_ONLY,
            capability=capability,
            params_model=params,
            timeout_seconds=timeout_seconds,
            side_effect=side_effect,
            requires_focus=requires_focus,
            risk_level=risk_level,
            binding=binding,
        )
        setattr(
            func,
            "__northcua_tool_contract__",
            contract,
        )
        setattr(
            func,
            "__northcua_tool_ref__",
            ToolUseRef.from_contract(contract, handler=func, model_visible=model_visible),
        )
        return func

    return _decorate


def cua_probe(
    name: str,
    *,
    params: type[BaseModel] | None = None,
    description: str = "",
    default: bool = True,
    persist: bool = True,
    prompt: bool = True,
    capability: Capability = Capability.DESKTOP_SCREENSHOT,
    server_name: str = "",
    registration_hook: str = "",
    timeout_seconds: float = 10.0,
) -> Callable[[Callable[P, R]], Callable[P, R]]:
    """Declare one observation/probe capability.

    Args:
        name: Stable probe id exposed through AgentKit and MCP.
        params: Optional Pydantic model used to validate probe arguments.
        description: Human-readable probe description; falls back to function
            docstring when empty.
        default: Whether the probe runs in the default observation policy.
        persist: Whether probe output is persisted as an artifact.
        prompt: Whether probe output is projected into prompts.
        capability: Runtime capability required to execute the probe.
        server_name: MCP server name when the probe is backed by MCP.
        registration_hook: Dotted hook that lazily registers the MCP server.
        timeout_seconds: Per-command execution timeout.

    Returns:
        Decorator that attaches a ``ProbeUseRef`` to the function.
    """

    def _decorate(func: Callable[P, R]) -> Callable[P, R]:
        resolved_description = description or (func.__doc__ or "").strip()
        binding = (
            McpBinding(server_name=server_name, registration_hook=registration_hook)
            if server_name
            else DirectHandlerBinding(handler_path=f"{func.__module__}:{func.__qualname__}")
        )
        contract = define_tool(
            tool_id=name,
            description=resolved_description,
            kind=ToolContractKind.PROBE,
            placement=ToolPlacement.MCP_SERVER if server_name else ToolPlacement.LOCAL_HANDLER,
            visibility=ToolVisibility.RUNTIME_ONLY,
            capability=capability,
            params_model=params,
            timeout_seconds=timeout_seconds,
            side_effect=False,
            requires_focus=False,
            risk_level=ToolRiskLevel.LOW,
            binding=binding,
        )
        setattr(
            func,
            "__northcua_probe_contract__",
            contract,
        )
        setattr(
            func,
            "__northcua_probe_ref__",
            ProbeUseRef.from_contract(contract, handler=func, default=default, persist=persist, prompt=prompt),
        )
        return func

    return _decorate


def register_agent(spec: AgentSpec) -> AgentSpec:
    """Register an agent spec in the process-global AgentKit registry.

    Args:
        spec: Fully constructed agent specification.

    Returns:
        The same spec, enabling decorator-style module declarations.
    """

    from .registry import get_agentkit_registry

    get_agentkit_registry().register(spec)
    return spec
