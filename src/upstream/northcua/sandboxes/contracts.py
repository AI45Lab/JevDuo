"""Typed contracts for NorthCUA-managed sandbox providers."""

from __future__ import annotations

import ipaddress
import re
from enum import Enum
from pathlib import Path, PurePosixPath
from typing import Literal, Protocol
from urllib.parse import urlparse

from pydantic import BaseModel, Field, field_validator, model_validator

from northcua.sandboxes.capacity_contracts import (
    SandboxCapacityProbeRequest,
    SandboxCapacityProbeResult,
    SandboxInventoryItem,
    SandboxInventoryState,
)
from northcua_protocol.capabilities import Capability, JsonObject, PlatformKind


class SandboxAllocationStrategy(str, Enum):
    """How batch execution obtains and retires sandbox capacity."""

    EPHEMERAL_PER_TASK = "ephemeral_per_task"
    CONNECT_EXISTING = "connect_existing"


class SandboxLifecycleState(str, Enum):
    """Lifecycle states emitted by sandbox providers."""

    CREATED = "created"
    CONNECTED = "connected"
    READY = "ready"
    DRAINING = "draining"
    TERMINATED = "terminated"
    FAILED = "failed"


class SandboxLifecycleMessage(str, Enum):
    """Stable sandbox lifecycle message names persisted in telemetry."""

    SANDBOX_ACQUIRE_STARTED = "sandbox_acquire_started"
    SANDBOX_ACQUIRED = "sandbox_acquired"
    DEVICE_WORKER_STARTED = "device_worker_started"
    DEVICE_READINESS_WAIT_STARTED = "device_readiness_wait_started"
    DEVICE_READINESS_WAIT_FINISHED = "device_readiness_wait_finished"
    DEVICE_READY = "device_ready"
    SANDBOX_RELEASED = "sandbox_released"


class SandboxLeaseState(str, Enum):
    """Batch-local sandbox lease lifecycle."""

    READY = "ready"
    ASSIGNED = "assigned"
    DRAINING = "draining"
    RELEASED = "released"
    FAILED = "failed"


class SandboxReadinessState(str, Enum):
    """Device readiness state observed after worker bootstrap."""

    UNKNOWN = "unknown"
    WAITING = "waiting"
    READY = "ready"
    TIMED_OUT = "timed_out"
    FAILED = "failed"


DEFAULT_DEVICE_READINESS_TIMEOUT_SECONDS = 600.0


class SandboxFailureReason(str, Enum):
    """Coarse reason categories for sandbox lifecycle failures."""

    PREFLIGHT_FAILED = "preflight_failed"
    CREATE_FAILED = "create_failed"
    STARTUP_COMMAND_FAILED = "startup_command_failed"
    DEVICE_BOOTSTRAP_FAILED = "device_bootstrap_failed"
    DEVICE_READINESS_TIMEOUT = "device_readiness_timeout"
    CONTROLLER_TASK_FAILED = "controller_task_failed"
    TEARDOWN_FAILED = "teardown_failed"
    UNKNOWN = "unknown"


class SandboxProviderFeature(str, Enum):
    """Optional provider capabilities used by orchestration code."""

    CONNECT_EXISTING = "connect_existing"
    REUSE = "reuse"
    UPLOAD = "upload"
    DOWNLOAD = "download"
    COLLECT_LOGS = "collect_logs"
    TEMPLATE_BUILD = "template_build"
    CAPACITY_PROBE = "capacity_probe"
    EXTEND_TIMEOUT = "extend_timeout"


class SandboxFileDeliveryMode(str, Enum):
    """How fixture bytes reach their final path inside a guest."""

    UPLOAD = "upload"
    CHUNKED_UPLOAD = "chunked_upload"
    GUEST_DOWNLOAD = "guest_download"


class SandboxResourceSpec(BaseModel):
    """Resource limits requested from a sandbox provider.

    Attributes:
        cpu_count: Optional virtual CPU count requested from the provider.
        memory_mb: Optional memory limit in MiB.
        disk_mb: Optional disk limit in MiB when the provider supports it.
        timeout_seconds: Provider-side sandbox lifetime or creation timeout.
    """

    cpu_count: int = 0
    memory_mb: int = 0
    disk_mb: int = 0
    timeout_seconds: int = 300


