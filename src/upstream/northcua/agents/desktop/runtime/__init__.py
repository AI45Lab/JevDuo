"""Desktop adapters that bind NorthCUA tasks to shared AgentKit runtime."""

from .contracts import (
    DesktopModelStepResult,
    DesktopModelStopReason,
    DesktopObservationContextProvider,
    DesktopObservationSource,
    DesktopPromptBundle,
    DesktopRuntimeEventName,
    DesktopRuntimeOptions,
    DesktopStepObservation,
)
from .factory import DesktopRuntimeFactory
from .model import DesktopModelStepRunner
from .observation import DesktopObservationProvider
from .prompt import DesktopPromptProjector
from .state import DesktopRuntimeStateStore
from .step import DesktopStepExecutor
from .task import DesktopTaskFacade, DesktopTaskRunResult, DesktopTaskRuntime
from .terminal import DesktopTerminalPersistenceFacade, DesktopTerminalProjectionPort

__all__ = [
    "DesktopModelStopReason",
    "DesktopModelStepResult",
    "DesktopObservationContextProvider",
    "DesktopObservationProvider",
    "DesktopObservationSource",
    "DesktopPromptBundle",
    "DesktopPromptProjector",
    "DesktopRuntimeEventName",
    "DesktopRuntimeFactory",
    "DesktopRuntimeOptions",
    "DesktopRuntimeStateStore",
    "DesktopStepExecutor",
    "DesktopStepObservation",
    "DesktopTaskFacade",
    "DesktopTaskRunResult",
    "DesktopTaskRuntime",
    "DesktopTerminalPersistenceFacade",
    "DesktopTerminalProjectionPort",
]
