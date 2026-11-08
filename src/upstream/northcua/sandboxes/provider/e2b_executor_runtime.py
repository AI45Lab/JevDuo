"""Async ownership helpers for blocking E2B executor calls."""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Callable
from concurrent.futures import Future
from typing import Protocol, TypeVar, cast

from northcua.sandboxes.provider.e2b_executor import (
    E2BExecutorCall,
    E2BExecutorLane,
    executor_telemetry,
)
from northcua.sandboxes.provider.e2b_runtime_support import executor_telemetry_deadline

logger = logging.getLogger(__name__)
_BlockingResultT = TypeVar("_BlockingResultT")


class _ProgressReporter(Protocol):
    @staticmethod
    def _progress(message: str, **fields: object) -> None: ...


class E2BExecutorRuntimeMixin:
    """Route blocking SDK work and retain ownership after cancellation."""

    async def _run_executor_call(
        self,
        *,
        lane: E2BExecutorLane,
        operation: str,
        call: Callable[[], _BlockingResultT],
        late_result_handler: Callable[[_BlockingResultT], None] | None = None,
        late_error_handler: Callable[[BaseException], None] | None = None,
    ) -> _BlockingResultT:
        """Run one SDK call on its bounded lane and report queue timing."""

        progress = cast(_ProgressReporter, self)._progress
        executor_call = E2BExecutorCall(lane, call)
        call_task = asyncio.create_task(executor_call.run())
        started_at = asyncio.get_running_loop().time()
        telemetry_index = 0
        try:
            while True:
                deadline = executor_telemetry_deadline(telemetry_index)
                elapsed = asyncio.get_running_loop().time() - started_at
                done, _ = await asyncio.wait({call_task}, timeout=max(deadline - elapsed, 0.0))
                if done:
                    result = await call_task
                    break
                progress(
                    "e2b sdk call: waiting",
                    operation=operation,
                    telemetry_deadline_seconds=deadline,
                    **executor_call.timing().progress_fields(),
                    **executor_telemetry(lane),
                )
                telemetry_index += 1
        except asyncio.CancelledError:
            call_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await call_task
            if late_result_handler is None:
                self._track_late_executor_completion(executor_call, operation=operation)
            else:
                executor_call.add_concurrent_done_callback(
                    lambda future: self._handle_late_executor_result(
                        future,
                        operation=operation,
                        handler=late_result_handler,
                        error_handler=late_error_handler,
                    )
                )
            progress(
                "e2b sdk call: cancelled",
                operation=operation,
                **executor_call.timing().progress_fields(),
                **executor_telemetry(lane),
            )
            raise
        except Exception as exc:
            progress(
                "e2b sdk call: failed",
                operation=operation,
                error_type=type(exc).__name__,
                **executor_call.timing().progress_fields(),
                **executor_telemetry(lane),
            )
            raise
        progress(
            "e2b sdk call: completed",
            operation=operation,
            **executor_call.timing().progress_fields(),
            **executor_telemetry(lane),
        )
        return result

    def _handle_late_executor_result(
        self,
        future: Future[_BlockingResultT],
        *,
        operation: str,
        handler: Callable[[_BlockingResultT], None],
        error_handler: Callable[[BaseException], None] | None = None,
    ) -> None:
        """Transfer a late SDK result to loop-independent cleanup ownership."""

        try:
            result = future.result()
        except BaseException as exc:
            logger.debug(
                "E2B late executor result completed with error",
                extra={"operation": operation, "error_type": type(exc).__name__},
            )
            if error_handler is not None:
                try:
                    error_handler(exc)
                except Exception:
                    logger.exception(
                        "E2B late executor error handler failed",
                        extra={"operation": operation},
                    )
            return
        try:
            handler(result)
        except Exception:
            logger.exception(
                "E2B late executor result handler failed",
                extra={"operation": operation},
            )

    def _track_late_executor_completion(
        self,
        executor_call: E2BExecutorCall[_BlockingResultT],
        *,
        operation: str,
    ) -> None:
        """Own a synchronous SDK call that outlived its cancelled waiter."""

        tasks = getattr(self, "_late_executor_completion_tasks", None)
        if tasks is None:
            tasks = set()
            setattr(self, "_late_executor_completion_tasks", tasks)
        task = asyncio.create_task(
            self._consume_late_executor_completion(executor_call, operation=operation),
            name="northcua-e2b-late-sdk-completion",
        )
        tasks.add(task)
        task.add_done_callback(tasks.discard)

    async def _consume_late_executor_completion(
        self,
        executor_call: E2BExecutorCall[_BlockingResultT],
        *,
        operation: str,
    ) -> None:
        progress = cast(_ProgressReporter, self)._progress
        try:
            await executor_call.wait_for_completion()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            progress(
                "e2b sdk call: late completion failed",
                operation=operation,
                error_type=type(exc).__name__,
                **executor_call.timing().progress_fields(),
            )
        else:
            progress(
                "e2b sdk call: late completion consumed",
                operation=operation,
                **executor_call.timing().progress_fields(),
            )
