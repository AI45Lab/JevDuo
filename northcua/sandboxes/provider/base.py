"""Provider protocol for NorthCUA sandbox backends."""

from __future__ import annotations

from pathlib import Path
from typing import Protocol, runtime_checkable

from northcua.sandboxes.contracts import (
    SandboxCapacityProbeRequest,
    SandboxCapacityProbeResult,
    SandboxCommandResult,
    SandboxCommandSpec,
    SandboxDeviceBinding,
    SandboxHandle,
    SandboxLogRef,
    SandboxProviderCapabilities,
    SandboxProviderSpec,
    SandboxTemplateSpec,
)


@runtime_checkable
class DirectDeviceWorkerProvider(Protocol):
    """Optional typed bootstrap for providers without a guest shell."""

    async def start_device_worker(self, handle: SandboxHandle, binding: SandboxDeviceBinding) -> None: ...


class SandboxProvider(Protocol):
    """Backend adapter used by batch runs to manage sandbox capacity."""

    @property
    def provider_id(self) -> str:
        """Return the stable provider id used in manifests.

        Returns:
            Provider id registered in ``SandboxProviderRegistry``.
        """
        ...

    def capabilities(self) -> SandboxProviderCapabilities:
        """Return optional feature support for this provider.

        Returns:
            Feature set used by orchestration code and CLI introspection.
        """
        ...

    async def preflight(self, spec: SandboxProviderSpec) -> None:
        """Validate local configuration before creating a sandbox.

        Args:
            spec: Provider allocation request.

        Raises:
            RuntimeError: If required dependencies or credentials are missing.
        """
        ...

    async def create(self, spec: SandboxProviderSpec) -> SandboxHandle:
        """Create a new sandbox from a provider template.

        Args:
            spec: Provider allocation request.

        Returns:
            Handle for the created sandbox.
        """
        ...

    async def connect(self, spec: SandboxProviderSpec) -> SandboxHandle:
        """Connect to an existing sandbox without taking ownership of deletion.

        Args:
            spec: Provider allocation request containing an existing sandbox id.

        Returns:
            Handle for the connected sandbox.
        """
        ...

    async def run_command(self, handle: SandboxHandle, command: SandboxCommandSpec) -> SandboxCommandResult:
        """Run a foreground command inside a sandbox.

        Args:
            handle: Sandbox receiving the command.
            command: Command and timeout policy.

        Returns:
            Provider-normalized command result.
        """
        ...

    async def start_process(self, handle: SandboxHandle, command: SandboxCommandSpec) -> SandboxCommandResult:
        """Start a long-lived background process inside a sandbox.

        Args:
            handle: Sandbox receiving the command.
            command: Process command and launch policy.

        Returns:
            Launch result; it only confirms process start when provider supports it.
        """
        ...

    async def upload(self, handle: SandboxHandle, local: Path, remote: str) -> None:
        """Upload one local file into a sandbox.

        Args:
            handle: Sandbox receiving the file.
            local: Local file to upload.
            remote: Remote absolute path.
        """
        ...

    async def download(self, handle: SandboxHandle, remote: str, local: Path) -> None:
        """Download one file from a sandbox.

        Args:
            handle: Sandbox that owns the file.
            remote: Remote absolute path.
            local: Destination local path.
        """
        ...

    async def collect_logs(self, handle: SandboxHandle, destination: Path) -> list[SandboxLogRef]:
        """Collect provider/device logs into a local destination.

        Args:
            handle: Sandbox whose logs should be collected.
            destination: Local directory for collected logs.

        Returns:
            Local log file references.
        """
        ...

    async def terminate(self, handle: SandboxHandle) -> None:
        """Terminate a provider-owned sandbox.

        Args:
            handle: Sandbox to terminate.
        """
        ...

    async def extend_timeout(self, handle: SandboxHandle, timeout_seconds: int) -> None:
        """Extend the provider-side sandbox lifetime from now.

        Args:
            handle: Sandbox whose provider-side lifetime should be extended.
            timeout_seconds: Desired lifetime in seconds from the extension call.
        """
        ...

    async def build_template(self, spec: SandboxTemplateSpec) -> str:
        """Build or update a provider template.

        Args:
            spec: Template definition.

        Returns:
            Provider template alias or id.
        """
        ...

    async def probe(self, spec: SandboxProviderSpec) -> SandboxCommandResult:
        """Run a lightweight provider smoke probe.

        Args:
            spec: Provider allocation request.

        Returns:
            Probe command result.
        """
        ...

    async def capacity_probe(self, request: SandboxCapacityProbeRequest) -> SandboxCapacityProbeResult:
        """Inspect provider inventory without creating sandbox capacity.

        Args:
            request: Passive capacity probe request and configured limits.

        Returns:
            Provider capacity summary derived from existing sandboxes.
        """
        ...
