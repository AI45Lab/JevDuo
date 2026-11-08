"""Execution handlers for desktop CLI commands."""

from __future__ import annotations

import argparse
import asyncio
import signal
from pathlib import Path
from typing import cast

from northcua.agentinfra.runtime.cli import AgentCliAdapterBase
from northcua.agentinfra.runtime.protocol import DesktopRunRequest, DesktopRunResult
from northcua.core.enums import TaskStatus
from northcua.submitter import ControllerSubmitterClient, DesktopSubmitSpec
from northcua_protocol.capabilities import Capability
from northcua_protocol.state import TaskState
from northcua_runtime_shared.redaction import redact_for_log

from .shared_args import default_session_id, resolve_app_config_path, resolve_log_root

_DEFAULT_REMOTE_DESKTOP_CAPABILITIES = {
    "desktop.input",
    "desktop.screenshot",
    "desktop.window",
}


async def run_task_command(
    app_config_path: str | None,
    query: str,
    session_id: str,
    max_steps: int,
    max_session_steps: int,
    log_dir: str | None,
) -> int:
    """Run a task to terminal state or until the step cap is reached.

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

    effective_session_id = session_id or default_session_id("task")
    resolved_app_config_path = resolve_app_config_path(app_config_path)
    resolved_log_root = resolve_log_root(log_dir)
    from northcua.agentkit.presentation import (
        AgentRenderContext,
        resolve_panel_projector,
        resolve_terminal_renderer,
        run_with_terminal_renderer,
    )
    from northcua.agents.desktop.application import NorthCUAApplication

    app = NorthCUAApplication.from_app_config(
        str(resolved_app_config_path),
        session_id=effective_session_id,
        artifact_root_dir_override=str(resolved_log_root) if resolved_log_root is not None else None,
    )
    context = AgentRenderContext(
        agent_kind="desktop",
        display_name="NorthCUA Desktop",
        session_id=effective_session_id,
        log_dir=app.artifact_store.root,
        query=query,
    )
    renderer = resolve_terminal_renderer("desktop")
    projector = resolve_panel_projector("desktop")

    async def _run(callbacks: object) -> DesktopRunResult:
        on_before_step = getattr(callbacks, "on_before_step")
        on_step = getattr(callbacks, "on_step")
        return cast(
            DesktopRunResult,
            await app.run_query(
                DesktopRunRequest(
                    session_id=effective_session_id,
                    query=query,
                    max_steps=max_steps,
                    max_session_steps=max_session_steps,
                ),
                on_before_step=on_before_step,
                on_step=on_step,
            ),
        )

    try:
        result = await run_with_terminal_renderer(
            context=context,
            renderer=renderer,
            projector=projector,
            run=_run,
            footer_fields=lambda run_result: {
                "step_log_path": run_result.step_log_path or app.artifact_store.step_log_path,
                "runtime_log_path": run_result.runtime_log_path or app.artifact_store.runtime_log_path,
                "evaluation_log_path": run_result.evaluation_log_path or app.artifact_store.evaluation_log_path,
            },
        )
    finally:
        await app.stop()

    if getattr(result, "step_count", 0) == 0:
        print("No steps were produced.", flush=True)
    if result.hit_step_limit or result.terminal_status in {TaskStatus.FAIL.value, TaskStatus.ERROR.value}:
        return 1
    return 0


async def evaluate_artifact_command(
    app_config_path: str | None,
    artifact_dir: str,
    round_num: int = 0,
    no_rollup: bool = False,
) -> int:
    """Run the evaluator over an existing artifact directory.

    Args:
        app_config_path: Optional app config override.
        artifact_dir: Session artifact directory containing ``steps.jsonl``.
        round_num: Round number to evaluate. ``0`` selects the latest round.
        no_rollup: Whether to skip the session-level rollup.

    Returns:
        Process-style exit code. A produced evaluator report returns ``0`` even
        when the evaluator verdict is failure.
    """

    resolved_app_config_path = resolve_app_config_path(app_config_path)
    from northcua.agents.desktop.evaluation.events import DesktopEvaluationEvent
    from northcua.agents.desktop.evaluation.offline import (
        DesktopArtifactEvaluationRunner,
        OfflineEvaluationRequest,
    )
    from northcua.app.terminal_renderer import NorthCUAEvaluatorTerminalRenderer

    renderer = NorthCUAEvaluatorTerminalRenderer()
    artifact_path = Path(artifact_dir)
    print(
        renderer.render_header(
            app_config_path=resolved_app_config_path,
            artifact_dir=artifact_path,
            round_num=round_num,
            rollup=not no_rollup,
        ),
        flush=True,
    )

    def _render_event(event: DesktopEvaluationEvent) -> None:
        print(renderer.render_event(event), flush=True)

    result = await DesktopArtifactEvaluationRunner(event_callback=_render_event).run(
        OfflineEvaluationRequest(
            app_config_path=resolved_app_config_path,
            artifact_dir=artifact_path,
            round_num=round_num,
            write_session_rollup=not no_rollup,
        )
    )
    print(renderer.render_footer(result=result), flush=True)
    return 0


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
    """Submit one desktop task to the remote controller.

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

    del app_config_path
    capability_values = set(target_capabilities or []) or set(_DEFAULT_REMOTE_DESKTOP_CAPABILITIES)
    spec = DesktopSubmitSpec(
        query=query,
        session_id=session_id,
        max_steps=max_steps,
        max_session_steps=max_session_steps,
        target_device_id=target_device_id,
        required_capabilities={Capability(value) for value in capability_values},
    )
    async with ControllerSubmitterClient(controller_url) as client:
        submit_payload = spec.submit_payload()
        accepted = await client.submit_task(
            token=controller_token,
            agent_kind="desktop",
            request=query,
            target_device_id=target_device_id,
            required_capabilities=spec.required_capabilities,
            metadata=dict(submit_payload["metadata"]) if isinstance(submit_payload["metadata"], dict) else {},
        )
        print(f"task_id={accepted.task_id}", flush=True)
        print(f"trace_id={accepted.trace_id}", flush=True)
        print(f"session_id={accepted.session_id}", flush=True)
        print("status=accepted", flush=True)
        if wait_for_controller_result:
            finished = await client.wait_for_finished(
                accepted.task_id,
                timeout_seconds=controller_wait_timeout,
            )
            print(f"status={finished.state.value}", flush=True)
            if finished.result is not None:
                print(f"result={redact_for_log(finished.result)}", flush=True)
            if finished.artifacts:
                print(f"artifacts={len(finished.artifacts)}", flush=True)
            if finished.error:
                print(f"error={redact_for_log(finished.error)}", flush=True)
            return 0 if finished.error is None and finished.state == TaskState.FINISHED else 1
    return 0


