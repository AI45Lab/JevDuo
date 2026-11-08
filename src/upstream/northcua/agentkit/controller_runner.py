"""Generic controller-owned remote runner for AgentKit specs."""

from __future__ import annotations

import base64
import hashlib
import logging
import tempfile
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from os import PathLike
from pathlib import Path
from typing import Protocol, cast

from northcua.batch.context_snapshot import (
    context_management_enabled,
    snapshot_controller_app_config,
    validate_published_controller_app_config_snapshot,
)
from northcua.controller.execution.executor_protocol import ControllerExecutionContext
from northcua.controller.execution.runner_plugins import ControllerRunnerRegistrationContext
from northcua.controller.execution.runner_registry import RunnerRegistry
from northcua.controller.execution.sdk import ControllerSDK
from northcua.controller.storage.file_artifacts import file_artifact_ref
from northcua.core.terminal import AgentTerminalDecision, AgentTerminalReason
from northcua_protocol.artifacts import ArtifactKind, ArtifactRef, ArtifactScope, RetentionPolicy, StorageKind
from northcua_protocol.capabilities import JsonObject
from northcua_protocol.payloads import TaskFinished
from northcua_protocol.state import TaskState

from .imports import resolve_dotted
from .registry import load_all_agent_specs
from .spec import AgentSpec

logger = logging.getLogger(__name__)
_MAX_INLINE_ARTIFACT_BYTES = 4_000_000


@dataclass(slots=True, frozen=True)
class AgentKitRemoteRunnerConfig:
    """Configuration for generic controller-owned AgentKit runners.

    Attributes:
        app_config_path: Controller-side app config used to construct agents.
        artifact_root_dir_override: Optional artifact root for runner outputs.
        command_timeout_seconds: Timeout for device-side command dispatches.
        max_steps_per_task: Controller safety cap for one submitted task.
    """

    app_config_path: str
    artifact_root_dir_override: str | None = None
    command_timeout_seconds: float = 30.0
    max_steps_per_task: int = 100


class _RunnerFactory(Protocol):
    def __call__(
        self,
        *,
        spec: AgentSpec,
        config: AgentKitRemoteRunnerConfig,
        sdk: ControllerSDK,
    ) -> "_RunnerLike | Callable[[ControllerExecutionContext], Awaitable[TaskFinished]]": ...


class _RunnerLike(Protocol):
    async def run(self, context: ControllerExecutionContext) -> TaskFinished:
        """Execute one controller task.

        Args:
            context: Controller execution context with request and device lease.

        Returns:
            Terminal task event reported back to the controller.
        """
        ...


class AgentKitRemoteTaskRunnerBase:
    """Common helpers for AgentKit remote runners.

    The base class keeps controller result creation consistent across built-in
    and low-code agents while leaving domain-specific execution to subclasses.
    """

    def __init__(self, *, config: AgentKitRemoteRunnerConfig, sdk: ControllerSDK | None) -> None:
        self.config = config
        self.sdk = sdk

    def _terminal(
        self,
        context: ControllerExecutionContext,
        *,
        state: TaskState,
        result: object | None = None,
        error: str | None = None,
        artifacts: list[ArtifactRef] | None = None,
        terminal_decision: JsonObject | None = None,
    ) -> TaskFinished:
        """Build a controller terminal event for one task.

        Args:
            context: Controller execution context for the submitted task.
            state: Final task state reported to submitters.
            result: Optional domain result, serialized when no error occurred.
            error: Optional error message; present values suppress ``result``.
            artifacts: Inline artifact refs to attach to the terminal event.
            terminal_decision: Optional shared terminal decision payload.

        Returns:
            Protocol ``TaskFinished`` event with lease and trace metadata.
        """

        return TaskFinished(
            task_id=context.request.task_id,
            trace_id=context.request.trace_id,
            session_id=context.session_id,
            device_id=context.lease.device_id,
            lease_id=context.lease.lease_id,
            state=state,
            result=protocol_result(result) if error is None else None,
            error=error,
            artifacts=artifacts or [],
            terminal_decision=terminal_decision or terminal_decision_from_result(result, error=error),
        )


