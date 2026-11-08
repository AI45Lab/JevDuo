"""NorthCUA-specific exceptions shared across runtime subsystems."""

from __future__ import annotations


class NorthCUAError(RuntimeError):
    """Base exception for the NorthCUA runtime."""


class ConfigValidationError(NorthCUAError):
    """Raised when a NorthCUA configuration payload is invalid."""


class SessionNotFoundError(NorthCUAError):
    """Raised when a websocket or runtime session cannot be resolved."""


class MCPRegistryError(NorthCUAError):
    """Raised when MCP registry lookup or registration fails."""


class PromptBuildError(NorthCUAError):
    """Raised when typed prompt construction fails."""


class ObservationProbeExecutionError(NorthCUAError):
    """Raised when an internal observation probe fails to execute successfully."""
