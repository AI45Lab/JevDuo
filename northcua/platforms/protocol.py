"""Typed platform runtime contracts for multi-platform CUA agents.

The platform layer is intentionally narrower than an agent. It describes how a
desktop, browser, mobile device, or remote VM captures state and executes
actions, while leaving prompt, round, evaluator, and task semantics to concrete
agents.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum
from typing import Generic, Mapping, TypeVar

from northcua.core.json_types import JsonValue

TObservationState = TypeVar("TObservationState")
TActionPayload = TypeVar("TActionPayload")


class PlatformKind(str, Enum):
    """Supported platform runtime families.

    Attributes:
        DESKTOP: Native desktop runtime.
        BROWSER: Browser/page runtime.
        ANDROID: Reserved Android runtime.
        IOS: Reserved iOS runtime.
        REMOTE_VM: Reserved remote VM runtime.
    """

    DESKTOP = "desktop"
    BROWSER = "browser"
    ANDROID = "android"
    IOS = "ios"
    REMOTE_VM = "remote_vm"


class PlatformActionStatus(str, Enum):
    """Normalized platform action status values.

    Attributes:
        SUCCESS: Action was accepted and executed successfully.
        FAILURE: Action failed in a controllable platform/tool path.
        UNKNOWN: Adapter could not determine final action status.
    """

    SUCCESS = "success"
    FAILURE = "failure"
    UNKNOWN = "unknown"


class PlatformArtifactKind(str, Enum):
    """Common artifact families produced by platform adapters.

    Attributes:
        IMAGE: Screenshot or visual artifact.
        A11Y: Accessibility tree or focused subtree artifact.
        TOOL: Tool result payload artifact.
        RUNTIME: Runtime event artifact.
        TRACE: Trace/log artifact.
        OTHER: Extension point for platform-specific artifacts.
    """

    IMAGE = "image"
    A11Y = "a11y"
    TOOL = "tool"
    RUNTIME = "runtime"
    TRACE = "trace"
    OTHER = "other"


class PlatformArtifactRole(str, Enum):
    """Common artifact roles within an observation/action lifecycle.

    Attributes:
        CURRENT_CLEAN: Current clean observation artifact.
        CURRENT_OVERLAY: Current overlay/annotated observation artifact.
        ACTION_RESULT: Artifact produced by an executed action.
        EVIDENCE: Supporting evidence artifact.
        UNKNOWN: Role is intentionally platform-specific or not known.
    """

    CURRENT_CLEAN = "current_clean"
    CURRENT_OVERLAY = "current_overlay"
    ACTION_RESULT = "action_result"
    EVIDENCE = "evidence"
    UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class PlatformCapability:
    """One capability advertised by a platform runtime.

    Attributes:
        name: Stable capability name, such as ``screenshot`` or ``a11y``.
        enabled: Whether the capability is currently available.
        metadata: Small JSON-like metadata; must not contain large payloads.
    """

    name: str
    enabled: bool = True
    metadata: Mapping[str, JsonValue] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class PlatformArtifactRef:
    """Reference to an artifact produced by a platform observation or action.

    Attributes:
        kind: Artifact kind, for example ``image`` or ``a11y``.
        path: Optional local artifact path.
        url: Optional data URL or external reference.
        role: Role within the observation/action lifecycle.
        metadata: Small structured metadata.
    """

    kind: PlatformArtifactKind | str
    path: str = ""
    url: str = ""
    role: PlatformArtifactRole | str = PlatformArtifactRole.UNKNOWN
    metadata: Mapping[str, JsonValue] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class PlatformObservation(Generic[TObservationState]):
    """Normalized observation envelope returned by platform runtimes.

    Attributes:
        platform_kind: Platform family that produced the observation.
        artifacts: Artifact refs associated with the observation.
        raw: Optional typed domain-specific state object for adapter layers.
        metadata: Small structured metadata.
    """

    platform_kind: PlatformKind
    artifacts: tuple[PlatformArtifactRef, ...] = ()
    raw: TObservationState | None = None
    metadata: Mapping[str, JsonValue] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class PlatformActionRequest:
    """One action request routed to a platform executor.

    Attributes:
        name: Stable action/tool name.
        parameters: JSON-like action parameters.
        call_id: Optional upstream tool-call id.
        server_name: Optional MCP/server routing hint.
        metadata: Small structured metadata.
    """

    name: str
    parameters: Mapping[str, JsonValue] = field(default_factory=dict)
    call_id: str = ""
    server_name: str = ""
    metadata: Mapping[str, JsonValue] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class PlatformActionResult(Generic[TActionPayload]):
    """Result returned by a platform action executor.

    Attributes:
        call_id: Upstream tool-call id, if provided.
        status: Normalized action status.
        result: Optional typed domain-specific result payload.
        error: Optional error message.
        artifacts: Artifact refs produced by the action.
        metadata: Small structured metadata.
    """

    call_id: str = ""
    status: PlatformActionStatus = PlatformActionStatus.SUCCESS
    result: TActionPayload | None = None
    error: str = ""
    artifacts: tuple[PlatformArtifactRef, ...] = ()
    metadata: Mapping[str, JsonValue] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class PlatformHealthStatus:
    """Health snapshot for a platform runtime.

    Attributes:
        platform_kind: Platform family being checked.
        ok: Whether the platform runtime is usable.
        message: Human-readable diagnostic.
        capabilities: Capability set advertised by the runtime.
    """

    platform_kind: PlatformKind
    ok: bool
    message: str = ""
    capabilities: tuple[PlatformCapability, ...] = ()


class PlatformObservationProvider(ABC):
    """Provider that captures state for a platform runtime."""

    @abstractmethod
    async def capture_observation(
        self,
        *,
        session_step: int,
        previous_steps: list[object] | None = None,
    ) -> PlatformObservation:
        """Capture one platform observation.

        Args:
            session_step: Current one-based step number.
            previous_steps: Optional prior agent step records for history-aware capture.

        Returns:
            Platform observation envelope.
        """


class PlatformActionExecutor(ABC):
    """Executor that applies actions to a platform runtime."""

    @abstractmethod
    async def execute_action(self, request: PlatformActionRequest) -> PlatformActionResult:
        """Execute one action request.

        Args:
            request: Action request.

        Returns:
            Platform action result.
        """


class PlatformRuntime(ABC):
    """Template-method runtime boundary for platform implementations."""

    platform_kind: PlatformKind

    @abstractmethod
    async def healthcheck(self) -> PlatformHealthStatus:
        """Return platform runtime health.

        Returns:
            Platform health status.
        """

    @abstractmethod
    async def capture_observation(
        self,
        *,
        session_step: int,
        previous_steps: list[object] | None = None,
    ) -> PlatformObservation:
        """Capture one platform observation.

        Args:
            session_step: Current one-based step number.
            previous_steps: Optional prior step records.

        Returns:
            Platform observation envelope.
        """

    @abstractmethod
    async def execute_action(self, request: PlatformActionRequest) -> PlatformActionResult:
        """Execute one platform action.

        Args:
            request: Action request.

        Returns:
            Platform action result.
        """
