"""Web remote-runner hooks for post-run evaluator execution.

The controller runner owns the Web agent process, but the evaluator itself is
still the shared AgentKit evaluator runtime.  This module keeps that wiring out
of the hot-path runner so the remote runner only decides *when* evaluation is
enabled and how the resulting artifacts are attached to the terminal event.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from northcua.agentkit.controller_dispatch import command_result_record
from northcua.agentkit.evaluator.routing import EvaluatorClientToolDispatcher, RemoteEvaluatorClientToolDispatcher
from northcua.agentkernel.tools.surface import ToolSurfaceService
from northcua.controller.execution.remote_dispatcher import RemoteCommandDispatcher
from northcua.controller.storage.file_artifacts import file_artifact_ref
from northcua.core.enums import ResultStatus
from northcua.core.json_types import JsonObject
from northcua.core.models import CommandExecutionResultRecord
from northcua_protocol.artifacts import ArtifactKind, ArtifactRef, ArtifactScope, RetentionPolicy

if TYPE_CHECKING:
    from northcua.agentkit.model_step import AgentKitModelStepExecutor

logger = logging.getLogger(__name__)

_WEB_EVALUATOR_KIND = "web_round_evaluator"
_DEFAULT_EVALUATOR_RETRY_DELAYS_SECONDS = (15.0, 45.0, 90.0)
_EVALUATION_ARTIFACT_MIME_TYPES: dict[str, tuple[ArtifactKind, str]] = {
    "bundle.json": (ArtifactKind.JSON, "application/json"),
    "evidence_manifest.json": (ArtifactKind.JSON, "application/json"),
    "final_report.json": (ArtifactKind.JSON, "application/json"),
    "llm_trace.jsonl": (ArtifactKind.TEXT, "application/jsonl"),
    "context_summary_llm_trace.jsonl": (ArtifactKind.TEXT, "application/jsonl"),
    "context_compaction.jsonl": (ArtifactKind.TEXT, "application/jsonl"),
    "runtime.jsonl": (ArtifactKind.TEXT, "application/jsonl"),
    "tool_trace.jsonl": (ArtifactKind.TEXT, "application/jsonl"),
}


@dataclass(slots=True)
class WebEvaluatorOutcome:
    """Controller-facing summary of one Web evaluator run."""

    report: dict[str, object] = field(default_factory=dict)
    report_path: str = ""
    artifacts: list[ArtifactRef] = field(default_factory=list)
    error: str = ""


def persist_web_evaluation_result(artifact_store: object, outcome: WebEvaluatorOutcome) -> None:
    """Mirror the evaluator summary into the Web agent ``result.json``.

    The controller terminal payload already carries evaluator fields, but the
    artifact tree is also consumed directly by experiment reports, viewers, and
    replay tools.  Keep that file in sync without touching the full evaluator
    artifacts or changing the main Web trace format.
    """

    if not outcome.report:
        return
    result_path = getattr(artifact_store, "result_path", None)
    if result_path is None:
        return
    path = Path(result_path)
    try:
        payload = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    except (OSError, json.JSONDecodeError):
        logger.warning("web_evaluator_result_json_read_failed", extra={"path": str(path)}, exc_info=True)
        return
    if not isinstance(payload, dict):
        payload = {"result": payload}
    payload["evaluation_report"] = outcome.report
    if outcome.report_path:
        payload["evaluation_report_path"] = outcome.report_path
    try:
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    except OSError:
        logger.warning("web_evaluator_result_json_write_failed", extra={"path": str(path)}, exc_info=True)


def web_evaluation_enabled(app: object) -> bool:
    """Return whether post-run Web evaluation is enabled for this app config."""

    evaluation = getattr(getattr(app, "app_config", None), "evaluation", None)
    schedule = getattr(evaluation, "schedule", "")
    schedule_value = getattr(schedule, "value", schedule)
    return bool(getattr(evaluation, "enabled", False)) and str(schedule_value) != "disabled"


def web_evaluation_fail_open(app: object) -> bool:
    """Return the configured failure policy for post-run Web evaluation."""

    evaluation = getattr(getattr(app, "app_config", None), "evaluation", None)
    return bool(getattr(evaluation, "fail_open", True))


async def run_web_round_evaluator(
    app: object,
    *,
    round_num: int = 1,
    remote: bool = True,
    task_id: str = "",
    remote_dispatcher: RemoteCommandDispatcher | None = None,
    command_timeout_seconds: float = 10.0,
    client_tool_dispatcher: EvaluatorClientToolDispatcher | None = None,
) -> WebEvaluatorOutcome:
    """Run the configured Web evaluator for a completed Web agent session.

    Args:
        app: ``WebAgentApplication``-like instance produced by the remote runner.
        remote: Whether evaluator tool calls should be externalized.
        task_id: Controller task id used for evaluator artifact ids.
        remote_dispatcher: Bound controller/device dispatcher for live probes.
        command_timeout_seconds: Timeout used for evaluator live-probe commands.
        client_tool_dispatcher: Optional prebuilt evaluator client dispatcher.

    Returns:
        A typed controller summary with parsed final report and artifact refs.
    """

    artifact_store = getattr(app, "artifact_store")
    artifact_root = Path(getattr(artifact_store, "root"))
    enable_live_probe = web_evaluation_live_probe_enabled(app)
    allow_client_mutating_tools = web_evaluation_mutating_tools_enabled(app)
    dispatcher = client_tool_dispatcher or _remote_evaluator_dispatcher(
        enabled=enable_live_probe,
        remote=remote,
        remote_dispatcher=remote_dispatcher,
        command_timeout_seconds=command_timeout_seconds,
    )
    timeout_seconds = _evaluation_round_timeout_seconds(app)
    await _evaluate_artifact_root_with_retries(
        app,
        artifact_root=artifact_root,
        round_num=round_num,
        remote=remote,
        enable_live_probe=enable_live_probe,
        allow_client_mutating_tools=allow_client_mutating_tools,
        client_tool_dispatcher=dispatcher,
        timeout_seconds=timeout_seconds,
        task_id=task_id,
    )
    report_path = artifact_root / "evaluations" / _WEB_EVALUATOR_KIND / f"round_{round_num:04d}" / "final_report.json"
    if not _path_has_bytes(report_path):
        raise RuntimeError(f"Web evaluator did not produce a report for round {round_num}")
    return WebEvaluatorOutcome(
        report=_read_json_object(report_path) if report_path is not None else {},
        report_path=str(report_path) if report_path is not None else "",
        artifacts=collect_web_evaluation_artifacts(
            artifact_root=artifact_root,
            task_id=task_id or str(getattr(artifact_store, "session_id", "web-session")),
            evaluator_kind=_WEB_EVALUATOR_KIND,
        ),
    )


def web_evaluation_live_probe_enabled(app: object) -> bool:
    """Return whether Web evaluator live browser probes are enabled."""

    tools = _evaluation_tools(app)
    return bool(getattr(tools, "enabled", True)) and bool(getattr(tools, "allow_client_live_probe", False))


def web_evaluation_mutating_tools_enabled(app: object) -> bool:
    """Return whether Web evaluator client-side mutating tools may run."""

    tools = _evaluation_tools(app)
    return web_evaluation_live_probe_enabled(app) and bool(getattr(tools, "allow_client_mutating_tools", False))


def _evaluation_round_timeout_seconds(app: object) -> float:
    evaluation = getattr(getattr(app, "app_config", None), "evaluation", None)
    raw_timeout = getattr(evaluation, "round_timeout_seconds", 300.0)
    try:
        timeout = float(raw_timeout)
    except (TypeError, ValueError):
        timeout = 300.0
    return timeout if timeout > 0 else 300.0


async def _evaluate_artifact_root_with_retries(
    app: object,
    *,
    artifact_root: Path,
    round_num: int,
    remote: bool,
    enable_live_probe: bool,
    allow_client_mutating_tools: bool,
    client_tool_dispatcher: EvaluatorClientToolDispatcher | None,
    timeout_seconds: float,
    task_id: str,
) -> None:
    from northcua.agents.web.evaluation.service import WebRoundEvaluatorService

    retry_delays = _evaluation_retry_delays_seconds(app)
    max_attempts = len(retry_delays) + 1
    for attempt_index in range(max_attempts):
        try:
            service = WebRoundEvaluatorService(model_step_executor=_build_model_step_executor(app))
            await asyncio.wait_for(
                service.evaluate_artifact_root(
                    artifact_root,
                    round_num=round_num,
                    max_steps=getattr(app.app_config.evaluation, "max_steps", 12),
                    force_finalizer_after_steps=getattr(app.app_config.evaluation, "force_finalizer_after_steps", 8),
                    remote=remote,
                    enable_live_probe=enable_live_probe,
                    allow_client_mutating_tools=allow_client_mutating_tools,
                    client_tool_dispatcher=client_tool_dispatcher,
                ),
                timeout=timeout_seconds,
            )
            return
        except TimeoutError as exc:
            raise TimeoutError(f"Web evaluator timed out after {timeout_seconds:.2f}s") from exc
        except Exception as exc:
            if attempt_index >= len(retry_delays) or not _is_transient_evaluator_llm_error(exc):
                raise
            delay = retry_delays[attempt_index] + _stable_retry_jitter_seconds(task_id, attempt_index)
            logger.warning(
                "web_evaluator_transient_llm_error_retrying",
                extra={
                    "task_id": task_id,
                    "attempt": attempt_index + 1,
                    "max_attempts": max_attempts,
                    "delay_seconds": delay,
                    "error_type": type(exc).__name__,
                    "error": str(exc)[:240],
                },
            )
            await asyncio.sleep(delay)


def _evaluation_retry_delays_seconds(app: object) -> tuple[float, ...]:
    evaluation = getattr(getattr(app, "app_config", None), "evaluation", None)
    configured = getattr(evaluation, "retry_delays_seconds", None)
    if configured is None:
        configured = os.environ.get("NORTHCUA_WEB_EVALUATOR_RETRY_DELAYS_SECONDS")
    if configured is None:
        return _DEFAULT_EVALUATOR_RETRY_DELAYS_SECONDS
    if isinstance(configured, str):
        parts: list[object] = [part.strip() for part in configured.split(",")]
    elif isinstance(configured, (list, tuple)):
        parts = list(configured)
    else:
        parts = [configured]
    delays: list[float] = []
    for part in parts:
        if not isinstance(part, (int, float, str)):
            continue
        try:
            delay = float(part)
        except (TypeError, ValueError):
            continue
        if delay >= 0:
            delays.append(delay)
    return tuple(delays)


def _stable_retry_jitter_seconds(task_id: str, attempt_index: int) -> float:
    if not task_id:
        return 0.0
    return float((sum(task_id.encode("utf-8")) + attempt_index * 17) % 7)


def _is_transient_evaluator_llm_error(exc: Exception) -> bool:
    text = f"{type(exc).__name__}: {exc}".lower()
    transient_markers = (
        "没有可用token",
        "no available token",
        "no available tokens",
        "rate limit",
        "too many requests",
        "temporarily unavailable",
        "overloaded",
        "capacity",
        "server error",
        "internal server error",
        "bad gateway",
        "service unavailable",
        " 429",
        " 500",
        " 502",
        " 503",
        "status code: 429",
        "status code: 500",
        "status code: 502",
        "status code: 503",
    )
    return any(marker in text for marker in transient_markers)


def _evaluation_tools(app: object) -> object:
    evaluation = getattr(getattr(app, "app_config", None), "evaluation", None)
    return getattr(evaluation, "tools", None)


def _remote_evaluator_dispatcher(
    *,
    enabled: bool,
    remote: bool,
    remote_dispatcher: RemoteCommandDispatcher | None,
    command_timeout_seconds: float,
) -> EvaluatorClientToolDispatcher | None:
    if not enabled:
        return None
    if not remote:
        return None
    if remote_dispatcher is None:
        raise ValueError("Web live probe evaluation requires the bound remote command dispatcher.")
    return RemoteEvaluatorClientToolDispatcher(
        _remote_evaluator_tool_dispatcher(
            dispatcher=remote_dispatcher,
            command_timeout_seconds=command_timeout_seconds,
        )
    )


def _remote_evaluator_tool_dispatcher(
    *,
    dispatcher: RemoteCommandDispatcher,
    command_timeout_seconds: float,
) -> Callable[[list[JsonObject]], Awaitable[object]]:
    async def _dispatch(pending_tool_calls: list[JsonObject]) -> object:
        from northcua.transport.ws.result_blocks import RemoteToolDispatchResult, command_result_to_tool_result_block

        command_results: list[CommandExecutionResultRecord] = []
        for call in pending_tool_calls:
            call_id = str(call.get("id") or "")
            tool_name = str(call.get("name") or "")
            parameters = _json_object(call.get("input"))
            try:
                result = await dispatcher.dispatch(
                    tool_id=tool_name,
                    parameters=parameters,
                    command_id=call_id,
                    timeout_seconds=command_timeout_seconds,
                )
                command_result = command_result_record(call_id, result)
            except Exception as exc:
                logger.warning(
                    "web_evaluator_remote_live_probe_dispatch_failed",
                    extra={
                        "tool_name": tool_name,
                        "call_id": call_id,
                        "parameter_keys": sorted(parameters),
                        "error": str(exc),
                    },
                    exc_info=True,
                )
                command_result = _failed_command_result_record(call_id=call_id, error=str(exc))
            command_results.append(command_result)
        return RemoteToolDispatchResult(
            tool_blocks=[command_result_to_tool_result_block(result) for result in command_results],
            command_results=command_results,
        )

    return _dispatch


def collect_web_evaluation_artifacts(
    *,
    artifact_root: Path,
    task_id: str,
    evaluator_kind: str = _WEB_EVALUATOR_KIND,
) -> list[ArtifactRef]:
    """Catalog evaluator artifacts without copying bytes into a second store."""

    evaluation_root = artifact_root / "evaluations" / evaluator_kind
    refs: list[ArtifactRef] = []
    for path in sorted(item for item in evaluation_root.rglob("*") if item.name in _EVALUATION_ARTIFACT_MIME_TYPES):
        if not _path_has_bytes(path):
            continue
        kind, mime_type = _EVALUATION_ARTIFACT_MIME_TYPES[path.name]
        label = "-".join(path.relative_to(evaluation_root).with_suffix("").parts)
        materializer_label = _safe_artifact_label(f"{evaluator_kind}-{label}")
        refs.append(
            file_artifact_ref(
                artifact_id=f"{task_id}-evaluation-{materializer_label}",
                kind=kind,
                path=path,
                mime_type=mime_type,
                artifact_root=artifact_root,
                artifact_scope=ArtifactScope.TASK,
                retention_policy=RetentionPolicy.TASK,
            )
        )
    return refs


def _latest_evaluation_file(*, artifact_root: Path, evaluator_kind: str, filename: str) -> Path | None:
    evaluation_root = artifact_root / "evaluations" / evaluator_kind
    candidates = [path for path in evaluation_root.glob(f"round_*/{filename}") if _path_has_bytes(path)]
    if not candidates:
        return None
    return max(candidates, key=lambda path: (path.parent.name, path.stat().st_mtime, str(path)))


def _safe_artifact_label(value: str) -> str:
    return "".join(ch if ch.isalnum() or ch in {"-", "_", "."} else "-" for ch in value).strip("-") or "artifact"


def _path_has_bytes(path: Path) -> bool:
    return path.exists() and path.is_file() and path.stat().st_size > 0


def _failed_command_result_record(*, call_id: str, error: str) -> CommandExecutionResultRecord:
    return CommandExecutionResultRecord(
        call_id=call_id,
        status=ResultStatus.FAILURE,
        error=error,
        result={"status": "error", "error": error},
    )


def _json_object(value: object) -> JsonObject:
    if isinstance(value, dict):
        return {str(key): item for key, item in value.items()}
    return {}


def _build_model_step_executor(app: object) -> AgentKitModelStepExecutor:
    from northcua.agentkit.model_step import AgentKitModelStepComponents, AgentKitModelStepExecutor

    return AgentKitModelStepExecutor(
        AgentKitModelStepComponents(
            app_config=getattr(app, "app_config"),
            agent_config=getattr(app, "agent_config"),
            execution_transport=getattr(app, "execution_transport"),
            user_id=str(getattr(getattr(app, "app_config"), "execution").user_id),
            transport_capabilities=getattr(app, "transport_capabilities"),
            tool_surface_service=ToolSurfaceService(),
        )
    )


def _read_json_object(path: Path) -> dict[str, object]:
    try:
        payload: Any = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        logger.warning("web_evaluator_report_read_failed", extra={"path": str(path)})
        return {}
    return payload if isinstance(payload, dict) else {}
