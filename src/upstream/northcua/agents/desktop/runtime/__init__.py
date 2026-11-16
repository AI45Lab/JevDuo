"""Desktop adapters that bind NorthCUA tasks to shared AgentKit runtime."""

from .contracts import (
    DesktopObservationContextProvider,
    DesktopObservationSource,
    DesktopRuntimeOptions,
    DesktopStepObservation,
)
from .factory import DesktopRuntimeFactory
from .observation import DesktopObservationProvider
from .state import DesktopRuntimeStateStore
from .task import DesktopTaskFacade, DesktopTaskRunResult, DesktopTaskRuntime

__all__ = [
    "DesktopObservationContextProvider",
    "DesktopObservationProvider",
    "DesktopObservationSource",
    "DesktopRuntimeFactory",
    "DesktopRuntimeOptions",
    "DesktopRuntimeStateStore",
    "DesktopStepObservation",
    "DesktopTaskFacade",
    "DesktopTaskRunResult",
    "DesktopTaskRuntime",
]
