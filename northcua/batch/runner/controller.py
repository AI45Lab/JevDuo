"""Controller submit/watch helpers for batch execution."""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import inspect
import logging
import os
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Protocol, TypeVar

from websockets.exceptions import ConnectionClosed

from northcua.batch.planner import PlannedTaskSubmission
from northcua.submitter.client import ControllerSubmitterClient
from northcua_protocol.artifacts import ArtifactRef
from northcua_protocol.capabilities import Capability
from northcua_protocol.payloads import (
    TaskAccepted,
    TaskCancel,
    TaskFinished,
    TaskStatusFilter,
    TaskStatusSnapshot,
)
from northcua_protocol.state import TaskState

DEFAULT_CANCEL_GRACE_SECONDS = 30.0
DEFAULT_DISCONNECT_STATUS_POLL_SECONDS = 5.0
DEFAULT_SUBMIT_WAIT_STATUS_POLL_SECONDS = 30.0
DEFAULT_STATUS_POLL_BATCH_WINDOW_SECONDS = 1.0
DEFAULT_STATUS_POLL_BATCH_SIZE = 500
DEFAULT_STATUS_POLL_REQUEST_TIMEOUT_SECONDS = 30.0
DEFAULT_STATUS_POLL_SHUTDOWN_TIMEOUT_SECONDS = 30.0
DEFAULT_SUBMIT_JITTER_OVERSHOOT_THRESHOLD_SECONDS = 5.0
DEFAULT_SUBMIT_JITTER_OVERSHOOT_REJITTER_SECONDS = 0.0
DEFAULT_SUBMIT_JITTER_OVERSHOOT_OPEN_INTERVAL_SECONDS = 0.0

logger = logging.getLogger(__name__)
_T = TypeVar("_T")


def _consume_background_task_result(task: asyncio.Task[object]) -> None:
    """Retrieve a detached shutdown task result to avoid warning leaks."""

    with contextlib.suppress(asyncio.CancelledError, Exception):
        task.result()


class ControllerTaskTimeoutError(TimeoutError):
    """Raised after a controller task wait times out and cancellation is attempted."""

    def __init__(
        self,
        message: str,
        *,
        accepted: TaskAccepted,
        finished: TaskFinished | None = None,
    ) -> None:
        super().__init__(message)
        self.accepted = accepted
        self.finished = finished


class TaskSubmitter(Protocol):
    """Minimal controller submitter surface used by the batch runner."""

    async def __aenter__(self) -> "TaskSubmitter":
        """Open any underlying connection and return the submitter."""
        ...

    async def __aexit__(self, *exc_info: object) -> None:
        """Close any underlying connection."""
        ...

    async def submit_task(
        self,
        *,
        agent_kind: str,
        request: str,
        token: str,
        target_device_id: str,
        required_capabilities: set[Capability],
        input_artifacts: list[ArtifactRef],
        metadata: dict[str, object],
        task_id: str = "",
        trace_id: str = "",
        on_request_sent: Callable[[], None] | None = None,
    ) -> TaskAccepted:
        """Submit a task to the controller and return acceptance metadata.

        Args:
            agent_kind: AgentKit kind to execute.
            request: User task text sent to the agent runner.
            token: Submitter authorization token.
            target_device_id: Strict device id selected by the sandbox lease.
            required_capabilities: Capabilities required for scheduling.
            input_artifacts: Artifacts attached to the controller task.
            metadata: JSON metadata used for trace and resume lookup.
            task_id: Optional submitter-controlled idempotent task id.
            trace_id: Optional submitter-controlled trace id.

        Returns:
            Controller acceptance payload containing task/session ids.
        """
        ...

    async def wait_for_finished(self, task_id: str, *, timeout_seconds: float | None = None) -> TaskFinished:
        """Wait for a terminal controller task result.

        Args:
            task_id: Controller task id returned by ``submit_task``.
            timeout_seconds: Optional receive timeout for the terminal result.

        Returns:
            Terminal task payload emitted by the controller.
        """
        ...

    async def task_statuses(self, task_ids: list[str], *, token: str = "") -> list[TaskStatusSnapshot]:
        """Fetch explicit task statuses.

        Args:
            task_ids: Controller task ids to query.
            token: Submitter read token for status APIs.

        Returns:
            Snapshots for known tasks in controller storage.
        """
        ...

    async def list_tasks(self, task_filter: TaskStatusFilter, *, token: str = "") -> list[TaskStatusSnapshot]:
        """Fetch filtered task statuses.

        Args:
            task_filter: Controller-side filter for tasks to list.
            token: Submitter read token for status APIs.

        Returns:
            Matching task snapshots, capped by controller limits.
        """
        ...

    async def cancel_task(self, cancel: TaskCancel) -> None:
        """Send a task cancellation request.

        Args:
            cancel: Task id, trace id, and optional session-scoped reason.
        """
        ...