_TEMPLATE_GIT_REF_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9._/@:-]*$"
_TEMPLATE_SOURCE_ENV_PATTERN = r"^$|^[A-Z_][A-Z0-9_]*$"
_SHA256_PATTERN = r"^$|^[A-Fa-f0-9]{64}$"
_NON_ROUTABLE_E2B_TEMPLATE_PREFIXES = ("northcua-e2b-osworld-v2-overlay-seed-",)
_NON_ROUTABLE_E2B_TEMPLATE_IDS = {
    "5ao1na7b5pzo3tqbuodh",
    "lx6n2crezt7lbh7oyr45",
    "t2t60gk116nvkvk5yl6r",
    "uy5kojf3yra068mcdtjd",
    "mk9tqumjfsiotzy9px5s",
    "a3cefoqnhstm5aow54ae",
    "95yyo17n9fvhlk7hcd01",
    "vc5q8fiqsosz7zeug40w",
    "wss1wgae7w03ma1i2d40",
    "484i686qsy8o6coy8vu1",
    "3ha35i28tsxnn4mdkuve",
    "r9pi1wn4va7tcgh4hjgl",
    "nxrfbnqnlrg8fu5w1u1g",
    "nrjzyxy9w6k5u0fchmr6",
    "02wekmavogtgzxyyubhw",
    "aq88a3jsg4e8o4m6fb5v",
    "feycztfh8lxqk4if5gmh",
    "tm0k7s6qkqegxa41ipap",
    "df0sm826re26p1t09bjk",
    "nqe0bnmz4mvbho03w1rt",
    "me3vazd8lwx9ftfnr3a4",
    "impbjctir9smn48v8i7s",
    "57zq325jpuf0txsok7up",
    "b2y4ehnqo4t6h3359afj",
}


def is_non_routable_e2b_template(template: str) -> bool:
    """Return whether an E2B template is a privileged build seed, not a task runtime."""

    return template in _NON_ROUTABLE_E2B_TEMPLATE_IDS or template.startswith(_NON_ROUTABLE_E2B_TEMPLATE_PREFIXES)


_LINUX_PRINCIPAL_PATTERN = r"^(?:[A-Za-z_][A-Za-z0-9_.-]{0,31}\$?|[0-9]+)$"
_PROTECTED_UPLOAD_PARENT_PATHS = {"/", "/tmp", "/home", "/home/user", "/home/oai", "/root"}


def _validate_upload_mode(mode: str, *, field_name: str) -> None:
    if not mode:
        return
    if not mode.isdigit() or any(char not in "01234567" for char in mode):
        raise ValueError(f"{field_name} must be an octal digit string")
    if len(mode) not in {3, 4}:
        raise ValueError(f"{field_name} must be three or four octal digits")


def _validate_template_remote_source(*, source: str, source_env: str, destination: str) -> None:
    """Validate a build-time remote source without allowing secret-bearing URLs."""

    if bool(source) == bool(source_env):
        raise ValueError("remote template layers require exactly one of source or source_env")
    parsed = urlparse(source)
    if source and (parsed.scheme not in {"http", "https"} or not parsed.netloc):
        raise ValueError("remote template layers require an http(s) source")
    if source and (parsed.username or parsed.password):
        raise ValueError("remote template layer source must not embed credentials")
    if not destination.startswith("/"):
        raise ValueError("remote template layer destination must be absolute")


