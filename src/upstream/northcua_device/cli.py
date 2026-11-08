"""CLI entrypoint for standalone NorthCUA device workers."""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import uuid
from typing import TYPE_CHECKING

from northcua_device.config import DeviceClientConfig
from northcua_protocol.capabilities import Capability, PlatformKind
from northcua_protocol.payloads import TaskRequest

if TYPE_CHECKING:
    from northcua_device.worker import DeviceWorker

_SUPPRESSED_OPTIONAL_DEPENDENCY_LOGGERS = (
    "nexau.archs.sandbox.e2b_sandbox",
    "nexau.archs.sandbox",
)


def _suppress_optional_dependency_noise() -> None:
    """Silence unrelated NexAU sandbox warnings on device-only CLI paths."""

    for logger_name in _SUPPRESSED_OPTIONAL_DEPENDENCY_LOGGERS:
        logging.getLogger(logger_name).setLevel(logging.ERROR)


_suppress_optional_dependency_noise()


def build_parser() -> argparse.ArgumentParser:
    """Build the northcua-device CLI parser.

    Returns:
        Configured argparse parser for the standalone device worker CLI.
    """

    parser = argparse.ArgumentParser(description="NorthCUA device worker")
    subparsers = parser.add_subparsers(dest="command", required=True)
    connect = subparsers.add_parser("connect", help="Connect this device to a NorthCUA controller")
    connect.add_argument("--config", default="", help="Path to device YAML config")
    connect.add_argument("--server", default="", help="Controller websocket URL")
    connect.add_argument("--token", default="", help="Bearer token")
    connect.add_argument("--device-id", default="", help="Stable device id")
    connect.add_argument("--display-name", default="", help="Human-readable display name")
    connect.add_argument(
        "--platform",
        choices=[
            PlatformKind.LINUX.value,
            PlatformKind.MACOS.value,
            PlatformKind.WINDOWS.value,
            PlatformKind.BROWSER.value,
        ],
        default="",
    )
    connect.add_argument(
        "--gui-resource-id",
        default="",
        help="Shared GUI resource id for workers controlling the same desktop/display",
    )
    connect.add_argument("--max-sessions", type=int, default=0, help="Maximum active sessions")
    connect.add_argument("--max-gui-sessions", type=int, default=0, help="Maximum GUI sessions")
    connect.add_argument("--max-concurrent-commands", type=int, default=0, help="Maximum concurrent commands")
    connect.add_argument(
        "--supports-parallel-sessions",
        action="store_true",
        help="Allow the controller to place multiple sessions on this worker",
    )
    connect.add_argument("--log-dir", default="", help="Directory for device runtime JSONL events")
    connect.add_argument(
        "--allowed-capability",
        action="append",
        default=[],
        help="Allowed device capability; repeatable. Empty advertises every enabled adapter capability.",
    )
    connect.add_argument("--allowed-tool", action="append", default=[], help="Allowed tool id; repeatable")
    connect.add_argument(
        "--agentkit-agent-kind",
        action="append",
        default=[],
        help="AgentKit agent kind to expose on this worker; repeatable",
    )
    connect.add_argument("--insecure-dev-no-auth", action="store_true", help="Disable token for local development")
    connect.add_argument(
        "--submit-task", "--task", dest="submit_task", default="", help="Optional task to submit after connecting"
    )
    connect.add_argument("--agent-kind", default="desktop", help="Agent kind for --task submissions")
    connect.add_argument(
        "--required-capability",
        action="append",
        default=[],
        help="Capability required by --task; repeatable",
    )

    doctor = subparsers.add_parser("doctor", help="Validate local device worker configuration")
    doctor.add_argument("--config", default="")
    doctor.add_argument("--server", default="")
    doctor.add_argument(
        "--metadata-only",
        action="store_true",
        help="Only validate static config and advertised tool metadata",
    )
    list_tools = subparsers.add_parser("list-tools", help="List locally available worker tools")
    list_tools.add_argument("--config", default="")
    list_tools.add_argument(
        "--platform",
        choices=[
            PlatformKind.LINUX.value,
            PlatformKind.MACOS.value,
            PlatformKind.WINDOWS.value,
            PlatformKind.BROWSER.value,
        ],
        default="",
    )
    list_tools.add_argument("--json", action="store_true", help="Emit JSON tool descriptors")
    list_tools.add_argument(
        "--allowed-capability",
        action="append",
        default=[],
        help="Allowed device capability before listing tools; repeatable",
    )
    list_tools.add_argument("--allowed-tool", action="append", default=[], help="Allowed tool id; repeatable")
    list_tools.add_argument(
        "--agentkit-agent-kind",
        action="append",
        default=[],
        help="AgentKit agent kind to expose before listing tools; repeatable",
    )
    ping = subparsers.add_parser("ping", help="Validate websocket URL parsing for a controller")
    ping.add_argument("--server", required=True)
    run_once = subparsers.add_parser("run-once", help="Connect once and exit when disconnected")
    run_once.add_argument("--config", default="")
    run_once.add_argument("--server", default="")
    run_once.add_argument(
        "--platform",
        choices=[
            PlatformKind.LINUX.value,
            PlatformKind.MACOS.value,
            PlatformKind.WINDOWS.value,
            PlatformKind.BROWSER.value,
        ],
        default="",
    )
    run_once.add_argument("--allowed-capability", action="append", default=[], help="Allowed device capability")
    run_once.add_argument(
        "--submit-task", "--task", dest="submit_task", default="", help="Optional task to submit after connecting"
    )
    run_once.add_argument("--agent-kind", default="desktop", help="Agent kind for --task submissions")
    run_once.add_argument("--required-capability", action="append", default=[], help="Capability required by --task")
    run_once.add_argument(
        "--agentkit-agent-kind",
        action="append",
        default=[],
        help="AgentKit agent kind to expose on this worker; repeatable",
    )
    return parser


