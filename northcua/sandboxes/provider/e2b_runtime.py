"""E2B SDK runtime helpers for the sandbox provider facade."""

from __future__ import annotations

import asyncio
import base64
import contextlib
import importlib
import json
import logging
import os
import shlex
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Protocol, cast
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from northcua.sandboxes.contracts import (
    SandboxCommandResult,
    SandboxCommandSpec,
    SandboxHandle,
    SandboxInventoryItem,
    SandboxInventoryState,
    SandboxProviderSpec,
)
from northcua.sandboxes.provider.e2b_executor import (
    E2BExecutorCall,
    cancel_e2b_reaper_campaign,
    executor_telemetry,
    submit_e2b_reaper_campaign,
    submit_e2b_reaper_fallback_job,
    submit_e2b_reaper_job,
)
from northcua.sandboxes.provider.e2b_executor_runtime import E2BExecutorRuntimeMixin
from northcua.sandboxes.provider.e2b_runtime_support import control_request_timeout as _control_request_timeout
from northcua.sandboxes.provider.e2b_runtime_support import create_attempt_spec as _create_attempt_spec
from northcua.sandboxes.provider.e2b_runtime_support import int_env as _int_env
from northcua.sandboxes.provider.e2b_runtime_support import is_file_write_unsupported as _is_file_write_unsupported
from northcua.sandboxes.provider.e2b_runtime_support import is_retryable_e2b_error as _is_retryable_e2b_error
from northcua.sandboxes.provider.e2b_runtime_support import (
    is_unexpected_request_timeout_keyword as _is_unexpected_request_timeout_keyword,
)
from northcua.sandboxes.provider.e2b_runtime_support import metadata_matches as _metadata_matches
from northcua.sandboxes.provider.e2b_runtime_support import orphan_match_metadata as _orphan_match_metadata
from northcua.sandboxes.provider.e2b_runtime_support import retry_delay_seconds as _retry_delay_seconds
from northcua.sandboxes.provider.e2b_template_runtime import E2BTemplateRuntimeMixin

logger = logging.getLogger(__name__)
_BUILD_HEARTBEAT_SECONDS = 30.0
_CREATE_RETRY_BACKOFF_SECONDS = (1.0, 3.0, 7.0)
_CREATE_RETRY_JITTER_SECONDS = 0.5
_ORPHAN_RECONCILE_DELAYS_SECONDS = (0.0, 5.0, 15.0)
_ORPHAN_INVENTORY_REQUEST_TIMEOUT_SECONDS = 30
_LATE_CREATE_TERMINATE_RETRY_BACKOFF_SECONDS = (1.0, 3.0)
_SHELL_UPLOAD_RAW_CHUNK_BYTES = 96 * 1024
_SHELL_UPLOAD_SINGLE_COMMAND_RAW_BYTES = 64 * 1024
_DEFAULT_UPLOAD_REQUEST_TIMEOUT_SECONDS = 300
_DEFAULT_COMMAND_REQUEST_TIMEOUT_SECONDS = 180
_DEFAULT_COMMAND_REQUEST_TIMEOUT_CAP_SECONDS = 300
_DEFAULT_COMMAND_REQUEST_TIMEOUT_MARGIN_SECONDS = 120


class _E2BCommandResultLike(Protocol):
    exit_code: int
    stdout: str
    stderr: str


class _E2BCommandRunnerLike(Protocol):
    def run(
        self,
        command: str,
        *,
        timeout: float | int,
        background: bool = False,
        request_timeout: float | None = None,
    ) -> _E2BCommandResultLike:
        """Run an E2B command.

        Args:
            command: Shell command passed to the E2B SDK.
            timeout: Provider-side command timeout in seconds.
        background: Whether to ask the SDK to launch the command detached.

        Returns:
            SDK command result object with exit code and streams.
        """
        ...


class _E2BFilesLike(Protocol):
    def write(
        self,
        path: str,
        content: bytes | str,
        *,
        request_timeout: float | None = None,
    ) -> None:
        """Write a file into the sandbox.

        Args:
            path: Remote sandbox destination path.
            content: Bytes or text content to write.
            request_timeout: HTTP request timeout supported by current E2B SDKs.
        """
        ...

    def read(
        self,
        path: str,
        *,
        format: str = "text",
        request_timeout: float | None = None,
    ) -> bytes | bytearray | str:
        """Read a file from the sandbox.

        Args:
            path: Remote sandbox source path.
            format: E2B file read format. Binary downloads pass ``bytes`` so
                screenshots and archives are not decoded as text.
            request_timeout: HTTP request timeout supported by current E2B SDKs.

        Returns:
            Bytes or text returned by the SDK.
        """
        ...


class _E2BSandboxLike(Protocol):
    sandbox_id: str
    id: str
    stream_url: str
    commands: _E2BCommandRunnerLike
    files: _E2BFilesLike

    def set_timeout(self, timeout: int, *, request_timeout: float | None = None) -> None:
        """Set provider-side sandbox lifetime in seconds."""

        ...


class _E2BSandboxClassLike(Protocol):
    @classmethod
    def create(cls, **kwargs: object) -> _E2BSandboxLike:
        """Create a sandbox from SDK keyword arguments.

        Args:
            **kwargs: Provider-specific create options such as template,
                timeout, resolution, and DPI.

        Returns:
            SDK sandbox instance.
        """
        ...

    @classmethod
    def connect(cls, sandbox_id: str, **kwargs: object) -> _E2BSandboxLike:
        """Connect to an existing sandbox.

        Args:
            sandbox_id: Existing E2B sandbox id.

        Returns:
            SDK sandbox instance.
        """
        ...