SubmitterFactory = Callable[[str], TaskSubmitter]
SubmitAcceptedCallback = Callable[[TaskAccepted, float], None]
BeforeSubmitCallback = Callable[[], Awaitable[None]]
SubmitOpenReadyCallback = Callable[[dict[str, object]], Awaitable[None]]
SubmitRequestSentCallback = Callable[[dict[str, object]], None]


@dataclass(frozen=True)
class _QueuedStatusPoll:
    future: asyncio.Future[TaskStatusSnapshot | None]
    queued_at: float


class _BatchedStatusPoller:
    """Coalesce per-task recovery polls into bounded controller requests."""

    def __init__(
        self,
        submitter_factory: SubmitterFactory,
        *,
        batch_window_seconds: float,
        max_batch_size: int,
        request_timeout_seconds: float,
        shutdown_timeout_seconds: float,
    ) -> None:
        self._submitter_factory = submitter_factory
        self._batch_window_seconds = max(0.0, batch_window_seconds)
        self._max_batch_size = max(1, max_batch_size)
        self._request_timeout_seconds = max(0.1, request_timeout_seconds)
        self._shutdown_timeout_seconds = max(0.0, shutdown_timeout_seconds)
        self._pending: dict[tuple[str, str], dict[str, list[_QueuedStatusPoll]]] = {}
        self._flush_tasks: dict[tuple[str, str], asyncio.Task[None]] = {}
        self._closed = False
        self._close_task: asyncio.Task[None] | None = None

    @property
    def request_timeout_seconds(self) -> float:
        """Return the per-batch controller request deadline."""

        return self._request_timeout_seconds

    async def snapshot(
        self,
        *,
        controller_url: str,
        token: str,
        task_id: str,
        deadline: float | None,
    ) -> TaskStatusSnapshot | None:
        if self._closed:
            raise RuntimeError("controller status poller is closed")
        loop = asyncio.get_running_loop()
        future: asyncio.Future[TaskStatusSnapshot | None] = loop.create_future()
        key = (controller_url, token)
        queued = _QueuedStatusPoll(future=future, queued_at=time.monotonic())
        self._pending.setdefault(key, {}).setdefault(task_id, []).append(queued)
        task = self._flush_tasks.get(key)
        if task is None or task.done():
            self._flush_tasks[key] = asyncio.create_task(
                self._flush(key),
                name="northcua-controller-status-poll-batch",
            )
        try:
            return await _await_with_deadline(future, deadline=deadline)
        finally:
            self._remove_waiter(key, task_id, queued)
            if not future.done():
                future.cancel()

    async def aclose(self) -> None:
        """Cancel pending batches and reject new status polls."""

        close_task = self._close_task
        if close_task is None:
            close_task = asyncio.create_task(
                self._close_impl(),
                name="northcua-controller-status-poller-close",
            )
            self._close_task = close_task
        await asyncio.shield(close_task)

    async def _close_impl(self) -> None:
        self._closed = True
        pending = self._pending
        self._pending = {}
        for task_groups in pending.values():
            for queued_group in task_groups.values():
                for queued in queued_group:
                    if not queued.future.done():
                        queued.future.cancel()
        flush_tasks = list(self._flush_tasks.values())
        self._flush_tasks.clear()
        for task in flush_tasks:
            task.cancel()
        if flush_tasks:
            done, still_running = await asyncio.wait(
                flush_tasks,
                timeout=self._shutdown_timeout_seconds,
            )
            if done:
                await asyncio.gather(*done, return_exceptions=True)
            for task in still_running:
                task.add_done_callback(_consume_background_task_result)
            if still_running:
                logger.warning(
                    "controller status poller close deadline elapsed",
                    extra={
                        "pending_flush_tasks": len(still_running),
                        "timeout_seconds": self._shutdown_timeout_seconds,
                    },
                )

    def _remove_waiter(
        self,
        key: tuple[str, str],
        task_id: str,
        queued: _QueuedStatusPoll,
    ) -> None:
        pending = self._pending.get(key)
        if pending is None:
            return
        queued_group = pending.get(task_id)
        if queued_group is None:
            return
        with contextlib.suppress(ValueError):
            queued_group.remove(queued)
        if not queued_group:
            pending.pop(task_id, None)
        if not pending:
            self._pending.pop(key, None)

    async def _flush(self, key: tuple[str, str]) -> None:
        controller_url, token = key
        try:
            if self._batch_window_seconds > 0:
                await asyncio.sleep(self._batch_window_seconds)
            while not self._closed:
                pending = self._pending.get(key)
                if not pending:
                    return
                task_ids = list(pending)[: self._max_batch_size]
                requests = {
                    task_id: [queued for queued in pending.pop(task_id) if not queued.future.done()]
                    for task_id in task_ids
                }
                requests = {task_id: group for task_id, group in requests.items() if group}
                if not pending:
                    self._pending.pop(key, None)
                if not requests:
                    continue
                task_ids = list(requests)
                started_at = time.monotonic()
                try:
                    async with self._submitter_factory(controller_url) as submitter:
                        statuses = await asyncio.wait_for(
                            submitter.task_statuses(task_ids, token=token),
                            timeout=self._request_timeout_seconds,
                        )
                except Exception as exc:
                    for queued_group in requests.values():
                        for queued in queued_group:
                            if not queued.future.done():
                                queued.future.set_exception(exc)
                else:
                    if self._closed:
                        for queued_group in requests.values():
                            for queued in queued_group:
                                if not queued.future.done():
                                    queued.future.cancel()
                        return
                    by_task_id = {status.task_id: status for status in statuses}
                    for task_id, queued_group in requests.items():
                        snapshot = by_task_id.get(task_id)
                        for queued in queued_group:
                            if not queued.future.done():
                                queued.future.set_result(snapshot)
                    queue_seconds = [
                        max(started_at - queued.queued_at, 0.0)
                        for queued_group in requests.values()
                        for queued in queued_group
                    ]
                    logger.debug(
                        "controller status poll batch completed",
                        extra={
                            "task_count": len(task_ids),
                            "waiter_count": sum(len(group) for group in requests.values()),
                            "queue_seconds_max": round(max(queue_seconds, default=0.0), 6),
                            "request_seconds": round(time.monotonic() - started_at, 6),
                        },
                    )
        finally:
            self._flush_tasks.pop(key, None)
            if not self._closed and self._pending.get(key):
                self._flush_tasks[key] = asyncio.create_task(
                    self._flush(key),
                    name="northcua-controller-status-poll-batch",
                )