def main() -> None:
    """Run the device CLI.

    Raises:
        SystemExit: Always exits with a process status after handling a command.
    """

    parser = build_parser()
    args = parser.parse_args()
    if args.command == "doctor":
        from northcua_device.worker import DeviceWorker

        config = DeviceClientConfig.from_sources(
            config_path=args.config or None,
            overrides={"server_url": args.server},
        )
        worker = DeviceWorker(config)
        raise SystemExit(asyncio.run(_doctor_worker(worker, metadata_only=args.metadata_only)))
    if args.command == "list-tools":
        from northcua_device.worker import DeviceWorker

        worker = DeviceWorker(
            DeviceClientConfig.from_sources(
                config_path=args.config or None,
                overrides={
                    "platform": getattr(args, "platform", ""),
                    "allowed_tools": set(getattr(args, "allowed_tool", []) or []),
                    "allowed_capabilities": set(getattr(args, "allowed_capability", []) or []),
                    "agentkit_agent_kinds": set(getattr(args, "agentkit_agent_kind", []) or []),
                },
            )
        )
        if args.json:
            print(json.dumps([tool.model_dump(mode="json") for tool in worker.executor.tools], ensure_ascii=False))
        else:
            for tool in worker.executor.tools:
                print(f"{tool.tool_id}\t{tool.capability}\trisk={tool.risk_level}")
        raise SystemExit(0)
    if args.command == "ping":
        print(f"server={args.server}")
        raise SystemExit(0)
    if args.command in {"connect", "run-once"}:
        from northcua_device.worker import DeviceWorker

        overrides = {
            "server_url": getattr(args, "server", ""),
            "token": getattr(args, "token", ""),
            "device_id": getattr(args, "device_id", ""),
            "display_name": getattr(args, "display_name", ""),
            "platform": getattr(args, "platform", ""),
            "gui_resource_id": getattr(args, "gui_resource_id", ""),
            "max_sessions": getattr(args, "max_sessions", 0) or None,
            "max_gui_sessions": getattr(args, "max_gui_sessions", 0) or None,
            "max_concurrent_commands": getattr(args, "max_concurrent_commands", 0) or None,
            "supports_parallel_sessions": True if getattr(args, "supports_parallel_sessions", False) else None,
            "allowed_tools": set(getattr(args, "allowed_tool", []) or []),
            "allowed_capabilities": set(getattr(args, "allowed_capability", []) or []),
            "agentkit_agent_kinds": set(getattr(args, "agentkit_agent_kind", []) or []),
            "insecure_dev_no_auth": getattr(args, "insecure_dev_no_auth", False),
            "log_dir": getattr(args, "log_dir", ""),
        }
        config = DeviceClientConfig.from_sources(config_path=getattr(args, "config", "") or None, overrides=overrides)
        worker = DeviceWorker(config)
        initial_task = _build_initial_task(
            args,
            config,
            worker_capabilities=worker.executor.capabilities,
        )
        worker.initial_task = initial_task
        raise SystemExit(asyncio.run(_run_worker(worker, reconnect=args.command == "connect")))
    parser.error(f"Unsupported command: {args.command}")


async def _run_worker(worker: "DeviceWorker", *, reconnect: bool = False) -> int:
    """Run a device worker in one-shot or reconnecting mode.

    Args:
        worker: Worker instance to run.
        reconnect: Whether to keep reconnecting after disconnects.

    Returns:
        Process exit code for non-reconnecting runs.
    """

    if reconnect:
        await worker.run_forever()
        return 0
    await worker.run_once()
    return 0


async def _doctor_worker(worker: "DeviceWorker", *, metadata_only: bool) -> int:
    """Print a doctor report and return a process exit code."""

    config = worker.config
    print(f"device_id={config.device_id}")
    print(f"platform={config.platform}")
    print(f"server={config.server_url}")
    print(f"tools={','.join(tool.tool_id for tool in worker.executor.tools)}")
    if metadata_only:
        print("runtime_ready=skipped")
        return 0
    report = await worker.health_report()
    print(f"runtime_ready={'yes' if report.healthy else 'no'}")
    capability_health = report.capability_health
    if capability_health:
        pairs = [f"{name}={'ok' if status else 'failed'}" for name, status in sorted(capability_health.items())]
        print("capabilities=" + ",".join(pairs))
    for message in report.messages:
        print(f"detail={message}")
    return 0 if report.healthy else 1


def _build_initial_task(
    args: argparse.Namespace,
    config: DeviceClientConfig,
    *,
    worker_capabilities: set[Capability],
) -> TaskRequest | None:
    """Build a self-affined task submission for sandbox-per-client runs.

    Args:
        args: Parsed argparse namespace.
        config: Device config used to populate target and auth fields.
        worker_capabilities: Capabilities advertised by the local worker.

    Returns:
        Task request to submit on connect, or ``None`` when no task was requested.
    """

    request = getattr(args, "submit_task", "")
    if not request:
        return None
    required_capabilities = {Capability(value) for value in (getattr(args, "required_capability", []) or [])}
    if not required_capabilities:
        required_capabilities = set(config.allowed_capabilities) or worker_capabilities
    trace_id = uuid.uuid4().hex
    return TaskRequest(
        task_id=uuid.uuid4().hex,
        trace_id=trace_id,
        agent_kind=getattr(args, "agent_kind", "desktop"),
        request=request,
        token=config.token,
        target_device_id=config.device_id,
        required_capabilities=required_capabilities,
        metadata={"submitted_by": "northcua-device"},
    )


if __name__ == "__main__":  # pragma: no cover
    main()
