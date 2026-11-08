"""Controller CLI commands for serving, inspecting, and operating the harness."""

from __future__ import annotations

import argparse

from northcua.cli.controller_command_runner import run_controller_command as _run_controller_command
from northcua.cli.controller_command_views import _task_row_json
from northcua.cli.parallelism_args import add_parallelism_arg


def register_controller_commands(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    """Register grouped controller subcommands on the root parser.

    Args:
        subparsers: Root argparse subparser collection owned by the NorthCUA CLI.
    """

    controller = subparsers.add_parser("controller", help="Manage the NorthCUA remote controller")
    controller_sub = controller.add_subparsers(dest="controller_command", required=True)

    serve = controller_sub.add_parser("serve", help="Start the remote controller websocket server")
    serve.add_argument("--config", default="", help="Path to controller YAML config")
    serve.add_argument("--app-config", default=None, help="Path to the NorthCUA app config used by desktop executor")
    serve.add_argument("--host", default="", help="Bind host override")
    serve.add_argument("--port", type=int, default=0, help="Bind port override")
    serve.add_argument("--websocket-backlog", type=int, default=0, help="Controller websocket TCP listen backlog")
    serve.add_argument(
        "--websocket-open-timeout",
        type=float,
        default=-1.0,
        help="Seconds allowed for incoming controller websocket opening handshakes; <=0 uses config/env",
    )
    serve.add_argument("--db-path", default="", help="SQLite database path")
    serve.add_argument("--blob-dir", default="", help="Controller blob storage directory")
    serve.add_argument("--artifact-root-dir", default="", help="Controller-owned agent artifact root directory")
    serve.add_argument(
        "--executor-mode",
        choices=("in_process", "process"),
        default="",
        help="Controller task executor mode; process runs agent tasks in worker processes",
    )
    add_parallelism_arg(serve)
    serve.add_argument("--runner-workers", type=int, default=0, help="Max concurrent process-executor workers")
    serve.add_argument("--worker-queue-max", type=int, default=None, help="Max queued process-executor tasks")
    serve.add_argument(
        "--worker-start-timeout-seconds",
        type=float,
        default=0.0,
        help="Seconds to wait for a process worker to connect",
    )
    serve.add_argument(
        "--worker-heartbeat-timeout-seconds",
        type=float,
        default=0.0,
        help="Seconds without process-worker heartbeat before failing the task",
    )
    serve.add_argument(
        "--process-worker-module",
        default="",
        help="Python module for process worker entrypoint; intended for no-LLM simulation/dev diagnostics",
    )
    serve.add_argument("--insecure-dev-no-auth", action="store_true", help="Disable token auth for local development")

    for command_name in ("devices", "sessions", "tasks"):
        parser = controller_sub.add_parser(command_name, help=f"List controller {command_name}")
        parser.add_argument("--config", default="", help="Path to controller YAML config")
        parser.add_argument("--db-path", default="", help="SQLite database path")
        parser.add_argument("--json", action="store_true", help="Emit JSON rows")

    task = controller_sub.add_parser("task", help="Inspect one controller task in detail")
    task.add_argument("task_id")
    task.add_argument("--config", default="", help="Path to controller YAML config")
    task.add_argument("--db-path", default="", help="SQLite database path")
    task.add_argument("--json", action="store_true", help="Emit JSON detail")
    task.add_argument("--watch", action="store_true", help="Poll until the task reaches a terminal state")
    task.add_argument("--poll-interval", type=float, default=1.0, help="Seconds between watch polls")

    events = controller_sub.add_parser("events", help="List controller events")
    events.add_argument("--config", default="", help="Path to controller YAML config")
    events.add_argument("--db-path", default="", help="SQLite database path")
    events.add_argument("--trace-id", default="", help="Optional trace id filter")
    events.add_argument("--json", action="store_true", help="Emit JSON rows")

    telemetry = controller_sub.add_parser("telemetry", help="List controller case telemetry")
    telemetry.add_argument("--config", default="", help="Path to controller YAML config")
    telemetry.add_argument("--db-path", default="", help="SQLite database path")
    telemetry.add_argument("--experiment-id", default="", help="Optional experiment id filter")
    telemetry.add_argument("--batch-id", default="", help="Optional batch id filter")
    telemetry.add_argument("--case-id", default="", help="Optional case id filter")
    telemetry.add_argument("--since-event-id", type=int, default=0, help="Only include events after this id")
    telemetry.add_argument("--json", action="store_true", help="Emit JSON rows")

    artifacts = controller_sub.add_parser("artifacts", help="List controller artifact catalog rows")
    artifacts.add_argument("--config", default="", help="Path to controller YAML config")
    artifacts.add_argument("--db-path", default="", help="SQLite database path")
    artifacts.add_argument("--trace-id", default="", help="Optional trace id filter")
    artifacts.add_argument("--task-id", default="", help="Optional task id filter")
    artifacts.add_argument("--json", action="store_true", help="Emit JSON rows")

    drain = controller_sub.add_parser("drain-device", help="Mark a device as draining")
    drain.add_argument("device_id")
    drain.add_argument("--config", default="", help="Path to controller YAML config")
    drain.add_argument("--db-path", default="", help="SQLite database path")

    health = controller_sub.add_parser("health", help="Validate controller config, DB, and runner plugins")
    health.add_argument("--config", default="", help="Path to controller YAML config")
    health.add_argument("--app-config", default=None, help="Path to the NorthCUA app config used by desktop executor")
    health.add_argument("--db-path", default="", help="SQLite database path")
    health.add_argument("--artifact-root-dir", default="", help="Controller-owned agent artifact root directory")
    health.add_argument("--json", action="store_true", help="Emit JSON detail")


async def run_controller_command(args: argparse.Namespace) -> int:
    """Dispatch one controller CLI command.

    Args:
        args: Parsed CLI namespace containing the selected controller subcommand.

    Returns:
        Process-style exit code from the controller command runner.
    """

    return await _run_controller_command(args)
