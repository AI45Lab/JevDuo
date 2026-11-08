"""Process-backed controller task executor."""

from __future__ import annotations

import asyncio
import logging
import uuid

from northcua.controller.execution.executor_protocol import ControllerExecutionContext, ControllerTaskExecutor
from northcua.controller.execution.processes.contracts import WorkerJobFailureKind, WorkerJobStatus
from northcua.controller.execution.processes.runners import ProcessWorkerRunnerRegistry
from northcua.controller.execution.processes.supervisor import ProcessWorkerSupervisor
from northcua.controller.execution.sdk import ControllerSDK
from northcua.controller.execution.tasks import TaskService
from northcua.controller.storage.async_store import ControllerStorePriority, controller_store_call
from northcua.controller.storage.persistence import SQLiteControllerStore
from northcua.controller.storage.worker_heartbeat_batcher import WorkerHeartbeatBatcher
from northcua.core.terminal import AgentTerminalDecision, AgentTerminalReason
from northcua_protocol.payloads import TaskFinished
from northcua_protocol.state import TaskState

logger = logging.getLogger(__name__)


class ProcessTaskExecutorService(ControllerTaskExecutor):
    """Schedule controller-owned agent executions in isolated worker processes."""

    def __init__(
        self,
        *,
        task_service: TaskService,
        sdk: ControllerSDK,
        store: SQLiteControllerStore,
        app_config_path: str,
        artifact_root_dir_override: str | None,
        command_timeout_seconds: float,
        runner_workers: int,
        worker_queue_max: int,
        worker_start_timeout_seconds: float,
        worker_heartbeat_timeout_seconds: float,
        supported_agent_kinds: set[str] | None = None,
        worker_module: str = "northcua.controller.execution.processes.worker_main",
        command_timeout_floor_seconds: float | None = None,
    ) -> None:
        self.task_service = task_service
        self.sdk = sdk
        self.store = store
        self.app_config_path = app_config_path
        self.artifact_root_dir_override = artifact_root_dir_override
        self.command_timeout_seconds = command_timeout_seconds
        self.command_timeout_floor_seconds = (
            None if command_timeout_floor_seconds is None else max(0.0, float(command_timeout_floor_seconds))
        )
        self.worker_start_timeout_seconds = worker_start_timeout_seconds
        self.worker_heartbeat_timeout_seconds = worker_heartbeat_timeout_seconds
        self.supported_agent_kinds = (
            set(ProcessWorkerRunnerRegistry.default().supported_agent_kinds())
            if supported_agent_kinds is None
            else set(supported_agent_kinds)
        )
        self.worker_module = worker_module
        self._worker_slots = asyncio.Semaphore(max(1, runner_workers))
        self._worker_queue_max = max(0, worker_queue_max)
        self._tasks_by_task_id: dict[str, asyncio.Task[None]] = {}
        self._supervisors_by_task_id: dict[str, ProcessWorkerSupervisor] = {}
        self._job_ids_by_task_id: dict[str, str] = {}
        self._cancel_reasons_by_task_id: dict[str, str] = {}
        self._completed_failures: list[BaseException] = []
        self._heartbeat_batcher = WorkerHeartbeatBatcher(self.store.path)
        self._recovery_task: asyncio.Task[int] | None = None
        self._recovery_reported = False

    async def schedule(self, context: ControllerExecutionContext) -> None:
        """Queue one task for process execution.

        Args:
            context: Controller execution context containing task request,
                lease, and artifact roots for the worker.
        """

        await self._ensure_recovered()
        if context.request.agent_kind not in self.supported_agent_kinds:
            await self.task_service.complete_task(
                payload=self._failed_payload(
                    context,
                    f"process executor does not support agent_kind: {context.request.agent_kind}",
                    retryable=False,
                    failure_category="preflight",
                )
            )
            return
        if context.request.task_id in self._tasks_by_task_id:
            logger.info("process executor task already scheduled", extra={"task_id": context.request.task_id})
            return
        if self._worker_queue_max and len(self._tasks_by_task_id) >= self._worker_queue_max:
            await self.task_service.complete_task(
                payload=self._failed_payload(
                    context,
                    "process executor worker queue is full",
                    retryable=True,
                    failure_kind=WorkerJobFailureKind.BACKPRESSURE,
                    failure_category="controller_submit",
                )
            )
            return
        job_id = uuid.uuid4().hex
        attempt_id = _attempt_id(context)
        created = await controller_store_call(
            self.store.path,
            lambda store: store.try_create_worker_job(
                job_id=job_id,
                task_id=context.request.task_id,
                attempt_id=attempt_id,
                session_id=context.session_id,
                lease_id=context.lease.lease_id,
                device_id=context.lease.device_id,
                agent_kind=context.request.agent_kind,
            ),
            operation_name="worker_job_create",
            priority=ControllerStorePriority.CRITICAL,
        )
        if not created:
            logger.info(
                "process executor task attempt already claimed",
                extra={"task_id": context.request.task_id, "attempt_id": attempt_id},
            )
            return
        task = asyncio.create_task(self._run_context(context, job_id=job_id, attempt_id=attempt_id))
        self._tasks_by_task_id[context.request.task_id] = task
        self._job_ids_by_task_id[context.request.task_id] = job_id
        task.add_done_callback(lambda finished, task_id=context.request.task_id: self._track_done(task_id, finished))

    async def drain(self) -> None:
        """Wait for scheduled process workers.

        Raises:
            BaseException: Re-raises the first worker scheduling failure
                collected from completed background tasks.
        """

        await self._ensure_recovered()
        pending = [task for task in self._tasks_by_task_id.values() if not task.done()]
        if pending:
            results = await asyncio.gather(*pending, return_exceptions=True)
            self._completed_failures.extend(result for result in results if isinstance(result, Exception))
        await self._heartbeat_batcher.aclose()
        failures = list(self._completed_failures)
        self._completed_failures.clear()
        if failures:
            raise failures[0]

    async def _ensure_recovered(self) -> None:
        """Run startup recovery once without blocking the controller event loop."""

        recovery_task = self._recovery_task
        if recovery_task is None:
            recovery_task = asyncio.create_task(
                controller_store_call(
                    self.store.path,
                    lambda store: store.fail_non_terminal_worker_jobs_and_tasks(
                        failure_kind=WorkerJobFailureKind.WORKER_CRASH,
                        error="controller process executor restarted before worker terminal payload",
                    ),
                    operation_name="worker_job_startup_recovery",
                    priority=ControllerStorePriority.CRITICAL,
                ),
                name="northcua-process-executor-startup-recovery",
            )
            self._recovery_task = recovery_task
        recovered = await asyncio.shield(recovery_task)
        if recovered and not self._recovery_reported:
            self._recovery_reported = True
            logger.warning(
                "process executor marked stale worker jobs retryable",
                extra={"stale_worker_jobs": recovered},
            )

    async def cancel_task(self, task_id: str, reason: str) -> None:
        """Cancel one scheduled process worker when its controller task stops.

        Args:
            task_id: Controller task id to cancel.
            reason: Cancellation reason persisted on the worker job.
        """

        self._cancel_reasons_by_task_id[task_id] = reason
        supervisor = self._supervisors_by_task_id.get(task_id)
        if supervisor is not None:
            await supervisor.cancel(reason=reason)
            return
        job_id = self._job_ids_by_task_id.get(task_id)
        if job_id is not None:
            await controller_store_call(
                self.store.path,
                lambda store: store.mark_worker_job_status(
                    job_id=job_id,
                    status=WorkerJobStatus.CANCELED,
                    failure_kind=WorkerJobFailureKind.CANCELED,
                    error=reason,
                ),
                operation_name="worker_job_cancel",
                priority=ControllerStorePriority.CRITICAL,
            )
        task = self._tasks_by_task_id.get(task_id)
        if task is not None and not task.done():
            task.cancel()

    def _track_done(self, task_id: str, task: asyncio.Task[None]) -> None:
        self._tasks_by_task_id.pop(task_id, None)
        self._job_ids_by_task_id.pop(task_id, None)
        if task.cancelled():
            if task_id in self._cancel_reasons_by_task_id:
                self._cancel_reasons_by_task_id.pop(task_id, None)
                return
            self._completed_failures.append(asyncio.CancelledError(f"controller process task {task_id} was cancelled"))
            return
        failure = task.exception()
        if failure is not None:
            self._completed_failures.append(failure)
        self._supervisors_by_task_id.pop(task_id, None)
        self._cancel_reasons_by_task_id.pop(task_id, None)

    async def _run_context(self, context: ControllerExecutionContext, *, job_id: str, attempt_id: str) -> None:
        async with self._worker_slots:
            cancel_reason = self._cancel_reasons_by_task_id.get(context.request.task_id)
            if cancel_reason is not None:
                await controller_store_call(
                    self.store.path,
                    lambda store: store.mark_worker_job_status(
                        job_id=job_id,
                        status=WorkerJobStatus.CANCELED,
                        failure_kind=WorkerJobFailureKind.CANCELED,
                        error=cancel_reason,
                    ),
                    operation_name="worker_job_cancel",
                    priority=ControllerStorePriority.CRITICAL,
                )
                return
            supervisor = ProcessWorkerSupervisor(
                context=context,
                sdk=self.sdk,
                store=self.store,
                job_id=job_id,
                attempt_id=attempt_id,
                app_config_path=self.app_config_path,
                artifact_root_dir_override=self.artifact_root_dir_override,
                command_timeout_seconds=self.command_timeout_seconds,
                command_timeout_floor_seconds=self.command_timeout_floor_seconds,
                start_timeout_seconds=self.worker_start_timeout_seconds,
                heartbeat_timeout_seconds=self.worker_heartbeat_timeout_seconds,
                worker_module=self.worker_module,
                heartbeat_batcher=self._heartbeat_batcher,
            )
            self._supervisors_by_task_id[context.request.task_id] = supervisor
            try:
                payload = await supervisor.run()
                await self.task_service.complete_task(payload=payload)
                await controller_store_call(
                    self.store.path,
                    lambda store: store.finalize_worker_job_from_task_result(job_id=job_id, payload=payload),
                    operation_name="worker_job_finalize",
                    priority=ControllerStorePriority.CRITICAL,
                )
            finally:
                self._supervisors_by_task_id.pop(context.request.task_id, None)

    def _failed_payload(
        self,
        context: ControllerExecutionContext,
        message: str,
        *,
        retryable: bool,
        failure_kind: WorkerJobFailureKind = WorkerJobFailureKind.TASK_FAILED,
        failure_category: str = "controller_submit",
    ) -> TaskFinished:
        return TaskFinished(
            task_id=context.request.task_id,
            trace_id=context.request.trace_id,
            session_id=context.session_id,
            device_id=context.lease.device_id,
            lease_id=context.lease.lease_id,
            state=TaskState.FAILED,
            error=message,
            terminal_decision=AgentTerminalDecision.error(
                reason=AgentTerminalReason.RUNTIME_ERROR,
                final_answer=message,
                summary="The controller process executor rejected the task.",
                retryable=retryable,
                metadata={"worker_failure_kind": failure_kind.value, "failure_category": failure_category},
            ).to_json(),
        )


def _attempt_id(context: ControllerExecutionContext) -> str:
    attempt = context.request.metadata.get("batch_attempt")
    if attempt is None:
        attempt = context.request.metadata.get("attempt")
    return f"{context.request.task_id}:{attempt or 1}"
