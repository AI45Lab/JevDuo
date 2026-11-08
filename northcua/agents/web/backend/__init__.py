"""Browser action backends for the NorthCUA web agent."""

from .errors import (
    WebActionBackendError,
    WebActionBackendOutputError,
    WebActionBackendTimeoutError,
    WebActionBackendUnavailableError,
    WebActionCommandError,
)
from .factory import build_playwright_web_backend
from .playwright import PlaywrightWebActionBackend
from .protocol import WebActionBackend, WebBackendFactory
from .result import WebActionCommandResult

__all__ = [
    "PlaywrightWebActionBackend",
    "WebActionBackend",
    "WebActionBackendError",
    "WebActionBackendOutputError",
    "WebActionBackendTimeoutError",
    "WebActionBackendUnavailableError",
    "WebActionCommandError",
    "WebActionCommandResult",
    "WebBackendFactory",
    "build_playwright_web_backend",
]
