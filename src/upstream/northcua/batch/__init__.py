"""Batch execution support with lazy public exports.

Importing a scheduler-neutral leaf such as ``northcua.batch.post_run`` must not
bootstrap ``BatchRunner`` and its optional runtime dependencies.
"""

from __future__ import annotations

from importlib import import_module
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from northcua.batch.contracts import (
        BatchDefaults,
        BatchManifest,
        BatchRunConfig,
        BatchRunResult,
        BatchRunResumeConfig,
        BatchTaskSpec,
        BatchTaskState,
        BatchTaskStatus,
    )
    from northcua.batch.runner.service import BatchRunner

_EXPORTS = {
    "BatchDefaults": ("northcua.batch.contracts", "BatchDefaults"),
    "BatchManifest": ("northcua.batch.contracts", "BatchManifest"),
    "BatchRunConfig": ("northcua.batch.contracts", "BatchRunConfig"),
    "BatchRunResumeConfig": ("northcua.batch.contracts", "BatchRunResumeConfig"),
    "BatchRunResult": ("northcua.batch.contracts", "BatchRunResult"),
    "BatchRunner": ("northcua.batch.runner.service", "BatchRunner"),
    "BatchTaskSpec": ("northcua.batch.contracts", "BatchTaskSpec"),
    "BatchTaskState": ("northcua.batch.contracts", "BatchTaskState"),
    "BatchTaskStatus": ("northcua.batch.contracts", "BatchTaskStatus"),
}


def __getattr__(name: str) -> object:
    """Resolve legacy ``northcua.batch`` exports only when requested."""

    try:
        module_name, attribute = _EXPORTS[name]
    except KeyError as exc:
        raise AttributeError(name) from exc
    value = getattr(import_module(module_name), attribute)
    globals()[name] = value
    return value


def __dir__() -> list[str]:
    return sorted({*globals(), *_EXPORTS})


__all__ = [
    "BatchDefaults",
    "BatchManifest",
    "BatchRunConfig",
    "BatchRunResumeConfig",
    "BatchRunResult",
    "BatchRunner",
    "BatchTaskSpec",
    "BatchTaskState",
    "BatchTaskStatus",
]
