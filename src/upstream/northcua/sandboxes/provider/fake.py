"""In-memory sandbox provider used by tests and local dry runs."""

from __future__ import annotations

import itertools
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


class FakeSandboxProvider:
    """Deterministic sandbox provider with no external side effects."""

    provider_id = "fake"

    def __init__(self) -> None:
        self._counter = itertools.count(1)
        self.handles: dict[str, SandboxHandle] = {}
        self.commands: list[tuple[str, SandboxCommandSpec]] = []
        self.terminated: list[str] = []
        self.extended_timeouts: list[tuple[str, int]] = []
        self.uploads: list[tuple[str, Path, str]] = []
        self.downloads: list[tuple[str, str, Path]] = []

    def capabilities(self) -> SandboxProviderCapabilities:
        """Return fake provider feature support.

        Returns:
            Full feature set so tests can exercise every orchestration path.
        """

        return SandboxProviderCapabilities(
            features={
                SandboxProviderFeature.CONNECT_EXISTING,
                SandboxProviderFeature.REUSE,
                SandboxProviderFeature.UPLOAD,
                SandboxProviderFeature.DOWNLOAD,
                SandboxProviderFeature.COLLECT_LOGS,
                SandboxProviderFeature.TEMPLATE_BUILD,
                SandboxProviderFeature.CAPACITY_PROBE,
                SandboxProviderFeature.EXTEND_TIMEOUT,
            }
        )

    async def preflight(self, spec: SandboxProviderSpec) -> None:
        """Validate that the fake provider was selected intentionally.

        Args:
            spec: Provider allocation request.

        Raises:
            RuntimeError: If a non-fake provider id is passed accidentally.
        """

        if spec.provider != self.provider_id:
            raise RuntimeError(f"fake provider received incompatible spec: {spec.provider}")

    async def create(self, spec: SandboxProviderSpec) -> SandboxHandle:
        """Create a synthetic sandbox handle.

        Args:
            spec: Provider allocation request.

        Returns:
            In-memory sandbox handle.
        """

        sandbox_id = f"fake-sandbox-{next(self._counter)}"
        handle = SandboxHandle(
            sandbox_id=sandbox_id,
            provider=self.provider_id,
            template=spec.template,
            state=SandboxLifecycleState.CREATED,
            metadata={**spec.metadata, "desktop": spec.desktop},
        )
        self.handles[sandbox_id] = handle
        return handle

    async def connect(self, spec: SandboxProviderSpec) -> SandboxHandle:
        """Connect to a synthetic existing sandbox.

        Args:
            spec: Provider allocation request containing ``sandbox_id``.

        Returns:
            Connected handle for the existing sandbox id.
        """

        handle = SandboxHandle(
            sandbox_id=spec.sandbox_id,
            provider=self.provider_id,
            template=spec.template,
            state=SandboxLifecycleState.CONNECTED,
            metadata={"connected": True},
        )
        self.handles[handle.sandbox_id] = handle
        return handle

    async def run_command(self, handle: SandboxHandle, command: SandboxCommandSpec) -> SandboxCommandResult:
        """Record and echo a foreground command.

        Args:
            handle: Sandbox receiving the command.
            command: Command policy to record.

        Returns:
            Successful echo result unless the command contains ``fail``.
        """

        self.commands.append((handle.sandbox_id, command))
        if "fail" in command.command:
            return SandboxCommandResult(exit_code=1, stderr=self._redact("fake command failed", command))
        return SandboxCommandResult(stdout=self._redact(f"fake:{handle.sandbox_id}:{command.command}", command))

    async def start_process(self, handle: SandboxHandle, command: SandboxCommandSpec) -> SandboxCommandResult:
        """Record and echo a background process launch.

        Args:
            handle: Sandbox receiving the command.
            command: Process launch policy.

        Returns:
            Successful launch result.
        """

        self.commands.append((handle.sandbox_id, command))
        return SandboxCommandResult(stdout=self._redact(f"started:{handle.sandbox_id}:{command.command}", command))

    async def terminate(self, handle: SandboxHandle) -> None:
        """Mark a synthetic sandbox as terminated.

        Args:
            handle: Sandbox to terminate.
        """

        self.terminated.append(handle.sandbox_id)
        if handle.sandbox_id in self.handles:
            self.handles[handle.sandbox_id] = handle.model_copy(update={"state": SandboxLifecycleState.TERMINATED})

    async def extend_timeout(self, handle: SandboxHandle, timeout_seconds: int) -> None:
        """Record a synthetic provider-side timeout extension.

        Args:
            handle: Synthetic sandbox handle whose timeout would be extended.
            timeout_seconds: Requested provider timeout in seconds.
        """

        self.extended_timeouts.append((handle.sandbox_id, timeout_seconds))

    async def upload(self, handle: SandboxHandle, local: Path, remote: str) -> None:
        """Record a synthetic file upload.

        Args:
            handle: Sandbox receiving the file.
            local: Local source path.
            remote: Remote destination path.
        """

        self.uploads.append((handle.sandbox_id, local, remote))

    async def download(self, handle: SandboxHandle, remote: str, local: Path) -> None:
        """Write a deterministic synthetic downloaded file.

        Args:
            handle: Sandbox owning the synthetic file.
            remote: Remote source path recorded for assertions.
            local: Local destination path to create.
        """

        self.downloads.append((handle.sandbox_id, remote, local))
        local.parent.mkdir(parents=True, exist_ok=True)
        local.write_text(f"fake-download:{handle.sandbox_id}:{remote}\n", encoding="utf-8")

    async def collect_logs(self, handle: SandboxHandle, destination: Path) -> list[SandboxLogRef]:
        """Write a deterministic fake worker log.

        Args:
            handle: Sandbox whose logs should be collected.
            destination: Local directory for the generated log.

        Returns:
            Single log reference for the generated worker log.
        """

        destination.mkdir(parents=True, exist_ok=True)
        path = destination / f"{handle.sandbox_id}-worker.log"
        path.write_text(f"fake log for {handle.sandbox_id}\n", encoding="utf-8")
        return [SandboxLogRef(name="worker.log", local_path=path, size_bytes=path.stat().st_size)]

    async def build_template(self, spec: SandboxTemplateSpec) -> str:
        """Return the fake template alias without external work.

        Args:
            spec: Template definition.

        Returns:
            Template alias from the spec.
        """

        return spec.alias

    async def probe(self, spec: SandboxProviderSpec) -> SandboxCommandResult:
        """Run a synthetic provider probe.

        Args:
            spec: Provider allocation request.

        Returns:
            Successful probe result.
        """

        return SandboxCommandResult(stdout=f"fake provider ready: {spec.template}")

    async def capacity_probe(self, request: SandboxCapacityProbeRequest) -> SandboxCapacityProbeResult:
        """Inspect in-memory fake sandbox handles without creating capacity.

        Args:
            request: Passive capacity probe request.

        Returns:
            Capacity summary derived from active fake handles.
        """

        items = [
            SandboxInventoryItem(
                sandbox_id=handle.sandbox_id,
                provider=self.provider_id,
                template=handle.template,
                state=(
                    SandboxInventoryState.TERMINATED
                    if handle.state == SandboxLifecycleState.TERMINATED
                    else SandboxInventoryState.RUNNING
                ),
                metadata=handle.metadata,
                desktop_url=handle.desktop_url,
            )
            for handle in self.handles.values()
        ]
        return build_capacity_probe_result(request=request, source="fake_memory", items=items)

    @staticmethod
    def _redact(text: str, command: SandboxCommandSpec) -> str:
        redacted = text
        for secret in command.redact:
            if secret:
                redacted = redacted.replace(secret, "[REDACTED]")
        return redacted
