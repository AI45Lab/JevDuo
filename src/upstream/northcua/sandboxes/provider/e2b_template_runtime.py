"""E2B template build helpers for the sandbox provider runtime."""

from __future__ import annotations

import asyncio
import importlib
import inspect
import itertools
import logging
import os
import re
import shlex
import sys
import threading
from contextlib import asynccontextmanager
from pathlib import Path
from typing import AsyncIterator, Awaitable, Callable, Protocol, SupportsInt, cast
from urllib.parse import urlsplit, urlunsplit

from northcua.sandboxes.contracts import SandboxImageLayer, SandboxTemplateLayer, SandboxTemplateSpec

logger = logging.getLogger(__name__)
_BUILD_HEARTBEAT_SECONDS = 30.0
_DEFAULT_ARCHIVE_DOWNLOAD_MAX_TIME_SECONDS = 3600
_DEFAULT_ARCHIVE_DOWNLOAD_CONNECT_TIMEOUT_SECONDS = 15
_SECRET_PATTERNS = (
    re.compile(r"e2b_[A-Za-z0-9]{20,}"),
    re.compile(r"""(?i)(authorization\s*:\s*bearer\s+)(?:"[^"\r\n]*"|'[^'\r\n]*'|[^\s,}\"']+)"""),
    re.compile(r"""(?ix)
        (["']?(?:api[_-]?key|token|secret|password)["']?\s*[:=]\s*)
        (?:"(?:\\.|[^"\\\r\n])*"|'(?:\\.|[^'\\\r\n])*'|[^\s,}]+)
        """),
    re.compile(r"(https?://)([^/@\s]+)@"),
    re.compile(
        r"(?i)([?&](?:token|sig|signature|x-amz-signature|x-amz-credential|x-tos-signature|"
        r"x-tos-credential|api_key|key|credential)=)[^&\s]+"
    ),
)
_REPO_ROOT = Path(__file__).resolve().parents[3]
_HK_TOS_BROKEN_SUFFIX = ".tos-s3-cn-hongkong.ivolces.com"
_HK_TOS_PUBLIC_SUFFIX = ".tos-s3-cn-hongkong.volces.com"
_E2B_SDK_PATCH_LOCK = threading.Lock()
_UPLOAD_CHUNK_SIZE_BYTES = 1024 * 1024


@asynccontextmanager
async def _exclusive_e2b_sdk_patch() -> AsyncIterator[None]:
    """Serialize process-global E2B SDK patches without blocking an event loop."""

    state_guard = threading.Lock()
    abandoned = False
    acquired = False

    def acquire_or_release_if_abandoned() -> bool:
        nonlocal acquired
        _E2B_SDK_PATCH_LOCK.acquire()
        with state_guard:
            if abandoned:
                _E2B_SDK_PATCH_LOCK.release()
                return False
            acquired = True
            return True

    acquire_task = asyncio.create_task(asyncio.to_thread(acquire_or_release_if_abandoned))
    try:
        acquired_by_owner = await asyncio.shield(acquire_task)
    except asyncio.CancelledError:
        with state_guard:
            abandoned = True
            if acquired:
                _E2B_SDK_PATCH_LOCK.release()
                acquired = False
        raise
    if not acquired_by_owner:
        raise RuntimeError("failed to acquire the E2B SDK patch lock")
    try:
        yield
    finally:
        with state_guard:
            if acquired:
                _E2B_SDK_PATCH_LOCK.release()
                acquired = False


class _E2BTemplateBuilder(Protocol):
    """Subset of the E2B TemplateBuilder API used by NorthCUA."""

    def copy(self, src: str, dest: str) -> "_E2BTemplateBuilder":
        """Add a local file or directory copy layer to the template.

        Args:
            src: Local source path in the build context.
            dest: Destination path inside the template image.

        Returns:
            Builder updated with the copy layer.
        """

        ...

    def run_cmd(self, command: str | list[str], user: str | None = None) -> "_E2BTemplateBuilder":
        """Add one or more shell commands to the template build.

        Args:
            command: Shell command string or command list accepted by E2B.
            user: Optional existing guest user used to execute the command.

        Returns:
            Builder updated with the command layer.
        """

        ...

    def set_envs(self, envs: dict[str, str]) -> "_E2BTemplateBuilder":
        """Set build-time environment variables on the template builder.

        Args:
            envs: Environment variables used by later build layers.

        Returns:
            Builder updated with the environment values.
        """

        ...

    def set_user(self, user: str) -> "_E2BTemplateBuilder":
        """Set the user used by subsequent template build layers.

        Args:
            user: Linux user name for later build commands.

        Returns:
            Builder updated with the active user.
        """

        ...

    def set_start_cmd(self, start_cmd: str, ready_cmd: str) -> "_E2BTemplateBuilder":
        """Configure the sandbox start and readiness commands."""

        ...


