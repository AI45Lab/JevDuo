"""Controller-owned task-runner registry and executor service."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from contextlib import contextmanager
from typing import Iterator

from northcua.controller.execution.executor_protocol import ControllerExecutionContext, ControllerTaskExecutor
from northcua.controller.execution.tasks import TaskService
from northcua.core.terminal import AgentTerminalDecision, AgentTerminalReason
from northcua_protocol.payloads import TaskFinished
from northcua_protocol.state import TaskState

logger = logging.getLogger(__name__)

TaskRunner = Callable[[ControllerExecutionContext], Awaitable[TaskFinished]]


class RunnerRegistry:
    """Controller runner registry keyed by agent kind."""

    def __init__(self, runners: dict[str, TaskRunner] | None = None) -> None:
        self._runners = dict(runners or {})
        self._registration_sources = {agent_kind: "constructor" for agent_kind in self._runners}
        self._app_config_paths: dict[str, str] = {}
        self._active_registration_source = "direct"

    def register(self, agent_kind: str, runner: TaskRunner, *, app_config_path: str | None = None) -> None:
        """Register one task runner for an agent kind.

        Args:
            agent_kind: Protocol agent kind handled by the runner.
            runner: Async callable that executes one leased controller task.
            app_config_path: Optional immutable config graph used to construct
                this runner. Process executors reuse this exact path.
        """

        self._runners[agent_kind] = runner
        self._registration_sources[agent_kind] = self._active_registration_source
        if app_config_path is None:
            self._app_config_paths.pop(agent_kind, None)
        else:
            self._app_config_paths[agent_kind] = app_config_path

    def resolve(self, agent_kind: str) -> TaskRunner | None:
        """Resolve one registered task runner.

        Args:
            agent_kind: Protocol agent kind to resolve.

        Returns:
            Registered runner callable, or `None` when unsupported.
        """

        return self._runners.get(agent_kind)

    def registered_agent_kinds(self) -> tuple[str, ...]:
        """Return registered agent kinds in stable order.

        Returns:
            Sorted tuple of agent kinds available to the controller executor.
        """

        return tuple(sorted(self._runners))

    def registration_source(self, agent_kind: str) -> str | None:
        """Return the plugin identity that last registered an agent kind."""

        return self._registration_sources.get(agent_kind)

    def registered_app_config_path(self, agent_kind: str) -> str | None:
        """Return the immutable config graph registered for an agent kind."""

        return self._app_config_paths.get(agent_kind)

    @contextmanager
    def registration_scope(self, source: str) -> Iterator[None]:
        """Attribute registrations performed by one discovered plugin."""

        previous = self._active_registration_source
        self._active_registration_source = source
        try:
            yield
        finally:
            self._active_registration_source = previous


class TaskExecutorService(ControllerTaskExecutor):
    """Spawn background controller-owned agent executions for leased tasks."""

    def __init__(self, *, task_service: TaskService, runner_registry: RunnerRegistry) -> None:
        self.task_service = task_service
        self.runner_registry = runner_registry
        self._tasks_by_task_id: dict[str, asyncio.Task[None]] = {}
        self._expected_cancellations: set[str] = set()
        self._completed_failures: list[BaseException] = []

    async def schedule(self, context: ControllerExecutionContext) -> None:
        """Schedule one leased task on the matching controller-owned runner.

        Args:
            context: Leased task execution context from the scheduler.
        """

        existing = self._tasks_by_task_id.get(context.request.task_id)
        if existing is not None and not existing.done():
            logger.info(
                "controller task runner already scheduled",
                extra={
                    "task_id": context.request.task_id,
                    "session_id": context.session_id,
                    "agent_kind": context.request.agent_kind,
                },
            )
            return
        task = asyncio.create_task(self._run_context(context))
        self._tasks_by_task_id[context.request.task_id] = task
        task.add_done_callback(lambda finished, task_id=context.request.task_id: self._track_done(task_id, finished))

    async def drain(self) -> None:
        """Wait for all controller-owned background executions to finish.

        Raises:
            BaseException: Re-raises completed runner failures after logging.
        """

        pending = [task for task in self._tasks_by_task_id.values() if not task.done()]
        if pending:
            results = await asyncio.gather(*pending, return_exceptions=True)
            self._completed_failures.extend(result for result in results if isinstance(result, Exception))
        failures = list(self._completed_failures)
        self._completed_failures.clear()
        if failures:
            for failure in failures:
                logger.error(
                    "controller task executor background failure",
                    extra={"error": str(failure), "error_type": type(failure).__name__},
                    exc_info=(type(failure), failure, failure.__traceback__),
                )
            raise failures[0]

    async def cancel_task(self, task_id: str, reason: str) -> None:
        """Cancel one in-process runner task when requested by submitter.

        Args:
            task_id: Controller task id to cancel.
            reason: Human-readable cancellation reason.
        """

        del reason
        self._expected_cancellations.add(task_id)
        task = self._tasks_by_task_id.get(task_id)
        if task is not None and not task.done():
            task.cancel()

    def _track_done(self, task_id: str, task: asyncio.Task[None]) -> None:
        self._tasks_by_task_id.pop(task_id, None)
        if task.cancelled():
            if task_id in self._expected_cancellations:
                self._expected_cancellations.discard(task_id)
                return
            self._completed_failures.append(asyncio.CancelledError(f"controller task {task_id} was cancelled"))
            return
        failure = task.exception()
        if failure is not None:
            self._completed_failures.append(failure)
        self._expected_cancellations.discard(task_id)

    async def _run_context(self, context: ControllerExecutionContext) -> None:
        runner = self.runner_registry.resolve(context.request.agent_kind)
        if runner is None:
            logger.warning(
                "controller task runner missing for agent kind",
                extra={
                    "task_id": context.request.task_id,
                    "session_id": context.session_id,
                    "agent_kind": context.request.agent_kind,
                },
            )
            await self.task_service.complete_task(
                payload=TaskFinished(
                    task_id=context.request.task_id,
                    trace_id=context.request.trace_id,
                    session_id=context.session_id,
                    device_id=context.lease.device_id,
                    lease_id=context.lease.lease_id,
                    state=TaskState.FAILED,
                    error=f"unsupported controller executor agent_kind: {context.request.agent_kind}",
                    terminal_decision=AgentTerminalDecision.error(
                        reason=AgentTerminalReason.RUNTIME_ERROR,
                        final_answer=f"unsupported controller executor agent_kind: {context.request.agent_kind}",
                        summary="The controller has no runner for this agent kind.",
                        retryable=False,
                    ).to_json(),
                )
            )
            return
        logger.info(
            "controller task runner started",
            extra={
                "task_id": context.request.task_id,
                "session_id": context.session_id,
                "agent_kind": context.request.agent_kind,
                "device_id": context.lease.device_id,
            },
        )
        try:
            payload = await runner(context)
        except Exception as exc:  # pragma: no cover - defensive controller boundary
            logger.exception("controller task executor failed", extra={"task_id": context.request.task_id})
            payload = TaskFinished(
                task_id=context.request.task_id,
                trace_id=context.request.trace_id,
                session_id=context.session_id,
                device_id=context.lease.device_id,
                lease_id=context.lease.lease_id,
                state=TaskState.FAILED,
                error=str(exc),
                terminal_decision=AgentTerminalDecision.error(
                    reason=AgentTerminalReason.RUNTIME_ERROR,
                    final_answer=str(exc),
                    summary="The controller runner failed before returning a terminal payload.",
                    retryable=True,
                ).to_json(),
            )
        else:
            logger.info(
                "controller task runner finished",
                extra={
                    "task_id": context.request.task_id,
                    "session_id": context.session_id,
                    "agent_kind": context.request.agent_kind,
                    "state": payload.state.value,
                },
            )
        logger.info(
            "controller task completion started",
            extra={
                "task_id": context.request.task_id,
                "session_id": context.session_id,
                "agent_kind": context.request.agent_kind,
                "state": payload.state.value,
                "artifact_count": len(payload.artifacts),
            },
        )
        await self.task_service.complete_task(payload=payload)
        logger.info(
            "controller task completion finished",
            extra={
                "task_id": context.request.task_id,
                "session_id": context.session_id,
                "agent_kind": context.request.agent_kind,
                "state": payload.state.value,
            },
        )
