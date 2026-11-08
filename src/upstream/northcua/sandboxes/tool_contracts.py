"""Canonical contracts for sandbox/provider control-plane operations."""

from __future__ import annotations

from pathlib import Path

from pydantic import BaseModel, Field

from northcua.sandboxes.contracts import (
    SandboxCapacityProbeRequest,
    SandboxCapacityProbeResult,
    SandboxCommandResult,
    SandboxCommandSpec,
    SandboxHandle,
    SandboxLogRef,
    SandboxProviderCapabilities,
    SandboxProviderSpec,
    SandboxTemplateSpec,
)
from northcua.sandboxes.provider.base import SandboxProvider
from northcua_protocol.capabilities import Capability, ToolRiskLevel
from northcua_runtime_shared.tool_contracts import (
    SandboxProviderBinding,
    ToolAudience,
    ToolContract,
    ToolContractKind,
    ToolDispatchEnvelope,
    ToolDispatcher,
    ToolDispatchPolicy,
    ToolPlacement,
    ToolVisibility,
)


class SandboxProviderSpecParams(BaseModel):
    """Envelope for provider operations that validate a sandbox allocation spec.

    Attributes:
        spec: Provider allocation request being validated before execution.
    """

    spec: SandboxProviderSpec


class SandboxHandleParams(BaseModel):
    """Envelope for provider operations scoped to one sandbox handle.

    Attributes:
        handle_id: Provider sandbox handle id used in audit and validation.
    """

    handle_id: str = Field(description="Provider sandbox handle id.")


class SandboxCommandParams(SandboxHandleParams):
    """Envelope for command-like operations against one sandbox.

    Attributes:
        command: Command policy passed to the provider after validation.
    """

    command: SandboxCommandSpec


class SandboxUploadParams(SandboxHandleParams):
    """Envelope for uploading one local file into a sandbox.

    Attributes:
        local: Local source path for the upload.
        remote: Absolute destination path inside the sandbox.
    """

    local: Path
    remote: str


class SandboxDownloadParams(SandboxHandleParams):
    """Envelope for downloading one sandbox file to the host.

    Attributes:
        remote: Absolute source path inside the sandbox.
        local: Local destination path on the host.
    """

    remote: str
    local: Path


class SandboxCollectLogsParams(SandboxHandleParams):
    """Envelope for collecting provider and device logs.

    Attributes:
        destination: Local directory that receives collected logs.
    """

    destination: Path


class SandboxExtendTimeoutParams(SandboxHandleParams):
    """Envelope for extending provider-side sandbox lifetime.

    Attributes:
        timeout_seconds: New positive timeout requested from the provider.
    """

    timeout_seconds: int = Field(gt=0)


class SandboxTemplateParams(BaseModel):
    """Envelope for provider template build/update operations.

    Attributes:
        spec: Provider-neutral template definition.
    """

    spec: SandboxTemplateSpec


class SandboxCapacityProbeParams(BaseModel):
    """Envelope for passive sandbox provider capacity probes.

    Attributes:
        request: Inventory and filtering request for the capacity probe.
    """

    request: SandboxCapacityProbeRequest


def _sandbox_contract(
    tool_id: str,
    *,
    description: str,
    provider_method: str,
    params_model: type[BaseModel],
    side_effect: bool = True,
    risk_level: ToolRiskLevel = ToolRiskLevel.HIGH,
    phase: str = "control_plane",
) -> ToolContract:
    return ToolContract(
        tool_id=tool_id,
        public_name=tool_id,
        description=description,
        namespace="env.sandbox",
        agent_kind="env_builder",
        kind=ToolContractKind.SANDBOX_PROVIDER,
        audience=ToolAudience.ENV_BUILDER,
        placement=ToolPlacement.SANDBOX_PROVIDER,
        visibility=ToolVisibility.OPERATOR_ONLY,
        capability=Capability.SHELL_EXEC,
        params_model=params_model,
        timeout_seconds=60.0,
        side_effect=side_effect,
        readonly=not side_effect,
        risk_level=risk_level,
        binding=SandboxProviderBinding(
            provider_method=provider_method,
            phase=phase,
            capability="sandbox.provider",
        ),
        profiles=("env.sandbox.control_plane",),
        audit_tags=("sandbox", "control_plane"),
        resource_requirements=("sandbox_provider",),
        lease_requirements=("sandbox_lease",) if provider_method not in {"create", "connect", "capacity_probe"} else (),
        source="northcua.sandboxes.provider.base.SandboxProvider",
    )


