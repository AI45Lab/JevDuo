"""Submitter client for the NorthCUA controller protocol."""

from __future__ import annotations

import asyncio
import os
import uuid
from collections.abc import AsyncIterator, Callable

import websockets
from websockets.asyncio.client import ClientConnection

from northcua.transport.ws.keepalive import client_ping_interval_seconds, client_ping_timeout_seconds
from northcua_protocol.artifacts import ArtifactRef
from northcua_protocol.capabilities import Capability
from northcua_protocol.enums import MessageType
from northcua_protocol.envelope import Envelope, parse_envelope
from northcua_protocol.payloads import (
    AgentKind,
    TaskAccepted,
    TaskCancel,
    TaskFinished,
    TaskListRequest,
    TaskListResponse,
    TaskRequest,
    TaskStatusFilter,
    TaskStatusRequest,
    TaskStatusResponse,
    TaskStatusSnapshot,
    TelemetrySubscribeRequest,
    TelemetryUpdate,
)

DEFAULT_MAX_MESSAGE_BYTES = 64_000_000
DEFAULT_OPEN_TIMEOUT_SECONDS = 30.0
DEFAULT_RESPONSE_TIMEOUT_SECONDS = 60.0
_OPEN_TIMEOUT_ENV = "NORTHCUA_CONTROLLER_CLIENT_OPEN_TIMEOUT_SECONDS"
_RESPONSE_TIMEOUT_ENV = "NORTHCUA_CONTROLLER_CLIENT_RESPONSE_TIMEOUT_SECONDS"


