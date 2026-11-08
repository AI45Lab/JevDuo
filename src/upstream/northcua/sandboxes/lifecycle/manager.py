"""Lifecycle orchestration for provider-owned sandboxes."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import shlex
import tempfile
import time
from collections.abc import Callable
from pathlib import Path

from northcua.sandboxes.contracts import (
    SandboxAllocationStrategy,
    SandboxCommandResult,
    SandboxCommandSpec,
    SandboxFileDeliveryMode,
    SandboxFileUploadSpec,
    SandboxHandle,
    SandboxProviderFeature,
    SandboxProviderSpec,
)
from northcua.sandboxes.guest_capability_evidence import (
    GUEST_CAPABILITY_EVIDENCE_PREFIX,
    guest_capability_evidence_payloads,
)
from northcua.sandboxes.lifecycle.errors import SandboxAcquireCleanupError
from northcua.sandboxes.provider.base import SandboxProvider
from northcua.sandboxes.tool_contracts import SandboxProviderDispatcher

logger = logging.getLogger(__name__)
StageSink = Callable[[str, dict[str, object]], None]
_PROXY_ENV_NAMES = (
    "ALL_PROXY",
    "FTP_PROXY",
    "HTTPS_PROXY",
    "HTTP_PROXY",
    "NO_PROXY",
    "all_proxy",
    "ftp_proxy",
    "https_proxy",
    "http_proxy",
    "no_proxy",
)


class SandboxLifecycleManager:
    """Create, prepare, and terminate sandboxes with consistent cleanup."""

    def __init__(self, provider: SandboxProvider) -> None:
        self._provider_dispatcher = SandboxProviderDispatcher(provider)

    async def acquire(
        self,
        spec: SandboxProviderSpec,
        *,
        startup_commands: list[SandboxCommandSpec],
        create_semaphore: asyncio.Semaphore | None = None,
        stage_sink: StageSink | None = None,
        on_handle_acquired: Callable[[SandboxHandle], None] | None = None,
    ) -> SandboxHandle:
        """Acquire sandbox capacity and run startup commands.

        Args:
            spec: Provider allocation request.
            startup_commands: Commands that prepare the sandbox before device startup.
            create_semaphore: Optional semaphore guarding only provider create/connect.

        Returns:
            Ready sandbox handle.

        Raises:
            RuntimeError: If creation, connection, or required startup commands fail.
        """

        handle = await self._acquire_handle(spec, create_semaphore=create_semaphore, stage_sink=stage_sink)
        try:
            if on_handle_acquired is not None:
                on_handle_acquired(handle)
            guest_uploads = [upload for upload in spec.file_uploads if upload.visibility == "guest"]
            if guest_uploads:
                upload_started = time.perf_counter()
                try:
                    await self._upload_files(handle, guest_uploads, stage_sink=stage_sink)
                except BaseException as exc:
                    logger.debug("sandbox file upload failed", exc_info=True)
                    _emit_stage(
                        stage_sink,
                        "file_upload",
                        started_at=upload_started,
                        status="failed",
                        sandbox_id=handle.sandbox_id,
                        provider=handle.provider,
                        upload_count=len(guest_uploads),
                        error=type(exc).__name__,
                    )
                    raise
                _emit_stage(
                    stage_sink,
                    "file_upload",
                    started_at=upload_started,
                    status="succeeded",
                    sandbox_id=handle.sandbox_id,
                    provider=handle.provider,
                    upload_count=len(guest_uploads),
                )
            if startup_commands:
                startup_started = time.perf_counter()
                try:
                    for command in startup_commands:
                        result = await self._provider_dispatcher.run_command(handle, command)
                        if not result.ok and not command.allow_failure:
                            raise RuntimeError(
                                _sandbox_command_failure_message("sandbox startup command failed", command, result)
                            )
                except BaseException as exc:
                    logger.debug("sandbox startup command failed", exc_info=True)
                    _emit_stage(
                        stage_sink,
                        "startup_commands",
                        started_at=startup_started,
                        status="failed",
                        sandbox_id=handle.sandbox_id,
                        provider=handle.provider,
                        command_count=len(startup_commands),
                        error=_short_error(exc),
                    )
                    raise
                _emit_stage(
                    stage_sink,
                    "startup_commands",
                    started_at=startup_started,
                    status="succeeded",
                    sandbox_id=handle.sandbox_id,
                    provider=handle.provider,
                    command_count=len(startup_commands),
                )
        except asyncio.CancelledError:
            logger.warning(
                "sandbox startup preparation was canceled; releasing provider capacity",
                extra={"sandbox_id": handle.sandbox_id, "provider": handle.provider},
                exc_info=True,
            )
            try:
                await asyncio.shield(self.release(handle, strategy=spec.allocation_strategy))
            except Exception as cleanup_exc:
                raise SandboxAcquireCleanupError(
                    sandbox_id=handle.sandbox_id,
                    provider=handle.provider,
                    message="sandbox startup preparation was canceled",
                    cleanup_error=cleanup_exc,
                ) from cleanup_exc
            raise
        except Exception as startup_exc:
            logger.warning(
                "sandbox startup preparation failed; releasing provider capacity",
                extra={"sandbox_id": handle.sandbox_id, "provider": handle.provider},
                exc_info=True,
            )
            try:
                await self.release(handle, strategy=spec.allocation_strategy)
            except Exception as cleanup_exc:
                raise SandboxAcquireCleanupError(
                    sandbox_id=handle.sandbox_id,
                    provider=handle.provider,
                    message="sandbox startup preparation failed",
                    cleanup_error=cleanup_exc,
                ) from cleanup_exc
            raise
        return handle

    async def _acquire_handle(
        self,
        spec: SandboxProviderSpec,
        *,
        create_semaphore: asyncio.Semaphore | None = None,
        stage_sink: StageSink | None = None,
    ) -> SandboxHandle:
        preflight_started = time.perf_counter()
        try:
            await self._provider_dispatcher.preflight(spec)
        except BaseException as exc:
            logger.debug("sandbox provider preflight failed", exc_info=True)
            _emit_stage(
                stage_sink,
                "provider_preflight",
                started_at=preflight_started,
                status="failed",
                provider=spec.provider,
                template=spec.template,
                error=_short_error(exc),
            )
            raise
        _emit_stage(
            stage_sink,
            "provider_preflight",
            started_at=preflight_started,
            status="succeeded",
            provider=spec.provider,
            template=spec.template,
        )
        if (
            spec.allocation_strategy == SandboxAllocationStrategy.CONNECT_EXISTING
            and not self._provider_dispatcher.capabilities().supports(SandboxProviderFeature.CONNECT_EXISTING)
        ):
            raise RuntimeError(f"sandbox provider does not support connect_existing: {spec.provider}")
        stage = (
            "sandbox_connect"
            if spec.allocation_strategy == SandboxAllocationStrategy.CONNECT_EXISTING
            else "sandbox_create"
        )
        if create_semaphore is not None:
            async with create_semaphore:
                return await self._shielded_provider_acquire_once(spec, stage_sink=stage_sink, stage=stage)
        return await self._shielded_provider_acquire_once(spec, stage_sink=stage_sink, stage=stage)

    async def _shielded_provider_acquire_once(
        self,
        spec: SandboxProviderSpec,
        *,
        stage_sink: StageSink | None = None,
        stage: str,
    ) -> SandboxHandle:
        acquire_task = asyncio.create_task(self._provider_acquire_once(spec, stage_sink=stage_sink, stage=stage))
        try:
            return await asyncio.shield(acquire_task)
        except asyncio.CancelledError:
            if not acquire_task.done():
                logger.warning("sandbox acquire canceled; waiting for provider handle to clean up", exc_info=True)
            try:
                handle = await asyncio.shield(acquire_task)
            except Exception:
                logger.warning(
                    "sandbox acquire failed before a provider handle was returned",
                    extra={"provider": spec.provider, "template": spec.template},
                    exc_info=True,
                )
                raise
            try:
                await asyncio.shield(self.release(handle, strategy=spec.allocation_strategy))
            except Exception as cleanup_exc:
                raise SandboxAcquireCleanupError(
                    sandbox_id=handle.sandbox_id,
                    provider=handle.provider,
                    message="sandbox acquire was canceled after provider capacity existed",
                    cleanup_error=cleanup_exc,
                ) from cleanup_exc
            raise

    async def _provider_acquire_once(
        self,
        spec: SandboxProviderSpec,
        *,
        stage_sink: StageSink | None = None,
        stage: str,
    ) -> SandboxHandle:
        started = time.perf_counter()
        try:
            if spec.allocation_strategy == SandboxAllocationStrategy.CONNECT_EXISTING:
                handle = await self._provider_dispatcher.connect(spec)
            else:
                handle = await self._provider_dispatcher.create(spec)
        except BaseException as exc:
            logger.debug("sandbox provider acquire failed", exc_info=True)
            _emit_stage(
                stage_sink,
                stage,
                started_at=started,
                status="failed",
                provider=spec.provider,
                template=spec.template,
                error=_short_error(exc),
            )
            raise
        _emit_stage(
            stage_sink,
            stage,
            started_at=started,
            status="succeeded",
            sandbox_id=handle.sandbox_id,
            provider=handle.provider,
            template=handle.template,
        )
        return handle

    async def release(
        self,
        handle: SandboxHandle,
        *,
        strategy: SandboxAllocationStrategy,
        teardown_commands: list[SandboxCommandSpec] | None = None,
    ) -> None:
        """Run teardown commands and retire sandbox capacity.

        Args:
            handle: Sandbox being released.
            strategy: Allocation strategy that decides whether termination is owned here.
            teardown_commands: Best-effort commands to run before termination.
        """

        for command in teardown_commands or []:
            try:
                result = await self._provider_dispatcher.run_command(handle, command)
                if not result.ok and not command.allow_failure:
                    logger.warning("sandbox teardown command failed: %s", result.stderr or result.stdout)
            except Exception:
                logger.warning("sandbox teardown command raised", exc_info=True)
        if strategy != SandboxAllocationStrategy.CONNECT_EXISTING:
            await self._provider_dispatcher.terminate(handle)

    async def _upload_files(
        self,
        handle: SandboxHandle,
        uploads: list[SandboxFileUploadSpec],
        *,
        stage_sink: StageSink | None = None,
    ) -> None:
        """Upload files and apply optional modes before startup commands.

        Args:
            handle: Sandbox receiving the files.
            uploads: Ordered host-to-sandbox file staging requests.

        Raises:
            RuntimeError: If an upload source is missing, a provider upload
                fails, or a required chmod command fails.
        """

        for upload in uploads:
            artifact_started = time.perf_counter()
            try:
                parent = upload.remote_path.rsplit("/", maxsplit=1)[0] or "/"
                if upload.delivery != SandboxFileDeliveryMode.UPLOAD:
                    await self._deliver_large_artifact(handle, upload=upload, parent=parent)
                else:
                    await self._prepare_upload_parent(handle, upload=upload, parent=parent)
                    if upload.local_path is None:
                        raise RuntimeError(f"sandbox {upload.delivery.value} source path is missing")
                    source = upload.local_path.expanduser().resolve()
                    if not source.is_file():
                        raise RuntimeError(f"sandbox upload source is not a file: {source}")
                    destination_guard = SandboxCommandSpec(
                        command=self._symlink_guard_script(upload.remote_path),
                        label="sandbox-upload-destination-symlink-guard",
                        timeout_seconds=30,
                    )
                    await self._run_required_upload_command(
                        handle,
                        destination_guard,
                        operation="destination symlink guard",
                    )
                    await self._provider_dispatcher.upload(handle, source, upload.remote_path)
                    if upload.sha256:
                        checksum_command = SandboxCommandSpec(
                            command=f"echo {shlex.quote(upload.sha256 + '  ' + upload.remote_path)} | sha256sum -c -",
                            timeout_seconds=60,
                        )
                        await self._run_required_upload_command(handle, checksum_command, operation="checksum")
                    for command, operation in self._ownership_commands(
                        path=upload.remote_path,
                        owner=upload.owner,
                        group=upload.group,
                        mode=upload.mode,
                        operation_prefix="file",
                    ):
                        await self._run_required_upload_command(handle, command, operation=operation)
            except BaseException as exc:
                if upload.delivery != SandboxFileDeliveryMode.UPLOAD:
                    _emit_stage(
                        stage_sink,
                        "large_artifact_delivery",
                        started_at=artifact_started,
                        status="failed",
                        sandbox_id=handle.sandbox_id,
                        provider=handle.provider,
                        **self._large_artifact_stage_payload(upload, guest_verified=False),
                        error=_short_error(exc),
                    )
                raise
            if upload.delivery != SandboxFileDeliveryMode.UPLOAD:
                _emit_stage(
                    stage_sink,
                    "large_artifact_delivery",
                    started_at=artifact_started,
                    status="succeeded",
                    sandbox_id=handle.sandbox_id,
                    provider=handle.provider,
                    **self._large_artifact_stage_payload(upload, guest_verified=True),
                )

    async def upload_files(self, handle: SandboxHandle, uploads: list[SandboxFileUploadSpec]) -> None:
        """Stage an already-scoped set of files into an acquired sandbox."""

        await self._upload_files(handle, uploads)

    async def _deliver_large_artifact(
        self,
        handle: SandboxHandle,
        *,
        upload: SandboxFileUploadSpec,
        parent: str,
    ) -> None:
        """Transfer content into an isolated inode, then finalize it in-guest."""

        source: Path | None = None
        if upload.delivery == SandboxFileDeliveryMode.CHUNKED_UPLOAD:
            if upload.local_path is None:
                raise RuntimeError("sandbox chunked_upload source path is missing")
            source = upload.local_path.expanduser().resolve()
            if not source.is_file():
                raise RuntimeError(f"sandbox upload source is not a file: {source}")
            actual_sha256, actual_size = await asyncio.to_thread(self._content_address, source)
            if actual_sha256 != upload.sha256 or actual_size != upload.size_bytes:
                raise RuntimeError(
                    "sandbox chunked upload content address mismatch: expected "
                    f"{upload.sha256}/{upload.size_bytes}, got {actual_sha256}/{actual_size}"
                )

        staging_dir, staging_path = self._large_artifact_staging_paths(upload, parent=parent)
        await self._run_required_upload_command(
            handle,
            self._large_artifact_staging_command(upload, parent=parent, staging_dir=staging_dir),
            operation="large artifact staging",
        )
        if upload.delivery == SandboxFileDeliveryMode.GUEST_DOWNLOAD:
            await self._run_required_upload_command(
                handle,
                self._guest_download_command(upload, staging_path=staging_path),
                operation="guest download",
            )
        else:
            assert source is not None
            await self._upload_chunked_file(
                handle,
                upload=upload,
                source=source,
                staging_dir=staging_dir,
                staging_path=staging_path,
            )
        await self._run_required_upload_command(
            handle,
            self._large_artifact_finalize_command(
                upload,
                parent=parent,
                staging_dir=staging_dir,
                staging_path=staging_path,
            ),
            operation="large artifact finalize",
        )

    @staticmethod
    def _large_artifact_stage_payload(
        upload: SandboxFileUploadSpec,
        *,
        guest_verified: bool,
    ) -> dict[str, object]:
        return {
            "app_profile": upload.intended_app,
            "fixture_remote_path": upload.remote_path,
            "source_kind": ("url" if upload.delivery == SandboxFileDeliveryMode.GUEST_DOWNLOAD else "local_path"),
            "source": upload.source_host if upload.source_host else str(upload.local_path),
            "sha256": upload.sha256,
            "size_bytes": upload.size_bytes,
            "delivery": upload.delivery.value,
            "chunk_size_bytes": upload.chunk_size_bytes,
            "host_verification": upload.host_verification,
            "guest_verification": ("sha256_and_size_verified" if guest_verified else "not_verified"),
            "verified_before_init": guest_verified,
        }

    async def _prepare_upload_parent(
        self,
        handle: SandboxHandle,
        *,
        upload: SandboxFileUploadSpec,
        parent: str,
    ) -> None:
        guard_command = SandboxCommandSpec(
            command=self._symlink_guard_script(parent),
            label="sandbox-upload-symlink-guard",
            timeout_seconds=30,
        )
        await self._run_required_upload_command(handle, guard_command, operation="symlink guard")
        mkdir_command = SandboxCommandSpec(command=f"mkdir -p -- {shlex.quote(parent)}", timeout_seconds=30)
        await self._run_required_upload_command(handle, mkdir_command, operation="mkdir")
        for command, operation in self._ownership_commands(
            path=parent,
            owner=upload.parent_owner,
            group=upload.parent_group,
            mode=upload.parent_mode,
            operation_prefix="parent",
        ):
            await self._run_required_upload_command(handle, command, operation=operation)

    async def _upload_chunked_file(
        self,
        handle: SandboxHandle,
        *,
        upload: SandboxFileUploadSpec,
        source: Path,
        staging_dir: str,
        staging_path: str,
    ) -> None:
        chunk_dir = f"{staging_dir}/chunks"
        with tempfile.TemporaryDirectory(prefix="northcua-upload-chunks-") as temp_dir:
            with source.open("rb") as source_handle:
                index = 0
                while data := await asyncio.to_thread(source_handle.read, upload.chunk_size_bytes):
                    chunk = Path(temp_dir) / f"chunk-{index:012d}"
                    await asyncio.to_thread(chunk.write_bytes, data)
                    try:
                        remote_chunk = f"{chunk_dir}/chunk-{index:012d}"
                        await self._provider_dispatcher.upload(handle, chunk, remote_chunk)
                    finally:
                        chunk.unlink(missing_ok=True)
                    index += 1
        assemble_command = SandboxCommandSpec(
            command=(
                'set -eu; test "$(id -u)" -eq 0; '
                f"northcua_stage={shlex.quote(staging_dir)}; northcua_chunks={shlex.quote(chunk_dir)}; "
                f"northcua_artifact={shlex.quote(staging_path)}; "
                'test -d "$northcua_stage"; test ! -L "$northcua_stage"; '
                'test "$(stat -c %u -- "$northcua_stage")" -eq 0; '
                'test "$(stat -c %a -- "$northcua_stage")" = 700; '
                'for northcua_chunk in "$northcua_chunks"/chunk-*; do '
                'test -f "$northcua_chunk"; test ! -L "$northcua_chunk"; done; '
                'rm -f -- "$northcua_artifact"; '
                '(umask 077; cat "$northcua_chunks"/chunk-* > "$northcua_artifact"); '
                'chown root:root -- "$northcua_artifact"; chmod 0600 -- "$northcua_artifact"; '
                'rm -rf -- "$northcua_chunks"'
            ),
            label="sandbox-upload-chunk-assemble",
            timeout_seconds=300,
        )
        await self._run_required_upload_command(handle, assemble_command, operation="chunk assembly")

    @classmethod
    def _guest_download_command(
        cls,
        upload: SandboxFileUploadSpec,
        *,
        staging_path: str,
    ) -> SandboxCommandSpec:
        resolved_address = f"[{upload.source_address}]" if ":" in upload.source_address else upload.source_address
        resolve_entry = f"{upload.source_host}:{upload.source_port}:{resolved_address}"
        command = (
            'set -eu; test "$(id -u)" -eq 0; command -v curl >/dev/null; '
            f'northcua_artifact={shlex.quote(staging_path)}; rm -f -- "$northcua_artifact"; '
            "for northcua_proxy_var in $(env | sed -n "
            "'s/^\\([A-Za-z_][A-Za-z0-9_]*[Pp][Rr][Oo][Xx][Yy]\\)=.*/\\1/p'); do "
            'unset "$northcua_proxy_var"; done; '
            "if env | grep -E '^[A-Za-z_][A-Za-z0-9_]*[Pp][Rr][Oo][Xx][Yy]=' >/dev/null; then exit 1; fi; "
            "umask 077; "
            f"northcua_status=$(curl --disable --fail --silent --show-error --noproxy '*' --proto '=http,https' "
            f"--connect-timeout 30 --max-time 540 --max-filesize {upload.size_bytes} "
            f"--resolve {shlex.quote(resolve_entry)} "
            f"--write-out '%{{http_code}}' --output \"$northcua_artifact\" {shlex.quote(upload.source_url)}); "
            'case "$northcua_status" in 2??) ;; *) exit 1 ;; esac; '
            'chown root:root -- "$northcua_artifact"; chmod 0600 -- "$northcua_artifact"'
        )
        return SandboxCommandSpec(
            command=command,
            label="sandbox-guest-download",
            timeout_seconds=600,
            env={name: "" for name in _PROXY_ENV_NAMES},
            redact=[upload.source_url],
        )

    @staticmethod
    def _large_artifact_staging_paths(upload: SandboxFileUploadSpec, *, parent: str) -> tuple[str, str]:
        destination_key = hashlib.sha256(upload.remote_path.encode("utf-8")).hexdigest()[:12]
        parent_prefix = "" if parent == "/" else parent
        staging_dir = f"{parent_prefix}/.northcua-stage-{upload.sha256[:16]}-{destination_key}"
        return staging_dir, f"{staging_dir}/artifact"

    @classmethod
    def _large_artifact_staging_command(
        cls,
        upload: SandboxFileUploadSpec,
        *,
        parent: str,
        staging_dir: str,
    ) -> SandboxCommandSpec:
        chunks_command = ""
        if upload.delivery == SandboxFileDeliveryMode.CHUNKED_UPLOAD:
            chunks_command = (
                f"northcua_chunks={shlex.quote(staging_dir + '/chunks')}; "
                '(umask 077; mkdir -- "$northcua_chunks"); '
                'chown root:root -- "$northcua_chunks"; chmod 0700 -- "$northcua_chunks"; '
            )
        command = (
            f'{cls._symlink_guard_script(parent)}; test "$(id -u)" -eq 0; '
            f"northcua_parent={shlex.quote(parent)}; northcua_stage={shlex.quote(staging_dir)}; "
            'umask 022; mkdir -p -- "$northcua_parent"; '
            f"{cls._secure_ancestry_script(parent)}; "
            'rm -rf -- "$northcua_stage"; (umask 077; mkdir -- "$northcua_stage"); '
            'chown root:root -- "$northcua_stage"; chmod 0700 -- "$northcua_stage"; '
            'test -d "$northcua_stage"; test ! -L "$northcua_stage"; '
            'test "$(stat -c %u -- "$northcua_stage")" -eq 0; '
            'test "$(stat -c %a -- "$northcua_stage")" = 700; '
            f"{chunks_command}"
        )
        return SandboxCommandSpec(
            command=command,
            label="sandbox-large-artifact-staging",
            timeout_seconds=30,
        )

    @classmethod
    def _large_artifact_finalize_command(
        cls,
        upload: SandboxFileUploadSpec,
        *,
        parent: str,
        staging_dir: str,
        staging_path: str,
    ) -> SandboxCommandSpec:
        file_ownership = cls._inline_ownership_script(
            path_variable="northcua_artifact",
            owner=upload.owner,
            group=upload.group,
            mode=upload.mode,
        )
        parent_ownership = cls._inline_ownership_script(
            path_variable="northcua_parent",
            owner=upload.parent_owner,
            group=upload.parent_group,
            mode=upload.parent_mode,
        )
        command = (
            f'{cls._symlink_guard_script(upload.remote_path)}; test "$(id -u)" -eq 0; '
            f"northcua_parent={shlex.quote(parent)}; northcua_stage={shlex.quote(staging_dir)}; "
            f"northcua_artifact={shlex.quote(staging_path)}; northcua_final={shlex.quote(upload.remote_path)}; "
            f"{cls._secure_ancestry_script(parent)}; "
            'test -d "$northcua_stage"; test ! -L "$northcua_stage"; '
            'test "$(stat -c %u -- "$northcua_stage")" -eq 0; '
            'test "$(stat -c %a -- "$northcua_stage")" = 700; '
            'test -f "$northcua_artifact"; test ! -L "$northcua_artifact"; '
            'test "$(stat -c %u -- "$northcua_artifact")" -eq 0; '
            'test "$(stat -c %a -- "$northcua_artifact")" = 600; '
            'test "$(stat -c %h -- "$northcua_artifact")" -eq 1; '
            f'test "$(wc -c < "$northcua_artifact")" -eq {upload.size_bytes}; '
            f"echo {shlex.quote(upload.sha256 + '  ' + staging_path)} | sha256sum -c -; "
            'northcua_inode=$(stat -c %d:%i -- "$northcua_artifact"); '
            f"{cls._symlink_guard_script(upload.remote_path)}; {cls._secure_ancestry_script(parent)}; "
            'if [ -e "$northcua_final" ]; then test -f "$northcua_final"; test ! -L "$northcua_final"; fi; '
            f"{file_ownership}"
            'mv -fT -- "$northcua_artifact" "$northcua_final"; '
            'test -f "$northcua_final"; test ! -L "$northcua_final"; '
            'test "$(stat -c %d:%i -- "$northcua_final")" = "$northcua_inode"; '
            'rmdir -- "$northcua_stage"; '
            f"{parent_ownership}"
        )
        return SandboxCommandSpec(
            command=command,
            label="sandbox-large-artifact-finalize",
            timeout_seconds=300,
        )

    @staticmethod
    def _inline_ownership_script(
        *,
        path_variable: str,
        owner: str,
        group: str,
        mode: str,
    ) -> str:
        quoted_variable = f'"${path_variable}"'
        commands: list[str] = []
        if owner and group:
            commands.append(f"chown {shlex.quote(f'{owner}:{group}')} -- {quoted_variable}")
        elif owner:
            commands.append(f"chown {shlex.quote(owner)} -- {quoted_variable}")
        elif group:
            commands.append(f"chgrp {shlex.quote(group)} -- {quoted_variable}")
        if mode:
            commands.append(f"chmod {shlex.quote(mode)} -- {quoted_variable}")
        return "; ".join(commands) + ("; " if commands else "")

    @staticmethod
    def _content_address(path: Path) -> tuple[str, int]:
        digest = hashlib.sha256()
        size_bytes = 0
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
                size_bytes += len(chunk)
        return digest.hexdigest(), size_bytes

    @staticmethod
    def _symlink_guard_script(path: str) -> str:
        return (
            f"set -eu; northcua_path={shlex.quote(path)}; "
            'while [ "$northcua_path" != / ]; do '
            'if [ -L "$northcua_path" ]; then '
            'echo "sandbox upload path contains symlink: $northcua_path" >&2; exit 1; fi; '
            'northcua_path=${northcua_path%/*}; [ -n "$northcua_path" ] || northcua_path=/; done'
        )

    @staticmethod
    def _secure_ancestry_script(path: str) -> str:
        return (
            f"northcua_path={shlex.quote(path)}; "
            "while :; do "
            'if [ -L "$northcua_path" ] || [ ! -d "$northcua_path" ]; then '
            'echo "sandbox artifact ancestry is not a directory: $northcua_path" >&2; exit 1; fi; '
            'if [ "$(stat -c %u -- "$northcua_path")" -ne 0 ]; then '
            'echo "sandbox artifact ancestry is not root-owned: $northcua_path" >&2; exit 1; fi; '
            'northcua_mode_text=$(stat -c %a -- "$northcua_path"); northcua_mode=$((0$northcua_mode_text)); '
            "if [ $((northcua_mode & 022)) -ne 0 ] && [ $((northcua_mode & 01000)) -eq 0 ]; then "
            'echo "sandbox artifact ancestry is writable without sticky protection: $northcua_path" >&2; exit 1; fi; '
            '[ "$northcua_path" = / ] && break; '
            'northcua_path=${northcua_path%/*}; [ -n "$northcua_path" ] || northcua_path=/; '
            "done"
        )

    async def _run_required_upload_command(
        self,
        handle: SandboxHandle,
        command: SandboxCommandSpec,
        *,
        operation: str,
    ) -> None:
        result = await self._provider_dispatcher.run_command(handle, command)
        if not result.ok:
            raise RuntimeError(f"sandbox upload {operation} failed: {result.stderr or result.stdout}")

    @staticmethod
    def _ownership_commands(
        *,
        path: str,
        owner: str,
        group: str,
        mode: str,
        operation_prefix: str,
    ) -> list[tuple[SandboxCommandSpec, str]]:
        quoted_path = shlex.quote(path)
        guard = SandboxLifecycleManager._symlink_guard_script(path)
        commands: list[tuple[SandboxCommandSpec, str]] = []
        if owner and group:
            principal_command = f"{guard}; chown {shlex.quote(f'{owner}:{group}')} {quoted_path}"
            commands.append(
                (SandboxCommandSpec(command=principal_command, timeout_seconds=30), f"{operation_prefix} chown")
            )
        elif owner:
            principal_command = f"{guard}; chown {shlex.quote(owner)} {quoted_path}"
            commands.append(
                (SandboxCommandSpec(command=principal_command, timeout_seconds=30), f"{operation_prefix} chown")
            )
        elif group:
            principal_command = f"{guard}; chgrp {shlex.quote(group)} {quoted_path}"
            commands.append(
                (SandboxCommandSpec(command=principal_command, timeout_seconds=30), f"{operation_prefix} chgrp")
            )
        if mode:
            chmod_command = f"{guard}; chmod {shlex.quote(mode)} {quoted_path}"
            commands.append(
                (SandboxCommandSpec(command=chmod_command, timeout_seconds=30), f"{operation_prefix} chmod")
            )
        return commands


def _sandbox_command_failure_message(
    prefix: str,
    command: SandboxCommandSpec,
    result: SandboxCommandResult,
) -> str:
    capability_evidence = guest_capability_evidence_payloads(f"{result.stdout}\n{result.stderr}")
    evidence_suffix = ""
    if capability_evidence:
        evidence_suffix = " evidence=" + " ".join(
            f"{GUEST_CAPABILITY_EVIDENCE_PREFIX}{json.dumps(item, separators=(',', ':'))}"
            for item in capability_evidence
        )
    stderr_head = _head_if_truncated(result.stderr)
    stdout_head = _head_if_truncated(result.stdout)
    head_suffix = ""
    if stderr_head:
        head_suffix += f" stderr_head={stderr_head!r}"
    if stdout_head:
        head_suffix += f" stdout_head={stdout_head!r}"
    return (
        f"{prefix}: "
        f"label={command.label or '<unlabeled>'!r} "
        f"exit_code={result.exit_code} "
        f"timed_out={result.timed_out} "
        f"failure_kind={_sandbox_command_failure_kind(result.stderr or result.stdout)} "
        f"stderr_tail={_tail(result.stderr)!r} "
        f"stdout_tail={_tail(result.stdout)!r}"
        f"{head_suffix}"
        f"{evidence_suffix}"
    )


def _sandbox_command_failure_kind(output: str) -> str:
    normalized = output.lower()
    if GUEST_CAPABILITY_EVIDENCE_PREFIX.lower() in normalized and '"status":"unavailable"' in normalized:
        return "guest_capability_unavailable"
    if "window readiness timed out" in normalized:
        if "before process appeared" in normalized:
            return "window_readiness_timeout_before_process_appeared"
        if "after process appeared" in normalized:
            return "window_readiness_timeout_after_process_appeared"
        return "window_readiness_timeout"
    if "dbus" in normalized or "d-bus" in normalized:
        return "dbus"
    if "no display" in normalized or "cannot open display" in normalized:
        return "display"
    return "sandbox_command_failed"


def _tail(value: str, limit: int = 1000) -> str:
    stripped = value.strip()
    if len(stripped) <= limit:
        return stripped
    return stripped[-limit:]


def _head_if_truncated(value: str, limit: int = 1000) -> str:
    stripped = value.strip()
    if len(stripped) <= limit:
        return ""
    return stripped[:limit]


def _emit_stage(
    sink: StageSink | None,
    stage: str,
    *,
    started_at: float,
    status: str,
    **payload: object,
) -> None:
    if sink is None:
        return
    try:
        sink(
            stage,
            {
                **payload,
                "stage": stage,
                "status": status,
                "duration_ms": max(0, int((time.perf_counter() - started_at) * 1000)),
            },
        )
    except Exception:
        logger.warning("sandbox stage telemetry sink failed", extra={"stage": stage}, exc_info=True)


def _short_error(exc: BaseException, limit: int = 300) -> str:
    return f"{type(exc).__name__}: {str(exc)[:limit]}"
