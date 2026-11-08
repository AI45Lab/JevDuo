"""Backend-neutral command result for web-agent actions."""

from __future__ import annotations

from pydantic import BaseModel, Field

from .errors import WebActionCommandError


class WebActionCommandResult(BaseModel):
    """Normalized result returned by any web action backend.

    This is intentionally independent from any concrete browser substrate.
    """

    success: bool
    data: object | None = None
    error: str = ""
    warning: str = ""
    code: str = ""
    reason_code: str = ""
    hint: str = ""
    concise_log_summary: str = ""
    feedback_source: str = ""
    debug_error: str = ""
    raw_stdout: str = ""
    raw_stderr: str = ""
    returncode: int = 0
    argv: list[str] = Field(default_factory=list)

    def require_success(self) -> "WebActionCommandResult":
        """Return this result or raise a backend-neutral command error."""

        if not self.success:
            message = self.error or self.raw_stderr or self.raw_stdout or "web action command failed"
            raise WebActionCommandError(
                message,
                code=self.code,
                reason_code=self.reason_code,
                hint=self.hint,
                concise_log_summary=self.concise_log_summary,
                feedback_source=self.feedback_source,
                debug_error=self.debug_error,
            )
        return self
