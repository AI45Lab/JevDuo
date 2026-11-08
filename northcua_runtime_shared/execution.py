"""Local command deadlines and typed failures over the existing status/error wire."""

from __future__ import annotations

import time
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Iterator, NoReturn

from northcua_protocol.payloads import CommandResult
from northcua_protocol.state import CommandState


class RuntimeExecutionError(RuntimeError):
    """Infrastructure failure, distinct from an agent's invalid tool arguments.

    Codes use the existing CommandResult.error field. They contain no request,
    endpoint, exception text or credentials. Retry is owned by the task/runtime
    owner after cleanup, never by the agent on the same uncertain runtime.
    """

    def __init__(self, code: str, *, status: CommandState = CommandState.FAILED) -> None:
        super().__init__(code)
        self.code = code
        self.status = status


_INFRASTRUCTURE_CODES = frozenset(
    {
        "COMMAND_TRANSPORT_FAILED",
        "COMMAND_DELIVERY_UNKNOWN",
        "COMMAND_EXECUTION_STILL_RUNNING",
    }
)


def raise_for_execution_failure(result: CommandResult) -> None:
    """Preserve protocol timeout/cancellation and explicitly typed driver errors.

    Args:
        result: The trusted device/controller result, not model-generated text.

    Raises:
        RuntimeExecutionError: The protocol or a typed driver reports an
            infrastructure failure. Ordinary tool failures remain results.
    """

    if result.status in {CommandState.TIMED_OUT, CommandState.CANCELLED}:
        raise RuntimeExecutionError(result.error or result.status.value, status=result.status)
    if result.status == CommandState.FAILED and result.error in _INFRASTRUCTURE_CODES:
        raise RuntimeExecutionError(result.error)


@dataclass
class CommandExecutionContext:
    """One process-local deadline, copied into the existing GUI worker thread.

    The failure slot retains a typed cause when an MCP implementation wraps or
    serializes an exception. It is never populated from model/tool text.

    Attributes:
        deadline: Absolute monotonic deadline in this process only.
        failure: Typed driver failure retained across an MCP boundary.
    """

    deadline: float
    failure: RuntimeExecutionError | None = None

    def remaining(self) -> float:
        """Check the deadline before any further driver work or retry.

        Returns:
            Positive remaining seconds for a nested call.

        Raises:
            RuntimeExecutionError: No execution budget remains.
        """

        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            fail_execution("COMMAND_TIMED_OUT", status=CommandState.TIMED_OUT)
        return remaining


_CURRENT: ContextVar[CommandExecutionContext | None] = ContextVar("northcua_command_execution", default=None)


def current_execution() -> CommandExecutionContext | None:
    """Return the local command scope, never a remote-host monotonic timestamp.

    Returns:
        The current process-local context, or None for standalone driver use.
    """

    return _CURRENT.get()


@contextmanager
def execution_scope(deadline: float) -> Iterator[CommandExecutionContext]:
    """Narrow an inherited deadline; nested calls cannot restart its budget.

    Args:
        deadline: Proposed absolute monotonic deadline in the current process.

    Returns:
        Context manager yielding the narrowed execution context.
    """

    parent = current_execution()
    context = CommandExecutionContext(min(deadline, parent.deadline) if parent else deadline)
    token = _CURRENT.set(context)
    try:
        yield context
    finally:
        _CURRENT.reset(token)


def fail_execution(code: str, *, status: CommandState = CommandState.FAILED) -> NoReturn:
    """Raise a safe typed failure while retaining it across MCP wrappers.

    Args:
        code: Stable driver error code with no request or endpoint data.
        status: Existing terminal command state to preserve on the wire.

    Raises:
        RuntimeExecutionError: Always; request exception chains are suppressed.

    Returns:
        Never returns to the caller.
    """

    error = RuntimeExecutionError(code, status=status)
    context = current_execution()
    if context is not None:
        context.failure = error
    raise error from None
