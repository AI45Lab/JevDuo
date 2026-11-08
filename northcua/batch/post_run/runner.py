"""Run post-run hooks against a live sandbox before lease release."""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

from northcua.batch.post_run.contracts import (
    PostRunArtifactSpec,
    PostRunHookResult,
    PostRunHookSpec,
    PostRunHookStatus,
)
from northcua.sandboxes.contracts import SandboxCommandResult, SandboxCommandSpec, SandboxLease
from northcua.sandboxes.lifecycle.task_lease import SandboxTaskLeaseManager
from northcua_protocol.capabilities import JsonObject

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from northcua.evaluation.script.runtime import ScriptEvaluatorRuntimePort


@dataclass(frozen=True, slots=True)
class _ArtifactCollection:
    paths: dict[str, str] = field(default_factory=dict)
    errors: dict[str, str] = field(default_factory=dict)


class PostRunHookRunner:
    """Execute typed post-run hooks inside one task sandbox."""

    def __init__(self, *, output_dir: Path) -> None:
        self._output_dir = output_dir

    async def run_all(
        self,
        *,
        hooks: list[PostRunHookSpec],
        manager: SandboxTaskLeaseManager,
        lease: SandboxLease,
        task_id: str,
        attempt: int,
    ) -> list[PostRunHookResult]:
        """Run hooks sequentially and return durable result contracts.

        Args:
            hooks: Hook specs to run in declaration order.
            manager: Lease manager used for sandbox commands and file copies.
            lease: Live sandbox lease owned by the just-finished attempt.
            task_id: Batch task id used for output paths and logs.
            attempt: Attempt number used for output paths and logs.

        Returns:
            One normalized hook result per input hook.
        """

        results: list[PostRunHookResult] = []
        for hook in hooks:
            results.append(
                await self.run_one(
                    hook=hook,
                    manager=manager,
                    lease=lease,
                    task_id=task_id,
                    attempt=attempt,
                )
            )
        return results

    async def run_one(
        self,
        *,
        hook: PostRunHookSpec,
        manager: SandboxTaskLeaseManager,
        lease: SandboxLease,
        task_id: str,
        attempt: int,
    ) -> PostRunHookResult:
        """Run one hook and normalize command/json/artifact outcomes.

        Args:
            hook: Hook spec to execute.
            manager: Lease manager used for the sandbox command and artifacts.
            lease: Live sandbox lease owned by the just-finished attempt.
            task_id: Batch task id used for output paths and logs.
            attempt: Attempt number used for output paths and logs.

        Returns:
            A persisted result contract that records command, parse, and
            artifact outcomes without raising into the batch cleanup path.
        """

        if hook.argv:
            from northcua.evaluation.script.runtime import SandboxLeaseScriptEvaluatorRuntime

            return await self.run_one_runtime(
                hook=hook,
                runtime=SandboxLeaseScriptEvaluatorRuntime(manager=manager, lease=lease),
                task_id=task_id,
                attempt=attempt,
            )
        command = hook.command

        async def execute() -> SandboxCommandResult:
            return await manager.run_command(
                lease,
                SandboxCommandSpec(
                    command=command,
                    label=hook.label or hook.hook_id,
                    timeout_seconds=hook.timeout_seconds,
                    cwd=hook.cwd,
                    env=hook.env,
                    allow_failure=hook.allow_command_failure,
                ),
            )

        async def download(remote_path: str, local_path: Path) -> None:
            await manager.download_file(lease, remote=remote_path, local=local_path)

        return await self._run_one(
            hook=hook,
            execute=execute,
            download=download,
            task_id=task_id,
            attempt=attempt,
            redact_runtime_errors=False,
        )

    async def run_one_runtime(
        self,
        *,
        hook: PostRunHookSpec,
        runtime: ScriptEvaluatorRuntimePort,
        task_id: str,
        attempt: int,
    ) -> PostRunHookResult:
        """Run one evaluator through an externally owned argv-safe runtime."""

        if not hook.argv:
            raise ValueError("external script evaluation requires argv")
        if hook.file_uploads:
            await runtime.stage_files(hook.file_uploads)

        async def execute() -> SandboxCommandResult:
            return await runtime.run_argv(
                tuple(hook.argv),
                timeout_seconds=hook.timeout_seconds,
                cwd=hook.cwd,
                env=dict(hook.env),
                allow_failure=hook.allow_command_failure,
            )

        async def download(remote_path: str, local_path: Path) -> None:
            await runtime.download_file(remote_path, local_path)

        return await self._run_one(
            hook=hook,
            execute=execute,
            download=download,
            task_id=task_id,
            attempt=attempt,
            redact_runtime_errors=True,
        )

    async def _run_one(
        self,
        *,
        hook: PostRunHookSpec,
        execute: Callable[[], Awaitable[SandboxCommandResult]],
        download: Callable[[str, Path], Awaitable[None]],
        task_id: str,
        attempt: int,
        redact_runtime_errors: bool,
    ) -> PostRunHookResult:
        started = time.monotonic()
        result: SandboxCommandResult | None = None
        for retry_index in range(hook.infra_retries + 1):
            try:
                result = await execute()
                break
            except Exception as exc:  # noqa: BLE001 - evaluators must report typed errors.
                log_fields = {
                    "task_id": task_id,
                    "attempt": attempt,
                    "hook_id": hook.hook_id,
                    "error_type": type(exc).__name__,
                    "infra_retry": retry_index,
                }
                if retry_index < hook.infra_retries:
                    logger.info("post-run hook transport failed; retrying", extra=log_fields)
                    if hook.infra_retry_backoff_seconds:
                        await asyncio.sleep(hook.infra_retry_backoff_seconds)
                    continue
                if redact_runtime_errors:
                    logger.warning("external script evaluator runtime failed", extra=log_fields)
                else:
                    logger.warning("post-run hook command raised", extra=log_fields, exc_info=True)
                return PostRunHookResult(
                    hook_id=hook.hook_id,
                    kind=hook.kind,
                    label=hook.label,
                    status=PostRunHookStatus.ERROR,
                    score_threshold=hook.score_threshold,
                    duration_seconds=round(time.monotonic() - started, 3),
                    error=(
                        f"script evaluator runtime failed [{type(exc).__name__}]" if redact_runtime_errors else str(exc)
                    ),
                    required_evidence_ids=list(hook.required_evidence_ids),
                    metadata=hook.metadata,
                )
        if result is None:
            raise RuntimeError("post-run hook execution ended without a result")
        parsed = _parse_result_json(result.stdout)
        explicit_status = _status_from_payload(parsed, key=hook.status_key)
        score = _score_from_payload(parsed, key=hook.score_key)
        passed = _passed_from_payload(parsed, key=hook.passed_key)
        if passed is None and score is not None:
            passed = score >= hook.score_threshold
        if explicit_status == PostRunHookStatus.PASSED and passed is None:
            passed = True
        elif explicit_status == PostRunHookStatus.FAILED and passed is None:
            passed = False
        elif explicit_status in {PostRunHookStatus.ERROR, PostRunHookStatus.TIMED_OUT}:
            passed = None
        status = explicit_status or _status_for_command_result(
            result=result,
            passed=passed,
            allow_failure=hook.allow_command_failure,
        )
        artifacts = await self._collect_artifacts(
            artifacts=hook.artifacts,
            download=download,
            task_id=task_id,
            attempt=attempt,
            hook_id=hook.hook_id,
            redact_runtime_errors=redact_runtime_errors,
        )
        if artifacts.errors:
            status = PostRunHookStatus.ERROR
            passed = None
        return PostRunHookResult(
            hook_id=hook.hook_id,
            kind=hook.kind,
            label=hook.label,
            status=status,
            passed=passed,
            score=score,
            score_threshold=hook.score_threshold,
            exit_code=result.exit_code,
            timed_out=result.timed_out,
            duration_seconds=round(time.monotonic() - started, 3),
            stdout=result.stdout,
            stderr=result.stderr,
            error=_error_for_result(result=result, status=status, parsed=parsed, artifact_errors=artifacts.errors),
            result_json=parsed,
            artifacts=artifacts.paths,
            artifact_errors=artifacts.errors,
            required_evidence_ids=list(hook.required_evidence_ids),
            metadata=hook.metadata,
        )

    async def _collect_artifacts(
        self,
        *,
        artifacts: list[PostRunArtifactSpec],
        download: Callable[[str, Path], Awaitable[None]],
        task_id: str,
        attempt: int,
        hook_id: str,
        redact_runtime_errors: bool,
    ) -> _ArtifactCollection:
        collected: dict[str, str] = {}
        errors: dict[str, str] = {}
        for artifact in artifacts:
            name = artifact.name or Path(artifact.remote_path).name
            local = self._output_dir / "post-run" / task_id / f"attempt-{attempt}" / hook_id / name
            try:
                await download(artifact.remote_path, local)
            except Exception as exc:  # noqa: BLE001 - optional artifact failures are reported by omission.
                if artifact.required:
                    error_fields = {
                        "task_id": task_id,
                        "attempt": attempt,
                        "hook_id": hook_id,
                        "remote_path": artifact.remote_path,
                        "error_type": type(exc).__name__,
                    }
                    if redact_runtime_errors:
                        errors[artifact.remote_path] = f"artifact download failed [{type(exc).__name__}]"
                        logger.warning("external evaluator artifact download failed", extra=error_fields)
                    else:
                        errors[artifact.remote_path] = str(exc)
                        logger.warning(
                            "required post-run artifact download failed",
                            extra=error_fields,
                            exc_info=True,
                        )
                continue
            collected[artifact.remote_path] = str(local)
        return _ArtifactCollection(paths=collected, errors=errors)


