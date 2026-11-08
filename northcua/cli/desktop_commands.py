"""Desktop CLI commands and adapter."""

from __future__ import annotations

import asyncio

from northcua.cli import desktop_command_handlers as _handlers
from northcua.cli.desktop_command_handlers import _DEFAULT_REMOTE_DESKTOP_CAPABILITIES
from northcua.submitter import ControllerSubmitterClient


async def run_task_command(
    app_config_path: str | None,
    query: str,
    session_id: str,
    max_steps: int,
    max_session_steps: int,
    log_dir: str | None,
) -> int:
    """Run one desktop task through the implementation handler.

    Args:
        app_config_path: Optional app config override.
        query: User task/query text.
        session_id: Optional explicit session id.
        max_steps: Maximum number of steps for the current round/query.
        max_session_steps: Optional cumulative session step cap.
        log_dir: Optional artifact root override.

    Returns:
        Process-style exit code.
    """

    return await _handlers.run_task_command(
        app_config_path,
        query,
        session_id,
        max_steps,
        max_session_steps,
        log_dir,
    )


async def evaluate_artifact_command(
    app_config_path: str | None,
    artifact_dir: str,
    round_num: int = 0,
    no_rollup: bool = False,
) -> int:
    """Evaluate a desktop artifact directory through the implementation handler.

    Args:
        app_config_path: Optional app config override.
        artifact_dir: Session artifact directory containing ``steps.jsonl``.
        round_num: Round number to evaluate, where ``0`` selects latest.
        no_rollup: Whether to skip updating session rollup artifacts.

    Returns:
        Process-style exit code.
    """

    return await _handlers.evaluate_artifact_command(app_config_path, artifact_dir, round_num, no_rollup)


async def submit_task_command(
    app_config_path: str | None,
    query: str,
    session_id: str,
    max_steps: int,
    max_session_steps: int,
    *,
    controller_url: str,
    controller_token: str,
    wait_for_controller_result: bool,
    controller_wait_timeout: float,
    target_device_id: str,
    target_capabilities: set[str] | None = None,
) -> int:
    """Submit one desktop task through the implementation handler.

    Args:
        app_config_path: Reserved app config override kept for CLI symmetry.
        query: User task/query text.
        session_id: Optional explicit session id.
        max_steps: Maximum desktop round/query steps before terminal stop.
        max_session_steps: Optional cumulative session step cap.
        controller_url: Controller websocket URL.
        controller_token: Submitter token for controller auth.
        wait_for_controller_result: Whether to wait for TASK_FINISHED.
        controller_wait_timeout: Per-receive timeout while waiting.
        target_device_id: Optional strict sandbox/device target.
        target_capabilities: Optional capability values requested from devices.

    Returns:
        Process-style exit code.
    """

    _handlers.ControllerSubmitterClient = ControllerSubmitterClient
    return await _handlers.submit_task_command(
        app_config_path,
        query,
        session_id,
        max_steps,
        max_session_steps,
        controller_url=controller_url,
        controller_token=controller_token,
        wait_for_controller_result=wait_for_controller_result,
        controller_wait_timeout=controller_wait_timeout,
        target_device_id=target_device_id,
        target_capabilities=target_capabilities,
    )


async def run_server_command(app_config_path: str | None, session_id: str, log_dir: str | None) -> int:
    """Run the desktop websocket server through the implementation handler.

    Args:
        app_config_path: Optional app config override.
        session_id: Optional explicit session id.
        log_dir: Optional artifact root override.

    Returns:
        Process-style exit code.
    """

    _handlers.asyncio = asyncio
    return await _handlers.run_server_command(app_config_path, session_id, log_dir)
