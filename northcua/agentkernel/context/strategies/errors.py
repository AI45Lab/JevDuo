"""Typed failures raised before an unsafe provider request is sent."""


class ContextSummaryError(RuntimeError):
    """A summary invocation or validation failed."""


class ContextSummaryLifecycleError(ContextSummaryError):
    """Summary failure carrying stable correlation identifiers for audit sinks."""

    def __init__(
        self,
        message: str,
        *,
        event_id: str,
        checkpoint_id: str,
        source_digest: str,
    ) -> None:
        super().__init__(message)
        self.event_id = event_id
        self.checkpoint_id = checkpoint_id
        self.source_digest = source_digest


class ContextBudgetExceededError(RuntimeError):
    """Pinned/current context alone exceeds the safe provider budget."""


class ContextSideEffectReplayError(RuntimeError):
    """A fresh fallback candidate would replay an already-started tool."""


class ContextStaleRequestError(ContextSummaryError):
    """A request predates the durable checkpoint and must not see future state."""
