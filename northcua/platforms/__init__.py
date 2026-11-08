"""Platform runtime extension points for NorthCUA agents."""

from northcua.platforms.protocol import (
    PlatformActionExecutor,
    PlatformActionRequest,
    PlatformActionResult,
    PlatformActionStatus,
    PlatformArtifactKind,
    PlatformArtifactRef,
    PlatformArtifactRole,
    PlatformCapability,
    PlatformHealthStatus,
    PlatformKind,
    PlatformObservation,
    PlatformObservationProvider,
    PlatformRuntime,
)
from northcua.platforms.registry import PlatformRuntimeRegistry, default_platform_runtime_registry

__all__ = [
    "PlatformActionExecutor",
    "PlatformActionRequest",
    "PlatformActionResult",
    "PlatformActionStatus",
    "PlatformArtifactKind",
    "PlatformArtifactRef",
    "PlatformArtifactRole",
    "PlatformCapability",
    "PlatformHealthStatus",
    "PlatformKind",
    "PlatformObservation",
    "PlatformObservationProvider",
    "PlatformRuntime",
    "PlatformRuntimeRegistry",
    "default_platform_runtime_registry",
]
