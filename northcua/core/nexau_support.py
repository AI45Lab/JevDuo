"""Helpers for NexAU runtime integration."""

from __future__ import annotations

import logging
from typing import NoReturn

_SUPPRESSED_NEXAU_LOGGERS = (
    "nexau.archs.sandbox.e2b_sandbox",
    "nexau.archs.sandbox",
)
_STRUCTURED_TOOL_WARNING_LOGGER = "nexau.archs.main_sub.execution.executor"
_STRUCTURED_TOOL_WARNING_FRAGMENT = (
    "Structured tool call mode enabled but no structured tool definitions were provided."
)


class _SuppressStructuredToolBootstrapWarning(logging.Filter):
    """Drop NexAU's transient warning before async MCP tools are fully initialized."""

    def filter(self, record: logging.LogRecord) -> bool:
        """Return whether a log record should be emitted.

        Args:
            record: Log record being filtered.

        Returns:
            False for the known bootstrap warning, otherwise True.
        """

        return _STRUCTURED_TOOL_WARNING_FRAGMENT not in record.getMessage()


_STRUCTURED_TOOL_WARNING_FILTER = _SuppressStructuredToolBootstrapWarning()


def suppress_nexau_optional_dependency_noise() -> None:
    """Silence noisy optional-dependency warnings on normal CLI/runtime paths."""

    for logger_name in _SUPPRESSED_NEXAU_LOGGERS:
        logger = logging.getLogger(logger_name)
        logger.setLevel(logging.ERROR)
    executor_logger = logging.getLogger(_STRUCTURED_TOOL_WARNING_LOGGER)
    if not any(f is _STRUCTURED_TOOL_WARNING_FILTER for f in executor_logger.filters):
        executor_logger.addFilter(_STRUCTURED_TOOL_WARNING_FILTER)


def rethrow_missing_nexau(exc: ModuleNotFoundError) -> NoReturn:
    """Raise a clearer error when NexAU is missing from the active environment.

    Args:
        exc: Original import error.

    Raises:
        ModuleNotFoundError: Re-raised original error for non-NexAU imports, or a clearer NexAU install error.
    Returns:
        This function never returns.
    """

    missing_name = exc.name or ""
    if missing_name != "nexau" and not missing_name.startswith("nexau."):
        raise exc
    raise ModuleNotFoundError(
        "NexAU is not installed in the active NorthCUA environment. "
        "For local agents, run `uv sync --extra local` in the NorthCUA repo; "
        "for development, run `uv sync --extra dev`."
    ) from exc