class E2BProviderRuntimeMixin(E2BExecutorRuntimeMixin, E2BTemplateRuntimeMixin):
    """Private SDK, transfer, command, template, and inventory operations."""

    _sandboxes: dict[str, _E2BSandboxLike]

    def _load_sdk(self, *, desktop: bool) -> _E2BSandboxClassLike:
        """Load the requested E2B SDK class lazily."""

        try:
            if desktop:
                return cast(_E2BSandboxClassLike, importlib.import_module("e2b_desktop").Sandbox)
            return cast(_E2BSandboxClassLike, importlib.import_module("e2b").Sandbox)
        except ModuleNotFoundError as exc:
            raise RuntimeError(
                "Install E2B support with `uv sync --extra e2b` or `pip install northcua[e2b]`."
            ) from exc

    def _load_inventory_sdk(self) -> None:
        """Validate that the base E2B SDK is installed for inventory calls."""

        try:
            importlib.import_module("e2b")
        except ModuleNotFoundError as exc:
            raise RuntimeError(
                "Install E2B support with `uv sync --extra e2b` or `pip install northcua[e2b]`."
            ) from exc

    def _create_sync(self, spec: SandboxProviderSpec) -> _E2BSandboxLike:
        sandbox_cls = self._load_sdk(desktop=spec.desktop)
        kwargs: dict[str, object] = {
            "template": spec.template,
            "timeout": spec.resources.timeout_seconds,
            "request_timeout": _control_request_timeout("NORTHCUA_E2B_CREATE_REQUEST_TIMEOUT_SECONDS"),
            **_sdk_proxy_kwargs(),
        }
        if spec.desktop:
            kwargs.update({"resolution": spec.resolution, "dpi": spec.dpi})
        if spec.metadata:
            kwargs["metadata"] = {str(key): str(value) for key, value in spec.metadata.items()}
        try:
            return sandbox_cls.create(**kwargs)
        except TypeError as exc:
            if not _is_unexpected_request_timeout_keyword(exc):
                raise
            kwargs.pop("request_timeout", None)
            return sandbox_cls.create(**kwargs)

    def _list_sandboxes_sync(self) -> list[dict[str, object]]:
        api_key = os.getenv("E2B_API_KEY", "")
        if not api_key:
            raise RuntimeError("E2B_API_KEY is required for E2B sandbox inventory")
        api_url = os.getenv("E2B_API_URL", "https://api.e2b.app").rstrip("/")
        timeout_seconds = _int_env("NORTHCUA_E2B_LIST_TIMEOUT_SECONDS", 15)
        request = Request(
            f"{api_url}/sandboxes",
            headers={"X-API-Key": api_key, "Accept": "application/json"},
            method="GET",
        )
        try:
            with urlopen(request, timeout=timeout_seconds) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except HTTPError as exc:
            raise RuntimeError(f"E2B sandbox inventory failed: HTTP {exc.code}") from exc
        except URLError as exc:
            raise RuntimeError(f"E2B sandbox inventory failed: {exc.reason}") from exc
        if isinstance(payload, list):
            return [row for row in payload if isinstance(row, dict)]
        if isinstance(payload, dict):
            sandboxes = payload.get("sandboxes") or payload.get("items") or payload.get("data") or []
            if isinstance(sandboxes, list):
                return [row for row in sandboxes if isinstance(row, dict)]
        raise RuntimeError("E2B sandbox inventory response has unexpected shape")

    def _list_matching_orphan_sandboxes_sync(
        self,
        expected: dict[str, str],
    ) -> list[object]:
        """List a tiny, exact create-token inventory through the SDK paginator."""

        e2b_module = importlib.import_module("e2b")
        sandbox_cls = e2b_module.Sandbox
        query = e2b_module.SandboxQuery(
            metadata={"create_call_token": expected["create_call_token"]},
            state=[e2b_module.SandboxState.RUNNING, e2b_module.SandboxState.PAUSED],
        )
        paginator = sandbox_cls.list(
            query=query,
            limit=20,
            **_sdk_proxy_kwargs(),
            request_timeout=min(
                _control_request_timeout("NORTHCUA_E2B_LIST_REQUEST_TIMEOUT_SECONDS"),
                _ORPHAN_INVENTORY_REQUEST_TIMEOUT_SECONDS,
            ),
        )
        rows: list[object] = []
        pages = 0
        while paginator.has_next:
            if pages >= 3:
                raise RuntimeError("E2B exact orphan inventory exceeded 3 pages")
            rows.extend(paginator.next_items())
            pages += 1
        return rows

    def _kill_sandbox_id_sync(self, sandbox_id: str) -> None:
        """Terminate one exact orphan without requiring a connect round trip."""

        sandbox_cls = importlib.import_module("e2b").Sandbox
        try:
            sandbox_cls.kill(
                sandbox_id,
                **_sdk_proxy_kwargs(),
                request_timeout=_control_request_timeout("NORTHCUA_E2B_TERMINATE_REQUEST_TIMEOUT_SECONDS"),
            )
        except TypeError as exc:
            if not _is_unexpected_request_timeout_keyword(exc):
                raise
            sandbox_cls.kill(sandbox_id)

    def _connect_sync(self, spec: SandboxProviderSpec) -> _E2BSandboxLike:
        sandbox_cls = self._load_sdk(desktop=spec.desktop)
        kwargs: dict[str, object] = {
            "request_timeout": _control_request_timeout("NORTHCUA_E2B_CONNECT_REQUEST_TIMEOUT_SECONDS"),
            **_sdk_proxy_kwargs(),
        }
        try:
            return sandbox_cls.connect(spec.sandbox_id, **kwargs)
        except TypeError as exc:
            if not _is_unexpected_request_timeout_keyword(exc):
                raise
            return sandbox_cls.connect(spec.sandbox_id)

    async def _create_sandbox_once(self, spec: SandboxProviderSpec) -> _E2BSandboxLike:
        """Create a sandbox with provider-local retries for transport flakes.

        E2B create can fail while the SDK verifies a newly allocated sandbox,
        especially on TLS/HTTP stream interruptions. Those transport failures
        should not consume an entire case retry budget. Non-transport failures
        still surface immediately to the batch attempt policy.
        """

        max_attempts = _int_env("NORTHCUA_E2B_CREATE_MAX_ATTEMPTS", len(_CREATE_RETRY_BACKOFF_SECONDS) + 1)
        max_attempts = max(1, max_attempts)
        last_exc: Exception | None = None
        for attempt in range(1, max_attempts + 1):
            attempt_spec = _create_attempt_spec(spec)
            self._progress(
                "e2b create: start",
                template=spec.template,
                desktop=spec.desktop,
                attempt=attempt,
                max_attempts=max_attempts,
            )
            try:
                create_task = asyncio.create_task(
                    self._run_executor_call(
                        lane="control",
                        operation="create",
                        call=lambda: self._create_sync(attempt_spec),
                        late_result_handler=lambda sandbox: self._queue_late_create_terminate(
                            sandbox,
                            spec=attempt_spec,
                        ),
                        late_error_handler=lambda exc: self._queue_late_create_orphan_reconcile(
                            exc,
                            spec=attempt_spec,
                        ),
                    )
                )
                hard_timeout = _int_env("NORTHCUA_E2B_CREATE_HARD_TIMEOUT_SECONDS", 0)
                try:
                    if hard_timeout > 0:
                        done, _ = await asyncio.wait({create_task}, timeout=float(hard_timeout))
                        if not done:
                            self._track_late_create_cleanup(create_task, spec=attempt_spec)
                            self._progress(
                                "e2b create: hard timeout",
                                template=spec.template,
                                hard_timeout_seconds=hard_timeout,
                            )
                            raise TimeoutError(f"E2B sandbox create exceeded hard timeout of {hard_timeout}s")
                    sandbox = await asyncio.shield(create_task)
                except asyncio.CancelledError:
                    # Cancellation can arrive after the SDK call completed but
                    # before its result was handed to the caller. In both that
                    # race and the still-running case, this caller no longer
                    # owns the result, so one cleanup task must own it.
                    self._track_late_create_cleanup(create_task, spec=attempt_spec)
                    raise
            except Exception as exc:
                last_exc = exc
                retryable = _is_retryable_e2b_error(exc)
                if attempt >= max_attempts or not retryable:
                    if retryable:
                        await self._cleanup_possible_orphaned_create(
                            attempt_spec,
                            delays=_ORPHAN_RECONCILE_DELAYS_SECONDS,
                            ambiguous_error=exc,
                        )
                    logger.exception(
                        "E2B sandbox create failed",
                        extra={
                            "template": spec.template,
                            "attempt": attempt,
                            "max_attempts": max_attempts,
                            "retryable": retryable,
                        },
                    )
                    raise
                backoff = _retry_delay_seconds(
                    _CREATE_RETRY_BACKOFF_SECONDS,
                    attempt=attempt,
                    max_jitter_seconds=_CREATE_RETRY_JITTER_SECONDS,
                )
                logger.warning(
                    "E2B sandbox create failed; retrying",
                    extra={
                        "template": spec.template,
                        "attempt": attempt,
                        "max_attempts": max_attempts,
                        "backoff_seconds": backoff,
                        "retryable_error_type": type(exc).__name__,
                    },
                    exc_info=True,
                )
                self._progress(
                    "e2b create: retrying",
                    template=spec.template,
                    attempt=attempt,
                    max_attempts=max_attempts,
                    backoff_seconds=backoff,
                    error_type=type(exc).__name__,
                )
                await self._cleanup_possible_orphaned_create(
                    attempt_spec,
                    delays=_ORPHAN_RECONCILE_DELAYS_SECONDS,
                    ambiguous_error=exc,
                )
                await asyncio.sleep(backoff)
                continue
            sandbox_id = str(getattr(sandbox, "sandbox_id", "") or getattr(sandbox, "id", ""))
            self._progress("e2b create: completed", sandbox_id=sandbox_id or "unknown", attempt=attempt)
            return sandbox
        raise RuntimeError("E2B sandbox create failed") from last_exc

    def _queue_late_create_terminate(
        self,
        sandbox: _E2BSandboxLike,
        *,
        spec: SandboxProviderSpec,
    ) -> None:
        """Hand one exact late create result to the process reaper."""

        submit_e2b_reaper_job(
            operation="late_create_terminate",
            job=lambda: self._terminate_late_create_sync(sandbox, spec=spec),
            fallback_to_caller_thread=True,
        )

    def _queue_late_create_orphan_reconcile(
        self,
        exc: BaseException,
        *,
        spec: SandboxProviderSpec,
    ) -> None:
        """Reconcile one ambiguous create error after its event loop is gone."""

        if not isinstance(exc, Exception) or not _is_retryable_e2b_error(exc):
            return
        expected = _orphan_match_metadata(spec)
        if expected is None:
            return
        completed = threading.Event()
        reconcile_lock = threading.Lock()
        campaign_key = expected["create_call_token"]
        elapsed = 0.0
        jobs: list[tuple[float, Callable[[], None]]] = []

        def reconcile_once() -> bool:
            with reconcile_lock:
                if completed.is_set():
                    return True
                if not self._reconcile_possible_orphaned_create_sync(expected, spec=spec):
                    return False
                completed.set()
                cancel_e2b_reaper_campaign(campaign_key)
                return True

        for delay in _ORPHAN_RECONCILE_DELAYS_SECONDS:
            elapsed += max(float(delay), 0.0)

            def reconcile() -> None:
                reconcile_once()

            jobs.append((elapsed, reconcile))
        accepted = submit_e2b_reaper_campaign(
            operation="late_create_orphan_reconcile",
            jobs=tuple(jobs),
            dedupe_key=campaign_key,
        )
        if not accepted:
            # Shutdown has already closed the process reaper. Keep the same
            # exact-token, eventually-consistent campaign off the callback
            # thread and register it with the process shutdown fence.
            def fallback_campaign() -> None:
                for delay in _ORPHAN_RECONCILE_DELAYS_SECONDS:
                    if delay > 0:
                        time.sleep(delay)
                    if reconcile_once():
                        return

            submit_e2b_reaper_fallback_job(
                operation="late_create_orphan_reconcile_fallback",
                job=fallback_campaign,
            )

    def _reconcile_possible_orphaned_create_sync(
        self,
        expected: dict[str, str],
        *,
        spec: SandboxProviderSpec,
    ) -> bool:
        """Perform one exact, loop-independent orphan reconciliation pass."""

        try:
            rows = self._list_matching_orphan_sandboxes_sync(expected)
        except Exception:
            logger.warning(
                "failed to inspect exact E2B inventory after late create failure",
                extra={"template": spec.template, "metadata_keys": sorted(expected)},
                exc_info=True,
            )
            return False
        cleaned = False
        for row in rows:
            metadata = dict(getattr(row, "metadata", {}) or {})
            sandbox_id = str(getattr(row, "sandbox_id", "") or "")
            if not sandbox_id or not _metadata_matches(metadata, expected):
                continue
            try:
                self._kill_sandbox_id_sync(sandbox_id)
            except Exception:
                logger.warning(
                    "failed to cleanup possible orphaned E2B sandbox after event-loop shutdown",
                    extra={"sandbox_id": sandbox_id, "template": spec.template},
                    exc_info=True,
                )
                continue
            logger.info(
                "cleaned possible orphaned E2B sandbox after event-loop shutdown",
                extra={"sandbox_id": sandbox_id, "template": spec.template},
            )
            cleaned = True
        return cleaned

    def _terminate_late_create_sync(
        self,
        sandbox: _E2BSandboxLike,
        *,
        spec: SandboxProviderSpec,
    ) -> None:
        sandbox_id = str(getattr(sandbox, "sandbox_id", "") or getattr(sandbox, "id", ""))
        max_attempts = max(
            1,
            _int_env(
                "NORTHCUA_E2B_TERMINATE_MAX_ATTEMPTS",
                len(_LATE_CREATE_TERMINATE_RETRY_BACKOFF_SECONDS) + 1,
            ),
        )
        for attempt in range(1, max_attempts + 1):
            try:
                self._kill_sync(sandbox)
            except Exception as exc:
                retryable = _is_retryable_e2b_error(exc)
                if attempt >= max_attempts or not retryable:
                    logger.exception(
                        "failed to reap E2B sandbox returned after event-loop shutdown",
                        extra={
                            "template": spec.template,
                            "sandbox_id": sandbox_id,
                            "attempt": attempt,
                            "max_attempts": max_attempts,
                            "retryable": retryable,
                        },
                    )
                    return
                backoff = _LATE_CREATE_TERMINATE_RETRY_BACKOFF_SECONDS[
                    min(attempt - 1, len(_LATE_CREATE_TERMINATE_RETRY_BACKOFF_SECONDS) - 1)
                ]
                time.sleep(backoff)
                continue
            logger.info(
                "reaped E2B sandbox returned after event-loop shutdown",
                extra={"template": spec.template, "sandbox_id": sandbox_id, "attempt": attempt},
            )
            return

    def _track_late_create_cleanup(
        self,
        create_task: asyncio.Task[_E2BSandboxLike],
        *,
        spec: SandboxProviderSpec,
    ) -> None:
        """Terminate a sandbox returned after its caller timed out or was cancelled."""

        claimed = getattr(self, "_late_create_cleanup_claimed", None)
        if claimed is None:
            claimed = set()
            setattr(self, "_late_create_cleanup_claimed", claimed)
        if create_task in claimed:
            return
        claimed.add(create_task)
        if create_task.done():
            try:
                sandbox = create_task.result()
            except Exception as exc:
                self._queue_late_create_orphan_reconcile(exc, spec=spec)
                return
            except BaseException:
                return
            self._queue_late_create_terminate(sandbox, spec=spec)
            return

        tracked = getattr(self, "_late_create_cleanup_by_create", None)
        if tracked is None:
            tracked = {}
            setattr(self, "_late_create_cleanup_by_create", tracked)
        cleanup_task = asyncio.create_task(
            self._cleanup_late_create(create_task, spec=spec),
            name="northcua-e2b-late-create-cleanup",
        )
        tracked[create_task] = cleanup_task
        tasks = getattr(self, "_late_create_cleanup_tasks", None)
        if tasks is None:
            tasks = set()
            setattr(self, "_late_create_cleanup_tasks", tasks)
        tasks.add(cleanup_task)

        def discard(completed: asyncio.Task[None]) -> None:
            tasks.discard(completed)
            tracked.pop(create_task, None)

        cleanup_task.add_done_callback(discard)

    async def _cleanup_late_create(
        self,
        create_task: asyncio.Task[_E2BSandboxLike],
        *,
        spec: SandboxProviderSpec,
    ) -> None:
        try:
            sandbox = await create_task
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._queue_late_create_orphan_reconcile(exc, spec=spec)
            return
        sandbox_id = str(getattr(sandbox, "sandbox_id", "") or getattr(sandbox, "id", ""))
        self._queue_late_create_terminate(sandbox, spec=spec)
        self._progress(
            "e2b create: late result handed to reaper",
            template=spec.template,
            sandbox_id=sandbox_id,
        )

    async def _cleanup_possible_orphaned_create(
        self,
        spec: SandboxProviderSpec,
        *,
        delays: tuple[float, ...] = (0.0,),
        ambiguous_error: Exception | None = None,
    ) -> None:
        """Best-effort cleanup for E2B creates that failed after allocation.

        The SDK can raise while verifying a sandbox stream after the provider
        has already allocated capacity. When the task metadata is specific
        enough, scan the provider inventory and terminate matching same-attempt
        sandboxes before retrying.
        """

        expected = _orphan_match_metadata(spec)
        if expected is None:
            return
        schedule = tuple(max(float(delay), 0.0) for delay in delays) or (0.0,)
        cancellation_owned_by_terminate = False
        try:
            for reconcile_attempt, delay in enumerate(schedule, start=1):
                if delay:
                    await asyncio.sleep(delay)
                try:
                    rows = await self._run_executor_call(
                        lane="control",
                        operation="orphan_inventory",
                        call=lambda: self._list_matching_orphan_sandboxes_sync(expected),
                    )
                except Exception:
                    logger.warning(
                        "failed to inspect exact E2B inventory after create failure",
                        extra={
                            "template": spec.template,
                            "metadata_keys": sorted(expected),
                            "reconcile_attempt": reconcile_attempt,
                            "reconcile_attempts": len(schedule),
                        },
                        exc_info=True,
                    )
                    continue
                terminated = False
                for row in rows:
                    metadata = dict(getattr(row, "metadata", {}) or {})
                    sandbox_id = str(getattr(row, "sandbox_id", "") or "")
                    if not sandbox_id or not _metadata_matches(metadata, expected):
                        continue
                    cancellation_owned_by_terminate = True
                    try:
                        await self._run_executor_call(
                            lane="control",
                            operation="orphan_terminate",
                            call=lambda sandbox_id=sandbox_id: self._kill_sandbox_id_sync(sandbox_id),
                            late_result_handler=lambda _result: None,
                            late_error_handler=(
                                lambda _exc: (
                                    self._queue_late_create_orphan_reconcile(
                                        ambiguous_error,
                                        spec=spec,
                                    )
                                    if ambiguous_error is not None
                                    else None
                                )
                            ),
                        )
                    except Exception:
                        cancellation_owned_by_terminate = False
                        logger.warning(
                            "failed to cleanup possible orphaned E2B sandbox",
                            extra={
                                "sandbox_id": sandbox_id,
                                "template": spec.template,
                                "reconcile_attempt": reconcile_attempt,
                            },
                            exc_info=True,
                        )
                        continue
                    cancellation_owned_by_terminate = False
                    self._progress(
                        "e2b create: cleaned possible orphan",
                        sandbox_id=sandbox_id,
                        template=spec.template,
                        reconcile_attempt=reconcile_attempt,
                    )
                    terminated = True
                if terminated:
                    return
        except asyncio.CancelledError:
            if ambiguous_error is not None and not cancellation_owned_by_terminate:
                self._queue_late_create_orphan_reconcile(ambiguous_error, spec=spec)
            raise

    def _run_command_sync(
        self,
        sandbox: _E2BSandboxLike,
        command: SandboxCommandSpec,
    ) -> SandboxCommandResult:
        try:
            result = _run_sdk_command(
                sandbox,
                self._command_line(command),
                timeout=command.timeout_seconds,
            )
        except Exception as exc:
            exit_result = _command_exit_result(exc, command)
            if exit_result is None:
                raise
            logger.debug(
                "normalized E2B command exit exception",
                extra={"label": command.label, "exit_code": exit_result.exit_code},
            )
            return exit_result
        return SandboxCommandResult(
            exit_code=int(getattr(result, "exit_code", 0) or 0),
            stdout=self._redact(str(getattr(result, "stdout", "") or ""), command),
            stderr=self._redact(str(getattr(result, "stderr", "") or ""), command),
        )

    def _start_process_sync(
        self,
        sandbox: _E2BSandboxLike,
        command: SandboxCommandSpec,
    ) -> SandboxCommandResult:
        setup_command = (
            "mkdir -p /tmp/northcua-device-logs && "
            ": >/tmp/northcua-device-worker.out && "
            ": >/tmp/northcua-device-worker.err"
        )
        setup_result = _run_sdk_command(
            sandbox,
            setup_command,
            timeout=min(command.timeout_seconds, 10),
        )
        setup_exit_code = int(getattr(setup_result, "exit_code", 0) or 0)
        if setup_exit_code != 0:
            return SandboxCommandResult(
                exit_code=setup_exit_code,
                stdout=self._redact(str(getattr(setup_result, "stdout", "") or ""), command),
                stderr=self._redact(str(getattr(setup_result, "stderr", "") or ""), command),
            )
        background_command = (
            "bash -lc "
            + shlex.quote(self._command_script(command))
            + " </dev/null >/tmp/northcua-device-worker.out 2>/tmp/northcua-device-worker.err"
        )
        try:
            result = _run_sdk_command(
                sandbox,
                background_command,
                background=True,
                timeout=command.timeout_seconds,
            )
        except TypeError:
            detached_command = (
                "setsid bash -lc "
                + shlex.quote(self._command_script(command))
                + " </dev/null >/tmp/northcua-device-worker.out 2>/tmp/northcua-device-worker.err &"
            )
            result = _run_sdk_command(
                sandbox,
                detached_command,
                timeout=min(command.timeout_seconds, 10),
            )
        return SandboxCommandResult(
            exit_code=int(getattr(result, "exit_code", 0) or 0),
            stdout=self._redact(str(getattr(result, "stdout", "") or ""), command),
            stderr=self._redact(str(getattr(result, "stderr", "") or ""), command),
        )

    async def _run_command_with_progress(
        self,
        *,
        sandbox: _E2BSandboxLike,
        sandbox_id: str,
        command: SandboxCommandSpec,
        operation: str,
    ) -> SandboxCommandResult:
        """Run an SDK command while emitting local progress heartbeats."""

        label = command.label or operation
        self._progress(
            f"e2b command: {operation} start",
            sandbox_id=sandbox_id,
            label=label,
            timeout_seconds=command.timeout_seconds,
        )
        started_at = asyncio.get_running_loop().time()
        if operation == "start_process":
            executor_call = E2BExecutorCall(
                "command",
                lambda: self._start_process_sync(sandbox, command),
            )
        else:
            executor_call = E2BExecutorCall(
                "command",
                lambda: self._run_command_sync(sandbox, command),
            )
        task = asyncio.create_task(executor_call.run())
        try:
            while True:
                done, _ = await asyncio.wait({task}, timeout=_BUILD_HEARTBEAT_SECONDS)
                if done:
                    try:
                        result = task.result()
                    except BaseException as exc:
                        self._progress(
                            f"e2b command: {operation} failed",
                            sandbox_id=sandbox_id,
                            label=label,
                            error_type=type(exc).__name__,
                            **executor_call.timing().progress_fields(),
                            **executor_telemetry("command"),
                        )
                        raise
                    elapsed = int(asyncio.get_running_loop().time() - started_at)
                    self._progress(
                        f"e2b command: {operation} completed",
                        sandbox_id=sandbox_id,
                        label=label,
                        exit_code=result.exit_code,
                        elapsed_seconds=elapsed,
                        **executor_call.timing().progress_fields(),
                        **executor_telemetry("command"),
                    )
                    return result
                elapsed = int(asyncio.get_running_loop().time() - started_at)
                self._progress(
                    f"e2b command: {operation} waiting",
                    sandbox_id=sandbox_id,
                    label=label,
                    elapsed_seconds=elapsed,
                    timeout_seconds=command.timeout_seconds,
                    **executor_call.timing().progress_fields(),
                    **executor_telemetry("command"),
                )
        except asyncio.CancelledError:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
            self._track_late_executor_completion(executor_call, operation=operation)
            raise

    def _upload_sync(
        self,
        sandbox: _E2BSandboxLike,
        local: Path,
        remote: str,
    ) -> None:
        data = local.read_bytes()
        files = getattr(sandbox, "files", None)
        write = getattr(files, "write", None)
        if callable(write):
            try:
                request_timeout = max(
                    _int_env(
                        "NORTHCUA_E2B_UPLOAD_REQUEST_TIMEOUT_SECONDS",
                        _DEFAULT_UPLOAD_REQUEST_TIMEOUT_SECONDS,
                    ),
                    1,
                )
                try:
                    write_kwargs: dict[str, object] = {
                        "request_timeout": request_timeout,
                    }
                    write(remote, data, **write_kwargs)
                except TypeError as exc:
                    # Older E2B SDKs predate the request_timeout keyword. Keep
                    # their behavior without swallowing TypeErrors raised by
                    # the implementation itself.
                    if not _is_unexpected_request_timeout_keyword(exc):
                        raise
                    write(remote, data)
                return
            except Exception as exc:
                if not _is_file_write_unsupported(exc):
                    raise
                logger.warning(
                    "E2B SDK file upload failed; falling back to shell upload",
                    extra={"remote": remote, "size_bytes": len(data)},
                    exc_info=True,
                )
        self._upload_sync_via_shell(
            sandbox,
            data=data,
            remote=remote,
        )

    def _upload_sync_via_shell(
        self,
        sandbox: _E2BSandboxLike,
        *,
        data: bytes,
        remote: str,
    ) -> None:
        remote_parent = str(Path(remote).parent)
        quoted_parent = shlex.quote(remote_parent)
        quoted_remote = shlex.quote(remote)
        if len(data) <= _SHELL_UPLOAD_SINGLE_COMMAND_RAW_BYTES:
            encoded = base64.b64encode(data).decode("ascii")
            command = (
                f"mkdir -p {quoted_parent} && base64 -d > {quoted_remote} <<'NORTHCUA_UPLOAD_B64'\n"
                f"{encoded}\n"
                "NORTHCUA_UPLOAD_B64\n"
            )
            result = _run_sdk_command(
                sandbox,
                command,
                timeout=120,
            )
            if int(getattr(result, "exit_code", 0) or 0) != 0:
                stderr = str(getattr(result, "stderr", "") or "")
                stdout = str(getattr(result, "stdout", "") or "")
                raise RuntimeError(stderr or stdout or "E2B upload failed")
            return
        temp_b64 = f"{remote}.northcua-upload.b64"
        quoted_temp = shlex.quote(temp_b64)
        prepare = _run_sdk_command(
            sandbox,
            f"mkdir -p {quoted_parent} && : > {quoted_temp}",
            timeout=60,
        )
        if int(getattr(prepare, "exit_code", 0) or 0) != 0:
            stderr = str(getattr(prepare, "stderr", "") or "")
            stdout = str(getattr(prepare, "stdout", "") or "")
            raise RuntimeError(stderr or stdout or "E2B upload fallback prepare failed")
        for start in range(0, len(data), _SHELL_UPLOAD_RAW_CHUNK_BYTES):
            encoded = base64.b64encode(data[start : start + _SHELL_UPLOAD_RAW_CHUNK_BYTES]).decode("ascii")
            append = _run_sdk_command(
                sandbox,
                f"printf %s {shlex.quote(encoded)} >> {quoted_temp}",
                timeout=60,
            )
            if int(getattr(append, "exit_code", 0) or 0) != 0:
                stderr = str(getattr(append, "stderr", "") or "")
                stdout = str(getattr(append, "stdout", "") or "")
                raise RuntimeError(stderr or stdout or "E2B upload fallback append failed")
        result = _run_sdk_command(
            sandbox,
            f"base64 -d {quoted_temp} > {quoted_remote} && rm -f {quoted_temp}",
            timeout=120,
        )
        if int(getattr(result, "exit_code", 0) or 0) != 0:
            stderr = str(getattr(result, "stderr", "") or "")
            stdout = str(getattr(result, "stdout", "") or "")
            raise RuntimeError(stderr or stdout or "E2B upload failed")

    def _download_sync(
        self,
        sandbox: _E2BSandboxLike,
        remote: str,
        local: Path,
    ) -> None:
        local.parent.mkdir(parents=True, exist_ok=True)
        files = getattr(sandbox, "files", None)
        read = getattr(files, "read", None)
        if callable(read):
            content = self._read_file_sync(cast(_E2BFilesLike, files), remote)
            if isinstance(content, bytearray):
                payload = bytes(content)
            elif isinstance(content, bytes):
                payload = content
            else:
                payload = content.encode("utf-8")
            local.write_bytes(payload)
            return
        result = _run_sdk_command(
            sandbox,
            f"base64 -w 0 {shlex.quote(remote)}",
            timeout=60,
        )
        if int(getattr(result, "exit_code", 0) or 0) != 0:
            raise RuntimeError(str(getattr(result, "stderr", "") or "E2B download failed"))
        local.write_bytes(base64.b64decode(str(getattr(result, "stdout", "") or "")))

    @staticmethod
    def _read_file_sync(files: _E2BFilesLike, remote: str) -> bytes | bytearray | str:
        read_file = cast(Callable[..., bytes | bytearray | str], files.read)
        kwargs: dict[str, object] = {
            "format": "bytes",
            "request_timeout": _control_request_timeout("NORTHCUA_E2B_DOWNLOAD_REQUEST_TIMEOUT_SECONDS"),
        }
        try:
            return read_file(remote, **kwargs)
        except TypeError as exc:
            if not _is_unexpected_request_timeout_keyword(exc):
                raise
            kwargs.pop("request_timeout", None)
            return read_file(remote, **kwargs)

    def _kill_sync(self, sandbox: _E2BSandboxLike) -> None:
        kill = getattr(sandbox, "kill", None) or getattr(sandbox, "close", None)
        if kill is not None:
            kwargs: dict[str, object] = {
                "request_timeout": _control_request_timeout("NORTHCUA_E2B_TERMINATE_REQUEST_TIMEOUT_SECONDS"),
            }
            try:
                kill(**kwargs)
            except TypeError as exc:
                if not _is_unexpected_request_timeout_keyword(exc):
                    raise
                kill()

    @staticmethod
    def _set_timeout_sync(sandbox: _E2BSandboxLike, timeout_seconds: int) -> None:
        set_timeout = cast(Callable[..., None], sandbox.set_timeout)
        kwargs: dict[str, object] = {
            "request_timeout": _control_request_timeout("NORTHCUA_E2B_SET_TIMEOUT_REQUEST_TIMEOUT_SECONDS"),
        }
        try:
            set_timeout(timeout_seconds, **kwargs)
        except TypeError as exc:
            if not _is_unexpected_request_timeout_keyword(exc):
                raise
            set_timeout(timeout_seconds)

    def _command_line(self, command: SandboxCommandSpec) -> str:
        """Return the provider command string with cwd/env applied."""

        if command.cwd or command.env:
            return "bash -lc " + shlex.quote(self._command_script(command))
        return command.command

    @staticmethod
    def _command_script(command: SandboxCommandSpec) -> str:
        lines = ["set -e"]
        if command.cwd:
            lines.append(f"cd {shlex.quote(command.cwd)}")
        for key, value in sorted(command.env.items()):
            lines.append(f"export {key}={shlex.quote(value)}")
        lines.append(command.command)
        return "\n".join(lines)

    def _require_sandbox(self, handle: SandboxHandle) -> _E2BSandboxLike:
        try:
            return self._sandboxes[handle.sandbox_id]
        except KeyError as exc:
            raise RuntimeError(f"E2B sandbox handle is not active: {handle.sandbox_id}") from exc

    @staticmethod
    def _redact(text: str, command: SandboxCommandSpec) -> str:
        redacted = text
        for secret in command.redact:
            if secret:
                redacted = redacted.replace(secret, "[REDACTED]")
        return redacted


