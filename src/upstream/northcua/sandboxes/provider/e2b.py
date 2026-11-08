"""E2B sandbox provider adapter with lazy optional imports."""

from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path

from northcua.sandboxes.capacity import build_capacity_probe_result
from northcua.sandboxes.contracts import (
    SandboxCapacityProbeRequest,
    SandboxCapacityProbeResult,
    SandboxCommandResult,
    SandboxCommandSpec,
    SandboxHandle,
    SandboxInventoryItem,
    SandboxInventoryState,
    SandboxLifecycleState,
    SandboxLogRef,
    SandboxProviderCapabilities,
    SandboxProviderFeature,
    SandboxProviderSpec,
    SandboxTemplateSpec,
)
from northcua.sandboxes.provider.e2b_runtime import (
    E2BProviderRuntimeMixin,
    _E2BSandboxLike,
    _int_env,
    _is_retryable_e2b_error,
    _sandbox_inventory_item,
)

logger = logging.getLogger(__name__)
_BUILD_HEARTBEAT_SECONDS = 30.0
_UPLOAD_RETRY_BACKOFF_SECONDS = (1.0, 3.0, 7.0)
_TERMINATE_RETRY_BACKOFF_SECONDS = (1.0, 3.0)
_DEFAULT_E2B_MAX_ACTIVE_SANDBOXES = 20
_DEFAULT_E2B_CREATE_RATE_PER_SECOND = 1