async def run_server_command(app_config_path: str | None, session_id: str, log_dir: str | None) -> int:
    """Start the websocket server and keep it alive until interrupted.

    Args:
        app_config_path: Optional app config override.
        session_id: Optional explicit session id.
        log_dir: Optional artifact root override.

    Returns:
        Process-style exit code.
    """

    effective_session_id = session_id or default_session_id("server")
    resolved_app_config_path = resolve_app_config_path(app_config_path)
    resolved_log_root = resolve_log_root(log_dir)
    from northcua.agents.desktop.application import NorthCUAApplication

    app = NorthCUAApplication.from_app_config(
        str(resolved_app_config_path),
        session_id=effective_session_id,
        artifact_root_dir_override=str(resolved_log_root) if resolved_log_root is not None else None,
    )
    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()

    def _request_stop() -> None:
        stop_event.set()

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _request_stop)
        except NotImplementedError:  # pragma: no cover
            pass

    await app.start()
    print(f"session_id={effective_session_id}", flush=True)
    print(f"log_dir={app.artifact_store.root}", flush=True)
    print(f"websocket=ws://{app.app_config.websocket.host}:{app.app_config.websocket.port}", flush=True)
    print("Press Ctrl+C to stop.", flush=True)
    try:
        await stop_event.wait()
    finally:
        await app.stop()
    return 0