class SandboxImageLayer(BaseModel):
    """One immutable sandbox image/template layer.

    Attributes:
        kind: Layer operation kind.
        packages: Package names for package-manager layers.
        command: Shell command for run layers.
        env: Environment variables for env layers.
        ports: Ports exposed by the image/template.
        files: Local-to-remote file copy mapping.
        source: Remote source URL for git/archive layers.
        source_env: Build-time environment variable containing the source URL.
        ref: Git branch, tag, or commit-ish for git layers.
        destination: Absolute path where a git/archive layer is materialized.
        depth: Fetch depth for git layers.
        sha256: Optional expected checksum for archive layers.
        strip_components: Number of leading path components stripped from archive entries.
    """

    kind: Literal["apt", "pip", "run", "env", "expose", "copy", "git", "archive"]
    packages: list[str] = Field(default_factory=list)
    command: str = ""
    env: dict[str, str] = Field(default_factory=dict)
    ports: list[int] = Field(default_factory=list)
    files: dict[str, str] = Field(default_factory=dict)
    source: str = ""
    source_env: str = Field(default="", pattern=_TEMPLATE_SOURCE_ENV_PATTERN)
    ref: str = Field(default="main", pattern=_TEMPLATE_GIT_REF_PATTERN)
    destination: str = ""
    depth: int = Field(default=1, ge=1)
    sha256: str = Field(default="", pattern=_SHA256_PATTERN)
    strip_components: int = Field(default=0, ge=0)

    @model_validator(mode="after")
    def _validate_remote_source_layer(self) -> "SandboxImageLayer":
        if self.kind not in {"git", "archive"}:
            return self
        _validate_template_remote_source(
            source=self.source,
            source_env=self.source_env,
            destination=self.destination,
        )
        return self


class SandboxImageSpec(BaseModel):
    """Pure-data image description inspired by CUA's immutable Image builder.

    Attributes:
        os_type: Operating system family.
        distro: Distribution or image family.
        version: Image version/tag.
        kind: Provider-neutral image kind.
        registry: Optional registry reference.
        layers: Ordered image/template layers.
        resources: Default resource request for sandboxes created from it.
        metadata: Provider-specific non-secret metadata.
    """

    os_type: PlatformKind = PlatformKind.LINUX
    distro: str = "ubuntu"
    version: str = "24.04"
    kind: Literal["container", "vm", "desktop"] = "desktop"
    registry: str = ""
    layers: list[SandboxImageLayer] = Field(default_factory=list)
    resources: SandboxResourceSpec = Field(default_factory=SandboxResourceSpec)
    metadata: JsonObject = Field(default_factory=dict)

    def apt_install(self, *packages: str) -> "SandboxImageSpec":
        """Return a copy with an apt package layer appended.

        Args:
            *packages: Debian package names to install in order.

        Returns:
            New image spec with the apt layer appended.
        """

        return self._with_layer(SandboxImageLayer(kind="apt", packages=list(packages)))

    def pip_install(self, *packages: str) -> "SandboxImageSpec":
        """Return a copy with a pip package layer appended.

        Args:
            *packages: Python package specifiers to install in order.

        Returns:
            New image spec with the pip layer appended.
        """

        return self._with_layer(SandboxImageLayer(kind="pip", packages=list(packages)))

    def run(self, command: str) -> "SandboxImageSpec":
        """Return a copy with a shell command layer appended.

        Args:
            command: Shell command executed while building the template.

        Returns:
            New image spec with the command layer appended.
        """

        return self._with_layer(SandboxImageLayer(kind="run", command=command))

    def env(self, **values: str) -> "SandboxImageSpec":
        """Return a copy with environment variables appended.

        Args:
            **values: Environment variable names and values to set.

        Returns:
            New image spec with the environment layer appended.
        """

        return self._with_layer(SandboxImageLayer(kind="env", env=dict(values)))

    def expose(self, *ports: int) -> "SandboxImageSpec":
        """Return a copy with exposed ports appended.

        Args:
            *ports: TCP ports to expose from the sandbox template.

        Returns:
            New image spec with the expose layer appended.
        """

        return self._with_layer(SandboxImageLayer(kind="expose", ports=list(ports)))

    def copy(self, local: str | Path, remote: str) -> "SandboxImageSpec":
        """Return a copy with a file-copy layer appended.

        Args:
            local: Local source path captured by the template builder.
            remote: Destination path inside the sandbox image.

        Returns:
            New image spec with the copy layer appended.
        """

        return self._with_layer(SandboxImageLayer(kind="copy", files={str(local): remote}))

    def git_source(self, source: str, destination: str, *, ref: str = "main", depth: int = 1) -> "SandboxImageSpec":
        """Return a copy with a remote git source layer appended.

        Args:
            source: Public http(s) git remote URL. Credentials are rejected.
            destination: Absolute path inside the image where source is cloned.
            ref: Branch, tag, or commit-ish fetched at build time.
            depth: Fetch depth for branch/tag refs.

        Returns:
            New image spec with the git layer appended.
        """

        return self._with_layer(
            SandboxImageLayer(kind="git", source=source, ref=ref, destination=destination, depth=depth)
        )

    def remote_archive(
        self,
        source: str,
        destination: str,
        *,
        sha256: str = "",
        strip_components: int = 0,
    ) -> "SandboxImageSpec":
        """Return a copy with a remote archive extraction layer appended.

        Args:
            source: HTTP(S) archive URL fetched inside the template builder.
            destination: Absolute extraction path inside the image.
            sha256: Optional checksum used to verify the downloaded archive.
            strip_components: Leading archive path components to remove.

        Returns:
            New image spec with the archive layer appended.
        """

        return self._with_layer(
            SandboxImageLayer(
                kind="archive",
                source=source,
                destination=destination,
                sha256=sha256,
                strip_components=strip_components,
            )
        )

    def _with_layer(self, layer: SandboxImageLayer) -> "SandboxImageSpec":
        return self.model_copy(update={"layers": [*self.layers, layer]})