SANDBOX_PROVIDER_TOOL_CONTRACTS: tuple[ToolContract, ...] = (
    _sandbox_contract(
        "sandbox_preflight",
        description="Validate sandbox provider configuration before allocation.",
        provider_method="preflight",
        params_model=SandboxProviderSpecParams,
        side_effect=False,
        risk_level=ToolRiskLevel.LOW,
    ),
    _sandbox_contract(
        "sandbox_create",
        description="Create a new sandbox from a provider template.",
        provider_method="create",
        params_model=SandboxProviderSpecParams,
    ),
    _sandbox_contract(
        "sandbox_connect",
        description="Connect to an existing sandbox without taking ownership of deletion.",
        provider_method="connect",
        params_model=SandboxProviderSpecParams,
        risk_level=ToolRiskLevel.MEDIUM,
    ),
    _sandbox_contract(
        "sandbox_run_command",
        description="Run a foreground command inside a sandbox.",
        provider_method="run_command",
        params_model=SandboxCommandParams,
    ),
    _sandbox_contract(
        "sandbox_start_process",
        description="Start a long-lived background process inside a sandbox.",
        provider_method="start_process",
        params_model=SandboxCommandParams,
    ),
    _sandbox_contract(
        "sandbox_upload",
        description="Upload one local file into a sandbox.",
        provider_method="upload",
        params_model=SandboxUploadParams,
    ),
    _sandbox_contract(
        "sandbox_download",
        description="Download one file from a sandbox.",
        provider_method="download",
        params_model=SandboxDownloadParams,
        risk_level=ToolRiskLevel.MEDIUM,
    ),
    _sandbox_contract(
        "sandbox_collect_logs",
        description="Collect provider/device logs into a local destination.",
        provider_method="collect_logs",
        params_model=SandboxCollectLogsParams,
        side_effect=False,
        risk_level=ToolRiskLevel.LOW,
    ),
    _sandbox_contract(
        "sandbox_terminate",
        description="Terminate a provider-owned sandbox.",
        provider_method="terminate",
        params_model=SandboxHandleParams,
    ),
    _sandbox_contract(
        "sandbox_extend_timeout",
        description="Extend the provider-side sandbox lifetime from now.",
        provider_method="extend_timeout",
        params_model=SandboxExtendTimeoutParams,
        risk_level=ToolRiskLevel.MEDIUM,
    ),
    _sandbox_contract(
        "sandbox_build_template",
        description="Build or update a provider template.",
        provider_method="build_template",
        params_model=SandboxTemplateParams,
    ),
    _sandbox_contract(
        "sandbox_probe",
        description="Run a lightweight provider smoke probe.",
        provider_method="probe",
        params_model=SandboxProviderSpecParams,
        side_effect=False,
        risk_level=ToolRiskLevel.LOW,
    ),
    _sandbox_contract(
        "sandbox_capacity_probe",
        description="Inspect provider inventory without creating sandbox capacity.",
        provider_method="capacity_probe",
        params_model=SandboxCapacityProbeParams,
        side_effect=False,
        risk_level=ToolRiskLevel.LOW,
    ),
)


SANDBOX_PROVIDER_CONTRACT_BY_METHOD: dict[str, ToolContract] = {
    str(contract.binding.provider_method): contract
    for contract in SANDBOX_PROVIDER_TOOL_CONTRACTS
    if isinstance(contract.binding, SandboxProviderBinding)
}


