"""Runtime-only MCP probe server for web-agent diagnostics."""

from __future__ import annotations

from fastmcp import FastMCP

from northcua.runtime.mcp.contracts import register_contract_handlers
from northcua.runtime.mcp.invocation_context import current_mcp_invocation_context
from northcua.runtime.mcp.registry import MCPFactoryContext, NorthCUAMCPRegistry

from ..domain.actions import WebRefParams, WebRefsParams
from ..observation.ref_alias import canonical_source_ref
from ..session.registry import WebAgentSessionRegistry
from .descriptors import web_agent_probe_contracts


@NorthCUAMCPRegistry.register_decorator(
    "WebAgentProbeExecutor",
    tool_contracts=web_agent_probe_contracts(),
)
def create_web_agent_probe_mcp_server(context: MCPFactoryContext) -> FastMCP:
    """Create the runtime-only probe server for web-agent sessions.

    Args:
        context: MCP factory context supplied by the registry. The probe server
            uses invocation context at call time instead.

    Returns:
        FastMCP server exposing session-scoped web observation probes.
    """

    del context
    registry = WebAgentSessionRegistry()
    mcp = FastMCP("NorthCUA Web Agent Probe Executor")

    async def _web_agent_snapshot(params: dict[str, object]) -> dict[str, object]:
        del params
        session_id = current_mcp_invocation_context().session_id
        registered = registry.get_registered_session(session_id)
        return (await registered.client.snapshot()).model_dump(mode="json")

    async def _web_agent_get_url(params: dict[str, object]) -> dict[str, object]:
        del params
        session_id = current_mcp_invocation_context().session_id
        registered = registry.get_registered_session(session_id)
        result = await registered.client.get_url()
        return {"url": result.data}

    async def _web_agent_get_box(params: WebRefParams) -> dict[str, object]:
        session_id = current_mcp_invocation_context().session_id
        registered = registry.get_registered_session(session_id)
        result = await registered.client.get_box(canonical_source_ref(params.ref))
        payload = result.require_success().data
        return dict(payload) if isinstance(payload, dict) else {}

    async def _web_agent_get_boxes(params: WebRefsParams) -> dict[str, object]:
        session_id = current_mcp_invocation_context().session_id
        registered = registry.get_registered_session(session_id)
        result = await registered.client.get_boxes([canonical_source_ref(ref) for ref in params.refs])
        payload = result.require_success().data
        return dict(payload) if isinstance(payload, dict) else {"boxes": {}, "errors": {}}

    async def _web_agent_get_ref_states(params: WebRefsParams) -> dict[str, object]:
        session_id = current_mcp_invocation_context().session_id
        registered = registry.get_registered_session(session_id)
        result = await registered.client.get_ref_states([canonical_source_ref(ref) for ref in params.refs])
        payload = result.require_success().data
        return dict(payload) if isinstance(payload, dict) else {"states": {}, "errors": {}}

    async def _web_agent_viewport(params: dict[str, object]) -> dict[str, object]:
        del params
        session_id = current_mcp_invocation_context().session_id
        registered = registry.get_registered_session(session_id)
        result = await registered.client.get_viewport_size()
        payload = result.require_success().data
        if isinstance(payload, dict):
            nested = payload.get("result")
            return dict(nested) if isinstance(nested, dict) else dict(payload)
        return {}

    register_contract_handlers(
        mcp,
        web_agent_probe_contracts(),
        {
            "web_agent_snapshot": _web_agent_snapshot,
            "web_agent_get_url": _web_agent_get_url,
            "web_agent_get_box": _web_agent_get_box,
            "web_agent_get_boxes": _web_agent_get_boxes,
            "web_agent_get_ref_states": _web_agent_get_ref_states,
            "web_agent_viewport": _web_agent_viewport,
        },
    )

    return mcp