class SandboxCommandSpec(BaseModel):
    """A command executed inside a sandbox during bootstrap or cleanup.

    Attributes:
        command: Shell command understood by the sandbox provider.
        label: Operator-facing command label used in logs. The label should
            identify the stage without containing secrets or large payloads.
        timeout_seconds: Maximum time to wait for command completion.
        cwd: Optional working directory inside the sandbox.
        env: Extra environment variables for the command.
        allow_failure: Whether a non-zero exit code should continue execution.
        redact: Substrings that must be replaced in persisted logs.
    """

    command: str
    label: str = ""
    timeout_seconds: float = 60.0
    cwd: str = ""
    env: dict[str, str] = Field(default_factory=dict)
    allow_failure: bool = False
    redact: list[str] = Field(default_factory=list)


class SandboxFileUploadSpec(BaseModel):
    """One host file staged into a sandbox before startup commands.

    Attributes:
        local_path: Host-side source path. The path is resolved by the runner
            before the provider sees it and must point at a regular file.
        remote_path: Absolute destination path inside the sandbox.
        mode: Optional chmod mode applied after upload, expressed as an octal
            string such as ``"0644"`` or ``"0755"``.
        owner: Optional owner applied to the uploaded file.
        group: Optional group applied to the uploaded file.
        parent_mode: Optional chmod mode applied to the destination directory.
        parent_owner: Optional owner applied to the destination directory.
        parent_group: Optional group applied to the destination directory.
        visibility: Boundary allowed to consume the file. Only ``guest`` files
            are uploaded by the guest lifecycle manager.
        sha256: Optional expected source checksum used by higher-level suites
            to prove that generated fixtures were uploaded unchanged.
        size_bytes: Required byte length for content-addressed delivery modes.
        delivery: Direct, chunked, or guest-download delivery policy.
        source_url: HTTP(S) source used only by guest-download delivery.
        source_host: Original URL host pinned for TLS SNI and the HTTP Host header.
        source_port: Original URL port pinned for guest resolution.
        source_address: Prevalidated public IP used for the guest connection.
        chunk_size_bytes: Host-side chunk size used by chunked delivery.
        intended_app: Case app allowed to consume a large artifact.
        host_verification: Host-side verification completed before allocation.
    """

    local_path: Path | None = None
    remote_path: str
    mode: str = ""
    owner: str = ""
    group: str = ""
    parent_mode: str = ""
    parent_owner: str = ""
    parent_group: str = ""
    visibility: Literal["guest", "evaluator", "host"] = "guest"
    sha256: str = Field(default="", pattern=_SHA256_PATTERN)
    size_bytes: int = Field(default=0, ge=0)
    delivery: SandboxFileDeliveryMode = SandboxFileDeliveryMode.UPLOAD
    source_url: str = ""
    source_host: str = ""
    source_port: int = Field(default=0, ge=0, le=65535)
    source_address: str = ""
    chunk_size_bytes: int = Field(default=8 * 1024 * 1024, ge=1, le=50 * 1024 * 1024)
    intended_app: str = Field(default="", exclude_if=lambda value: not value)
    host_verification: Literal[
        "",
        "sha256_and_size_verified",
        "endpoint_and_size_verified",
        "endpoint_verified",
    ] = Field(default="", exclude_if=lambda value: not value)

    @field_validator("remote_path")
    @classmethod
    def _normalize_remote_path(cls, value: str) -> str:
        if not value.startswith("/") or value.startswith("//"):
            raise ValueError("sandbox upload remote_path must be absolute")
        if "\\" in value or "\x00" in value or "\n" in value or "\r" in value:
            raise ValueError("sandbox upload remote_path contains unsafe characters")
        path = PurePosixPath(value)
        if ".." in path.parts:
            raise ValueError("sandbox upload remote_path cannot contain traversal segments")
        normalized = "/" + "/".join(part for part in path.parts if part not in {"/", "."})
        if normalized == "/":
            raise ValueError("sandbox upload remote_path must name a file")
        return normalized

    @model_validator(mode="after")
    def _validate_paths(self) -> "SandboxFileUploadSpec":
        """Ensure uploads have explicit source and destination paths."""

        _validate_upload_mode(self.mode, field_name="sandbox upload mode")
        _validate_upload_mode(self.parent_mode, field_name="sandbox upload parent_mode")
        parent_path = str(PurePosixPath(self.remote_path).parent)
        if (
            any((self.parent_mode, self.parent_owner, self.parent_group))
            and parent_path in _PROTECTED_UPLOAD_PARENT_PATHS
        ):
            raise ValueError("sandbox upload parent ownership/mode cannot target a shared system directory")
        if self.delivery == SandboxFileDeliveryMode.GUEST_DOWNLOAD:
            parsed = urlparse(self.source_url)
            if self.local_path is not None:
                raise ValueError("guest_download sandbox files cannot set local_path")
            if parsed.scheme not in {"http", "https"} or not parsed.netloc:
                raise ValueError("guest_download sandbox files require an http(s) source_url")
            if parsed.username or parsed.password:
                raise ValueError("guest_download source_url must not embed credentials")
            expected_port = parsed.port or (443 if parsed.scheme == "https" else 80)
            if self.source_host != (parsed.hostname or "") or self.source_port != expected_port:
                raise ValueError("guest_download source endpoint must match source_url")
            try:
                address = ipaddress.ip_address(self.source_address)
            except ValueError as exc:
                raise ValueError("guest_download source_address must be a public IP") from exc
            if not address.is_global or any(
                (
                    address.is_private,
                    address.is_loopback,
                    address.is_link_local,
                    address.is_reserved,
                    address.is_multicast,
                    address.is_unspecified,
                )
            ):
                raise ValueError("guest_download source_address must be a public IP")
            self._require_content_address()
        else:
            if self.local_path is None:
                raise ValueError(f"{self.delivery.value} sandbox files require local_path")
            if any((self.source_url, self.source_host, self.source_port, self.source_address)):
                raise ValueError(f"{self.delivery.value} sandbox files cannot set source endpoint fields")
            if self.delivery == SandboxFileDeliveryMode.CHUNKED_UPLOAD:
                self._require_content_address()
            elif self.intended_app or self.host_verification:
                raise ValueError("upload sandbox files cannot set large artifact evidence fields")
        return self

    def _require_content_address(self) -> None:
        if not re.fullmatch(r"[0-9a-f]{64}", self.sha256) or self.size_bytes <= 0:
            raise ValueError(f"{self.delivery.value} sandbox files require sha256 and positive size_bytes")
        if not self.intended_app.strip() or not self.host_verification:
            raise ValueError(f"{self.delivery.value} sandbox files require intended_app and host_verification evidence")

    @field_validator("owner", "group", "parent_owner", "parent_group")
    @classmethod
    def _validate_principal(cls, value: str) -> str:
        if value and not re.fullmatch(_LINUX_PRINCIPAL_PATTERN, value):
            raise ValueError("sandbox upload owner/group values must be Linux names or numeric ids")
        return value