def register_controller_runner(
    runner_registry: RunnerRegistry,
    sdk: ControllerSDK,
    context: ControllerRunnerRegistrationContext,
) -> None:
    """Register all remote-enabled AgentKit specs with the controller.

    Args:
        runner_registry: Controller registry that maps agent kinds to runners.
        sdk: Controller SDK used by runners to dispatch device commands.
        context: Plugin registration context containing app config and runtime
            timeouts.
    """

    configured_app_path = Path(context.app_config_path).expanduser()
    enabled_remote_specs = tuple(spec for spec in load_all_agent_specs().enabled() if spec.remote.enabled)
    for spec in enabled_remote_specs:
        if not spec.remote.runner_factory:
            logger.warning("AgentKit remote spec has no runner_factory", extra={"agent_kind": spec.kind})
    remote_specs = tuple(spec for spec in enabled_remote_specs if spec.remote.runner_factory)
    app_config_path = configured_app_path
    if (
        configured_app_path.is_file()
        and not validate_published_controller_app_config_snapshot(configured_app_path)
        and any(context_management_enabled(configured_app_path, agent_kind=spec.kind) for spec in remote_specs)
    ):
        snapshot_root = (
            Path(context.artifact_root_dir_override).expanduser().resolve() / ".controller_config_snapshot"
            if context.artifact_root_dir_override
            else Path(tempfile.mkdtemp(prefix="northcua-controller-config-"))
        )
        app_config_path = snapshot_controller_app_config(
            configured_app_path,
            snapshot_root=snapshot_root,
        )
    for spec in remote_specs:
        assert spec.remote.runner_factory is not None
        config = AgentKitRemoteRunnerConfig(
            app_config_path=str(app_config_path),
            artifact_root_dir_override=context.artifact_root_dir_override,
            command_timeout_seconds=context.command_timeout_seconds,
        )
        factory = cast(_RunnerFactory, resolve_dotted(spec.remote.runner_factory))
        runner = factory(spec=spec, config=config, sdk=sdk)
        run = runner.run if hasattr(runner, "run") else runner
        runner_registry.register(
            spec.kind,
            cast(Callable[[ControllerExecutionContext], Awaitable[TaskFinished]], run),
            app_config_path=str(app_config_path),
        )


def inline_text_artifact(context: ControllerExecutionContext, artifact_id: str) -> str:
    """Return one inline text artifact from a submitted controller task.

    Args:
        context: Controller execution context containing input artifacts.
        artifact_id: Optional artifact id to match; an empty id accepts the
            first inline text-compatible artifact.

    Returns:
        Decoded UTF-8 artifact text, or an empty string when no match exists.

    Raises:
        ValueError: If the inline artifact exceeds AgentKit's safety cap.
    """

    for artifact in context.request.input_artifacts:
        if artifact_id and artifact.artifact_id != artifact_id:
            continue
        if artifact.storage_kind == StorageKind.INLINE and artifact.inline_data is not None:
            if artifact.size_bytes > _MAX_INLINE_ARTIFACT_BYTES:
                raise ValueError("inline task artifact exceeds AgentKit max inline artifact bytes")
            return base64.b64decode(artifact.inline_data).decode("utf-8")
        if artifact.storage_kind == StorageKind.CONTROLLER_BLOB and context.blob_store_root is not None:
            if artifact.size_bytes > _MAX_INLINE_ARTIFACT_BYTES:
                raise ValueError("controller blob task artifact exceeds AgentKit max text artifact bytes")
            path = context.blob_store_root / artifact.sha256
            data = path.read_bytes()
            if len(data) != artifact.size_bytes or hashlib.sha256(data).hexdigest() != artifact.sha256:
                raise ValueError("controller blob task artifact integrity mismatch")
            return data.decode("utf-8")
    return ""


def metadata_int(value: object, default: int, *, upper_bound: int | None = None) -> int:
    """Parse a positive integer from task metadata with bounds.

    Args:
        value: Raw metadata value from protocol payloads.
        default: Fallback value when parsing fails or yields non-positive data.
        upper_bound: Optional inclusive safety cap.

    Returns:
        Parsed positive integer constrained by ``upper_bound`` when provided.
    """

    if isinstance(value, int) and value > 0:
        parsed = value
    elif isinstance(value, str) and value.isdigit():
        parsed = int(value)
    else:
        parsed = default
    if parsed <= 0:
        parsed = default
    if upper_bound is not None:
        return min(parsed, upper_bound)
    return parsed