def sandbox_provider_contract_for_method(provider_method: str) -> ToolContract:
    """Return the canonical sandbox contract for one provider protocol method.

    Args:
        provider_method: Method name from the ``SandboxProvider`` protocol.

    Returns:
        Contract that owns validation and audit facts for the method.

    Raises:
        LookupError: If the provider method is not covered by a contract.
    """

    try:
        return SANDBOX_PROVIDER_CONTRACT_BY_METHOD[provider_method]
    except KeyError as exc:
        raise LookupError(f"Sandbox provider method {provider_method!r} has no ToolContract") from exc


def prepare_sandbox_provider_dispatch(
    provider_method: str,
    parameters: dict[str, object],
    *,
    policy: ToolDispatchPolicy | None = None,
) -> ToolDispatchEnvelope:
    """Validate a sandbox provider control-plane call against its contract.

    Args:
        provider_method: Method name from the ``SandboxProvider`` protocol.
        parameters: Raw provider call parameters before pydantic validation.
        policy: Optional dispatcher policy; defaults to operator-authorized
            sandbox control-plane dispatch.

    Returns:
        Validated dispatch envelope with route and audit metadata.
    """

    return ToolDispatcher().prepare(
        sandbox_provider_contract_for_method(provider_method),
        parameters,
        policy=policy or ToolDispatchPolicy(allow_operator=True),
    )