class ControllerSubmitterClient:
    """Small websocket client used by CLIs to submit tasks to a controller."""

    def __init__(
        self,
        server_url: str,
        *,
        max_message_bytes: int = DEFAULT_MAX_MESSAGE_BYTES,
        open_timeout_seconds: float | None = None,
        response_timeout_seconds: float | None = None,
    ) -> None:
        self.server_url = server_url
        self.max_message_bytes = max_message_bytes
        self.open_timeout_seconds = _resolve_open_timeout_seconds(open_timeout_seconds)
        self.response_timeout_seconds = _resolve_response_timeout_seconds(response_timeout_seconds)
        self._connection: ClientConnection | None = None

    async def __aenter__(self) -> "ControllerSubmitterClient":
        await self.connect()
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        await self.close()

    async def connect(self) -> None:
        """Open the controller websocket connection."""

        self._connection = await websockets.connect(
            self.server_url,
            proxy=None,
            open_timeout=self.open_timeout_seconds,
            max_size=self.max_message_bytes,
            ping_interval=client_ping_interval_seconds(),
            ping_timeout=client_ping_timeout_seconds(),
        )

    async def close(self) -> None:
        """Close the controller websocket connection."""

        if self._connection is not None:
            await self._connection.close()
            self._connection = None

    async def submit_task(
        self,
        *,
        agent_kind: AgentKind | str,
        request: str,
        token: str = "",
        target_device_id: str = "",
        required_capabilities: set[Capability] | None = None,
        input_artifacts: list[ArtifactRef] | None = None,
        metadata: dict[str, object] | None = None,
        task_id: str = "",
        trace_id: str = "",
        on_request_sent: Callable[[], None] | None = None,
    ) -> TaskAccepted:
        """Submit a controller task and wait for TASK_ACCEPTED.

        Args:
            agent_kind: Agent runner kind understood by the controller.
            request: User-facing task request text.
            token: Submitter token sent in the task request.
            target_device_id: Optional strict sandbox/device target.
            required_capabilities: Capabilities required from the selected device.
            input_artifacts: Input blobs or inline artifacts attached to the task.
            metadata: Small structured metadata used by the task runner.
            task_id: Optional submitter-controlled idempotent task id.
            trace_id: Optional submitter-controlled trace id.

        Returns:
            Controller acceptance payload containing task, trace, and session ids.

        Raises:
            RuntimeError: If the websocket is closed or the controller rejects the task.
        """

        if self._connection is None:
            raise RuntimeError("controller submitter connection is not open")
        trace_id = trace_id or uuid.uuid4().hex
        task = TaskRequest(
            task_id=task_id or uuid.uuid4().hex,
            trace_id=trace_id,
            agent_kind=agent_kind,
            request=request,
            token=token,
            target_device_id=target_device_id,
            required_capabilities=required_capabilities or set(),
            input_artifacts=input_artifacts or [],
            metadata=metadata or {},
        )
        await self._connection.send(Envelope.wrap(MessageType.TASK_REQUEST, task, trace_id=trace_id).model_dump_json())
        if on_request_sent is not None:
            on_request_sent()
        while True:
            envelope = await self._recv_envelope(timeout_seconds=self.response_timeout_seconds)
            if envelope.type == MessageType.TASK_ACCEPTED and isinstance(envelope.payload, TaskAccepted):
                return envelope.payload
            if envelope.type == MessageType.TASK_FINISHED:
                raise RuntimeError("controller returned TASK_FINISHED before TASK_ACCEPTED")
            if envelope.type == MessageType.ERROR:
                raise RuntimeError(f"controller rejected task: {envelope.payload}")
        raise RuntimeError("controller connection closed before TASK_ACCEPTED")

    async def messages(self) -> AsyncIterator[Envelope]:
        """Yield typed controller envelopes from the current websocket.

        Returns:
            Async iterator of parsed protocol envelopes.

        Raises:
            RuntimeError: If the websocket is not connected.
        """

        if self._connection is None:
            raise RuntimeError("controller submitter connection is not open")
        async for raw in self._connection:
            yield parse_envelope(raw)

    async def wait_for_finished(self, task_id: str, *, timeout_seconds: float | None = None) -> TaskFinished:
        """Wait until the controller emits TASK_FINISHED for a task.

        Args:
            task_id: Task id returned by `submit_task()`.
            timeout_seconds: Optional wall-clock timeout while waiting.

        Returns:
            Terminal task result envelope payload.

        Raises:
            RuntimeError: If the websocket is closed or the controller returns ERROR.
            TimeoutError: If the receive timeout expires.
        """

        if self._connection is None:
            raise RuntimeError("controller submitter connection is not open")
        deadline = _deadline_from_timeout(timeout_seconds)
        while True:
            envelope = await self._recv_envelope_until(deadline)
            if envelope.type == MessageType.TASK_FINISHED and isinstance(envelope.payload, TaskFinished):
                if envelope.payload.task_id == task_id:
                    return envelope.payload
            if envelope.type == MessageType.ERROR:
                raise RuntimeError(f"controller returned error: {envelope.payload}")

    async def task_statuses(self, task_ids: list[str], *, token: str = "") -> list[TaskStatusSnapshot]:
        """Fetch task status snapshots over the controller websocket.

        Args:
            task_ids: Explicit task ids to fetch.
            token: Submitter token for read authorization.

        Returns:
            Status snapshots for known tasks.

        Raises:
            RuntimeError: If the websocket is closed or the controller returns ERROR.
        """

        if self._connection is None:
            raise RuntimeError("controller submitter connection is not open")
        trace_id = uuid.uuid4().hex
        await self._connection.send(
            Envelope.wrap(
                MessageType.TASK_STATUS_REQUEST,
                TaskStatusRequest(task_ids=task_ids, token=token),
                trace_id=trace_id,
            ).model_dump_json()
        )
        while True:
            envelope = await self._recv_envelope(timeout_seconds=self.response_timeout_seconds)
            if envelope.type == MessageType.TASK_STATUS_RESPONSE and isinstance(envelope.payload, TaskStatusResponse):
                return envelope.payload.statuses
            if envelope.type == MessageType.ERROR:
                raise RuntimeError(f"controller returned error: {envelope.payload}")
        raise RuntimeError("controller connection closed before TASK_STATUS_RESPONSE")

    async def list_tasks(self, task_filter: TaskStatusFilter, *, token: str = "") -> list[TaskStatusSnapshot]:
        """Fetch filtered task status snapshots over the controller websocket.

        Args:
            task_filter: Filter selecting tasks to list.
            token: Submitter token for read authorization.

        Returns:
            Matching task snapshots.

        Raises:
            RuntimeError: If the websocket is closed or the controller returns ERROR.
        """

        if self._connection is None:
            raise RuntimeError("controller submitter connection is not open")
        trace_id = uuid.uuid4().hex
        await self._connection.send(
            Envelope.wrap(
                MessageType.TASK_LIST_REQUEST,
                TaskListRequest(filter=task_filter, token=token),
                trace_id=trace_id,
            ).model_dump_json()
        )
        while True:
            envelope = await self._recv_envelope(timeout_seconds=self.response_timeout_seconds)
            if envelope.type == MessageType.TASK_LIST_RESPONSE and isinstance(envelope.payload, TaskListResponse):
                return envelope.payload.tasks
            if envelope.type == MessageType.ERROR:
                raise RuntimeError(f"controller returned error: {envelope.payload}")
        raise RuntimeError("controller connection closed before TASK_LIST_RESPONSE")

    async def cancel_task(self, cancel: TaskCancel) -> None:
        """Send a task cancellation request.

        Args:
            cancel: Task cancellation payload.

        Raises:
            RuntimeError: If the submitter connection is not open.
        """

        if self._connection is None:
            raise RuntimeError("controller submitter connection is not open")
        await asyncio.wait_for(
            self._connection.send(
                Envelope.wrap(MessageType.TASK_CANCEL, cancel, trace_id=cancel.trace_id).model_dump_json()
            ),
            timeout=self.response_timeout_seconds,
        )

    async def telemetry_update(self, request: TelemetrySubscribeRequest) -> TelemetryUpdate:
        """Fetch one telemetry update over the controller websocket.

        Args:
            request: Monitor subscription request.

        Returns:
            Snapshot-plus-events telemetry update.

        Raises:
            RuntimeError: If the websocket is closed or the controller returns ERROR.
        """

        if self._connection is None:
            raise RuntimeError("controller submitter connection is not open")
        trace_id = uuid.uuid4().hex
        await self._connection.send(
            Envelope.wrap(MessageType.TELEMETRY_SUBSCRIBE, request, trace_id=trace_id).model_dump_json()
        )
        while True:
            envelope = await self._recv_envelope(timeout_seconds=self.response_timeout_seconds)
            if envelope.type == MessageType.TELEMETRY_UPDATE and isinstance(envelope.payload, TelemetryUpdate):
                return envelope.payload
            if envelope.type == MessageType.ERROR:
                raise RuntimeError(f"controller returned error: {envelope.payload}")
        raise RuntimeError("controller connection closed before TELEMETRY_UPDATE")

    async def watch_task(
        self,
        task_id: str,
        *,
        session_id: str = "",
        timeout_seconds: float | None = None,
    ) -> AsyncIterator[Envelope]:
        """Yield envelopes for one task until a terminal TASK_FINISHED arrives.

        This is a same-connection watch helper for submitter CLIs. The current
        controller protocol does not yet support cross-connection watch resume,
        so callers should keep the websocket open after `submit_task()`.

        Args:
            task_id: Task id to filter terminal task envelopes by.
            session_id: Optional task session id used to include progress
                envelopes, such as command request/result events, whose wire
                payloads are session-scoped rather than task-scoped.
            timeout_seconds: Optional wall-clock timeout while watching.

        Returns:
            Async iterator of matching task envelopes.

        Raises:
            RuntimeError: If the websocket is closed or the controller returns ERROR.
            TimeoutError: If the receive timeout expires.
        """

        if self._connection is None:
            raise RuntimeError("controller submitter connection is not open")
        deadline = _deadline_from_timeout(timeout_seconds)
        while True:
            envelope = await self._recv_envelope_until(deadline)
            if envelope.type == MessageType.ERROR:
                raise RuntimeError(f"controller returned error: {envelope.payload}")
            matches_task = _envelope_matches_task(envelope, task_id=task_id, session_id=session_id)
            if not matches_task:
                continue
            yield envelope
            if envelope.type == MessageType.TASK_FINISHED and isinstance(envelope.payload, TaskFinished):
                return

    async def _recv_envelope(self, *, timeout_seconds: float | None = None) -> Envelope:
        if self._connection is None:
            raise RuntimeError("controller submitter connection is not open")
        raw = await asyncio.wait_for(self._connection.recv(), timeout=timeout_seconds)
        return parse_envelope(raw)

    async def _recv_envelope_until(self, deadline: float | None) -> Envelope:
        if self._connection is None:
            raise RuntimeError("controller submitter connection is not open")
        timeout = _seconds_until(deadline)
        raw = await asyncio.wait_for(self._connection.recv(), timeout=timeout)
        return parse_envelope(raw)