class E2BSandboxProvider(E2BProviderRuntimeMixin):
    """Sandbox provider backed by the ``e2b`` and ``e2b-desktop`` SDKs."""

    provider_id = "e2b"

    def __init__(self) -> None:
        self._sandboxes: dict[str, _E2BSandboxLike] = {}
        self._late_create_cleanup_tasks: set[asyncio.Task[None]] = set()
        self._late_create_cleanup_by_create: dict[asyncio.Task[_E2BSandboxLike], asyncio.Task[None]] = {}
        self._late_create_cleanup_claimed: set[asyncio.Task[_E2BSandboxLike]] = set()
        self._late_executor_completion_tasks: set[asyncio.Task[None]] = set()

    async def aclose(self) -> None:
        """Wait one bounded deadline for this provider's owned late SDK work."""

        tasks: set[asyncio.Task[object]] = {
            *self._late_create_cleanup_tasks,
            *self._late_executor_completion_tasks,
        }
        timeout_seconds = max(_int_env("NORTHCUA_E2B_PROVIDER_CLOSE_TIMEOUT_SECONDS", 30), 0)
        pending: set[asyncio.Task[object]] = set()
        if tasks:
            _, pending = await asyncio.wait(tasks, timeout=float(timeout_seconds))
        if pending:
            logger.warning(
                "E2B provider close deadline elapsed with owned SDK work still running",
                extra={
                    "pending_tasks": len(pending),
                    "timeout_seconds": timeout_seconds,
                },
            )

    def capabilities(self) -> SandboxProviderCapabilities:
        """Return E2B provider feature support.

        Returns:
            Feature set implemented by the E2B adapter.
        """

        return SandboxProviderCapabilities(
            features={
                SandboxProviderFeature.CONNECT_EXISTING,
                SandboxProviderFeature.UPLOAD,
                SandboxProviderFeature.DOWNLOAD,
                SandboxProviderFeature.COLLECT_LOGS,
                SandboxProviderFeature.TEMPLATE_BUILD,
                SandboxProviderFeature.CAPACITY_PROBE,
                SandboxProviderFeature.EXTEND_TIMEOUT,
            }
        )

    async def preflight(self, spec: SandboxProviderSpec) -> None:
        """Validate local E2B dependencies and credentials.

        Args:
            spec: Provider allocation request.

        Raises:
            RuntimeError: If the E2B SDK or API key is unavailable.
        """

        self._load_sdk(desktop=spec.desktop)
        if not os.getenv("E2B_API_KEY"):
            raise RuntimeError("E2B_API_KEY is required for the E2B sandbox provider")

    async def create(self, spec: SandboxProviderSpec) -> SandboxHandle:
        """Create an E2B sandbox.

        Args:
            spec: Provider allocation request.

        Returns:
            Handle for the created sandbox.
        """

        sandbox = await self._create_sandbox_once(spec)
        sandbox_id = str(getattr(sandbox, "sandbox_id", "") or getattr(sandbox, "id", ""))
        if not sandbox_id:
            sandbox_id = f"e2b-{id(sandbox)}"
        self._sandboxes[sandbox_id] = sandbox
        return SandboxHandle(
            sandbox_id=sandbox_id,
            provider=self.provider_id,
            template=spec.template,
            state=SandboxLifecycleState.CREATED,
            desktop_url=str(getattr(sandbox, "stream_url", "") or ""),
            metadata={"desktop": spec.desktop},
        )

    async def connect(self, spec: SandboxProviderSpec) -> SandboxHandle:
        """Connect to an existing E2B sandbox.

        Args:
            spec: Provider allocation request containing ``sandbox_id``.

        Returns:
            Handle for the connected sandbox.
        """

        sandbox = await self._run_executor_call(
            lane="control",
            operation="connect",
            call=lambda: self._connect_sync(spec),
        )
        self._sandboxes[spec.sandbox_id] = sandbox
        return SandboxHandle(
            sandbox_id=spec.sandbox_id,
            provider=self.provider_id,
            template=spec.template,
            state=SandboxLifecycleState.CONNECTED,
            desktop_url=str(getattr(sandbox, "stream_url", "") or ""),
            metadata={"desktop": spec.desktop, "connected": True},
        )

    async def run_command(self, handle: SandboxHandle, command: SandboxCommandSpec) -> SandboxCommandResult:
        """Run a foreground command inside E2B.

        Args:
            handle: Sandbox receiving the command.
            command: Command policy to execute.

        Returns:
            Normalized command result.
        """

        sandbox = self._require_sandbox(handle)
        return await self._run_command_with_progress(
            sandbox=sandbox,
            sandbox_id=handle.sandbox_id,
            command=command,
            operation="run_command",
        )

    async def start_process(self, handle: SandboxHandle, command: SandboxCommandSpec) -> SandboxCommandResult:
        """Start a long-lived command inside E2B.

        Args:
            handle: Sandbox receiving the command.
            command: Process launch policy.

        Returns:
            Normalized launch result.
        """

        sandbox = self._require_sandbox(handle)
        return await self._run_command_with_progress(
            sandbox=sandbox,
            sandbox_id=handle.sandbox_id,
            command=command,
            operation="start_process",
        )

    async def upload(self, handle: SandboxHandle, local: Path, remote: str) -> None:
        """Upload a local file into E2B.

        Args:
            handle: Sandbox receiving the file.
            local: Local file path.
            remote: Remote absolute path.

        Raises:
            RuntimeError: If the SDK upload and fallback shell upload both
                fail after the configured retry budget.
        """

        sandbox = self._require_sandbox(handle)
        size_bytes: int | str = local.stat().st_size if local.exists() else "missing"
        max_attempts = len(_UPLOAD_RETRY_BACKOFF_SECONDS) + 1
        for attempt in range(1, max_attempts + 1):
            self._progress(
                "e2b upload: start",
                sandbox_id=handle.sandbox_id,
                local=local.name,
                remote=remote,
                size_bytes=size_bytes,
                attempt=attempt,
                max_attempts=max_attempts,
            )
            try:
                await self._run_executor_call(
                    lane="control",
                    operation="upload",
                    call=lambda: self._upload_sync(sandbox, local, remote),
                )
            except Exception:
                if attempt >= max_attempts:
                    logger.exception(
                        "E2B upload failed after retries",
                        extra={"sandbox_id": handle.sandbox_id, "remote": remote, "attempts": attempt},
                    )
                    raise
                backoff = _UPLOAD_RETRY_BACKOFF_SECONDS[attempt - 1]
                logger.warning(
                    "E2B upload failed; retrying",
                    extra={
                        "sandbox_id": handle.sandbox_id,
                        "remote": remote,
                        "attempt": attempt,
                        "backoff_seconds": backoff,
                    },
                    exc_info=True,
                )
                self._progress(
                    "e2b upload: retrying",
                    sandbox_id=handle.sandbox_id,
                    remote=remote,
                    attempt=attempt,
                    backoff_seconds=backoff,
                )
                await asyncio.sleep(backoff)
                continue
            self._progress("e2b upload: completed", sandbox_id=handle.sandbox_id, remote=remote, attempt=attempt)
            return

    async def download(self, handle: SandboxHandle, remote: str, local: Path) -> None:
        """Download a remote file from E2B.

        Args:
            handle: Sandbox owning the file.
            remote: Remote absolute path.
            local: Destination local path.
        """

        sandbox = self._require_sandbox(handle)
        self._progress("e2b download: start", sandbox_id=handle.sandbox_id, remote=remote, local=local.name)
        await self._run_executor_call(
            lane="control",
            operation="download",
            call=lambda: self._download_sync(sandbox, remote, local),
        )
        self._progress("e2b download: completed", sandbox_id=handle.sandbox_id, remote=remote)

    async def collect_logs(self, handle: SandboxHandle, destination: Path) -> list[SandboxLogRef]:
        """Collect known NorthCUA worker logs from E2B.

        Args:
            handle: Sandbox whose worker logs should be downloaded.
            destination: Local directory for downloaded logs.

        Returns:
            Log references for files that existed in the sandbox.
        """

        destination.mkdir(parents=True, exist_ok=True)
        collected: list[SandboxLogRef] = []
        await self._archive_device_log_dir(handle)
        for remote in (
            "/tmp/northcua-device-worker.out",
            "/tmp/northcua-device-worker.err",
            "/tmp/northcua-device-logs.tar.gz",
            "/tmp/northcua-device-logs/runtime.jsonl",
            "/tmp/northcua-device-logs/device_runtime.jsonl",
        ):
            local = destination / f"{handle.sandbox_id}-{Path(remote).name}"
            try:
                await self.download(handle, remote, local)
            except Exception:
                logger.debug("E2B log file not available", extra={"sandbox_id": handle.sandbox_id, "remote": remote})
                continue
            collected.append(SandboxLogRef(name=Path(remote).name, local_path=local, size_bytes=local.stat().st_size))
        return collected

    async def _archive_device_log_dir(self, handle: SandboxHandle) -> None:
        """Best-effort archive of sandbox-local device logs before teardown.

        Args:
            handle: Sandbox whose local device logs should be archived.
        """

        try:
            await self.run_command(
                handle,
                SandboxCommandSpec(
                    command=(
                        "if [ -d /tmp/northcua-device-logs ]; then "
                        "tar -czf /tmp/northcua-device-logs.tar.gz -C /tmp northcua-device-logs; "
                        "else exit 0; fi"
                    ),
                    label="archive-northcua-device-logs",
                    timeout_seconds=10,
                ),
            )
        except Exception:
            logger.debug("E2B device log archive failed", extra={"sandbox_id": handle.sandbox_id}, exc_info=True)

    async def terminate(self, handle: SandboxHandle) -> None:
        """Terminate an E2B sandbox if this provider owns the SDK object.

        Args:
            handle: Sandbox to terminate.

        Raises:
            Exception: Re-raises the final provider termination error after
                retry budget is exhausted or the error is not retryable.
        """

        sandbox = self._sandboxes.get(handle.sandbox_id)
        if sandbox is None:
            return
        max_attempts = _int_env("NORTHCUA_E2B_TERMINATE_MAX_ATTEMPTS", len(_TERMINATE_RETRY_BACKOFF_SECONDS) + 1)
        max_attempts = max(1, max_attempts)
        for attempt in range(1, max_attempts + 1):
            self._progress(
                "e2b terminate: start",
                sandbox_id=handle.sandbox_id,
                attempt=attempt,
                max_attempts=max_attempts,
            )
            try:
                await self._run_executor_call(
                    lane="control",
                    operation="terminate",
                    call=lambda: self._kill_sync(sandbox),
                )
            except Exception as exc:
                retryable = _is_retryable_e2b_error(exc)
                if attempt >= max_attempts or not retryable:
                    logger.exception(
                        "E2B terminate failed",
                        extra={
                            "sandbox_id": handle.sandbox_id,
                            "attempt": attempt,
                            "max_attempts": max_attempts,
                            "retryable": retryable,
                        },
                    )
                    raise
                backoff = _TERMINATE_RETRY_BACKOFF_SECONDS[min(attempt - 1, len(_TERMINATE_RETRY_BACKOFF_SECONDS) - 1)]
                logger.warning(
                    "E2B terminate failed; retrying",
                    extra={
                        "sandbox_id": handle.sandbox_id,
                        "attempt": attempt,
                        "max_attempts": max_attempts,
                        "backoff_seconds": backoff,
                    },
                    exc_info=True,
                )
                self._progress(
                    "e2b terminate: retrying",
                    sandbox_id=handle.sandbox_id,
                    attempt=attempt,
                    max_attempts=max_attempts,
                    backoff_seconds=backoff,
                    error_type=type(exc).__name__,
                )
                await asyncio.sleep(backoff)
                continue
            self._sandboxes.pop(handle.sandbox_id, None)
            self._progress("e2b terminate: completed", sandbox_id=handle.sandbox_id, attempt=attempt)
            return

    async def extend_timeout(self, handle: SandboxHandle, timeout_seconds: int) -> None:
        """Extend the provider-side E2B sandbox timeout from now.

        Args:
            handle: E2B sandbox handle whose timeout should be refreshed.
            timeout_seconds: New E2B timeout in seconds.
        """

        sandbox = self._require_sandbox(handle)
        await self._run_executor_call(
            lane="control",
            operation="set_timeout",
            call=lambda: self._set_timeout_sync(sandbox, timeout_seconds),
        )

    async def build_template(self, spec: SandboxTemplateSpec) -> str:
        """Build an E2B template from a provider-neutral template spec.

        Args:
            spec: Template definition.

        Returns:
            E2B template alias.

        Raises:
            RuntimeError: If E2B credentials or the async template API are missing.
        """

        if not os.getenv("E2B_API_KEY"):
            raise RuntimeError("E2B_API_KEY is required to build E2B templates")
        return await self._build_template_async(spec)

    async def probe(self, spec: SandboxProviderSpec) -> SandboxCommandResult:
        """Create, probe, and terminate a lightweight E2B sandbox.

        Args:
            spec: Provider allocation request.

        Returns:
            Result of the probe command.
        """

        await self.preflight(spec)
        handle = await self.create(spec)
        try:
            return await self.run_command(handle, SandboxCommandSpec(command="python3 --version", timeout_seconds=30))
        finally:
            await self.terminate(handle)

    async def capacity_probe(self, request: SandboxCapacityProbeRequest) -> SandboxCapacityProbeResult:
        """Inspect active E2B sandboxes without creating any new capacity.

        Args:
            request: Passive capacity probe request and optional configured
                limits.

        Returns:
            Capacity summary based on E2B's sandbox inventory endpoint.

        Raises:
            RuntimeError: If the E2B SDK or API key is unavailable.
        """

        self._load_inventory_sdk()
        if not os.getenv("E2B_API_KEY"):
            raise RuntimeError("E2B_API_KEY is required for the E2B sandbox provider")
        effective_request = request.model_copy(
            update={
                "provider": self.provider_id,
                "max_active_sandboxes": request.max_active_sandboxes
                or _int_env("NORTHCUA_E2B_MAX_ACTIVE_SANDBOXES", _DEFAULT_E2B_MAX_ACTIVE_SANDBOXES),
                "reserve_sandboxes": request.reserve_sandboxes or _int_env("NORTHCUA_E2B_RESERVE_SANDBOXES", 0),
                "create_rate_per_second": request.create_rate_per_second
                or _int_env("NORTHCUA_E2B_CREATE_RATE_PER_SECOND", _DEFAULT_E2B_CREATE_RATE_PER_SECOND),
            }
        )
        rows = await self._run_executor_call(
            lane="control",
            operation="inventory",
            call=self._list_sandboxes_sync,
        )
        items = [_sandbox_inventory_item(row, provider_id=self.provider_id) for row in rows]
        return build_capacity_probe_result(
            request=effective_request,
            source="e2b_api",
            items=items,
            warnings=["using configured/default E2B active limit; E2B quota is not modified by passive probe"],
        )