def _parse_result_json(stdout: str) -> JsonObject:
    for line in reversed(stdout.splitlines()):
        text = line.strip()
        if not text:
            continue
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict):
            return parsed
    return {}


def _score_from_payload(payload: JsonObject, *, key: str) -> float | None:
    value = payload.get(key)
    if isinstance(value, bool):
        return None
    if isinstance(value, int | float):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value)
        except ValueError:
            return None
    return None


def _passed_from_payload(payload: JsonObject, *, key: str) -> bool | None:
    value = payload.get(key)
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"true", "yes", "1", "passed"}:
            return True
        if normalized in {"false", "no", "0", "failed"}:
            return False
    return None


def _status_from_payload(payload: JsonObject, *, key: str) -> PostRunHookStatus | None:
    value = payload.get(key)
    if not isinstance(value, str):
        return None
    normalized = value.strip().lower()
    for status in PostRunHookStatus:
        if normalized == status.value:
            return status
    return None


def _status_for_command_result(
    *,
    result: SandboxCommandResult,
    passed: bool | None,
    allow_failure: bool,
) -> PostRunHookStatus:
    if result.timed_out:
        return PostRunHookStatus.TIMED_OUT
    if result.exit_code != 0 and not allow_failure:
        return PostRunHookStatus.ERROR
    if passed is True:
        return PostRunHookStatus.PASSED
    if passed is False:
        return PostRunHookStatus.FAILED
    return PostRunHookStatus.ERROR


def _error_for_result(
    *,
    result: SandboxCommandResult,
    status: PostRunHookStatus,
    parsed: JsonObject,
    artifact_errors: dict[str, str] | None = None,
) -> str:
    if artifact_errors:
        missing = ", ".join(sorted(artifact_errors))
        return f"required post-run artifact download failed: {missing}"
    if status in {PostRunHookStatus.PASSED, PostRunHookStatus.FAILED}:
        return ""
    payload_error = parsed.get("error")
    if isinstance(payload_error, str) and payload_error:
        return payload_error
    if result.timed_out:
        return "post-run hook timed out"
    if result.exit_code != 0:
        return result.stderr or result.stdout or f"post-run hook exited {result.exit_code}"
    if not parsed:
        return "post-run hook did not emit a JSON result"
    return "post-run hook result could not be interpreted"
