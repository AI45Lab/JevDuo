"""Generic web-agent CLI commands and adapter."""

from __future__ import annotations

import asyncio
import signal
from pathlib import Path
from typing import cast

from northcua.agentinfra.runtime.protocol import WebRunRequest, WebRunResult
from northcua.core.enums import RuntimeMode
from northcua.submitter import ControllerSubmitterClient, WebSubmitSpec
from northcua_protocol.capabilities import Capability
from northcua_protocol.state import TaskState

from .shared_args import default_session_id, resolve_app_config_path, resolve_log_root


async def run_web_command(
    app_config_path: str | None,
    query: str,
    url: str,
    headed: bool | None,
    max_steps: int,
    max_session_steps: int,
    session_id: str,
    log_dir: str | None,
    evaluate: bool | None = None,
) -> int:
    """Run one generic web-agent task locally.

    Args:
        app_config_path: Optional application config path override.
        query: User task for the web agent to complete.
        url: Optional URL to open before the first observation.
        headed: Optional per-run override for showing the browser window.
        max_steps: Positive override for the configured step limit, or ``0``
            to use config.
        max_session_steps: Optional cumulative session step cap.
        session_id: Optional artifact/session identifier.
        log_dir: Optional artifact root override.
        evaluate: Optional per-run evaluator override. ``None`` honors the
            application config, ``True`` forces local artifact-only evaluation,
            and ``False`` skips it.

    Returns:
        ``0`` on successful task completion, ``1`` on failed result, or ``2``
        when local execution is requested with a remote-only config.
    """

    effective_session_id = session_id or default_session_id("web")
    resolved_app_config_path = resolve_app_config_path(app_config_path)
    resolved_log_root = resolve_log_root(log_dir)
    from northcua.agentkit.presentation import (
        AgentRenderContext,
        resolve_panel_projector,
        resolve_terminal_renderer,
        run_with_terminal_renderer,
    )
    from northcua.agents.web import WebAgentApplication

    app = WebAgentApplication.from_app_config(
        str(resolved_app_config_path),
        session_id=effective_session_id,
        artifact_root_dir_override=str(resolved_log_root) if resolved_log_root is not None else None,
    )
    if app.app_config.websocket.runtime_mode == RuntimeMode.REMOTE:
        await app.stop()
        print(
            "local web run requires websocket.runtime_mode=local; use northcua submit web for controller mode.",
            flush=True,
        )
        return 2
    try:
        context = AgentRenderContext(
            agent_kind="web",
            display_name="NorthCUA Web Agent",
            session_id=effective_session_id,
            log_dir=app.artifact_store.root,
            query=query,
            extras={"url": url, "headed": headed},
        )
        renderer = resolve_terminal_renderer("web")
        projector = resolve_panel_projector("web")

        async def _run(callbacks: object) -> WebRunResult:
            on_before_step = getattr(callbacks, "on_before_step")
            on_step = getattr(callbacks, "on_step")
            return cast(
                WebRunResult,
                await app.run_query(
                    WebRunRequest(
                        session_id=effective_session_id,
                        query=query,
                        url=url,
                        headed=headed,
                        max_steps=max_steps or app.app_config.web.max_steps,
                        max_session_steps=max_session_steps,
                    ),
                    evaluate=evaluate,
                    on_before_step=on_before_step,
                    on_step=on_step,
                ),
            )

        result = await run_with_terminal_renderer(
            context=context,
            renderer=renderer,
            projector=projector,
            run=_run,
            footer_fields=lambda run_result: {
                "result_path": run_result.primary_result_path or app.artifact_store.result_path,
                "trace_path": run_result.trace_path or app.artifact_store.trace_path,
                "log_dir": app.artifact_store.root,
            },
        )
        return 0 if result.status == "success" else 1
    finally:
        await app.stop()


