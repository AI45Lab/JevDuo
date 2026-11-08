"""Helpers for starting a NorthCUA device worker inside a sandbox."""

from __future__ import annotations

import hashlib
import json
import re
import shlex
from typing import ClassVar, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field, model_validator

from northcua.sandboxes.contracts import (
    SandboxCommandSpec,
    SandboxDeviceBinding,
    SandboxHandle,
    SandboxReadinessState,
)
from northcua.sandboxes.device.readiness import DeviceReadinessProbe, NoopDeviceReadinessProbe, SandboxReadinessResult
from northcua.sandboxes.provider.base import DirectDeviceWorkerProvider, SandboxProvider
from northcua.sandboxes.tool_contracts import SandboxProviderDispatcher

_SANDBOX_DEVICE_CONFIG_PATH = "/tmp/northcua-device-worker.yaml"
_SANDBOX_DEVICE_HEARTBEAT_INTERVAL_SECONDS = 30.0
_SANDBOX_DEVICE_TELEMETRY_STREAM_MODE = "terminal_sampled"
_SANDBOX_DEVICE_TELEMETRY_SAMPLE_RATE = 0.02
_SECRET_REF_RE = re.compile(r"^(?:env:[A-Za-z_][A-Za-z0-9_]*|file-secret://[^\s]+)$")