class SandboxProviderDispatcher:
    """Contract-gated facade for sandbox provider protocol calls.

    The facade is intentionally thin: it validates each provider call through
    ``ToolDispatcher`` and then forwards the original typed values to the
    provider. Lifecycle code should depend on this facade instead of calling
    provider methods directly.

    Attributes:
        _provider: Concrete sandbox backend implementing ``SandboxProvider``.
    """

    def __init__(self, provider: SandboxProvider) -> None:
        self._provider = provider

    def capabilities(self) -> SandboxProviderCapabilities:
        """Return provider capabilities; this is metadata, not a tool call.

        Returns:
            Provider-advertised feature set used for orchestration decisions.
        """

        return self._provider.capabilities()

    async def preflight(self, spec: SandboxProviderSpec) -> None:
        """Validate and run provider preflight for an allocation request.

        Args:
            spec: Provider allocation request to check before acquisition.
        """

        prepare_sandbox_provider_dispatch("preflight", {"spec": spec})
        await self._provider.preflight(spec)

    async def create(self, spec: SandboxProviderSpec) -> SandboxHandle:
        """Validate and create a provider-owned sandbox.

        Args:
            spec: Provider allocation request.

        Returns:
            Handle returned by the provider after creation.
        """

        prepare_sandbox_provider_dispatch("create", {"spec": spec})
        return await self._provider.create(spec)

    async def connect(self, spec: SandboxProviderSpec) -> SandboxHandle:
        """Validate and connect to an existing sandbox.

        Args:
            spec: Provider allocation request containing an existing id.

        Returns:
            Connected sandbox handle.
        """

        prepare_sandbox_provider_dispatch("connect", {"spec": spec})
        return await self._provider.connect(spec)

    async def run_command(self, handle: SandboxHandle, command: SandboxCommandSpec) -> SandboxCommandResult:
        """Validate and run a foreground command in a sandbox.

        Args:
            handle: Sandbox receiving the command.
            command: Command and timeout policy.

        Returns:
            Provider-normalized command result.
        """

        prepare_sandbox_provider_dispatch(
            "run_command",
            {"handle_id": handle.sandbox_id, "command": command},
        )
        return await self._provider.run_command(handle, command)

    async def start_process(self, handle: SandboxHandle, command: SandboxCommandSpec) -> SandboxCommandResult:
        """Validate and start a long-lived sandbox process.

        Args:
            handle: Sandbox receiving the process command.
            command: Process launch command and policy.

        Returns:
            Provider-normalized launch result.
        """

        prepare_sandbox_provider_dispatch(
            "start_process",
            {"handle_id": handle.sandbox_id, "command": command},
        )
        return await self._provider.start_process(handle, command)

    async def upload(self, handle: SandboxHandle, local: Path, remote: str) -> None:
        """Validate and upload one local file into a sandbox.

        Args:
            handle: Sandbox receiving the file.
            local: Local file path.
            remote: Absolute destination path inside the sandbox.
        """

        prepare_sandbox_provider_dispatch(
            "upload",
            {"handle_id": handle.sandbox_id, "local": local, "remote": remote},
        )
        await self._provider.upload(handle, local, remote)

    async def download(self, handle: SandboxHandle, remote: str, local: Path) -> None:
        """Validate and download one sandbox file to the host.

        Args:
            handle: Sandbox that owns the file.
            remote: Absolute source path inside the sandbox.
            local: Local destination path.
        """

        prepare_sandbox_provider_dispatch(
            "download",
            {"handle_id": handle.sandbox_id, "remote": remote, "local": local},
        )
        await self._provider.download(handle, remote, local)

    async def collect_logs(self, handle: SandboxHandle, destination: Path) -> list[SandboxLogRef]:
        """Validate and collect provider/device logs.

        Args:
            handle: Sandbox whose logs should be collected.
            destination: Local directory for collected logs.

        Returns:
            Local log references emitted by the provider.
        """

        prepare_sandbox_provider_dispatch(
            "collect_logs",
            {"handle_id": handle.sandbox_id, "destination": destination},
        )
        return await self._provider.collect_logs(handle, destination)

    async def terminate(self, handle: SandboxHandle) -> None:
        """Validate and terminate a provider-owned sandbox.

        Args:
            handle: Sandbox to terminate.
        """

        prepare_sandbox_provider_dispatch("terminate", {"handle_id": handle.sandbox_id})
        await self._provider.terminate(handle)

    async def extend_timeout(self, handle: SandboxHandle, timeout_seconds: int) -> None:
        """Validate and extend provider-side sandbox lifetime.

        Args:
            handle: Sandbox whose timeout should be extended.
            timeout_seconds: Positive provider timeout extension.
        """

        prepare_sandbox_provider_dispatch(
            "extend_timeout",
            {"handle_id": handle.sandbox_id, "timeout_seconds": timeout_seconds},
        )
        await self._provider.extend_timeout(handle, timeout_seconds)

    async def build_template(self, spec: SandboxTemplateSpec) -> str:
        """Validate and build or update a sandbox template.

        Args:
            spec: Provider-neutral template definition.

        Returns:
            Provider template alias or id.
        """

        prepare_sandbox_provider_dispatch("build_template", {"spec": spec})
        return await self._provider.build_template(spec)

    async def probe(self, spec: SandboxProviderSpec) -> SandboxCommandResult:
        """Validate and run a lightweight provider smoke probe.

        Args:
            spec: Provider allocation request used by the probe.

        Returns:
            Provider-normalized probe result.
        """

        prepare_sandbox_provider_dispatch("probe", {"spec": spec})
        return await self._provider.probe(spec)

    async def capacity_probe(self, request: SandboxCapacityProbeRequest) -> SandboxCapacityProbeResult:
        """Validate and run a passive provider capacity probe.

        Args:
            request: Inventory probe request.

        Returns:
            Provider capacity summary.
        """

        prepare_sandbox_provider_dispatch("capacity_probe", {"request": request})
        return await self._provider.capacity_probe(request)


__all__ = [
    "SANDBOX_PROVIDER_CONTRACT_BY_METHOD",
    "SANDBOX_PROVIDER_TOOL_CONTRACTS",
    "SandboxProviderDispatcher",
    "prepare_sandbox_provider_dispatch",
    "sandbox_provider_contract_for_method",
]
