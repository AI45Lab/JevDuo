"""Per-invocation runtime context shared by in-process MCP server calls."""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Iterator


@dataclass(slots=True, frozen=True)
class MCPInvocationContext:
    """Context attached to one in-process MCP invocation batch.

    Attributes:
        session_id: NorthCUA session associated with the MCP invocation.
        task_name: Optional task/tool category for diagnostics.
    """

    session_id: str = ""
    task_name: str = ""


_CURRENT_INVOCATION_CONTEXT: ContextVar[MCPInvocationContext] = ContextVar(
    "northcua_current_mcp_invocation_context",
    default=MCPInvocationContext(),
)


def current_mcp_invocation_context() -> MCPInvocationContext:
    """Return the current in-process MCP invocation context.

    Returns:
        Active invocation context or an empty default context.
    """

    return _CURRENT_INVOCATION_CONTEXT.get()


@contextmanager
def use_mcp_invocation_context(context: MCPInvocationContext) -> Iterator[None]:
    """Temporarily install a per-call MCP invocation context.

    Args:
        context: Invocation context visible to nested MCP server code.

    Yields:
        None while the context variable is installed.

    Returns:
        Iterator used by ``contextlib.contextmanager``.
    """

    token = _CURRENT_INVOCATION_CONTEXT.set(context)
    try:
        yield
    finally:
        _CURRENT_INVOCATION_CONTEXT.reset(token)
