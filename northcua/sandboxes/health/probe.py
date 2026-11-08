"""Provider-neutral sandbox health probes."""

from __future__ import annotations

from pydantic import BaseModel, Field

from northcua.sandboxes.contracts import SandboxCommandSpec, SandboxHandle
from northcua.sandboxes.provider.base import SandboxProvider
from northcua.sandboxes.tool_contracts import SandboxProviderDispatcher


class SandboxHealthReport(BaseModel):
    """Health status for one sandbox/device worker pair.

    Attributes:
        sandbox_id: Provider sandbox id.
        healthy: Whether all configured probes passed.
        checks: Per-check pass/fail values.
        error: Last failed probe summary.
    """

    sandbox_id: str
    healthy: bool
    checks: dict[str, bool] = Field(default_factory=dict)
    error: str = ""


class SandboxHealthProbe:
    """Run lightweight commands to validate sandbox readiness."""

    def __init__(self, command: SandboxCommandSpec | None = None) -> None:
        self._command = command or SandboxCommandSpec(
            command="python3 - <<'PY'\nprint('northcua-health-ok')\nPY",
            label="northcua-health-python",
            timeout_seconds=15.0,
        )

    async def probe(self, *, provider: SandboxProvider, handle: SandboxHandle) -> SandboxHealthReport:
        """Run the configured health command in a sandbox.

        Args:
            provider: Provider that owns the sandbox handle.
            handle: Sandbox to probe.

        Returns:
            Health report suitable for telemetry and runbooks.
        """

        result = await SandboxProviderDispatcher(provider).run_command(handle, self._command)
        healthy = result.ok and "northcua-health-ok" in result.stdout
        return SandboxHealthReport(
            sandbox_id=handle.sandbox_id,
            healthy=healthy,
            checks={"python": healthy},
            error="" if healthy else (result.stderr or result.stdout or "health command failed"),
        )
