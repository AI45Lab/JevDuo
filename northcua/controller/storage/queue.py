"""Task queue facade over controller persistence."""

from __future__ import annotations

import asyncio
from collections.abc import Iterable

from northcua.controller.observability.event_log import ControllerEventLog
from northcua.controller.storage.async_store import ControllerStorePriority, controller_store_call
from northcua.controller.storage.persistence import SQLiteControllerStore
from northcua.controller.storage.result_compaction import compact_task_finished_for_sqlite
from northcua_protocol.artifacts import ArtifactRef
from northcua_protocol.payloads import TaskFinished, TaskRequest
from northcua_protocol.state import SessionState, TaskState


class TaskQueue:
    """Persist task lifecycle state for the single-process controller."""

    def __init__(self, *, store: SQLiteControllerStore, events: ControllerEventLog) -> None:
        self.store = store
        self.events = events

    def accept(self, request: TaskRequest, *, session_id: str, submitter_id: str) -> None:
        """Persist an accepted task with a pending session.

        Args:
            request: Accepted controller task request.
            session_id: Session id created for the task.
            submitter_id: Submitter identity used for ownership checks.
        """

        self.store.create_task(
            request,
            session_id=session_id,
            submitter_id=submitter_id,
            status=TaskState.ACCEPTED,
        )
        self.events.write(
            "task_accepted",
            trace_id=request.trace_id,
            entity_type="task",
            entity_id=request.task_id,
            session_id=session_id,
            agent_kind=request.agent_kind,
            submitter_id=submitter_id,
        )

    async def accept_async(
        self,
        request: TaskRequest,
        *,
        session_id: str,
        submitter_id: str,
        input_artifacts: Iterable[ArtifactRef] = (),
    ) -> None:
        """Persist an accepted task without blocking the controller event loop.

        Args:
            request: Accepted controller task request.
            session_id: Session id created for the task.
            submitter_id: Submitter identity used for ownership checks.
            input_artifacts: Already-materialized task input artifacts to index.

        Raises:
            PermissionError: If the task id is already present.
        """

        def _persist(store: SQLiteControllerStore) -> None:
            if store.task_row(request.task_id) is not None:
                raise PermissionError("task_id already exists")
            store.create_task(
                request,
                session_id=session_id,
                submitter_id=submitter_id,
                status=TaskState.ACCEPTED,
            )
            for artifact in input_artifacts:
                store.record_artifact_ref(
                    artifact,
                    trace_id=request.trace_id,
                    task_id=request.task_id,
                    session_id=session_id,
                    source="task_input",
                )

        await controller_store_call(
            self.store.path,
            _persist,
            operation_name="task_accept",
            priority=ControllerStorePriority.CRITICAL,
        )
        self.events.write(
            "task_accepted",
            trace_id=request.trace_id,
            entity_type="task",
            entity_id=request.task_id,
            session_id=session_id,
            agent_kind=request.agent_kind,
            submitter_id=submitter_id,
        )

    def mark_queued(self, request: TaskRequest, *, session_id: str, reason: str) -> bool:
        """Keep a task accepted but unscheduled due to backpressure.

        Args:
            request: Task request that could not be scheduled yet.
            session_id: Session id associated with the task.
            reason: Queue/backpressure reason written to the event log.

        Returns:
            ``True`` when the task is still queued for retry. ``False`` when a
            terminal or cleanup transition already won.
        """

        changed = self.store.mark_task_queued_if_pending_session(task_id=request.task_id, session_id=session_id)
        if not changed:
            self.events.write(
                "task_queued_deduped",
                trace_id=request.trace_id,
                entity_type="task",
                entity_id=request.task_id,
                session_id=session_id,
                reason=reason,
            )
            return False
        self.events.write(
            "task_queued",
            trace_id=request.trace_id,
            entity_type="task",
            entity_id=request.task_id,
            session_id=session_id,
            reason=reason,
        )
        return True

    async def mark_queued_async(self, request: TaskRequest, *, session_id: str, reason: str) -> bool:
        """Mark a task queued without blocking the controller event loop.

        Args:
            request: Task request that could not be scheduled yet.
            session_id: Session id associated with the task.
            reason: Queue/backpressure reason written to the event log.

        Returns:
            ``True`` when the task is still queued for retry. ``False`` when a
            terminal or cleanup transition already won.
        """

        changed = await controller_store_call(
            self.store.path,
            lambda store: store.mark_task_queued_if_pending_session(task_id=request.task_id, session_id=session_id),
            operation_name="task_mark_queued",
            priority=ControllerStorePriority.CRITICAL,
        )
        if not changed:
            self.events.write(
                "task_queued_deduped",
                trace_id=request.trace_id,
                entity_type="task",
                entity_id=request.task_id,
                session_id=session_id,
                reason=reason,
            )
            return False
        self.events.write(
            "task_queued",
            trace_id=request.trace_id,
            entity_type="task",
            entity_id=request.task_id,
            session_id=session_id,
            reason=reason,
        )
        return True

    def mark_running(self, *, task_id: str, trace_id: str, session_id: str, device_id: str) -> None:
        """Mark a task as running after a lease is delivered.

        Args:
            task_id: Task that received a device lease.
            trace_id: Trace id used for controller event correlation.
            session_id: Session associated with the task.
            device_id: Device that accepted the lease.

        """

        self.store.update_task_status(task_id, TaskState.RUNNING)
        self.store.update_session_status(session_id, status=SessionState.RUNNING)
        self.events.write(
            "task_running",
            trace_id=trace_id,
            entity_type="task",
            entity_id=task_id,
            session_id=session_id,
            device_id=device_id,
        )

    async def mark_running_async(self, *, task_id: str, trace_id: str, session_id: str, device_id: str) -> bool:
        """Mark a delivered task running without blocking the event loop.

        Args:
            task_id: Task that received a device lease.
            trace_id: Trace id used for controller event correlation.
            session_id: Session associated with the task.
            device_id: Device that accepted the lease.

        Returns:
            ``True`` when the task/session transitioned to running. ``False``
            when terminal cleanup or rollback already won.
        """

        changed = await controller_store_call(
            self.store.path,
            lambda store: store.mark_task_running_if_leased(task_id=task_id, session_id=session_id),
            operation_name="task_mark_running",
            priority=ControllerStorePriority.CRITICAL,
        )
        if not changed:
            self.events.write(
                "task_running_deduped",
                trace_id=trace_id,
                entity_type="task",
                entity_id=task_id,
                session_id=session_id,
                device_id=device_id,
            )
            return False
        self.events.write(
            "task_running",
            trace_id=trace_id,
            entity_type="task",
            entity_id=task_id,
            session_id=session_id,
            device_id=device_id,
        )
        return True

    def finish(self, payload: TaskFinished, *, result_json: str | None = None) -> bool:
        """Persist a terminal task result.

        Args:
            payload: Terminal task payload to store and project as an event.
            result_json: Optional pre-compacted terminal result JSON. Passing
                this lets async callers move CPU-heavy compaction off the
                controller event loop while preserving the same DB transition.

        Returns:
            ``True`` when the task transitioned to a terminal state. ``False``
            means another terminal result already won the race.
        """

        if result_json is None:
            result_json = compact_task_finished_for_sqlite(payload).model_dump_json()
        changed = self.store.finish_task_if_nonterminal(
            payload.task_id,
            payload.state,
            result_json=result_json,
        )
        if not changed:
            return False
        self.events.write(
            "task_finished",
            trace_id=payload.trace_id,
            entity_type="task",
            entity_id=payload.task_id,
            session_id=payload.session_id,
            device_id=payload.device_id,
            status=payload.state.value,
        )
        return True

    async def finish_async(self, payload: TaskFinished, *, result_json: str | None = None) -> bool:
        """Persist a terminal task result without blocking the event loop.

        Args:
            payload: Terminal task payload to store and project as an event.
            result_json: Optional pre-compacted terminal result JSON.

        Returns:
            ``True`` when the task transitioned to a terminal state. ``False``
            means another terminal result already won the race.
        """

        if result_json is None:
            result_json = await asyncio.to_thread(lambda: compact_task_finished_for_sqlite(payload).model_dump_json())
        changed = await controller_store_call(
            self.store.path,
            lambda store: store.finish_task_if_nonterminal(
                payload.task_id,
                payload.state,
                result_json=result_json,
            ),
            operation_name="task_finish",
            priority=ControllerStorePriority.CRITICAL,
        )
        if not changed:
            return False
        self.events.write(
            "task_finished",
            trace_id=payload.trace_id,
            entity_type="task",
            entity_id=payload.task_id,
            session_id=payload.session_id,
            device_id=payload.device_id,
            status=payload.state.value,
        )
        return True

    def terminalize_pending_orphan(self, payload: TaskFinished, *, trace_id: str, reason: str) -> bool:
        """Interrupt one accepted+pending task that can no longer reach its submitter.

        Args:
            payload: Terminal interrupted task result to persist.
            trace_id: Trace id for the orphan event.
            reason: Human-readable orphan reason.

        Returns:
            ``True`` when this call performed the terminal transition. Replays
            return ``False`` so callers can avoid event storms.
        """

        result_json = compact_task_finished_for_sqlite(payload).model_dump_json()
        changed = self.store.interrupt_pending_task_session(
            task_id=payload.task_id,
            session_id=payload.session_id,
            result_json=result_json,
        )
        if not changed:
            return False
        self.events.write(
            "task_orphaned_missing_submitter",
            trace_id=trace_id,
            entity_type="task",
            entity_id=payload.task_id,
            session_id=payload.session_id,
            status=payload.state.value,
            reason=reason,
        )
        return True

    async def terminalize_pending_orphan_async(self, payload: TaskFinished, *, trace_id: str, reason: str) -> bool:
        """Interrupt one accepted+pending orphan without blocking the event loop.

        Args:
            payload: Terminal interrupted task result to persist.
            trace_id: Trace id for the orphan event.
            reason: Human-readable orphan reason.

        Returns:
            ``True`` when this call performed the terminal transition. Replays
            return ``False`` so callers can avoid event storms.
        """

        result_json = await asyncio.to_thread(lambda: compact_task_finished_for_sqlite(payload).model_dump_json())
        changed = await controller_store_call(
            self.store.path,
            lambda store: store.interrupt_pending_task_session(
                task_id=payload.task_id,
                session_id=payload.session_id,
                result_json=result_json,
            ),
            operation_name="task_terminalize_orphan",
            priority=ControllerStorePriority.CRITICAL,
        )
        if not changed:
            return False
        self.events.write(
            "task_orphaned_missing_submitter",
            trace_id=trace_id,
            entity_type="task",
            entity_id=payload.task_id,
            session_id=payload.session_id,
            status=payload.state.value,
            reason=reason,
        )
        return True
