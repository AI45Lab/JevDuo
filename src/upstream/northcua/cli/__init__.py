"""Public CLI package entrypoint."""

from __future__ import annotations

import asyncio as _asyncio
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from northcua.agents.desktop.application import NorthCUAApplication
    from northcua.agents.web import WebAgentApplication
    from northcua.transport.ws.client import NorthCUAWebSocketClient

    from .desktop_commands import (
        evaluate_artifact_command,
        run_server_command,
        run_task_command,
        submit_task_command,
    )
    from .parser import build_parser, cli_adapters, resolve_cli_adapter
    from .provider_commands import register_provider_probe_command, run_provider_probe_command
    from .shared_args import default_app_config_path, default_session_id, resolve_app_config_path, resolve_log_root
    from .web_commands import (
        evaluate_web_artifact_command,
        run_web_command,
        run_web_server_command,
        submit_web_command,
    )

asyncio = _asyncio


def main() -> None:
    """Dispatch the NorthCUA CLI without importing the entrypoint eagerly."""

    from .main import main as _main

    _main()


def __getattr__(name: str) -> object:
    """Lazily expose CLI helpers without forcing controller imports on import."""

    exports = {
        "NorthCUAApplication": ("northcua.agents.desktop.application", "NorthCUAApplication"),
        "NorthCUAWebSocketClient": ("northcua.transport.ws.client", "NorthCUAWebSocketClient"),
        "WebAgentApplication": ("northcua.agents.web", "WebAgentApplication"),
        "build_parser": ("northcua.cli.parser", "build_parser"),
        "cli_adapters": ("northcua.cli.parser", "cli_adapters"),
        "default_app_config_path": ("northcua.cli.shared_args", "default_app_config_path"),
        "default_session_id": ("northcua.cli.shared_args", "default_session_id"),
        "main": ("northcua.cli.main", "main"),
        "register_provider_probe_command": ("northcua.cli.provider_commands", "register_provider_probe_command"),
        "resolve_app_config_path": ("northcua.cli.shared_args", "resolve_app_config_path"),
        "resolve_cli_adapter": ("northcua.cli.parser", "resolve_cli_adapter"),
        "resolve_log_root": ("northcua.cli.shared_args", "resolve_log_root"),
        "run_provider_probe_command": ("northcua.cli.provider_commands", "run_provider_probe_command"),
        "evaluate_artifact_command": ("northcua.cli.desktop_commands", "evaluate_artifact_command"),
        "run_server_command": ("northcua.cli.desktop_commands", "run_server_command"),
        "run_task_command": ("northcua.cli.desktop_commands", "run_task_command"),
        "submit_task_command": ("northcua.cli.desktop_commands", "submit_task_command"),
        "run_web_command": ("northcua.cli.web_commands", "run_web_command"),
        "run_web_server_command": ("northcua.cli.web_commands", "run_web_server_command"),
        "submit_web_command": ("northcua.cli.web_commands", "submit_web_command"),
        "evaluate_web_artifact_command": ("northcua.cli.web_commands", "evaluate_web_artifact_command"),
    }
    if name not in exports:
        raise AttributeError(name)
    module_name, attr_name = exports[name]
    module = __import__(module_name, fromlist=[attr_name])
    return getattr(module, attr_name)


__all__ = [
    "NorthCUAApplication",
    "NorthCUAWebSocketClient",
    "WebAgentApplication",
    "build_parser",
    "cli_adapters",
    "default_app_config_path",
    "default_session_id",
    "evaluate_artifact_command",
    "evaluate_web_artifact_command",
    "main",
    "register_provider_probe_command",
    "resolve_app_config_path",
    "resolve_cli_adapter",
    "resolve_log_root",
    "run_provider_probe_command",
    "run_server_command",
    "run_task_command",
    "submit_task_command",
    "run_web_command",
    "run_web_server_command",
    "submit_web_command",
    "asyncio",
]