class SandboxCommandResult(BaseModel):
    """Result returned by a provider after executing one sandbox command.

    Attributes:
        exit_code: Process exit status.
        stdout: Captured standard output, already redacted by the caller when needed.
        stderr: Captured standard error, already redacted by the caller when needed.
        timed_out: Whether provider-side execution exceeded the timeout.
    """

    exit_code: int = 0
    stdout: str = ""
    stderr: str = ""
    timed_out: bool = False

    @property
    def ok(self) -> bool:
        """Return whether the command completed successfully.

        Returns:
            ``True`` when the process exited with code zero and did not time out.
        """

        return self.exit_code == 0 and not self.timed_out


class SandboxTemplateLayer(BaseModel):
    """One reproducible template-building layer.

    Attributes:
        kind: Layer operation kind.
        packages: Package names for apt/pip layers.
        command: Shell command for run layers.
        env: Environment variables for env layers.
        source: Remote source URL for git/archive layers.
        source_env: Build-time environment variable containing the source URL.
        ref: Git branch, tag, or commit-ish for git layers.
        destination: Absolute path where a git/archive layer is materialized.
        depth: Fetch depth for git layers.
        sha256: Optional expected checksum for archive layers.
        strip_components: Number of leading path components stripped from archive entries.
    """

    kind: Literal["apt", "pip", "run", "env", "expose", "copy", "git", "archive"]
    packages: list[str] = Field(default_factory=list)
    command: str = ""
    env: dict[str, str] = Field(default_factory=dict)
    ports: list[int] = Field(default_factory=list)
    files: dict[str, str] = Field(default_factory=dict)
    source: str = ""
    source_env: str = Field(default="", pattern=_TEMPLATE_SOURCE_ENV_PATTERN)
    ref: str = Field(default="main", pattern=_TEMPLATE_GIT_REF_PATTERN)
    destination: str = ""
    depth: int = Field(default=1, ge=1)
    sha256: str = Field(default="", pattern=_SHA256_PATTERN)
    strip_components: int = Field(default=0, ge=0)

    @model_validator(mode="after")
    def _validate_remote_source_layer(self) -> "SandboxTemplateLayer":
        if self.kind not in {"git", "archive"}:
            return self
        _validate_template_remote_source(
            source=self.source,
            source_env=self.source_env,
            destination=self.destination,
        )
        return self