class ExternalDeviceBootstrapPlan(BaseModel):
    """Secret-free, provider-neutral plan for one externally owned runtime."""

    model_config: ClassVar[ConfigDict] = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["northcua.external-device-bootstrap/v1"] = "northcua.external-device-bootstrap/v1"
    operation_id: str = Field(min_length=1)
    runtime_session_ref: str = Field(min_length=1)
    service_key: str = Field(min_length=1)
    device_id: str = Field(min_length=1)
    argv: tuple[str, ...]
    environment: dict[str, str] = Field(default_factory=dict)
    environment_inheritance: Literal["none", "desktop_session"] = "none"
    secret_environment_refs: dict[str, str] = Field(default_factory=dict)
    readiness_timeout_seconds: float = Field(default=600.0, gt=0)

    @model_validator(mode="after")
    def _validate_secret_boundary(self) -> "ExternalDeviceBootstrapPlan":
        if not self.argv or not self.argv[0] or any("\x00" in item for item in self.argv):
            raise ValueError("external device bootstrap requires NUL-free argv")
        if "--token" in self.argv:
            raise ValueError("external device token cannot appear in argv")
        if "NORTHCUA_DEVICE_TOKEN" in self.environment:
            raise ValueError("external device token cannot appear in public environment")
        sensitive = sorted(
            key
            for key in self.environment
            if any(part in key.upper() for part in ("TOKEN", "KEY", "SECRET", "CREDENTIAL", "PASSWORD"))
        )
        if sensitive:
            raise ValueError(f"external device public environment contains sensitive key: {', '.join(sensitive)}")
        allowed_secret_sets = (
            {"NORTHCUA_DEVICE_TOKEN"},
            {"NORTHCUA_DEVICE_SERVER", "NORTHCUA_DEVICE_TOKEN"},
        )
        if set(self.secret_environment_refs) not in allowed_secret_sets:
            raise ValueError("external device bootstrap has unsupported secret environment references")
        if any(not _SECRET_REF_RE.fullmatch(ref) for ref in self.secret_environment_refs.values()):
            raise ValueError("external device bootstrap secrets must use approved secret references")
        if "NORTHCUA_DEVICE_SERVER" in self.secret_environment_refs and "NORTHCUA_DEVICE_SERVER" in self.environment:
            raise ValueError("external device server URL cannot be both public and secret")
        return self

    @property
    def plan_sha256(self) -> str:
        """Return a deterministic content hash over the public plan."""

        payload = json.dumps(
            self.model_dump(mode="json", exclude_none=True),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()


class ExternalDeviceBootstrapReceipt(BaseModel):
    """Identity receipt returned by an idempotent external runtime adapter."""

    model_config: ClassVar[ConfigDict] = ConfigDict(extra="forbid", frozen=True)

    operation_id: str = Field(min_length=1)
    runtime_session_ref: str = Field(min_length=1)
    service_key: str = Field(min_length=1)
    device_id: str = Field(min_length=1)
    plan_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    state: Literal["started", "already_started"]


class ExternalDeviceBootstrapPort(Protocol):
    """Start or reconcile one plan without owning runtime termination."""

    async def ensure_started(self, plan: ExternalDeviceBootstrapPlan) -> ExternalDeviceBootstrapReceipt: ...


class ExternalDeviceBootstrapService:
    """Validate adapter identity and wait for real Controller registration."""

    def __init__(self, *, adapter: ExternalDeviceBootstrapPort, readiness_probe: DeviceReadinessProbe) -> None:
        if isinstance(readiness_probe, NoopDeviceReadinessProbe):
            raise ValueError("external device bootstrap requires a real readiness probe")
        self._adapter = adapter
        self._readiness_probe = readiness_probe

    async def ensure_ready(self, plan: ExternalDeviceBootstrapPlan) -> ExternalDeviceBootstrapReceipt:
        """Idempotently start the worker and require exact registered identity."""

        receipt = await self._adapter.ensure_started(plan)
        expected = (
            plan.operation_id,
            plan.runtime_session_ref,
            plan.service_key,
            plan.device_id,
            plan.plan_sha256,
        )
        actual = (
            receipt.operation_id,
            receipt.runtime_session_ref,
            receipt.service_key,
            receipt.device_id,
            receipt.plan_sha256,
        )
        if actual != expected:
            raise RuntimeError("external device bootstrap receipt does not match the requested plan")
        readiness = await self._readiness_probe.wait_for_ready(
            plan.device_id,
            timeout_seconds=plan.readiness_timeout_seconds,
        )
        state = readiness.state if isinstance(readiness, SandboxReadinessResult) else readiness
        if state is not SandboxReadinessState.READY:
            raise TimeoutError(f"external device {plan.device_id!r} did not become ready: {state.value}")
        return receipt


def build_external_device_bootstrap_plan(
    *,
    operation_id: str,
    runtime_session_ref: str,
    binding: SandboxDeviceBinding,
    device_credential_ref: str,
    server_url_credential_ref: str = "",
    readiness_timeout_seconds: float = 600.0,
) -> ExternalDeviceBootstrapPlan:
    """Build a deterministic plan without serializing the device credential."""

    if binding.token:
        raise ValueError("external device binding must use a secret reference instead of token")
    digest = hashlib.sha256(f"{operation_id}\0{runtime_session_ref}\0{binding.device_id}".encode()).hexdigest()[:24]
    environment = {
        **binding.environment,
        "NORTHCUA_DEVICE_HEARTBEAT_INTERVAL_SECONDS": f"{_SANDBOX_DEVICE_HEARTBEAT_INTERVAL_SECONDS:g}",
        "NORTHCUA_DEVICE_TELEMETRY_STREAM_MODE": _SANDBOX_DEVICE_TELEMETRY_STREAM_MODE,
        "NORTHCUA_DEVICE_TELEMETRY_SAMPLE_RATE": f"{_SANDBOX_DEVICE_TELEMETRY_SAMPLE_RATE:g}",
    }
    desktop_capabilities = {
        "desktop.screenshot",
        "desktop.input",
        "desktop.background_input",
        "desktop.monitor",
        "desktop.window",
        "a11y.tree",
    }
    environment_inheritance: Literal["none", "desktop_session"] = (
        "desktop_session"
        if "desktop" in binding.agentkit_agent_kinds
        or any(capability.value in desktop_capabilities for capability in binding.capabilities)
        else "none"
    )
    public_binding = binding
    secret_refs = {"NORTHCUA_DEVICE_TOKEN": device_credential_ref}
    if server_url_credential_ref:
        public_binding = binding.model_copy(update={"server_url": ""})
        secret_refs["NORTHCUA_DEVICE_SERVER"] = server_url_credential_ref
    return ExternalDeviceBootstrapPlan(
        operation_id=operation_id,
        runtime_session_ref=runtime_session_ref,
        service_key=f"northcua-device-{digest}",
        device_id=binding.device_id,
        argv=tuple(_worker_argv(public_binding, include_token=False, include_config=False)),
        environment=environment,
        environment_inheritance=environment_inheritance,
        secret_environment_refs=secret_refs,
        readiness_timeout_seconds=readiness_timeout_seconds,
    )


class SandboxDeviceBootstrapper:
    """Build and launch the sandbox-local device worker process."""

    def __init__(self, provider: SandboxProvider) -> None:
        self._provider = provider
        self._provider_dispatcher = SandboxProviderDispatcher(provider)

    async def start_worker(self, handle: SandboxHandle, binding: SandboxDeviceBinding) -> None:
        """Start ``northcua_device`` inside a sandbox.

        Args:
            handle: Sandbox that should host the worker.
            binding: Controller/device identity and capability configuration.

        Raises:
            RuntimeError: If the provider reports a failed process launch.
        """

        if isinstance(self._provider, DirectDeviceWorkerProvider):
            await self._provider.start_device_worker(handle, binding)
            return
        command = SandboxCommandSpec(
            command=self._worker_command(binding),
            label="start-northcua-device-worker",
            timeout_seconds=30,
            redact=[binding.token] if binding.token else [],
        )
        result = await self._provider_dispatcher.start_process(handle, command)
        if not result.ok:
            raise RuntimeError(f"failed to start sandbox device worker: {result.stderr or result.stdout}")

    def _worker_command(self, binding: SandboxDeviceBinding) -> str:
        args = _worker_argv(binding, include_token=True, include_config=True)
        lines = [
            f"cat > {shlex.quote(_SANDBOX_DEVICE_CONFIG_PATH)} <<'NORTHCUA_DEVICE_CONFIG'",
            f"heartbeat_interval_seconds: {_SANDBOX_DEVICE_HEARTBEAT_INTERVAL_SECONDS:g}",
            f"telemetry_stream_mode: {_SANDBOX_DEVICE_TELEMETRY_STREAM_MODE}",
            f"telemetry_sample_rate: {_SANDBOX_DEVICE_TELEMETRY_SAMPLE_RATE:g}",
            "NORTHCUA_DEVICE_CONFIG",
        ]
        lines.extend(
            f"export {key}={shlex.quote(value)}" for key, value in sorted(binding.environment.items()) if key and value
        )
        lines.append(" ".join(shlex.quote(arg) for arg in args))
        return "\n".join(lines)


def _worker_argv(
    binding: SandboxDeviceBinding,
    *,
    include_token: bool,
    include_config: bool,
) -> list[str]:
    args = [
        binding.python_executable,
        "-m",
        "northcua_device.cli",
        "connect",
        "--server",
        binding.server_url,
        "--device-id",
        binding.device_id,
        "--display-name",
        binding.display_name,
        "--platform",
        binding.platform.value,
        "--gui-resource-id",
        f"{binding.platform.value}:{binding.device_id}:gui",
        "--max-sessions",
        "1",
        "--max-concurrent-commands",
        "1",
        "--log-dir",
        binding.log_dir,
    ]
    if include_config:
        args[4:4] = ["--config", _SANDBOX_DEVICE_CONFIG_PATH]
    if include_token and binding.token:
        args.extend(["--token", binding.token])
    for capability in sorted(binding.capabilities, key=lambda item: item.value):
        args.extend(["--allowed-capability", capability.value])
    for tool in sorted(binding.allowed_tools):
        args.extend(["--allowed-tool", tool])
    for agent_kind in sorted(binding.agentkit_agent_kinds):
        args.extend(["--agentkit-agent-kind", agent_kind])
    return args


__all__ = [
    "ExternalDeviceBootstrapPlan",
    "ExternalDeviceBootstrapPort",
    "ExternalDeviceBootstrapReceipt",
    "ExternalDeviceBootstrapService",
    "SandboxDeviceBootstrapper",
    "build_external_device_bootstrap_plan",
]
