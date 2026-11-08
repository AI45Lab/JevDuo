"""Typed contracts for controller-owned process workers."""

from __future__ import annotations

from enum import Enum
from pathlib import Path
from typing import ClassVar

from pydantic import BaseModel, ConfigDict, Field, field_validator

from northcua_protocol.capabilities import JsonObject
from northcua_protocol.enums import MessageType
from northcua_protocol.payloads import CommandResult, SessionLeaseGranted, TaskFinished, TaskRequest


class WorkerJobStatus(str, Enum):
    """Durable process-worker job lifecycle states."""

    QUEUED = "queued"
    STARTING = "starting"
    RUNNING = "running"
    FINISHING = "finishing"
    FINISHED = "finished"
    FAILED_RETRYABLE = "failed_retryable"
    FAILED_TERMINAL = "failed_terminal"
    CANCELED = "canceled"
    TIMED_OUT = "timed_out"


class WorkerJobFailureKind(str, Enum):
    """Controller-visible worker failure categories."""

    NONE = ""
    BACKPRESSURE = "backpressure"
    START_TIMEOUT = "start_timeout"
    HEARTBEAT_TIMEOUT = "heartbeat_timeout"
    WORKER_CRASH = "worker_crash"
    IPC_DISCONNECTED = "ipc_disconnected"
    IPC_PROTOCOL_ERROR = "ipc_protocol_error"
    TASK_FAILED = "task_failed"
    CANCELED = "canceled"


class WorkerMessageType(str, Enum):
    """IPC message types exchanged by the supervisor and worker."""

    HELLO = "hello"
    RUN_REQUEST = "run_request"
    CANCEL = "cancel"
    READY = "ready"
    HEARTBEAT = "heartbeat"
    PROGRESS = "progress"
    COMMAND_DISPATCH = "command_dispatch"
    COMMAND_RESPONSE = "command_response"
    TERMINAL = "terminal"
    WORKER_ERROR = "worker_error"


class WorkerCommandDispatchErrorCode(str, Enum):
    """Stable controller command errors that require typed worker handling."""

    TOOL_PARAMETER_VALIDATION = "tool_parameter_validation"


class WorkerArtifactManifest(BaseModel):
    """Worker-produced artifact manifest for controller audit and telemetry.

    Attributes:
        artifact_root: Root directory scanned by the worker before terminal.
        artifact_paths: Absolute artifact paths found below the root.
        artifact_count: Number of artifact files discovered.
        total_bytes: Total byte size of discovered artifact files.
    """

    model_config: ClassVar[ConfigDict] = ConfigDict(extra="forbid")

    artifact_root: str = ""
    artifact_paths: list[str] = Field(default_factory=list)
    artifact_count: int = 0
    total_bytes: int = 0


class WorkerRunRequest(BaseModel):
    """Serialized task context sent from controller supervisor to worker.

    Attributes:
        job_id: Durable worker job id owned by controller SQLite.
        attempt_id: Batch/controller attempt id propagated into artifacts.
        app_config_path: App configuration path loaded by the worker.
        artifact_root_dir_override: Optional controller-owned artifact root.
        command_timeout_seconds: Runner command timeout passed to the agent.
        command_response_timeout_seconds: Worker-side wait budget for command responses.
        heartbeat_interval_seconds: Interval for worker heartbeat messages.
        request: Original controller task request.
        session_id: Controller session id for the task.
        lease: Device lease granted by the controller.
        blob_store_root: Optional controller blob store path.
    """

    model_config: ClassVar[ConfigDict] = ConfigDict(extra="forbid")

    job_id: str
    attempt_id: str
    app_config_path: str
    artifact_root_dir_override: str | None = None
    command_timeout_seconds: float
    command_response_timeout_seconds: float = 0.0
    heartbeat_interval_seconds: float = 5.0
    request: TaskRequest
    session_id: str
    lease: SessionLeaseGranted
    blob_store_root: str | None = None


class WorkerCommandDispatchRequest(BaseModel):
    """Worker intent for the controller to build and dispatch one command.

    Attributes:
        request_id: Worker-local request id used to match IPC responses.
        tool_id: Tool name requested by the runner.
        parameters: JSON parameters for the tool.
        command_id: Protocol command id supplied by the runner.
        timeout_seconds: Tool timeout requested by the runner.
        response_timeout_seconds: Worker wait budget for the controller response.
        metadata_overrides: Allowlisted metadata extensions for command construction.
    """

    model_config: ClassVar[ConfigDict] = ConfigDict(extra="forbid")

    request_id: str
    tool_id: str
    parameters: JsonObject = Field(default_factory=dict)
    command_id: str
    timeout_seconds: float = 10.0
    response_timeout_seconds: float = 0.0
    metadata_overrides: JsonObject = Field(default_factory=dict)


class WorkerCommandDispatchResponse(BaseModel):
    """Controller response for a worker command dispatch request.

    Attributes:
        request_id: Worker request id this response resolves.
        result: Canonical command result when dispatch succeeded.
        error: Error text when the controller could not dispatch the command.
        error_code: Stable error category preserved across worker IPC.
    """

    model_config: ClassVar[ConfigDict] = ConfigDict(extra="forbid")

    request_id: str
    result: CommandResult | None = None
    error: str = ""
    error_code: WorkerCommandDispatchErrorCode | None = None