class SandboxTemplateSpec(BaseModel):
    """Provider-neutral sandbox template description.

    Attributes:
        provider: Provider id, for example ``e2b`` or ``fake``.
        alias: Provider template alias to create or use.
        base_template: Optional provider template alias/id to extend.
        base_image: Optional provider-specific base image.
        base_image_env: Optional environment variable containing a base image reference.
        build_context_env: Optional environment variable containing the local build context path.
        desktop: Whether the template is expected to expose a GUI desktop.
        layers: Ordered template operations inspired by CUA's image builder.
        resources: Default resources used when building or creating sandboxes.
        metadata: Provider-specific, non-secret template metadata.
    """

    provider: str = "e2b"
    alias: str
    base_template: str = ""
    base_image: str = ""
    base_image_env: str = Field(default="", pattern=_TEMPLATE_SOURCE_ENV_PATTERN)
    build_context_env: str = Field(default="", pattern=_TEMPLATE_SOURCE_ENV_PATTERN)
    desktop: bool = False
    layers: list[SandboxTemplateLayer] = Field(default_factory=list)
    image: SandboxImageSpec | None = None
    resources: SandboxResourceSpec = Field(default_factory=SandboxResourceSpec)
    metadata: JsonObject = Field(default_factory=dict)

    @model_validator(mode="after")
    def _validate_provider_alias(self) -> "SandboxTemplateSpec":
        """Reject E2B build aliases that the SDK interprets as name tags."""

        if self.provider == "e2b" and ":" in self.alias:
            raise ValueError(
                "E2B template alias must not contain ':'; use base_template for upstream name:tag references"
            )
        if self.base_image and self.base_image_env:
            raise ValueError("template base_image and base_image_env are mutually exclusive")
        if self.base_template and (self.base_image or self.base_image_env):
            raise ValueError("template base_template and base_image/base_image_env are mutually exclusive")
        return self