def _sdk_proxy_kwargs() -> dict[str, object]:
    """Return the explicit E2B SDK proxy selected by the deployment."""

    proxy = os.getenv("NORTHCUA_E2B_PROXY_URL", "").strip()
    return {"proxy": proxy} if proxy else {}


def _run_sdk_command(
    sandbox: _E2BSandboxLike,
    command: str,
    *,
    timeout: float | int,
    background: bool = False,
) -> _E2BCommandResultLike:
    """Call the E2B command API without breaking legacy public signatures."""

    command_timeout = max(int(float(timeout)), 1)
    margin = max(
        _int_env(
            "NORTHCUA_E2B_COMMAND_REQUEST_TIMEOUT_MARGIN_SECONDS",
            _DEFAULT_COMMAND_REQUEST_TIMEOUT_MARGIN_SECONDS,
        ),
        1,
    )
    configured_floor = max(
        _int_env(
            "NORTHCUA_E2B_COMMAND_REQUEST_TIMEOUT_SECONDS",
            _DEFAULT_COMMAND_REQUEST_TIMEOUT_SECONDS,
        ),
        1,
    )
    configured_cap = max(
        _int_env(
            "NORTHCUA_E2B_COMMAND_REQUEST_TIMEOUT_CAP_SECONDS",
            _DEFAULT_COMMAND_REQUEST_TIMEOUT_CAP_SECONDS,
        ),
        1,
    )
    request_timeout = max(command_timeout + margin, min(configured_floor, configured_cap))
    kwargs: dict[str, object] = {"timeout": timeout, "request_timeout": request_timeout}
    if background:
        kwargs["background"] = True
    run_command = cast(Callable[..., _E2BCommandResultLike], sandbox.commands.run)
    try:
        return run_command(command, **kwargs)
    except TypeError as exc:
        if not _is_unexpected_request_timeout_keyword(exc):
            raise
        kwargs.pop("request_timeout", None)
        return run_command(command, **kwargs)