def _deadline_from_timeout(timeout_seconds: float | None) -> float | None:
    if timeout_seconds is None:
        return None
    return asyncio.get_running_loop().time() + max(0.0, timeout_seconds)


def _seconds_until(deadline: float | None) -> float | None:
    if deadline is None:
        return None
    remaining = deadline - asyncio.get_running_loop().time()
    if remaining <= 0:
        raise TimeoutError
    return remaining


def _resolve_open_timeout_seconds(value: float | None) -> float | None:
    if value is not None:
        return None if value <= 0 else value
    raw = os.getenv(_OPEN_TIMEOUT_ENV, "").strip()
    if not raw:
        return DEFAULT_OPEN_TIMEOUT_SECONDS
    parsed = float(raw)
    if parsed <= 0:
        return None
    return parsed


def _resolve_response_timeout_seconds(value: float | None) -> float | None:
    if value is not None:
        return None if value <= 0 else value
    raw = os.getenv(_RESPONSE_TIMEOUT_ENV, "").strip()
    if not raw:
        return DEFAULT_RESPONSE_TIMEOUT_SECONDS
    parsed = float(raw)
    if parsed <= 0:
        return None
    return parsed


def _envelope_matches_task(envelope: Envelope, *, task_id: str, session_id: str = "") -> bool:
    """Return whether an envelope belongs to a watched task stream."""

    if envelope.type == MessageType.TELEMETRY_UPDATE and isinstance(envelope.payload, TelemetryUpdate):
        return _telemetry_matches_task(envelope.payload, task_id=task_id, session_id=session_id)
    payload_task_id = getattr(envelope.payload, "task_id", None)
    if isinstance(payload_task_id, str) and payload_task_id:
        return payload_task_id == task_id
    if envelope.type in {MessageType.COMMAND_REQUEST, MessageType.COMMAND_RESULT}:
        payload_session_id = getattr(envelope.payload, "session_id", None)
        return bool(session_id) and payload_session_id == session_id
    return False


def _telemetry_matches_task(payload: TelemetryUpdate, *, task_id: str, session_id: str = "") -> bool:
    """Return whether a telemetry batch contains events for the watched task."""

    for event in payload.events:
        if event.get("task_id") == task_id:
            return True
        if session_id and event.get("session_id") == session_id:
            return True
    return False
