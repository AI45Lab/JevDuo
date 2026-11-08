"""Controller CLI command execution and operator projections."""

from __future__ import annotations

import argparse
import asyncio
import logging
import signal
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, cast

from .controller_command_views import (
    print_controller_artifacts,
    print_controller_events,
    print_controller_health,
    print_controller_rows,
    print_controller_task_detail,
    print_controller_telemetry,
)
from .parallelism_args import apply_controller_parallelism_defaults
from .shared_args import resolve_app_config_path

if TYPE_CHECKING:
    from northcua.controller.api.server import ControllerServer
    from northcua.controller.config import ControllerConfig
    from northcua.controller.execution import ControllerRunnerRegistrationContext, RunnerRegistry, TaskExecutorService

logger = logging.getLogger(__name__)


async def run_controller_command(args: argparse.Namespace) -> int:
    """Dispatch one controller CLI command.

    The command handler intentionally lazy-imports controller internals so
    local-only CLI paths do not pull websocket/server dependencies at startup.

    Args:
        args: Parsed argparse namespace for the selected controller command.

    Returns:
        Process-style exit code, where zero indicates success.

    Raises:
        RuntimeError: If `serve` cannot find any installed task runner plugin.
        ValueError: If an unknown controller subcommand reaches the dispatcher.
    """

    from northcua.controller.api.server import ControllerServer
    from northcua.controller.config import ControllerConfig
    from northcua.controller.execution import (
        ControllerRunnerRegistrationContext,
        RunnerRegistry,
        TaskExecutorService,
        populate_runner_registry,
    )

    if getattr(args, "controller_command", "") == "serve":
        apply_controller_parallelism_defaults(args)

    config = ControllerConfig.from_sources(
        config_path=args.config or None,
        overrides={
            "host": getattr(args, "host", ""),
            "port": getattr(args, "port", 0) or None,
            "websocket_backlog": getattr(args, "websocket_backlog", 0) or None,
            "websocket_open_timeout_seconds": _positive_float_or_none(getattr(args, "websocket_open_timeout", -1.0)),
            "executor_mode": getattr(args, "executor_mode", "") or None,
            "runner_workers": getattr(args, "runner_workers", 0) or None,
            "worker_queue_max": getattr(args, "worker_queue_max", None),
            "worker_start_timeout_seconds": _positive_float_or_none(getattr(args, "worker_start_timeout_seconds", 0.0)),
            "worker_heartbeat_timeout_seconds": _positive_float_or_none(
                getattr(args, "worker_heartbeat_timeout_seconds", 0.0)
            ),
            "process_worker_module": getattr(args, "process_worker_module", "") or None,
            "db_path": getattr(args, "db_path", ""),
            "blob_dir": getattr(args, "blob_dir", ""),
            "security": {"insecure_dev_no_auth": True} if getattr(args, "insecure_dev_no_auth", False) else None,
        },
    )
    if args.controller_command == "serve":
        return await _serve_controller_command(
            args=args,
            config=config,
            controller_server_type=ControllerServer,
            runner_registry_type=RunnerRegistry,
            registration_context_type=ControllerRunnerRegistrationContext,
            task_executor_type=TaskExecutorService,
            populate_runner_registry=populate_runner_registry,
        )
    server = ControllerServer(config)
    try:
        return await _run_controller_operator_command(
            args=args,
            config=config,
            server=server,
            runner_registry_type=RunnerRegistry,
            registration_context_type=ControllerRunnerRegistrationContext,
            populate_runner_registry=populate_runner_registry,
        )
    finally:
        server.store.close()


