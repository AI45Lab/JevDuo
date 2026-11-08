"""Async helpers for batch state repositories."""

from __future__ import annotations

import asyncio
import os
import threading
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from typing import TypeVar

_R = TypeVar("_R")

_prepared_executor_lock = threading.Lock()
_prepared_executor_pid = os.getpid()
_prepared_executor: ThreadPoolExecutor | None = None


def _reset_prepared_executor_after_fork() -> None:
    """Discard inherited executor state in a forked child process."""

    global _prepared_executor_lock, _prepared_executor_pid, _prepared_executor
    _prepared_executor_lock = threading.Lock()
    _prepared_executor_pid = os.getpid()
    _prepared_executor = None


if hasattr(os, "register_at_fork"):
    os.register_at_fork(after_in_child=_reset_prepared_executor_after_fork)


async def repository_call(operation: Callable[[], _R]) -> _R:
    """Run a synchronous repository operation away from the event loop.

    Args:
        operation: Zero-argument callable that performs a blocking repository
            read or write.

    Returns:
        Value returned by ``operation`` after it completes in a worker thread.
    """

    return await asyncio.to_thread(operation)


async def prepared_repository_call(operation: Callable[[], _R]) -> _R:
    """Run one prepared-sandbox SQLite operation on its dedicated lane."""

    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(_prepared_repository_executor_for_process(), operation)


def _prepared_repository_executor_for_process() -> ThreadPoolExecutor:
    """Return the lazy, process-shared single-thread prepared DB executor."""

    global _prepared_executor_pid, _prepared_executor
    current_pid = os.getpid()
    with _prepared_executor_lock:
        if current_pid != _prepared_executor_pid:
            _prepared_executor = None
            _prepared_executor_pid = current_pid
        if _prepared_executor is None:
            _prepared_executor = ThreadPoolExecutor(
                max_workers=1,
                thread_name_prefix="northcua-prepared-repository",
            )
        return _prepared_executor


def _shutdown_prepared_repository_executor_for_testing() -> None:
    """Release the prepared DB executor between isolation tests."""

    global _prepared_executor
    with _prepared_executor_lock:
        executor = _prepared_executor
        _prepared_executor = None
    if executor is not None:
        executor.shutdown(wait=True, cancel_futures=True)