class SandboxProviderSpec(BaseModel):
    """Concrete sandbox allocation request for one task or pool member.

    Attributes:
        provider: Provider id resolved through ``SandboxProviderRegistry``.
        template: Provider template alias.
        allocation_strategy: Lifecycle strategy requested by batch execution.
        sandbox_id: Existing sandbox id for ``connect_existing``.
        desktop: Whether a desktop-capable provider API should be used.
        platform: NorthCUA device platform advertised by the worker.
        resources: Runtime resource request.
        resolution: Desktop resolution for GUI providers.
        dpi: Desktop DPI for GUI providers.
        device_python: Python executable used to install and start the sandbox
            device worker.
        file_uploads: Host files uploaded before startup commands.
        metadata: Small provider-specific metadata, never secrets.
    """

    provider: str = "e2b"
    template: str = "osworld_desktop"
    allocation_strategy: SandboxAllocationStrategy = SandboxAllocationStrategy.EPHEMERAL_PER_TASK
    sandbox_id: str = ""
    desktop: bool = True
    platform: PlatformKind = PlatformKind.LINUX
    resources: SandboxResourceSpec = Field(default_factory=SandboxResourceSpec)
    resolution: tuple[int, int] = (1024, 720)
    dpi: int = 96
    device_python: str = "python3"
    file_uploads: list[SandboxFileUploadSpec] = Field(default_factory=list)
    metadata: JsonObject = Field(default_factory=dict)

    @model_validator(mode="after")
    def _validate_existing_id(self) -> "SandboxProviderSpec":
        """Ensure existing sandbox allocation has an explicit provider id."""

        if self.allocation_strategy == SandboxAllocationStrategy.CONNECT_EXISTING and not self.sandbox_id:
            raise ValueError("connect_existing sandbox allocation requires sandbox_id")
        if self.provider == "e2b" and is_non_routable_e2b_template(self.template):
            raise ValueError("E2B build seed templates cannot be routed to task sandboxes")
        return self


class SandboxHandle(BaseModel):
    """A provider-owned sandbox instance leased to NorthCUA.

    Attributes:
        sandbox_id: Provider sandbox identifier.
        provider: Provider id.
        template: Template alias used to create or connect the sandbox.
        state: Current lifecycle state.
        desktop_url: Optional browser/VNC URL for operator debugging.
        metadata: Provider-specific metadata safe to persist in logs.
    """

    sandbox_id: str
    provider: str
    template: str
    state: SandboxLifecycleState = SandboxLifecycleState.CREATED
    desktop_url: str = ""
    metadata: JsonObject = Field(default_factory=dict)


class SandboxDeviceBinding(BaseModel):
    """Device-worker configuration derived from a sandbox handle.

    Attributes:
        device_id: Strict controller device id associated with the sandbox.
        display_name: Human-readable device label.
        server_url: Controller websocket URL.
        token: Device bearer token.
        platform: Platform advertised to the scheduler.
        capabilities: Capabilities exposed by the worker.
        agentkit_agent_kinds: AgentKit agent kinds served by the worker.
        allowed_tools: Optional tool allowlist.
        python_executable: Python executable used to launch the worker.
        log_dir: Sandbox-local or mounted log directory for the worker.
        environment: Environment variables exported for the worker process.
        metadata: Safe metadata added to the worker descriptor.
    """

    device_id: str
    display_name: str
    server_url: str
    token: str = ""
    platform: PlatformKind = PlatformKind.LINUX
    capabilities: set[Capability] = Field(default_factory=set)
    agentkit_agent_kinds: set[str] = Field(default_factory=set)
    allowed_tools: set[str] = Field(default_factory=set)
    python_executable: str = "python3"
    log_dir: str = "/tmp/northcua-device-logs"
    environment: dict[str, str] = Field(default_factory=dict)
    metadata: JsonObject = Field(default_factory=dict)