async def _serve_controller_command(
    *,
    args: argparse.Namespace,
    config: ControllerConfig,
    controller_server_type: type[ControllerServer],
    runner_registry_type: type[RunnerRegistry],
    registration_context_type: type[ControllerRunnerRegistrationContext],
    task_executor_type: type[TaskExecutorService],
    populate_runner_registry: Callable[..., None],
) -> int:
    """Start the controller server and wait until a process signal stops it."""

    resolved_app_config = resolve_app_config_path(getattr(args, "app_config", None))
    server = controller_server_type(config)
    runner_registry = runner_registry_type()
    populate_runner_registry(
        runner_registry=runner_registry,
        sdk=server.sdk,
        context=registration_context_type(
            app_config_path=str(resolved_app_config),
            command_timeout_seconds=config.command_timeout_seconds,
            artifact_root_dir_override=getattr(args, "artifact_root_dir", "") or None,
        ),
    )
    if not runner_registry.registered_agent_kinds():
        logger.warning(
            "No controller task runners are registered; starting controller in control-plane-only mode. "
            "Install `northcua[local]` or a runner plugin to execute agent tasks."
        )
    else:
        server.executor = task_executor_type(task_service=server.task_service, runner_registry=runner_registry)
        if config.executor_mode == "process":
            from northcua.controller.execution.processes.executor import ProcessTaskExecutorService
            from northcua.controller.execution.processes.runners import ProcessWorkerRunnerRegistry

            process_capabilities = set(ProcessWorkerRunnerRegistry.default().supported_agent_kinds())
            supported_agent_kinds = set(runner_registry.registered_agent_kinds()) & process_capabilities
            if not supported_agent_kinds:
                capabilities = ", ".join(sorted(process_capabilities)) or "none"
                raise RuntimeError(
                    "process executor mode requires a registered process-capable runner; "
                    f"built-in capabilities: {capabilities}"
                )
            _validate_process_runner_sources(runner_registry, supported_agent_kinds)
            process_app_config_path = _process_runner_app_config_path(
                runner_registry,
                supported_agent_kinds,
                fallback=str(resolved_app_config),
            )
            server.executor = ProcessTaskExecutorService(
                task_service=server.task_service,
                sdk=server.sdk,
                store=server.store,
                app_config_path=process_app_config_path,
                artifact_root_dir_override=getattr(args, "artifact_root_dir", "") or None,
                command_timeout_seconds=config.command_timeout_seconds,
                runner_workers=config.runner_workers or 4,
                worker_queue_max=config.worker_queue_max,
                worker_start_timeout_seconds=config.worker_start_timeout_seconds,
                worker_heartbeat_timeout_seconds=config.worker_heartbeat_timeout_seconds,
                supported_agent_kinds=supported_agent_kinds,
                worker_module=config.process_worker_module,
                command_timeout_floor_seconds=config.command_timeout_floor_seconds,
            )
        server.task_service.set_executor(server.executor)
    stop_event = asyncio.Event()
    _register_stop_handlers(stop_event)
    await server.start()
    print(f"controller=ws://{config.host}:{config.port}")
    print(f"db_path={config.db_path}")
    try:
        await stop_event.wait()
    finally:
        await server.stop()
    return 0


_BUILTIN_PROCESS_RUNNER_PLUGIN = "northcua_controller_plugins.desktop:register_controller_runner"


def _process_runner_app_config_path(
    runner_registry: RunnerRegistry,
    agent_kinds: set[str],
    *,
    fallback: str,
) -> str:
    """Resolve the exact config graph frozen by built-in runner registration."""

    paths = {
        path
        for agent_kind in agent_kinds
        if (path := runner_registry.registered_app_config_path(agent_kind)) is not None
    }
    missing = sorted(
        agent_kind for agent_kind in agent_kinds if runner_registry.registered_app_config_path(agent_kind) is None
    )
    if paths and missing:
        raise RuntimeError(
            "process runner registrations disagree on config snapshot metadata; "
            f"missing app config path for: {', '.join(missing)}"
        )
    if len(paths) > 1:
        raise RuntimeError("process runner registrations resolved different app config snapshots")
    return next(iter(paths), fallback)


def _validate_process_runner_sources(runner_registry: RunnerRegistry, agent_kinds: set[str]) -> None:
    """Reject inline plugin overrides that process workers cannot reconstruct."""

    mismatches = {
        agent_kind: runner_registry.registration_source(agent_kind) or "unknown"
        for agent_kind in agent_kinds
        if runner_registry.registration_source(agent_kind) != _BUILTIN_PROCESS_RUNNER_PLUGIN
    }
    if mismatches:
        rendered = ", ".join(f"{kind}={source}" for kind, source in sorted(mismatches.items()))
        raise RuntimeError(
            "process executor cannot use controller runner overrides because workers resolve immutable built-in factories; "
            f"unsupported registrations: {rendered}"
        )


def _register_stop_handlers(stop_event: asyncio.Event) -> None:
    loop = asyncio.get_running_loop()

    def _stop() -> None:
        stop_event.set()

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _stop)
        except NotImplementedError:  # pragma: no cover
            pass


async def _run_controller_operator_command(
    *,
    args: argparse.Namespace,
    config: ControllerConfig,
    server: ControllerServer,
    runner_registry_type: type[RunnerRegistry],
    registration_context_type: type[ControllerRunnerRegistrationContext],
    populate_runner_registry: Callable[..., None],
) -> int:
    """Run read-only or operator controller commands against a store-backed server."""

    if args.controller_command in {"devices", "sessions", "tasks"}:
        return print_controller_rows(server=server, args=args)
    if args.controller_command == "task":
        return await print_controller_task_detail(server=server, args=args)
    if args.controller_command == "events":
        return print_controller_events(server=server, args=args)
    if args.controller_command == "telemetry":
        return print_controller_telemetry(server=server, args=args)
    if args.controller_command == "artifacts":
        return print_controller_artifacts(server=server, args=args)
    if args.controller_command == "drain-device":
        server.registry.mark_draining(args.device_id, trace_id="manual", reason="cli")
        print(f"draining={args.device_id}")
        return 0
    if args.controller_command == "health":
        return print_controller_health(
            args=args,
            config=config,
            server=server,
            runner_registry_type=runner_registry_type,
            registration_context_type=registration_context_type,
            populate_runner_registry=populate_runner_registry,
        )
    raise ValueError(f"Unsupported controller command: {args.controller_command}")


def _positive_float_or_none(value: float) -> float | None:
    return value if value > 0 else None