def _remaining_seconds(deadline: float | None) -> float | None:
    if deadline is None:
        return None
    return max(0.0, deadline - time.monotonic())


def _round_seconds(value: float) -> float:
    return round(max(float(value), 0.0), 3)


async def _await_with_deadline(awaitable: Awaitable[_T], *, deadline: float | None) -> _T:
    remaining = _remaining_seconds(deadline)
    if remaining is None:
        return await awaitable
    return await asyncio.wait_for(awaitable, timeout=remaining)


def _finished_from_snapshot(snapshot: TaskStatusSnapshot, *, accepted: TaskAccepted) -> TaskFinished:
    return TaskFinished(
        task_id=snapshot.task_id,
        trace_id=snapshot.trace_id,
        session_id=snapshot.session_id or accepted.session_id,
        state=snapshot.state,
        device_id=snapshot.device_id,
        lease_id=snapshot.lease_id,
        result=snapshot.result,
        artifacts=snapshot.artifacts,
        error=snapshot.error or None,
        terminal_decision=snapshot.terminal_decision,
    )


def _stable_submit_jitter_seconds(planned: PlannedTaskSubmission, max_seconds: float) -> float:
    """Return a deterministic spread delay for controller submit openings."""

    if max_seconds <= 0:
        return 0.0
    key = f"{planned.controller_task_id}:{planned.trace_id}:{planned.target_device_id}"
    digest = hashlib.sha256(key.encode("utf-8")).digest()
    fraction = int.from_bytes(digest[:8], "big") / float(2**64 - 1)
    return fraction * max_seconds


def _env_float(name: str, default: float) -> float:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _submit_wait_status_poll_seconds() -> float:
    return max(
        _env_float(
            "NORTHCUA_BATCH_SUBMIT_WAIT_STATUS_POLL_SECONDS",
            DEFAULT_SUBMIT_WAIT_STATUS_POLL_SECONDS,
        ),
        0.0,
    )


def _submit_jitter_overshoot_rejitter_seconds(
    planned: PlannedTaskSubmission,
    *,
    requested_jitter_seconds: float,
    planned_delay_seconds: float,
    observed_sleep_seconds: float,
) -> tuple[float, float]:
    """Return optional extra spread delay when a jitter sleep wakes late.

    Overshoot is always reported in submit telemetry. By default it does not
    alter submit timing: a blocked runner loop is itself the thing to diagnose,
    and sleeping again in the hot path can turn a short scheduler stall into a
    long self-inflicted submit drain. Operators may opt in with
    NORTHCUA_BATCH_SUBMIT_JITTER_OVERSHOOT_REJITTER_SECONDS during an incident.
    """

    overshoot_seconds = max(observed_sleep_seconds - planned_delay_seconds, 0.0)
    if requested_jitter_seconds <= 0 or planned_delay_seconds <= 0 or overshoot_seconds <= 0:
        return 0.0, overshoot_seconds
    threshold = max(
        _env_float(
            "NORTHCUA_BATCH_SUBMIT_JITTER_OVERSHOOT_REJITTER_THRESHOLD_SECONDS",
            DEFAULT_SUBMIT_JITTER_OVERSHOOT_THRESHOLD_SECONDS,
        ),
        0.0,
    )
    if overshoot_seconds < threshold:
        return 0.0, overshoot_seconds
    default_cap = min(max(requested_jitter_seconds, 0.0), DEFAULT_SUBMIT_JITTER_OVERSHOOT_REJITTER_SECONDS)
    cap = max(
        _env_float("NORTHCUA_BATCH_SUBMIT_JITTER_OVERSHOOT_REJITTER_SECONDS", default_cap),
        0.0,
    )
    if cap <= 0:
        return 0.0, overshoot_seconds
    return _stable_submit_jitter_seconds(planned, cap), overshoot_seconds