class E2BTemplateRuntimeMixin:
    """Build E2B templates while emitting operator progress."""

    @staticmethod
    def _skip_build_cache(spec: SandboxTemplateSpec) -> bool:
        """Resolve the per-spec E2B cache policy while keeping rebuilds as the default."""

        value = spec.metadata.get("e2b_skip_cache", True)
        if not isinstance(value, bool):
            raise ValueError("template metadata e2b_skip_cache must be a boolean")
        return value

    @staticmethod
    def _run_batch_sizes(spec: SandboxTemplateSpec) -> tuple[int, ...]:
        """Return explicit RUN batching boundaries for cache-sensitive builds."""

        value = spec.metadata.get("e2b_run_batch_sizes")
        if value is None:
            return ()
        if (
            not isinstance(value, list)
            or not value
            or any(not isinstance(item, int) or isinstance(item, bool) or item <= 0 for item in value)
        ):
            raise ValueError("template metadata e2b_run_batch_sizes must be a non-empty list of positive integers")
        image_layers = list(spec.image.layers) if spec.image is not None else []
        all_layers = [*spec.layers, *image_layers]
        run_count = sum(layer.kind == "run" for layer in all_layers)
        if sum(value) != run_count:
            raise ValueError("template metadata e2b_run_batch_sizes must account for every run layer")
        unsupported = sorted({layer.kind for layer in all_layers if layer.kind not in {"run", "copy", "expose"}})
        if unsupported:
            raise ValueError(
                "template metadata e2b_run_batch_sizes only supports run/copy/expose layers; found: "
                + ", ".join(unsupported)
            )
        return tuple(value)

    @staticmethod
    def _explicit_run_user(spec: SandboxTemplateSpec) -> str | None:
        """Use an existing user per RUN without emitting an E2B USER layer."""

        value = spec.metadata.get("e2b_explicit_run_user")
        if value is None:
            return None
        if value != "root":
            raise ValueError("template metadata e2b_explicit_run_user currently supports only root")
        return value

    @staticmethod
    def _startup_commands(spec: SandboxTemplateSpec) -> tuple[str | None, str | None]:
        """Return an explicit paired E2B start/ready command contract."""

        start_cmd = spec.metadata.get("e2b_start_cmd")
        ready_cmd = spec.metadata.get("e2b_ready_cmd")
        if start_cmd is None and ready_cmd is None:
            return None, None
        if (
            not isinstance(start_cmd, str)
            or not start_cmd.strip()
            or "\0" in start_cmd
            or not isinstance(ready_cmd, str)
            or not ready_cmd.strip()
            or "\0" in ready_cmd
        ):
            raise ValueError("template metadata e2b_start_cmd/e2b_ready_cmd must be paired non-empty strings")
        return start_cmd, ready_cmd

    async def _build_template_async(self, spec: SandboxTemplateSpec) -> str:
        """Build a template from typed NorthCUA sandbox template specs.

        Args:
            spec: Template spec containing base image/template and layers.

        Returns:
            Built template alias returned by E2B.

        Raises:
            RuntimeError: When the optional E2B dependency is unavailable.
        """

        try:
            async_template_cls = importlib.import_module("e2b").AsyncTemplate
        except ModuleNotFoundError as exc:
            raise RuntimeError("Install E2B support with `uv sync --extra e2b`.") from exc

        self._progress(
            "e2b template build: preparing",
            alias=spec.alias,
            desktop=spec.desktop,
            layers=len(spec.layers) + (len(spec.image.layers) if spec.image is not None else 0),
        )
        template_root = async_template_cls()
        build_context = self._resolve_build_context(spec)
        skip_cache = self._skip_build_cache(spec)
        run_batch_sizes = self._run_batch_sizes(spec)
        explicit_run_user = self._explicit_run_user(spec)
        start_cmd, ready_cmd = self._startup_commands(spec)
        run_batch_boundaries = set(itertools.accumulate(run_batch_sizes))
        base_template = spec.base_template or os.getenv("E2B_BASE_TEMPLATE", "")
        base_image = self._resolve_base_image(spec)
        base_kind = self._base_kind(base_template=base_template, base_image=base_image)
        if base_template:
            template = self._template_from_base_selector(
                template_root,
                base_template=base_template,
                base_image=base_image,
            )
            self._set_file_context(template, build_context=build_context)
            self._progress(
                "e2b template build: using configured base template",
                alias=spec.alias,
                base_template=base_template,
            )
        elif base_image:
            template = self._template_from_base_selector(
                template_root,
                base_template=base_template,
                base_image=base_image,
            )
            self._set_file_context(template, build_context=build_context)
            self._progress(
                "e2b template build: using configured base image",
                alias=spec.alias,
                base_kind=base_kind,
            )
        else:
            template = cast(_E2BTemplateBuilder, template_root.from_ubuntu_image())
            self._set_file_context(template, build_context=build_context)
            self._progress("e2b template build: using ubuntu base image", alias=spec.alias)
        if explicit_run_user is None:
            template = template.set_user("root")
        image_layers = list(spec.image.layers) if spec.image is not None else []
        commands: list[str] = []
        submitted_commands = 0
        observed_run_layers = 0

        def flush_commands() -> None:
            """Submit buffered shell commands before non-command layers."""

            nonlocal submitted_commands, template, commands
            if not commands:
                return
            template = template.run_cmd(commands, user=explicit_run_user)
            submitted_commands += len(commands)
            commands = []

        for layer in [*spec.layers, *image_layers]:
            if layer.kind == "apt" and layer.packages:
                commands.append(
                    self._bash_command(
                        "apt-get update && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends "
                        + " ".join(shlex.quote(item) for item in layer.packages)
                    )
                )
            elif layer.kind == "pip" and layer.packages:
                commands.append(
                    self._bash_command(
                        "python3 -m pip install " + " ".join(shlex.quote(item) for item in layer.packages)
                    )
                )
            elif layer.kind == "run" and layer.command:
                commands.append(self._bash_command(layer.command))
                observed_run_layers += 1
                if observed_run_layers in run_batch_boundaries:
                    flush_commands()
            elif layer.kind == "env":
                for key, value in layer.env.items():
                    commands.append(f"printf '%s\\n' {shlex.quote(f'{key}={value}')} >> /etc/environment")
            elif layer.kind == "expose":
                flush_commands()
                logger.info("E2B template expose layer is metadata-only", extra={"alias": spec.alias})
            elif layer.kind == "copy":
                flush_commands()
                for local, remote in layer.files.items():
                    template = template.copy(local, remote)
            elif layer.kind == "git":
                if layer.source_env:
                    flush_commands()
                    template = self._set_build_source_env(template, layer.source_env)
                commands.append(self._git_source_command(layer))
            elif layer.kind == "archive":
                if layer.source_env:
                    flush_commands()
                    template = self._set_build_source_env(template, layer.source_env)
                commands.append(self._archive_source_command(layer))
        flush_commands()
        if start_cmd is not None and ready_cmd is not None:
            template = template.set_start_cmd(start_cmd, ready_cmd)
        self._progress(
            "e2b template build: submitting",
            alias=spec.alias,
            commands=submitted_commands,
            cpu_count=spec.resources.cpu_count or 2,
            memory_mb=spec.resources.memory_mb or 1024,
            skip_cache=skip_cache,
            run_batch_sizes=run_batch_sizes or None,
            explicit_run_user=explicit_run_user,
            startup_commands_configured=start_cmd is not None,
        )
        heartbeat_task = asyncio.create_task(self._template_build_heartbeat(spec.alias))
        try:
            async with _exclusive_e2b_sdk_patch():
                upload_module: object | None = None
                original_upload: object | None = None
                original_upload_link: object | None = None
                original_trigger_build: object | None = None
                if spec.metadata.get("e2b_safe_hk_tos_upload") is True:
                    upload_module = importlib.import_module("e2b.template_async.main")
                    original_upload = getattr(upload_module, "upload_file")
                    original_upload_link = getattr(upload_module, "get_file_upload_link")
                    original_trigger_build = getattr(upload_module, "trigger_build")

                    async def retrying_upload_link(*args: object, **kwargs: object) -> object:
                        return await self._retrying_get_file_upload_link(
                            cast(Callable[..., Awaitable[object]], original_upload_link),
                            *args,
                            **kwargs,
                        )

                    async def retrying_trigger_build(*args: object, **kwargs: object) -> object:
                        return await self._retrying_trigger_build(
                            cast(Callable[..., Awaitable[object]], original_trigger_build),
                            *args,
                            **kwargs,
                        )

                    setattr(upload_module, "upload_file", self._safe_hk_tos_upload_file)
                    setattr(upload_module, "get_file_upload_link", retrying_upload_link)
                    setattr(upload_module, "trigger_build", retrying_trigger_build)
                try:
                    await async_template_cls.build(
                        template,
                        alias=spec.alias,
                        memory_mb=spec.resources.memory_mb or 1024,
                        cpu_count=spec.resources.cpu_count or 2,
                        skip_cache=skip_cache,
                        on_build_logs=lambda log: self._progress(
                            "e2b template build log", alias=spec.alias, log=str(log)
                        ),
                    )
                finally:
                    if upload_module is not None and original_upload is not None:
                        setattr(upload_module, "upload_file", original_upload)
                    if upload_module is not None and original_upload_link is not None:
                        setattr(upload_module, "get_file_upload_link", original_upload_link)
                    if upload_module is not None and original_trigger_build is not None:
                        setattr(upload_module, "trigger_build", original_trigger_build)
        finally:
            heartbeat_task.cancel()
            try:
                await heartbeat_task
            except asyncio.CancelledError:
                pass
        self._progress("e2b template build: completed", alias=spec.alias)
        return spec.alias

    async def _retrying_get_file_upload_link(
        self,
        original: Callable[..., Awaitable[object]],
        *args: object,
        **kwargs: object,
    ) -> object:
        """Retry transient HK controller failures while requesting COPY upload links."""

        try:
            import httpx
            from e2b.exceptions import FileUploadException, RateLimitException
        except ModuleNotFoundError as exc:
            raise RuntimeError("Install E2B support with `uv sync --extra e2b`.") from exc

        retryable_status = re.compile(r"^(?:408|425|500|502|503|504):")
        retryable_exceptions = (FileUploadException, RateLimitException, httpx.TransportError)
        for attempt in range(1, 7):
            await asyncio.sleep(0.25)
            try:
                return await original(*args, **kwargs)
            except retryable_exceptions as exc:
                retryable = isinstance(exc, (RateLimitException, httpx.TransportError)) or (
                    isinstance(exc, FileUploadException) and retryable_status.match(str(exc)) is not None
                )
                if attempt == 6 or not retryable:
                    raise
                delay_seconds = min(2**attempt, 30)
                self._progress(
                    "e2b template upload-link retry",
                    attempt=attempt,
                    delay_seconds=delay_seconds,
                    error_type=type(exc).__name__,
                )
                await asyncio.sleep(delay_seconds)
        raise AssertionError("unreachable upload-link retry state")

    async def _retrying_trigger_build(
        self,
        original: Callable[..., Awaitable[object]],
        *args: object,
        **kwargs: object,
    ) -> object:
        """Retry only trigger failures that prove the POST was not accepted."""

        try:
            import httpx
            from e2b.exceptions import BuildException, RateLimitException
        except ModuleNotFoundError as exc:
            raise RuntimeError("Install E2B support with `uv sync --extra e2b`.") from exc

        retryable_exceptions = (
            BuildException,
            RateLimitException,
            httpx.ConnectError,
            httpx.ConnectTimeout,
        )
        for attempt in range(1, 7):
            try:
                return await original(*args, **kwargs)
            except retryable_exceptions as exc:
                retryable = isinstance(exc, (RateLimitException, httpx.ConnectError, httpx.ConnectTimeout))
                if attempt == 6 or not retryable:
                    raise
                delay_seconds = min(2**attempt, 30)
                self._progress(
                    "e2b template trigger retry",
                    attempt=attempt,
                    delay_seconds=delay_seconds,
                    error_type=type(exc).__name__,
                )
                await asyncio.sleep(delay_seconds)
        raise AssertionError("unreachable trigger retry state")

    @staticmethod
    async def _safe_hk_tos_upload_file(
        api_client: object,
        file_name: str,
        context_path: str,
        url: str,
        ignore_patterns: list[str],
        resolve_symlinks: bool,
        gzip: bool | object = True,
        stack_trace: object | None = None,
        request_timeout: float | None = None,
    ) -> None:
        """Upload one E2B COPY archive without forwarding API credentials to TOS."""

        del api_client
        if not isinstance(gzip, bool):
            if stack_trace is not None:
                raise TypeError("ambiguous legacy E2B upload arguments")
            stack_trace = gzip
            gzip = True
        try:
            import httpx
            from e2b.exceptions import FileUploadException
            from e2b.template.utils import tar_file_stream
        except ModuleNotFoundError as exc:
            raise RuntimeError("Install E2B support with `uv sync --extra e2b`.") from exc

        connect_url, signed_host = E2BTemplateRuntimeMixin._rewrite_hk_tos_upload_url(url)
        tar_args: tuple[object, ...] = (
            file_name,
            context_path,
            ignore_patterns,
            resolve_symlinks,
        )
        if "gzip" in inspect.signature(tar_file_stream).parameters:
            tar_args = (*tar_args, gzip)
        tar_buffer = await asyncio.to_thread(tar_file_stream, *tar_args)
        try:
            archive_size = await E2BTemplateRuntimeMixin._stream_size(tar_buffer)
            storage = await E2BTemplateRuntimeMixin._stream_storage_kind(tar_buffer)
            E2BTemplateRuntimeMixin._progress(
                "e2b template upload: archive ready",
                archive_bytes=archive_size,
                archive_storage=storage,
            )
            headers = {"Host": signed_host, "Content-Length": str(archive_size)}
            upload_timeout = max(float(request_timeout or 0.0), 1800.0)
            timeout = httpx.Timeout(connect=20.0, write=upload_timeout, read=120.0, pool=20.0)
            retryable_statuses = {408, 425, 429, 500, 502, 503, 504}
            retryable_errors = (
                httpx.ConnectError,
                httpx.ReadError,
                httpx.WriteError,
                httpx.TimeoutException,
                TimeoutError,
            )
            last_error = "unknown"
            async with httpx.AsyncClient(
                verify=True,
                trust_env=False,
                follow_redirects=False,
                timeout=timeout,
                headers={},
                cookies=None,
            ) as client:
                for attempt in range(1, 5):
                    await asyncio.to_thread(tar_buffer.seek, 0)
                    try:
                        async with asyncio.timeout(upload_timeout):
                            response = await client.put(
                                connect_url,
                                headers=headers,
                                content=E2BTemplateRuntimeMixin._aiter_file_chunks(tar_buffer),
                            )
                    except retryable_errors as exc:
                        last_error = type(exc).__name__
                    else:
                        if response.status_code < 300:
                            E2BTemplateRuntimeMixin._progress(
                                "e2b template upload: completed",
                                archive_bytes=archive_size,
                                attempts=attempt,
                            )
                            return
                        last_error = f"HTTP {response.status_code}"
                        if response.status_code not in retryable_statuses:
                            break
                    if attempt < 4:
                        await asyncio.sleep(min(2 ** (attempt - 1), 8))
            error = FileUploadException(f"safe TOS upload failed: {last_error}")
            if stack_trace is not None:
                raise error.with_traceback(stack_trace)  # type: ignore[arg-type]
            raise error
        finally:
            close = getattr(tar_buffer, "close", None)
            if callable(close):
                try:
                    await asyncio.to_thread(close)
                except Exception:
                    logger.warning("failed to close E2B template upload archive", exc_info=True)

    @staticmethod
    async def _stream_storage_kind(stream: object) -> str:
        """Classify a stream without assuming every ``fileno`` method works."""

        fileno = getattr(stream, "fileno", None)
        if not callable(fileno):
            return "memory"
        try:
            await asyncio.to_thread(fileno)
        except (OSError, ValueError):
            return "memory"
        return "disk"

    @staticmethod
    async def _stream_size(stream: object) -> int:
        """Return a seekable stream size without materializing its contents."""

        seek = getattr(stream, "seek", None)
        tell = getattr(stream, "tell", None)
        if not callable(seek) or not callable(tell):
            raise TypeError("E2B tar archive must be seekable")

        def size_and_rewind() -> int:
            seek(0, os.SEEK_END)
            raw_size = tell()
            if not isinstance(raw_size, (str, bytes, bytearray, SupportsInt)):
                raise TypeError("E2B tar archive returned an invalid position")
            size = int(raw_size)
            seek(0)
            return size

        return await asyncio.to_thread(size_and_rewind)

    @staticmethod
    async def _aiter_file_chunks(stream: object) -> AsyncIterator[bytes]:
        """Read a synchronous E2B archive in bounded chunks off the event loop."""

        read = getattr(stream, "read", None)
        if not callable(read):
            raise TypeError("E2B tar archive is not readable")
        while True:
            chunk = await asyncio.to_thread(read, _UPLOAD_CHUNK_SIZE_BYTES)
            if not chunk:
                return
            if not isinstance(chunk, bytes):
                raise TypeError("E2B tar archive produced a non-bytes chunk")
            yield chunk

    @staticmethod
    def _rewrite_hk_tos_upload_url(url: str) -> tuple[str, str]:
        """Return a public TLS endpoint while preserving the TOS-signed Host header."""

        parsed = urlsplit(url)
        host = parsed.hostname or ""
        if (
            parsed.scheme != "https"
            or parsed.username
            or parsed.password
            or parsed.port not in {None, 443}
            or not parsed.query
            or not host.endswith(_HK_TOS_BROKEN_SUFFIX)
            or host == _HK_TOS_BROKEN_SUFFIX.removeprefix(".")
        ):
            raise RuntimeError("E2B upload URL is not an allowlisted signed HK TOS endpoint")
        bucket = host.removesuffix(_HK_TOS_BROKEN_SUFFIX)
        if not re.fullmatch(r"[a-z0-9][a-z0-9.-]{0,61}[a-z0-9]", bucket):
            raise RuntimeError("E2B upload URL has an invalid TOS bucket host")
        public_host = f"{bucket}{_HK_TOS_PUBLIC_SUFFIX}"
        connect_url = urlunsplit(("https", public_host, parsed.path, parsed.query, ""))
        return connect_url, host

    async def _template_build_heartbeat(self, alias: str) -> None:
        """Emit local progress while the E2B SDK waits for remote build logs."""

        started_at = asyncio.get_running_loop().time()
        while True:
            await asyncio.sleep(_BUILD_HEARTBEAT_SECONDS)
            elapsed = int(asyncio.get_running_loop().time() - started_at)
            self._progress("e2b template build: waiting", alias=alias, elapsed_seconds=elapsed)

    @staticmethod
    def _progress(message: str, **fields: object) -> None:
        """Print a redacted operator-facing provider progress line."""

        rendered_fields = " ".join(
            f"{key}={E2BTemplateRuntimeMixin._redact(str(value))}" for key, value in fields.items() if key != "log"
        )
        log_text = E2BTemplateRuntimeMixin._redact(str(fields.get("log", "")))
        line = f"[northcua.sandbox.e2b] {message}"
        if rendered_fields:
            line = f"{line} {rendered_fields}"
        if log_text:
            line = f"{line}\n{log_text}"
        print(line, file=sys.stderr, flush=True)

    @staticmethod
    def _redact(value: str) -> str:
        redacted = value
        for pattern in _SECRET_PATTERNS:
            if pattern.pattern.startswith("(https?://)"):
                redacted = pattern.sub(lambda match: match.group(1) + "<redacted>@", redacted)
            elif pattern.pattern.startswith("(?i)([?&]"):
                redacted = pattern.sub(lambda match: match.group(1) + "<redacted>", redacted)
            elif pattern.pattern.startswith("(?i)(authorization") or pattern.pattern.startswith("(?i)([\"']?"):
                redacted = pattern.sub(lambda match: match.group(1) + "<redacted>", redacted)
            else:
                redacted = pattern.sub(
                    lambda match: match.group(1) + "<redacted>" if match.lastindex else "<redacted>",
                    redacted,
                )
        return redacted

    @staticmethod
    def _resolve_build_context(spec: SandboxTemplateSpec) -> Path:
        """Resolve an explicit local build context without accepting a missing path."""

        if not spec.build_context_env:
            return _REPO_ROOT
        raw_context = os.getenv(spec.build_context_env, "")
        if not raw_context:
            raise RuntimeError(f"template build context environment variable is unset: {spec.build_context_env}")
        build_context = Path(raw_context).expanduser().resolve()
        if not build_context.is_dir():
            raise RuntimeError(f"template build context is not a directory: {build_context}")
        return build_context

    @staticmethod
    def _resolve_base_image(spec: SandboxTemplateSpec) -> str:
        """Resolve a base image reference and enforce an optional immutable digest gate."""

        if spec.base_image_env:
            base_image = os.getenv(spec.base_image_env, "")
            if not base_image:
                raise RuntimeError(f"template base image environment variable is unset: {spec.base_image_env}")
        else:
            base_image = spec.base_image or os.getenv("E2B_BASE_IMAGE", "")
        if spec.metadata.get("require_digest_pinned_base_image") is True and not re.search(
            r"@sha256:[A-Fa-f0-9]{64}$", base_image
        ):
            raise RuntimeError("template base image must be pinned by an OCI sha256 digest")
        return base_image

    @staticmethod
    def _set_file_context(template: object, *, build_context: Path = _REPO_ROOT) -> None:
        """Pin E2B copy uploads to the validated local build context."""

        inner = getattr(template, "_template", None)
        if inner is not None and hasattr(inner, "_file_context_path"):
            inner._file_context_path = str(build_context)

    @staticmethod
    def _base_kind(*, base_template: str, base_image: str) -> str:
        if base_template:
            return "template"
        if base_image.startswith("e2b-python:"):
            return "e2b-python"
        if base_image.startswith("e2b-debian:"):
            return "e2b-debian"
        if base_image.startswith("e2b-ubuntu:"):
            return "e2b-ubuntu"
        if base_image == "e2b-base":
            return "e2b-base"
        return "registry-image" if base_image else "ubuntu"

    @staticmethod
    def _template_from_base_selector(
        template_root: object,
        *,
        base_template: str,
        base_image: str,
    ) -> _E2BTemplateBuilder:
        """Create an E2B template builder from a NorthCUA base selector.

        ``base_template`` preserves the existing provider-template behavior.
        ``base_image`` still accepts arbitrary registry images, but also supports
        explicit E2B managed base selectors such as ``e2b-python:3.12``. Those
        managed selectors avoid Docker registry pull flakes for templates that
        do not need a full desktop base.
        """

        if base_template:
            return cast(_E2BTemplateBuilder, template_root.from_template(base_template))
        if base_image.startswith("e2b-python:"):
            return cast(_E2BTemplateBuilder, template_root.from_python_image(base_image.split(":", 1)[1] or "3"))
        if base_image.startswith("e2b-debian:"):
            return cast(_E2BTemplateBuilder, template_root.from_debian_image(base_image.split(":", 1)[1] or "stable"))
        if base_image.startswith("e2b-ubuntu:"):
            return cast(_E2BTemplateBuilder, template_root.from_ubuntu_image(base_image.split(":", 1)[1] or "latest"))
        if base_image == "e2b-base":
            return cast(_E2BTemplateBuilder, template_root.from_base_image())

        image_kwargs: dict[str, str] = {"image": base_image}
        username = os.getenv("E2B_REGISTRY_USERNAME", "")
        password = os.getenv("E2B_REGISTRY_PASSWORD", "")
        if username:
            image_kwargs["username"] = username
        if password:
            image_kwargs["password"] = password
        return cast(_E2BTemplateBuilder, template_root.from_image(**image_kwargs))

    @staticmethod
    def _git_source_command(layer: SandboxImageLayer | SandboxTemplateLayer) -> str:
        """Build a no-copy command that pulls a template source repo remotely."""

        source = E2BTemplateRuntimeMixin._source_shell_value(layer)
        ref = shlex.quote(layer.ref)
        destination = shlex.quote(layer.destination)
        parent = shlex.quote(str(Path(layer.destination).parent))
        depth = int(layer.depth)
        return E2BTemplateRuntimeMixin._bash_command(
            "\n".join(
                [
                    f"rm -rf {destination}",
                    f"mkdir -p {parent}",
                    f"git init {destination}",
                    f"cd {destination}",
                    f"git remote add origin {source}",
                    (
                        f"git fetch --depth {depth} origin {ref} "
                        f"|| git fetch --depth {depth} origin refs/heads/{ref}:refs/remotes/origin/{ref} "
                        f"|| git fetch --depth {depth} origin refs/tags/{ref}:refs/tags/{ref} "
                        f"|| git fetch origin {ref}"
                    ),
                    "git checkout --detach FETCH_HEAD",
                    "git rev-parse HEAD | tee /opt/northcua/runtime-source-revision.txt",
                ]
            )
        )

    @staticmethod
    def _archive_source_command(layer: SandboxImageLayer | SandboxTemplateLayer) -> str:
        """Build a no-copy command that extracts a remote source archive."""

        source = E2BTemplateRuntimeMixin._source_shell_value(layer)
        destination = shlex.quote(layer.destination)
        parent = shlex.quote(str(Path(layer.destination).parent))
        sha256 = shlex.quote(layer.sha256)
        strip_components = int(layer.strip_components)
        max_time_seconds = E2BTemplateRuntimeMixin._int_env(
            "NORTHCUA_TEMPLATE_ARCHIVE_MAX_TIME_SECONDS",
            _DEFAULT_ARCHIVE_DOWNLOAD_MAX_TIME_SECONDS,
        )
        connect_timeout_seconds = E2BTemplateRuntimeMixin._int_env(
            "NORTHCUA_TEMPLATE_ARCHIVE_CONNECT_TIMEOUT_SECONDS",
            _DEFAULT_ARCHIVE_DOWNLOAD_CONNECT_TIMEOUT_SECONDS,
        )
        checksum_block = ""
        revision_value = "archive:unverified"
        if layer.sha256:
            checksum_block = f"printf '%s  %s\\n' {sha256} \"$archive_path\" | sha256sum -c -"
            revision_value = f"archive:{layer.sha256}"
        return E2BTemplateRuntimeMixin._bash_command(
            "\n".join(
                [
                    "work_dir=$(mktemp -d)",
                    "trap 'rm -rf \"$work_dir\"' EXIT",
                    'archive_path="$work_dir/runtime-source.tar"',
                    (
                        "curl --http1.1 -fsSL --retry 8 --retry-all-errors "
                        f"--retry-delay 2 --retry-max-time {max_time_seconds} "
                        f"--connect-timeout {connect_timeout_seconds} --max-time {max_time_seconds} "
                        f'{source} -o "$archive_path"'
                    ),
                    checksum_block,
                    f"rm -rf {destination}",
                    f"mkdir -p {destination} {parent}",
                    f'tar -xf "$archive_path" -C {destination} --strip-components={strip_components}',
                    "mkdir -p /opt/northcua",
                    f"printf '%s\\n' {shlex.quote(revision_value)} | tee /opt/northcua/runtime-source-revision.txt",
                ]
            )
        )

    @staticmethod
    def _int_env(name: str, default: int) -> int:
        raw = os.getenv(name, "")
        if not raw:
            return default
        try:
            parsed = int(raw)
        except ValueError:
            return default
        return parsed if parsed > 0 else default

    @staticmethod
    def _source_shell_value(layer: SandboxImageLayer | SandboxTemplateLayer) -> str:
        if layer.source_env:
            return '"${' + layer.source_env + ":?missing " + layer.source_env + '}"'
        return shlex.quote(layer.source)

    @staticmethod
    def _set_build_source_env(template: _E2BTemplateBuilder, env_name: str) -> _E2BTemplateBuilder:
        """Expose one local source URL env var to the E2B build environment."""

        value = os.getenv(env_name, "")
        if not value:
            raise RuntimeError(f"template source env var is required: {env_name}")
        return template.set_envs({env_name: value})

    @staticmethod
    def _bash_command(command: str) -> str:
        """Wrap a template layer command so multi-line snippets fail clearly."""

        return "bash -lc " + shlex.quote("set -euo pipefail\n" + command)