class SandboxLease(BaseModel):
    """Batch-local lease over one provider sandbox.

    Attributes:
        lease_id: Stable local lease id.
        handle: Provider sandbox handle.
        device_id: NorthCUA device id bound to the sandbox.
        state: Local lease lifecycle state.
        task_id: Batch task currently assigned to the lease.
        reusable: Whether the lease can return to a warm pool.
        readiness: Last observed worker readiness state.
        failure_reason: Optional coarse failure reason.
    """

    lease_id: str
    handle: SandboxHandle
    device_id: str
    state: SandboxLeaseState = SandboxLeaseState.READY
    task_id: str = ""
    reusable: bool = False
    readiness: SandboxReadinessState = SandboxReadinessState.UNKNOWN
    failure_reason: SandboxFailureReason | None = None


class SandboxProviderCapabilities(BaseModel):
    """Optional feature surface advertised by one sandbox provider.

    Attributes:
        features: Provider capabilities that orchestration code can opt into.
    """

    features: set[SandboxProviderFeature] = Field(default_factory=set)

    def supports(self, feature: SandboxProviderFeature) -> bool:
        """Return whether the provider advertises a feature.

        Args:
            feature: Provider feature to check.

        Returns:
            ``True`` when the feature is present in the advertised set.
        """

        return feature in self.features


class SandboxCommandChannel(Protocol):
    """Minimal command/file/log channel exposed by a sandbox provider."""

    async def run_command(self, handle: SandboxHandle, command: SandboxCommandSpec) -> SandboxCommandResult:
        """Run a foreground command in the sandbox.

        Args:
            handle: Provider sandbox handle.
            command: Command and timeout policy.

        Returns:
            Normalized command result.
        """
        ...

    async def start_process(self, handle: SandboxHandle, command: SandboxCommandSpec) -> SandboxCommandResult:
        """Start a long-lived process in the sandbox.

        Args:
            handle: Provider sandbox handle.
            command: Process launch policy.

        Returns:
            Launch result from the provider.
        """
        ...

    async def upload(self, handle: SandboxHandle, local: Path, remote: str) -> None:
        """Upload one local file into the sandbox.

        Args:
            handle: Provider sandbox handle.
            local: Local source file.
            remote: Remote destination path.
        """
        ...

    async def download(self, handle: SandboxHandle, remote: str, local: Path) -> None:
        """Download one sandbox file to the local host.

        Args:
            handle: Provider sandbox handle.
            remote: Remote source path.
            local: Local destination path.
        """
        ...


class SandboxLogRef(BaseModel):
    """Local file collected from a sandbox during cleanup.

    Attributes:
        name: Stable display name for the collected log.
        local_path: Host-side path where the log was written.
        size_bytes: Size of the collected file in bytes.
    """

    name: str
    local_path: Path
    size_bytes: int = 0


class SandboxLifecycleEvent(BaseModel):
    """Timestamp-free lifecycle event persisted by the batch runner.

    Attributes:
        batch_id: Batch owning the event.
        task_id: Optional task associated with the event.
        sandbox_id: Provider sandbox id.
        provider: Provider id.
        state: Lifecycle state reached by the sandbox.
        message: Human-readable diagnostic summary.
        metadata: Small structured diagnostics.
    """

    batch_id: str
    task_id: str = ""
    sandbox_id: str
    provider: str
    state: SandboxLifecycleState
    message: str = ""
    metadata: JsonObject = Field(default_factory=dict)