def _sandbox_inventory_item(row: dict[str, object], *, provider_id: str) -> SandboxInventoryItem:
    raw_metadata = row.get("metadata")
    metadata: dict[str, object] = raw_metadata if isinstance(raw_metadata, dict) else {}
    state = _inventory_state(str(row.get("state") or row.get("status") or "running"))
    sandbox_id = str(row.get("sandboxID") or row.get("sandboxId") or row.get("sandbox_id") or row.get("id") or "")
    template_id = str(row.get("templateID") or row.get("templateId") or row.get("template_id") or "")
    template = str(row.get("template") or row.get("templateAlias") or row.get("alias") or template_id)
    return SandboxInventoryItem(
        sandbox_id=sandbox_id,
        provider=provider_id,
        template=template,
        template_id=template_id,
        state=state,
        metadata={str(key): value for key, value in metadata.items()},
        cpu_count=_int_field(row, "cpuCount", "cpu_count", "cpu"),
        memory_mb=_int_field(row, "memoryMB", "memoryMb", "memory_mb", "memory"),
        disk_mb=_int_field(row, "diskMB", "diskMb", "disk_mb", "diskSizeMB"),
        started_at=str(row.get("startedAt") or row.get("started_at") or row.get("createdAt") or ""),
        desktop_url=str(row.get("stream_url") or row.get("streamUrl") or row.get("desktopUrl") or ""),
    )