def trace_artifacts(context: ControllerExecutionContext, artifact_store: object) -> list[ArtifactRef]:
    """Collect trace/result files as metadata-only controller artifacts.

    Args:
        context: Controller execution context used to build artifact ids.
        artifact_store: Agent artifact store exposing common path attributes.

    Returns:
        Device-local refs for existing non-empty files. The full payload remains
        under the agent artifact root instead of being duplicated into
        controller blob storage.
    """

    primary_trace_path = getattr(artifact_store, "primary_trace_path", None)
    primary_trace_label = _primary_trace_artifact_label(primary_trace_path)
    paths = [
        ("runtime", getattr(artifact_store, "runtime_log_path", None), ArtifactKind.JSON, "application/x-ndjson"),
        ("llm-trace", getattr(artifact_store, "llm_trace_log_path", None), ArtifactKind.JSON, "application/x-ndjson"),
        (
            "context-summary-llm-trace",
            getattr(artifact_store, "context_summary_llm_trace_log_path", None),
            ArtifactKind.JSON,
            "application/x-ndjson",
        ),
        (
            "context-compaction",
            getattr(artifact_store, "context_compaction_log_path", None),
            ArtifactKind.JSON,
            "application/x-ndjson",
        ),
        (primary_trace_label, primary_trace_path, ArtifactKind.JSON, "application/x-ndjson"),
        ("result", getattr(artifact_store, "result_path", None), ArtifactKind.JSON, "application/json"),
    ]
    artifact_root = getattr(artifact_store, "root", None)
    artifact_root_path = Path(artifact_root).parent if artifact_root is not None else None
    refs: list[ArtifactRef] = []
    for label, maybe_path, kind, mime_type in paths:
        path = Path(maybe_path) if maybe_path is not None else None
        if path is None or not path.exists() or not path.is_file() or path.stat().st_size <= 0:
            continue
        refs.append(
            file_artifact_ref(
                artifact_id=f"{context.request.task_id}-{label}",
                kind=kind,
                path=path,
                artifact_root=artifact_root_path,
                mime_type=mime_type,
                artifact_scope=ArtifactScope.TASK,
                retention_policy=RetentionPolicy.TASK,
            )
        )
    return refs


def _primary_trace_artifact_label(path: object) -> str:
    """Return the controller artifact label for an agent's primary step trace.

    Desktop agents persist their main step trace as ``steps.jsonl`` and already
    use the explicit ``steps`` label in controller artifact materialization.
    Web agents persist ``web_agent_trace.jsonl`` and use the explicit
    ``web-trace`` label.
    """

    if not isinstance(path, (str, PathLike)):
        return "trace"
    name = Path(path).name
    if name == "steps.jsonl":
        return "steps"
    if name == "web_agent_trace.jsonl":
        return "web-trace"
    return "trace"


def protocol_result(value: object | None) -> dict[str, object] | str | None:
    """Convert arbitrary runner output into protocol-safe result data.

    Args:
        value: Domain result returned by a remote runner.

    Returns:
        ``None``, a string, a shallow string-keyed mapping, or a stringified
        fallback for objects not directly accepted by the protocol.
    """

    if value is None or isinstance(value, str):
        return value
    if isinstance(value, dict):
        return {str(key): item for key, item in value.items()}
    return str(value)


def terminal_decision_from_result(value: object | None, *, error: str | None = None) -> JsonObject:
    """Extract a shared terminal decision from a domain result when present.

    Args:
        value: Domain result, protocol payload, or run-result dataclass.
        error: Optional runtime error text used when no result decision exists.

    Returns:
        JSON-compatible terminal decision payload, or an empty mapping.
    """

    if error:
        return AgentTerminalDecision.error(
            reason=AgentTerminalReason.RUNTIME_ERROR,
            final_answer=error,
            summary="The AgentKit remote runner failed before producing a domain result.",
            retryable=True,
        ).to_json()
    if value is None:
        return {}
    if isinstance(value, dict):
        maybe_decision = value.get("terminal_decision")
        return maybe_decision if isinstance(maybe_decision, dict) else {}
    to_terminal_decision = getattr(value, "to_terminal_decision", None)
    if callable(to_terminal_decision):
        decision = to_terminal_decision()
        to_json = getattr(decision, "to_json", None)
        if callable(to_json):
            payload = to_json()
            return payload if isinstance(payload, dict) else {}
    maybe_decision = getattr(value, "terminal_decision", None)
    return maybe_decision if isinstance(maybe_decision, dict) else {}
