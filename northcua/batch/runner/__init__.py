"""Batch orchestration exports, loaded lazily to avoid preparation import cycles."""

from __future__ import annotations
from importlib import import_module
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .controller import ControllerTaskWatcher
    from .leases import BatchTaskLeaseCoordinator
    from .resume import BatchStatusReconciler
    from .service import BatchRunner

_EXPORTS = {
    "ControllerTaskWatcher": "controller",
    "BatchTaskLeaseCoordinator": "leases",
    "BatchStatusReconciler": "resume",
    "BatchRunner": "service",
}
__all__ = list(_EXPORTS)


def __getattr__(name: str):
    if name not in _EXPORTS:
        raise AttributeError(name)
    value = getattr(import_module(f"{__name__}.{_EXPORTS[name]}"), name)
    globals()[name] = value
    return value