def _inventory_state(value: str) -> SandboxInventoryState:
    normalized = value.lower()
    if normalized in {"running", "active"}:
        return SandboxInventoryState.RUNNING
    if normalized == "paused":
        return SandboxInventoryState.PAUSED
    if normalized in {"terminated", "killed", "stopped"}:
        return SandboxInventoryState.TERMINATED
    return SandboxInventoryState.UNKNOWN


def _command_exit_result(exc: Exception, command: SandboxCommandSpec) -> SandboxCommandResult | None:
    """Convert E2B command-exit exceptions into the provider result contract."""

    if type(exc).__name__ != "CommandExitException":
        return None
    exit_code = getattr(exc, "exit_code", None)
    if not isinstance(exit_code, int):
        return None
    stderr = str(getattr(exc, "stderr", "") or getattr(exc, "error", "") or "")
    return SandboxCommandResult(
        exit_code=exit_code,
        stdout=E2BProviderRuntimeMixin._redact(str(getattr(exc, "stdout", "") or ""), command),
        stderr=E2BProviderRuntimeMixin._redact(stderr, command),
    )


def _int_field(row: dict[str, object], *names: str) -> int:
    for name in names:
        value = row.get(name)
        if isinstance(value, int):
            return value
        if isinstance(value, str) and value.isdigit():
            return int(value)
    return 0