class ControllerTaskWatcher:
    """Submit tasks and query controller task status through a submitter."""

    def __init__(
        self,
        submitter_factory: SubmitterFactory | None = None,
        *,
        disconnect_status_poll_seconds: float = DEFAULT_DISCONNECT_STATUS_POLL_SECONDS,
        submit_wait_status_poll_seconds: float | None = None,
        status_poll_batch_window_seconds: float | None = None,
        status_poll_batch_size: int | None = None,
        status_poll_request_timeout_seconds: float | None = None,
        status_poll_shutdown_timeout_seconds: float | None = None,
    ) -> None:
        self._submitter_factory = submitter_factory or (lambda url: ControllerSubmitterClient(url))
        self._disconnect_status_poll_seconds = disconnect_status_poll_seconds
        self._submit_wait_status_poll_seconds = (
            _submit_wait_status_poll_seconds()
            if submit_wait_status_poll_seconds is None
            else max(float(submit_wait_status_poll_seconds), 0.0)
        )
        self._overshoot_open_lock = asyncio.Lock()
        self._next_overshoot_open_at = 0.0
        self._request_sent_callback_support: dict[type[object], bool] = {}
        configured_batch_window = (
            max(
                _env_float(
                    "NORTHCUA_BATCH_STATUS_POLL_BATCH_WINDOW_SECONDS",
                    DEFAULT_STATUS_POLL_BATCH_WINDOW_SECONDS,
                ),
                0.0,
            )
            if status_poll_batch_window_seconds is None
            else max(float(status_poll_batch_window_seconds), 0.0)
        )
        positive_poll_intervals = [
            value
            for value in (self._disconnect_status_poll_seconds, self._submit_wait_status_poll_seconds)
            if value > 0
        ]
        if positive_poll_intervals:
            configured_batch_window = min(configured_batch_window, min(positive_poll_intervals))
        elif self._disconnect_status_poll_seconds <= 0:
            configured_batch_window = 0.0
        self._status_poller = _BatchedStatusPoller(
            self._submitter_factory,
            batch_window_seconds=configured_batch_window,
            max_batch_size=(
                max(_env_int("NORTHCUA_BATCH_STATUS_POLL_BATCH_SIZE", DEFAULT_STATUS_POLL_BATCH_SIZE), 1)
                if status_poll_batch_size is None
                else max(int(status_poll_batch_size), 1)
            ),
            request_timeout_seconds=(
                max(
                    _env_float(
                        "NORTHCUA_BATCH_STATUS_POLL_REQUEST_TIMEOUT_SECONDS",
                        DEFAULT_STATUS_POLL_REQUEST_TIMEOUT_SECONDS,
                    ),
                    0.1,
                )
                if status_poll_request_timeout_seconds is None
                else max(float(status_poll_request_timeout_seconds), 0.1)
            ),
            shutdown_timeout_seconds=(
                max(
                    _env_float(
                        "NORTHCUA_BATCH_STATUS_POLL_SHUTDOWN_TIMEOUT_SECONDS",
                        DEFAULT_STATUS_POLL_SHUTDOWN_TIMEOUT_SECONDS,
                    ),
                    0.0,
                )
                if status_poll_shutdown_timeout_seconds is None
                else max(float(status_poll_shutdown_timeout_seconds), 0.0)
            ),
        )

    async def aclose(self) -> None:
        """Release background status-poll batching tasks."""

        await self._status_poller.aclose()

    async def submit_and_wait(
        self,
        *,
        controller_url: str,
        token: str,
        planned: PlannedTaskSubmission,
        timeout_seconds: float | None,
        submit_semaphore: asyncio.Semaphore | None = None,
        submit_jitter_seconds: float = 0.0,
        on_accepted: SubmitAcceptedCallback | None = None,
        before_submit: BeforeSubmitCallback | None = None,
        on_submit_open_ready: SubmitOpenReadyCallback | None = None,
        on_request_sent: SubmitRequestSentCallback | None = None,
    ) -> tuple[TaskAccepted, TaskFinished]:
        """Submit a planned task and wait for the terminal controller result.

        Args:
            controller_url: Controller websocket URL.
            token: Submitter token used by the controller.
            planned: Controller request built from the batch manifest.
            timeout_seconds: Optional wait timeout for the task result.
            submit_semaphore: Optional gate around controller submitter
                websocket opening and TASK_ACCEPTED waiting.
            submit_jitter_seconds: Deterministic delay applied inside the
                submit gate to avoid handshake bursts.
            on_accepted: Optional diagnostic callback invoked after TASK_ACCEPTED
                with elapsed seconds for connection/opening/submit acceptance.
            before_submit: Optional callback invoked immediately before opening
                the submitter connection, after any semaphore wait and jitter.
            on_submit_open_ready: Optional diagnostic callback invoked
                immediately before opening the submitter connection.
            on_request_sent: Optional diagnostic callback invoked immediately
                before handing the request to the submitter client.

        Returns:
            Pair of acceptance metadata and terminal task payload.

        Raises:
            ControllerTaskTimeoutError: If the controller task times out and
                cancellation is issued.
        """

        deadline = None if timeout_seconds is None else time.monotonic() + timeout_seconds
        submitter: TaskSubmitter | None = None
        submit_started = time.monotonic()
        try:
            submitter, accepted = await self._connect_and_submit(
                controller_url=controller_url,
                token=token,
                planned=planned,
                submit_semaphore=submit_semaphore,
                submit_jitter_seconds=submit_jitter_seconds,
                before_submit=before_submit,
                on_submit_open_ready=on_submit_open_ready,
                on_request_sent=on_request_sent,
            )
            if on_accepted is not None:
                try:
                    on_accepted(accepted, time.monotonic() - submit_started)
                except Exception:
                    logger.warning(
                        "controller submit accepted diagnostic callback failed; continuing to wait for task",
                        extra={"task_id": accepted.task_id, "trace_id": accepted.trace_id},
                        exc_info=True,
                    )
            try:
                remaining = _remaining_seconds(deadline)
                finished = await self._wait_for_finished_with_idle_status_poll(
                    submitter=submitter,
                    controller_url=controller_url,
                    token=token,
                    accepted=accepted,
                    timeout_seconds=remaining,
                    deadline=deadline,
                )
            except asyncio.CancelledError:
                cancel = TaskCancel(
                    task_id=accepted.task_id,
                    trace_id=accepted.trace_id,
                    session_id=accepted.session_id,
                    reason="batch task interrupted by local scheduler",
                )
                await submitter.cancel_task(cancel)
                raise
            except ConnectionClosed as exc:
                recovered = await self._recover_finished_after_disconnect(
                    controller_url=controller_url,
                    token=token,
                    accepted=accepted,
                    deadline=deadline,
                )
                if recovered is None:
                    raise RuntimeError(
                        f"controller connection closed before terminal result for task {accepted.task_id}"
                    ) from exc
                finished = recovered
            except TimeoutError as exc:
                cancel = TaskCancel(
                    task_id=accepted.task_id,
                    trace_id=accepted.trace_id,
                    session_id=accepted.session_id,
                    reason=f"batch task timed out after {timeout_seconds}s",
                )
                await submitter.cancel_task(cancel)
                try:
                    cancelled = await submitter.wait_for_finished(
                        accepted.task_id,
                        timeout_seconds=DEFAULT_CANCEL_GRACE_SECONDS,
                    )
                except TimeoutError as grace_exc:
                    raise ControllerTaskTimeoutError(
                        f"controller task {accepted.task_id} timed out and did not finish after cancellation",
                        accepted=accepted,
                    ) from grace_exc
                raise ControllerTaskTimeoutError(
                    f"controller task {accepted.task_id} timed out and was cancelled",
                    accepted=accepted,
                    finished=cancelled,
                ) from exc
        finally:
            if submitter is not None:
                await submitter.__aexit__(None, None, None)
        return accepted, finished

    async def _wait_for_finished_with_idle_status_poll(
        self,
        *,
        submitter: TaskSubmitter,
        controller_url: str,
        token: str,
        accepted: TaskAccepted,
        timeout_seconds: float | None,
        deadline: float | None,
    ) -> TaskFinished:
        wait_task = asyncio.create_task(
            submitter.wait_for_finished(accepted.task_id, timeout_seconds=timeout_seconds),
            name=f"northcua-controller-wait:{accepted.task_id}",
        )
        poll_seconds = self._submit_wait_status_poll_seconds
        try:
            while True:
                remaining = _remaining_seconds(deadline)
                if remaining is not None and remaining <= 0:
                    raise TimeoutError
                if poll_seconds <= 0:
                    return await _await_with_deadline(wait_task, deadline=deadline)
                wait_seconds = poll_seconds if remaining is None else min(poll_seconds, remaining)
                done, _ = await asyncio.wait({wait_task}, timeout=wait_seconds)
                if wait_task in done:
                    return await wait_task
                remaining = _remaining_seconds(deadline)
                if remaining is not None and remaining <= 0:
                    raise TimeoutError
                snapshot = await self._terminal_status_snapshot(
                    controller_url=controller_url,
                    token=token,
                    accepted=accepted,
                    deadline=deadline,
                )
                if snapshot is None:
                    continue
                logger.warning(
                    "controller task reached terminal state while submitter wait socket was idle",
                    extra={
                        "task_id": accepted.task_id,
                        "trace_id": accepted.trace_id,
                        "controller_state": snapshot.state.value,
                    },
                )
                return _finished_from_snapshot(snapshot, accepted=accepted)
        finally:
            if not wait_task.done():
                wait_task.cancel()
                try:
                    await wait_task
                except asyncio.CancelledError:
                    pass
                except Exception:
                    logger.debug(
                        "controller wait task raised after idle status poll cancellation",
                        extra={"task_id": accepted.task_id, "trace_id": accepted.trace_id},
                        exc_info=True,
                    )

    async def _connect_and_submit(
        self,
        *,
        controller_url: str,
        token: str,
        planned: PlannedTaskSubmission,
        submit_semaphore: asyncio.Semaphore | None,
        submit_jitter_seconds: float,
        before_submit: BeforeSubmitCallback | None,
        on_submit_open_ready: SubmitOpenReadyCallback | None,
        on_request_sent: SubmitRequestSentCallback | None,
    ) -> tuple[TaskSubmitter, TaskAccepted]:
        """Open a submitter connection and wait only through TASK_ACCEPTED."""

        queued_at = time.monotonic()
        if submit_semaphore is None:
            before_submit_elapsed = 0.0
            if before_submit is not None:
                before_started_at = time.monotonic()
                await before_submit()
                before_submit_elapsed = time.monotonic() - before_started_at
            if on_submit_open_ready is not None:
                await on_submit_open_ready(
                    {
                        "submit_semaphore": False,
                        "submit_gate_wait_seconds": 0.0,
                        "planned_submit_jitter_seconds": 0.0,
                        "actual_submit_jitter_sleep_seconds": 0.0,
                        "submit_jitter_overshoot_seconds": 0.0,
                        "submit_jitter_secondary_delay_seconds": 0.0,
                        "submit_open_rate_limit_wait_seconds": 0.0,
                        "before_submit_elapsed_seconds": _round_seconds(before_submit_elapsed),
                        "submit_started_to_open_started_seconds": _round_seconds(time.monotonic() - queued_at),
                    }
                )
            return await self._connect_and_submit_now(
                controller_url=controller_url,
                token=token,
                planned=planned,
                open_started_at=time.monotonic(),
                on_request_sent=on_request_sent,
            )
        async with submit_semaphore:
            acquired_at = time.monotonic()
            delay = _stable_submit_jitter_seconds(planned, submit_jitter_seconds)
            observed_sleep = 0.0
            overshoot = 0.0
            extra_delay = 0.0
            rate_limit_wait = 0.0
            if delay > 0:
                sleep_started_at = time.monotonic()
                await asyncio.sleep(delay)
                observed_sleep = time.monotonic() - sleep_started_at
                extra_delay, overshoot = _submit_jitter_overshoot_rejitter_seconds(
                    planned,
                    requested_jitter_seconds=submit_jitter_seconds,
                    planned_delay_seconds=delay,
                    observed_sleep_seconds=observed_sleep,
                )
                if extra_delay > 0:
                    logger.warning(
                        "controller submit jitter overshot; applying secondary spread before opening websocket",
                        extra={
                            "task_id": planned.controller_task_id,
                            "trace_id": planned.trace_id,
                            "planned_delay_seconds": round(delay, 3),
                            "overshoot_seconds": round(overshoot, 3),
                            "extra_delay_seconds": round(extra_delay, 3),
                        },
                    )
                    await asyncio.sleep(extra_delay)
                    rate_limit_wait = await self._rate_limit_overshot_submit_open(
                        planned,
                        overshoot_seconds=overshoot,
                    )
            before_submit_elapsed = 0.0
            if before_submit is not None:
                before_started_at = time.monotonic()
                await before_submit()
                before_submit_elapsed = time.monotonic() - before_started_at
            if on_submit_open_ready is not None:
                await on_submit_open_ready(
                    {
                        "submit_semaphore": True,
                        "submit_gate_wait_seconds": _round_seconds(acquired_at - queued_at),
                        "planned_submit_jitter_seconds": _round_seconds(delay),
                        "actual_submit_jitter_sleep_seconds": _round_seconds(observed_sleep),
                        "submit_jitter_overshoot_seconds": _round_seconds(overshoot),
                        "submit_jitter_secondary_delay_seconds": _round_seconds(extra_delay),
                        "submit_open_rate_limit_wait_seconds": _round_seconds(rate_limit_wait),
                        "before_submit_elapsed_seconds": _round_seconds(before_submit_elapsed),
                        "submit_started_to_open_started_seconds": _round_seconds(time.monotonic() - queued_at),
                    }
                )
            return await self._connect_and_submit_now(
                controller_url=controller_url,
                token=token,
                planned=planned,
                open_started_at=time.monotonic(),
                on_request_sent=on_request_sent,
            )

    async def _rate_limit_overshot_submit_open(
        self,
        planned: PlannedTaskSubmission,
        *,
        overshoot_seconds: float,
    ) -> float:
        interval_seconds = max(
            _env_float(
                "NORTHCUA_BATCH_SUBMIT_JITTER_OVERSHOOT_OPEN_INTERVAL_SECONDS",
                DEFAULT_SUBMIT_JITTER_OVERSHOOT_OPEN_INTERVAL_SECONDS,
            ),
            0.0,
        )
        if interval_seconds <= 0:
            return 0.0
        total_wait_seconds = 0.0
        while True:
            async with self._overshoot_open_lock:
                now = time.monotonic()
                wait_seconds = max(self._next_overshoot_open_at - now, 0.0)
                if wait_seconds <= 0:
                    self._next_overshoot_open_at = max(now, self._next_overshoot_open_at) + interval_seconds
                    return total_wait_seconds
            logger.warning(
                "controller submit open delayed by overshoot rate limiter",
                extra={
                    "task_id": planned.controller_task_id,
                    "trace_id": planned.trace_id,
                    "overshoot_seconds": round(overshoot_seconds, 3),
                    "rate_limit_wait_seconds": round(wait_seconds, 3),
                    "rate_limit_interval_seconds": round(interval_seconds, 3),
                },
            )
            wait_started_at = time.monotonic()
            await asyncio.sleep(wait_seconds)
            total_wait_seconds += max(time.monotonic() - wait_started_at, 0.0)

    async def _connect_and_submit_now(
        self,
        *,
        controller_url: str,
        token: str,
        planned: PlannedTaskSubmission,
        open_started_at: float,
        on_request_sent: SubmitRequestSentCallback | None = None,
    ) -> tuple[TaskSubmitter, TaskAccepted]:
        submitter = self._submitter_factory(controller_url)
        entered = False
        connect_started_at = time.monotonic()
        request_send_started_at = 0.0
        request_sent_emitted = False

        def _emit_request_sent(*, supported: bool) -> None:
            nonlocal request_sent_emitted
            if on_request_sent is None or request_sent_emitted:
                return
            request_sent_emitted = True
            now = time.monotonic()
            try:
                on_request_sent(
                    {
                        "connection_open_elapsed_seconds": _round_seconds(request_send_started_at - connect_started_at),
                        "request_send_elapsed_seconds": _round_seconds(max(now - request_send_started_at, 0.0)),
                        "request_sent_after_open_started_seconds": _round_seconds(now - open_started_at),
                        "request_sent_callback_supported": supported,
                    }
                )
            except Exception:
                logger.warning(
                    "controller submit request-sent diagnostic callback failed; continuing submit",
                    extra={"task_id": planned.controller_task_id, "trace_id": planned.trace_id},
                    exc_info=True,
                )

        try:
            await submitter.__aenter__()
            entered = True
            request_send_started_at = time.monotonic()
            metadata = dict(planned.metadata)
            if self._submitter_accepts_request_sent_callback(submitter):
                try:
                    accepted = await submitter.submit_task(
                        agent_kind=planned.agent_kind,
                        request=planned.request,
                        token=token,
                        target_device_id=planned.target_device_id,
                        required_capabilities=planned.required_capabilities,
                        input_artifacts=planned.input_artifacts,
                        metadata=metadata,
                        task_id=planned.controller_task_id,
                        trace_id=planned.trace_id,
                        on_request_sent=lambda: _emit_request_sent(supported=True),
                    )
                except TypeError as exc:
                    if "on_request_sent" not in str(exc):
                        raise
                    self._request_sent_callback_support[type(submitter)] = False
                    accepted = await submitter.submit_task(
                        agent_kind=planned.agent_kind,
                        request=planned.request,
                        token=token,
                        target_device_id=planned.target_device_id,
                        required_capabilities=planned.required_capabilities,
                        input_artifacts=planned.input_artifacts,
                        metadata=metadata,
                        task_id=planned.controller_task_id,
                        trace_id=planned.trace_id,
                    )
            else:
                accepted = await submitter.submit_task(
                    agent_kind=planned.agent_kind,
                    request=planned.request,
                    token=token,
                    target_device_id=planned.target_device_id,
                    required_capabilities=planned.required_capabilities,
                    input_artifacts=planned.input_artifacts,
                    metadata=metadata,
                    task_id=planned.controller_task_id,
                    trace_id=planned.trace_id,
                )
            _emit_request_sent(supported=False)
        except BaseException:
            logger.debug("controller submitter failed before task was accepted", exc_info=True)
            if entered:
                await submitter.__aexit__(None, None, None)
            raise
        return submitter, accepted

    def _submitter_accepts_request_sent_callback(self, submitter: TaskSubmitter) -> bool:
        submitter_type = type(submitter)
        cached = self._request_sent_callback_support.get(submitter_type)
        if cached is not None:
            return cached
        try:
            signature = inspect.signature(submitter.submit_task)
        except (TypeError, ValueError):
            accepts = True
        else:
            accepts = "on_request_sent" in signature.parameters or any(
                parameter.kind == inspect.Parameter.VAR_KEYWORD for parameter in signature.parameters.values()
            )
        self._request_sent_callback_support[submitter_type] = accepts
        return accepts

    async def _recover_finished_after_disconnect(
        self,
        *,
        controller_url: str,
        token: str,
        accepted: TaskAccepted,
        deadline: float | None = None,
    ) -> TaskFinished | None:
        """Poll controller storage for terminal payload after a dropped wait socket."""

        while True:
            try:
                snapshot = await self._status_poller.snapshot(
                    controller_url=controller_url,
                    token=token,
                    task_id=accepted.task_id,
                    deadline=deadline,
                )
            except (ConnectionClosed, TimeoutError, OSError) as exc:
                remaining = _remaining_seconds(deadline)
                if remaining is not None and remaining <= 0:
                    return None
                sleep_seconds = self._disconnect_status_poll_seconds
                if remaining is not None:
                    sleep_seconds = min(sleep_seconds, remaining)
                logger.info(
                    "controller status polling failed after wait socket disconnect; retrying",
                    exc_info=exc,
                )
                await asyncio.sleep(max(0.0, sleep_seconds))
                continue
            if snapshot is None:
                return None
            if snapshot.state in {TaskState.FINISHED, TaskState.FAILED, TaskState.INTERRUPTED}:
                return _finished_from_snapshot(snapshot, accepted=accepted)
            remaining = _remaining_seconds(deadline)
            if remaining is not None and remaining <= 0:
                return None
            sleep_seconds = self._disconnect_status_poll_seconds
            if remaining is not None:
                sleep_seconds = min(sleep_seconds, remaining)
            logger.info(
                "controller wait socket disconnected while task is %s; polling status until terminal",
                snapshot.state.value,
            )
            await asyncio.sleep(max(0.0, sleep_seconds))

    async def _terminal_status_snapshot(
        self,
        *,
        controller_url: str,
        token: str,
        accepted: TaskAccepted,
        deadline: float | None,
    ) -> TaskStatusSnapshot | None:
        try:
            remaining = _remaining_seconds(deadline)
            status_poll_seconds = self._status_poller.request_timeout_seconds
            if remaining is not None:
                status_poll_seconds = min(status_poll_seconds, remaining)
            status_deadline = time.monotonic() + status_poll_seconds if status_poll_seconds > 0 else None
            snapshot = await self._status_poller.snapshot(
                controller_url=controller_url,
                token=token,
                task_id=accepted.task_id,
                deadline=status_deadline,
            )
        except Exception:
            logger.info(
                "controller idle wait status poll failed; continuing to wait for task",
                extra={"task_id": accepted.task_id, "trace_id": accepted.trace_id},
                exc_info=True,
            )
            return None
        if snapshot is None:
            return None
        if snapshot.state in {TaskState.FINISHED, TaskState.FAILED, TaskState.INTERRUPTED}:
            return snapshot
        return None

    async def statuses(
        self,
        *,
        controller_url: str,
        token: str,
        task_ids: list[str],
    ) -> list[TaskStatusSnapshot]:
        """Fetch explicit task statuses from the controller.

        Args:
            controller_url: Controller websocket URL.
            token: Submitter read token.
            task_ids: Controller task ids to query.

        Returns:
            Snapshots for tasks that still exist in controller storage.
        """

        async with self._submitter_factory(controller_url) as submitter:
            return await submitter.task_statuses(task_ids, token=token)

    async def list_batch(
        self,
        *,
        controller_url: str,
        token: str,
        batch_id: str,
        limit: int = 0,
    ) -> list[TaskStatusSnapshot]:
        """Fetch controller tasks tagged with a batch id.

        Args:
            controller_url: Controller websocket URL.
            token: Submitter read token.
            batch_id: Batch metadata value to match.
            limit: Maximum number of controller rows to return; zero uses the
                controller default page size.

        Returns:
            Controller task snapshots associated with the batch.
        """

        async with self._submitter_factory(controller_url) as submitter:
            return await submitter.list_tasks(TaskStatusFilter(batch_id=batch_id, limit=limit), token=token)

    async def cancel(self, *, controller_url: str, token: str, task: TaskCancel) -> None:
        """Send a best-effort task cancel request.

        Args:
            controller_url: Controller websocket URL.
            token: Submitter token retained for future authenticated cancel paths.
            task: Task cancellation payload.
        """

        async with self._submitter_factory(controller_url) as submitter:
            _ = token
            await submitter.cancel_task(task)
