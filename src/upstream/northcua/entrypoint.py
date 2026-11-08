"""CLI entrypoint that prepares the runtime environment before importing the app."""

from __future__ import annotations

import logging
from typing import NoReturn

_SUPPRESSED_NEXAU_LOGGERS = (
    "nexau.archs.sandbox.e2b_sandbox",
    "nexau.archs.sandbox",
)


def _suppress_bootstrap_optional_dependency_noise() -> None:
    """Silence NexAU optional dependency warnings before importing NorthCUA packages."""

    for logger_name in _SUPPRESSED_NEXAU_LOGGERS:
        logging.getLogger(logger_name).setLevel(logging.ERROR)


def _rethrow_missing_nexau(exc: ModuleNotFoundError) -> NoReturn:
    """Raise a clearer install hint without importing ``northcua.core`` on CLI startup."""

    missing_name = exc.name or ""
    if missing_name != "nexau" and not missing_name.startswith("nexau."):
        raise exc
    raise ModuleNotFoundError(
        "NexAU is not installed in the active NorthCUA environment. "
        "For local agents, run `uv sync --extra local` in the NorthCUA repo; "
        "for development, run `uv sync --extra dev`."
    ) from exc


def main() -> None:
    """Run the NorthCUA CLI from a uv-managed environment."""

    _suppress_bootstrap_optional_dependency_noise()
    try:
        from northcua.core.nexau_support import suppress_nexau_optional_dependency_noise

        suppress_nexau_optional_dependency_noise()
        from northcua.cli.main import main as cli_main
    except ModuleNotFoundError as exc:
        _rethrow_missing_nexau(exc)
    cli_main()
