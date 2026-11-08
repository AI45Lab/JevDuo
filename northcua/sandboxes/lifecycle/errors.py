"""Errors raised by sandbox lifecycle cleanup paths."""

from __future__ import annotations


class SandboxAcquireCleanupError(RuntimeError):
    """Acquire failed after capacity existed and cleanup also failed."""

    def __init__(
        self,
        *,
        sandbox_id: str,
        provider: str,
        message: str,
        cleanup_error: Exception,
    ) -> None:
        super().__init__(f"{message}; cleanup failed: {cleanup_error}")
        self.sandbox_id = sandbox_id
        self.provider = provider
        self.cleanup_error = cleanup_error
