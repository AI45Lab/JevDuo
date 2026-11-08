"""Provider-neutral classification for context-window failures."""

from __future__ import annotations

from northcua.agentkernel.context.strategies import ContextBudgetExceededError

_CONTEXT_OVERFLOW_MARKERS = (
    "context length exceeded",
    "context_length_exceeded",
    "context window exceeded",
    "exceeds the context window",
    "maximum context length",
    "max context length",
    "input is too long",
    "input too long",
    "prompt is too long",
    "too many input tokens",
    "request too large for model",
)


def is_context_overflow_error(error: BaseException) -> bool:
    """Return true only for recognizable provider context-capacity errors.

    The walk includes chained exceptions because provider SDKs commonly wrap
    the original HTTP error. It is deliberately conservative: transient 429,
    timeout, transport and generic validation failures must use normal fallback.
    """

    if isinstance(error, ContextBudgetExceededError):
        return True
    current: BaseException | None = error
    seen: set[int] = set()
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        text = f"{type(current).__name__}: {current}".lower()
        if any(marker in text for marker in _CONTEXT_OVERFLOW_MARKERS):
            return True
        current = current.__cause__ or current.__context__
    return False
