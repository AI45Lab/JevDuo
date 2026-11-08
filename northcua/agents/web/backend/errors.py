"""Backend-neutral browser action errors."""

from __future__ import annotations


class WebActionBackendError(RuntimeError):
    """Base class for web action backend failures."""


class WebActionBackendUnavailableError(WebActionBackendError):
    """Raised when the configured browser backend cannot be started."""


class WebActionBackendTimeoutError(WebActionBackendError):
    """Raised when a backend command exceeds its timeout."""


class WebActionBackendOutputError(WebActionBackendError):
    """Raised when backend output cannot be parsed or trusted."""


class WebActionCommandError(WebActionBackendError):
    """Raised when a backend command result reports failure."""

    def __init__(
        self,
        message: str,
        *,
        code: str = "",
        reason_code: str = "",
        hint: str = "",
        concise_log_summary: str = "",
        feedback_source: str = "",
        debug_error: str = "",
    ) -> None:
        super().__init__(message)
        self.code = code
        self.reason_code = reason_code
        self.hint = hint
        self.concise_log_summary = concise_log_summary
        self.feedback_source = feedback_source
        self.debug_error = debug_error
