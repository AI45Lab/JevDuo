"""Shared observation/state collection framework for NorthCUA."""

from .accessibility import AccessibilityObservationAugmentor
from .adapters import ProbeAdapterRegistry
from .base import (
    ProbeAdapterResult,
    ProbeExecutionRecord,
    ProbeExecutor,
    ProbeResultAdapter,
    StateSource,
    StateSourceResult,
)
from .coordinator import ObservationCoordinator
from .executors import LocalInternalProbeExecutor, RemoteInternalProbeExecutor
from .sources import ProbeChainStateSource, ToolResultStateSource

__all__ = [
    "AccessibilityObservationAugmentor",
    "LocalInternalProbeExecutor",
    "ObservationCoordinator",
    "ProbeAdapterRegistry",
    "ProbeAdapterResult",
    "ProbeChainStateSource",
    "ProbeExecutionRecord",
    "ProbeExecutor",
    "ProbeResultAdapter",
    "RemoteInternalProbeExecutor",
    "StateSource",
    "StateSourceResult",
    "ToolResultStateSource",
]