class WorkerProgressEvent(BaseModel):
    """Best-effort worker progress event forwarded to the submitter socket.

    Attributes:
        message_type: Safe protocol message type forwarded to submitters.
        payload_json: Serialized message payload supplied by the worker.
    """

    model_config: ClassVar[ConfigDict] = ConfigDict(extra="forbid")

    message_type: MessageType
    payload_json: str

    @field_validator("message_type")
    @classmethod
    def _validate_progress_type(cls, value: MessageType) -> MessageType:
        allowed = {
            MessageType.COMMAND_REQUEST,
            MessageType.COMMAND_RESULT,
            MessageType.TELEMETRY_UPDATE,
        }
        if value not in allowed:
            raise ValueError(f"worker progress cannot forward controller message type: {value.value}")
        return value


class WorkerHello(BaseModel):
    """One-shot worker authentication handshake sent before run context.

    Attributes:
        nonce: Supervisor-generated secret used to bind the first IPC client.
        worker_pid: Operating-system process id reported by the child.
    """

    model_config: ClassVar[ConfigDict] = ConfigDict(extra="forbid")

    nonce: str
    worker_pid: int = 0


class WorkerHeartbeat(BaseModel):
    """Worker liveness and phase heartbeat.

    Attributes:
        phase: Coarse worker phase shown in diagnostics.
        step_index: Optional agent step index, when known. ``None`` means the
            worker has not observed a runtime step yet; older workers used
            ``0`` for this unknown state, which made diagnostics ambiguous.
        active_command_id: Command currently blocking the worker.
        artifact_root: Artifact root currently being written.
        worker_pid: Operating-system process id.
        rss_bytes: Backward-compatible peak RSS value from ``ru_maxrss``.
        peak_rss_bytes: Peak resident memory observed by the worker process.
        current_rss_bytes: Current resident memory sampled from the process, or
            zero when the platform does not expose a cheap current-RSS source.
        cpu_time_seconds: User plus system CPU time consumed by the worker.
    """

    model_config: ClassVar[ConfigDict] = ConfigDict(extra="forbid")

    phase: str = ""
    step_index: int | None = None
    active_command_id: str = ""
    artifact_root: str = ""
    worker_pid: int = 0
    rss_bytes: int = 0
    peak_rss_bytes: int = 0
    current_rss_bytes: int = 0
    cpu_time_seconds: float = 0.0

    @field_validator("step_index", mode="before")
    @classmethod
    def _normalize_step_index(cls, value: object) -> int | None:
        if value in (None, ""):
            return None
        if isinstance(value, bool):
            return None
        if isinstance(value, int):
            return value if value > 0 else None
        if isinstance(value, float) and value.is_integer():
            step_index = int(value)
            return step_index if step_index > 0 else None
        if isinstance(value, str):
            try:
                as_float = float(value.strip())
            except ValueError:
                return None
            if as_float.is_integer():
                step_index = int(as_float)
                return step_index if step_index > 0 else None
        return None


class WorkerTerminalResult(BaseModel):
    """Worker terminal task result plus artifact manifest.

    Attributes:
        finished: Canonical task terminal payload returned to the controller.
        artifact_manifest: Worker-collected artifact inventory for telemetry.
    """

    model_config: ClassVar[ConfigDict] = ConfigDict(extra="forbid")

    finished: TaskFinished
    artifact_manifest: WorkerArtifactManifest = Field(default_factory=WorkerArtifactManifest)


class WorkerError(BaseModel):
    """Structured worker-side error returned before a terminal payload exists.

    Attributes:
        failure_kind: Controller-visible failure category.
        message: Human-readable worker error.
        retryable: Whether batch retry may start a fresh attempt.
    """

    model_config: ClassVar[ConfigDict] = ConfigDict(extra="forbid")

    failure_kind: WorkerJobFailureKind = WorkerJobFailureKind.TASK_FAILED
    message: str
    retryable: bool = True


class WorkerEnvelope(BaseModel):
    """Length-framed IPC envelope exchanged over the Unix socket.

    Attributes:
        message_type: Discriminator for the typed worker IPC payload.
        trace_id: Optional trace id for diagnostics.
        payload_json: Serialized payload whose schema depends on message type.
    """

    model_config: ClassVar[ConfigDict] = ConfigDict(extra="forbid")

    message_type: WorkerMessageType
    trace_id: str = ""
    payload_json: str = "{}"


def artifact_manifest_from_root(root: Path | None) -> WorkerArtifactManifest:
    """Build a small artifact manifest from a worker artifact root.

    Args:
        root: Directory to scan for worker-produced artifacts.

    Returns:
        Manifest with artifact paths and byte totals. Missing roots produce an
        empty manifest.
    """

    if root is None or not root.exists():
        return WorkerArtifactManifest()
    paths: list[str] = []
    total = 0
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        paths.append(str(path))
        try:
            total += path.stat().st_size
        except OSError:
            continue
    return WorkerArtifactManifest(
        artifact_root=str(root),
        artifact_paths=paths,
        artifact_count=len(paths),
        total_bytes=total,
    )
