"""CLI entrypoint and grouped command dispatcher for NorthCUA."""

from __future__ import annotations

import asyncio
import contextlib
import os
import select
import signal
import threading
from collections.abc import Awaitable
from typing import Any

from .parser import build_parser, resolve_cli_adapter
from .provider_commands import run_provider_probe_command

_SIGTERM_COMMAND_GRACE_SECONDS = 30.0
_SIGTERM_REAPER_GRACE_SECONDS = 30.0


def _sigterm_watchdog(
    watchdog_fd: int,
    disarmed: threading.Event,
    timeout_seconds: float,
) -> None:
    """Enforce the shutdown deadline independently of the asyncio event loop."""

    select.select([watchdog_fd], [], [])
    with contextlib.suppress(BlockingIOError, OSError):
        os.read(watchdog_fd, 1)
    if disarmed.is_set():
        return
    if not disarmed.wait(timeout=max(float(timeout_seconds), 0.0)):
        os._exit(128 + signal.SIGTERM)


async def _run_async_command(operation: Awaitable[int], *, trigger_fd: int) -> int:
    """Run one async command under cooperative, repeatable SIGTERM cancellation."""

    loop = asyncio.get_running_loop()
    stop_requested = asyncio.Event()

    def consume_stop_signal() -> None:
        with contextlib.suppress(BlockingIOError, OSError):
            os.read(trigger_fd, 4096)
        stop_requested.set()

    loop.add_reader(trigger_fd, consume_stop_signal)
    command_task = asyncio.ensure_future(operation)
    stop_task = asyncio.create_task(stop_requested.wait(), name="northcua-cli-sigterm")
    try:
        done, _pending = await asyncio.wait(
            {command_task, stop_task},
            return_when=asyncio.FIRST_COMPLETED,
        )
        if command_task in done:
            return await command_task
        command_task.cancel()
        await asyncio.wait({command_task}, timeout=_SIGTERM_COMMAND_GRACE_SECONDS)
        from northcua.sandboxes.provider.e2b_executor import drain_e2b_reaper

        # Keep accepting cleanup transferred by SDK calls that outlive their
        # cancelled waiter. The atexit fence performs the final stop; the
        # watchdog bounds the entire interpreter shutdown path.
        drain_e2b_reaper(timeout_seconds=_SIGTERM_REAPER_GRACE_SECONDS)
        if not command_task.done():
            os._exit(128 + signal.SIGTERM)
        with contextlib.suppress(BaseException):
            await command_task
        return 128 + signal.SIGTERM
    finally:
        loop.remove_reader(trigger_fd)
        stop_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await stop_task


def _run_async_command_with_sigterm(operation: Awaitable[int]) -> int:
    """Install a reentrant-safe SIGTERM bridge before starting async work."""

    if os.name == "nt":
        return asyncio.run(operation)

    trigger_read_fd, trigger_write_fd = os.pipe()
    watchdog_read_fd, watchdog_write_fd = os.pipe()
    os.set_blocking(trigger_read_fd, False)
    os.set_blocking(trigger_write_fd, False)
    os.set_blocking(watchdog_read_fd, False)
    os.set_blocking(watchdog_write_fd, False)
    disarmed = threading.Event()
    termination_requested = False
    previous_sigterm_handler: Any = signal.getsignal(signal.SIGTERM)

    def request_stop(_signum: int, _frame: object) -> None:
        nonlocal termination_requested
        termination_requested = True
        for write_fd in (trigger_write_fd, watchdog_write_fd):
            try:
                os.write(write_fd, b"\0")
            except (BlockingIOError, OSError):
                pass

    signal.signal(signal.SIGTERM, request_stop)
    watchdog_thread = threading.Thread(
        target=_sigterm_watchdog,
        args=(
            watchdog_read_fd,
            disarmed,
            _SIGTERM_COMMAND_GRACE_SECONDS + _SIGTERM_REAPER_GRACE_SECONDS,
        ),
        name="northcua-cli-sigterm-watchdog",
        daemon=True,
    )
    watchdog_thread.start()
    try:
        status = asyncio.run(_run_async_command(operation, trigger_fd=trigger_read_fd))
        if termination_requested:
            from northcua.sandboxes.provider.e2b_executor import (
                drain_e2b_reaper,
                shutdown_e2b_executors,
            )

            # SDK calls can complete after their asyncio waiter is cancelled.
            # Fence those callbacks before the final reaper stop so every
            # accepted cleanup remains inside the watchdog deadline.
            shutdown_e2b_executors()
            drain_e2b_reaper(timeout_seconds=_SIGTERM_REAPER_GRACE_SECONDS, stop=True)
        signal.signal(signal.SIGTERM, previous_sigterm_handler)
        try:
            pending_signal = bool(os.read(trigger_read_fd, 4096))
        except BlockingIOError:
            pending_signal = False
        return 128 + signal.SIGTERM if pending_signal else status
    finally:
        signal.signal(signal.SIGTERM, previous_sigterm_handler)
        if not termination_requested:
            disarmed.set()
            with contextlib.suppress(BlockingIOError, OSError):
                os.write(watchdog_write_fd, b"\0")
            watchdog_thread.join(timeout=1.0)
        file_descriptors = [trigger_read_fd, trigger_write_fd]
        if not termination_requested:
            file_descriptors.extend((watchdog_read_fd, watchdog_write_fd))
        for file_descriptor in file_descriptors:
            os.close(file_descriptor)


def main() -> None:
    """Dispatch the chosen CLI subcommand.

    Raises:
        SystemExit: Always exits with the selected subcommand's process-style status.
    """

    parser = build_parser()
    args = parser.parse_args()
    if args.command_group == "demo":
        from .demo_commands import run_demo_command

        raise SystemExit(run_demo_command(args))
    if args.command_group == "config":
        from .config_commands import run_config_command

        raise SystemExit(run_config_command(args))
    if args.command_group == "batch":
        from .batch_commands import run_batch_command

        raise SystemExit(_run_async_command_with_sigterm(run_batch_command(args)))
    if args.command_group == "sandbox":
        from .batch_commands import run_sandbox_command

        raise SystemExit(run_sandbox_command(args))
    if args.command_group == "controller":
        from .controller_commands import run_controller_command

        raise SystemExit(_run_async_command_with_sigterm(run_controller_command(args)))
    if args.command_group == "case":
        from .case_commands import run_case_command

        raise SystemExit(_run_async_command_with_sigterm(run_case_command(args)))
    if args.command_group == "experiment":
        from .experiment_commands import run_experiment_command

        raise SystemExit(_run_async_command_with_sigterm(run_experiment_command(args)))
    if args.command_group == "provider":
        raise SystemExit(run_provider_probe_command(args))
    adapter = resolve_cli_adapter(args.agent_kind)
    if adapter is None:
        parser.error(f"Unsupported agent namespace: {args.agent_kind}")
    raise SystemExit(_run_async_command_with_sigterm(adapter.dispatch(args)))
