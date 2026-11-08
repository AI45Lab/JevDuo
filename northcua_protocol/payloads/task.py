"""Task submitter/controller payloads for the v1 protocol."""

from __future__ import annotations

import hashlib
import json

from pydantic import Field, model_validator

from northcua_protocol.artifacts import ArtifactRef
from northcua_protocol.base import ProtocolModel
from northcua_protocol.capabilities import Capability, JsonObject
from northcua_protocol.state import TaskState


class TaskRequest(ProtocolModel):
    """Submitter -> controller task request.

    Attributes:
        task_id: Submitter-controlled task id.
        trace_id: End-to-end trace id.
        agent_kind: Agent runner kind, currently ``desktop`` or ``web``.
        request: User request or task instruction.
        token: Submitter bearer token.
        target_device_id: Optional strict device target.
        required_capabilities: Capabilities required by the task.
        input_artifacts: Blob/inline refs for large task inputs.
        metadata: Small structured task metadata only.
    """

    task_id: str
    trace_id: str
    agent_kind: str
    request: str
    token: str = ""
    target_device_id: str = ""
    required_capabilities: set[Capability] = Field(default_factory=set)
    input_artifacts: list[ArtifactRef] = Field(default_factory=list)
    metadata: JsonObject = Field(default_factory=dict)


class TaskAccepted(ProtocolModel):
    """Controller -> submitter task accepted payload.

    Attributes:
        task_id: Accepted task id.
        trace_id: End-to-end trace id.
        session_id: Allocated session id.
        device_id: Device selected when already scheduled.
        state: Accepted lifecycle state.
    """

    task_id: str
    trace_id: str
    session_id: str
    device_id: str = ""
    state: TaskState = TaskState.ACCEPTED

    @model_validator(mode="after")
    def _validate_accepted_state(self) -> "TaskAccepted":
        """Ensure task accepted payloads only carry the accepted state."""

        if self.state != TaskState.ACCEPTED:
            raise ValueError("TaskAccepted.state must be accepted")
        return self


class TaskFinished(ProtocolModel):
    """Controller -> submitter terminal task payload.

    Attributes:
        task_id: Finished task id.
        trace_id: End-to-end trace id.
        session_id: Session used by the task.
        state: Terminal task state.
        device_id: Device used by the task.
        lease_id: Lease used by the task.
        result: Small structured task result.
        artifacts: Artifact refs produced by the task.
        error: Optional terminal error.
        terminal_decision: Domain-neutral terminal decision payload when the
            agent produced or derived one.
    """

    task_id: str
    trace_id: str
    session_id: str
    state: TaskState
    device_id: str = ""
    lease_id: str = ""
    result: JsonObject | str | None = None
    artifacts: list[ArtifactRef] = Field(default_factory=list)
    error: str | None = None
    terminal_decision: JsonObject = Field(default_factory=dict)

    @model_validator(mode="after")
    def _validate_terminal_state(self) -> "TaskFinished":
        """Ensure task finished payloads carry only terminal task states."""

        if self.state not in {TaskState.FINISHED, TaskState.FAILED, TaskState.INTERRUPTED}:
            raise ValueError("TaskFinished.state must be terminal")
        return self


class TaskCancel(ProtocolModel):
    """Submitter/controller task cancellation request.

    Attributes:
        task_id: Task to cancel.
        trace_id: End-to-end trace id.
        session_id: Optional active session id.
        reason: Human-readable cancellation reason.
    """

    task_id: str
    trace_id: str
    session_id: str = ""
    reason: str = ""


class TaskStatusFilter(ProtocolModel):
    """Filter used when listing controller task snapshots.

    Attributes:
        task_ids: Optional explicit task ids to include.
        states: Optional task states to include.
        agent_kinds: Optional agent kinds to include.
        batch_id: Optional batch id from task metadata.
        limit: Maximum rows to return; zero lets the controller apply its
            default page size.
    """

    task_ids: list[str] = Field(default_factory=list)
    states: set[TaskState] = Field(default_factory=set)
    agent_kinds: set[str] = Field(default_factory=set)
    batch_id: str = ""
    limit: int = 0


class TaskStatusSnapshot(ProtocolModel):
    """Controller-readable task/session status projection.

    Attributes:
        task_id: Task id.
        trace_id: End-to-end trace id.
        session_id: Session id associated with the task.
        agent_kind: Agent runner kind.
        state: Current or terminal task state.
        device_id: Device currently or terminally associated with the task.
        lease_id: Lease currently or terminally associated with the task.
        result: Small structured terminal result when present.
        artifacts: Artifacts produced by the terminal task payload.
        error: Terminal error when present.
        terminal_decision: Domain-neutral terminal decision payload when
            available.
        request: User-facing task request text. Empty for older snapshots.
        metadata: Small structured task metadata from the original request.
        created_at: Controller task creation timestamp.
        finished_at: Controller terminal timestamp when present.
    """

    task_id: str
    trace_id: str
    session_id: str = ""
    agent_kind: str
    state: TaskState
    device_id: str = ""
    lease_id: str = ""
    result: JsonObject | str | None = None
    artifacts: list[ArtifactRef] = Field(default_factory=list)
    error: str = ""
    terminal_decision: JsonObject = Field(default_factory=dict)
    request: str = ""
    metadata: JsonObject = Field(default_factory=dict)
    request_fingerprint_sha256: str = Field(default="", pattern=r"^$|^[0-9a-f]{64}$")
    created_at: str = ""
    finished_at: str = ""


def task_request_fingerprint_sha256(request: TaskRequest) -> str:
    """Hash the complete public request identity while excluding its credential."""

    payload = request.model_dump(mode="json", exclude={"token"})
    payload["required_capabilities"] = sorted(capability.value for capability in request.required_capabilities)
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


class TaskStatusRequest(ProtocolModel):
    """Submitter -> controller request for explicit task statuses.

    Attributes:
        task_ids: Task ids to fetch.
        token: Submitter token used for controller read authorization.
    """

    task_ids: list[str]
    token: str = ""


class TaskStatusResponse(ProtocolModel):
    """Controller -> submitter response for explicit task statuses."""

    statuses: list[TaskStatusSnapshot] = Field(default_factory=list)


class TaskListRequest(ProtocolModel):
    """Submitter -> controller request for filtered task status snapshots.

    Attributes:
        filter: Status/list filter.
        token: Submitter token used for controller read authorization.
    """

    filter: TaskStatusFilter = Field(default_factory=TaskStatusFilter)
    token: str = ""


class TaskListResponse(ProtocolModel):
    """Controller -> submitter response for filtered task snapshots."""

    tasks: list[TaskStatusSnapshot] = Field(default_factory=list)


__all__ = [
    "TaskAccepted",
    "TaskCancel",
    "TaskFinished",
    "TaskListRequest",
    "TaskListResponse",
    "TaskRequest",
    "TaskStatusFilter",
    "TaskStatusRequest",
    "TaskStatusResponse",
    "TaskStatusSnapshot",
    "task_request_fingerprint_sha256",
]