async def evaluate_web_artifact_command(app_config_path: str | None, artifact_dir: str, round_num: int = 1) -> int:
    """Run the Web evaluator over an existing artifact directory.

    Args:
        app_config_path: Optional application config override.
        artifact_dir: Web session artifact directory containing ``result.json``.
        round_num: Round number to evaluate.

    Returns:
        Process-style exit code. A produced evaluator report returns ``0`` even
        when the evaluator verdict is failure.
    """

    resolved_app_config_path = resolve_app_config_path(app_config_path)
    from northcua.agents.web.evaluation.offline import WebArtifactEvaluationRunner, WebOfflineEvaluationRequest

    artifact_path = Path(artifact_dir)
    print(
        "NorthCUA Web Evaluator\n"
        f"app_config: {resolved_app_config_path}\n"
        f"artifact_dir: {artifact_path}\n"
        f"round_num: {round_num}",
        flush=True,
    )
    result = await WebArtifactEvaluationRunner().run(
        WebOfflineEvaluationRequest(
            app_config_path=resolved_app_config_path,
            artifact_dir=artifact_path,
            round_num=round_num,
        )
    )
    print(
        "Web Evaluator Summary\n"
        f"Final Report: {result.final_report_path}\n"
        f"LLM Trace: {result.llm_trace_path}\n"
        f"Tool Trace: {result.tool_trace_path}\n"
        f"Runtime Log: {result.runtime_log_path}",
        flush=True,
    )
    return 0


async def run_web_server_command(app_config_path: str | None, session_id: str, log_dir: str | None) -> int:
    """Start the local websocket server for generic web-agent runs.

    Args:
        app_config_path: Optional application config path override.
        session_id: Optional artifact/session identifier for server logs.
        log_dir: Optional artifact root override.

    Returns:
        ``0`` after graceful shutdown.
    """

    effective_session_id = session_id or default_session_id("web-server")
    resolved_app_config_path = resolve_app_config_path(app_config_path)
    resolved_log_root = resolve_log_root(log_dir)
    from northcua.agents.web import WebAgentApplication

    app = WebAgentApplication.from_app_config(
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


async def submit_web_command(
    app_config_path: str | None,
    query: str,
    url: str,
    headed: bool | None,
    max_steps: int,
    max_session_steps: int,
    session_id: str,
    *,
    controller_url: str,
    controller_token: str,
    wait_for_controller_result: bool,
    controller_wait_timeout: float,
    target_device_id: str,
    target_capabilities: set[str] | None = None,
) -> int:
    """Submit one generic web-agent task to the remote controller.

    Args:
        app_config_path: Reserved for parity with local command signatures.
        query: User task to send to the controller.
        url: Optional initial URL for the remote browser session.
        headed: Optional per-task override for showing the browser window.
        max_steps: Positive step limit override, or ``0`` for controller
            defaults.
        max_session_steps: Optional cumulative session step cap.
        session_id: Optional remote session identifier.
        controller_url: Base URL of the harness controller.
        controller_token: Bearer token or empty string for unsecured dev mode.
        wait_for_controller_result: Whether to block until task completion.
        controller_wait_timeout: Seconds to wait when blocking for completion.
        target_device_id: Optional strict device/sandbox lease target.
        target_capabilities: Browser capabilities required from the device.

    Returns:
        ``0`` when the controller accepted the task and, if waiting, finished
        successfully; otherwise ``1`` for completed error states.
    """

    del app_config_path
    capability_values = set(target_capabilities or []) or {"browser.page"}
    spec = WebSubmitSpec(
        query=query,
        url=url,
        headed=headed,
        session_id=session_id,
        max_steps=max_steps,
        max_session_steps=max_session_steps,
        target_device_id=target_device_id,
        required_capabilities={Capability(value) for value in capability_values},
    )
    async with ControllerSubmitterClient(controller_url) as client:
        accepted = await client.submit_task(token=controller_token, **spec.submit_payload())
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
                print(f"result={finished.result}", flush=True)
            if finished.error:
                print(f"error={finished.error}", flush=True)
            return 0 if finished.error is None and finished.state == TaskState.FINISHED else 1
    return 0
