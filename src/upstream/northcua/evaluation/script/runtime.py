"""Provider-neutral runtime port for argv-safe script evaluation."""

from __future__ import annotations

import base64
import json
import shlex
from pathlib import Path
from typing import Protocol

from northcua.sandboxes.contracts import (
    SandboxCommandResult,
    SandboxCommandSpec,
    SandboxFileUploadSpec,
    SandboxLease,
)
from northcua.sandboxes.lifecycle.task_lease import SandboxTaskLeaseManager


class ScriptEvaluatorRuntimePort(Protocol):
    """Run one argv command and download evaluator artifacts from a live runtime."""

    async def run_argv(
        self,
        argv: tuple[str, ...],
        *,
        timeout_seconds: float,
        cwd: str,
        env: dict[str, str],
        allow_failure: bool,
    ) -> SandboxCommandResult:
        """Execute argv without shell interpretation."""

        ...

    async def download_file(self, remote_path: str, local_path: Path) -> None:
        """Download one runtime file to an explicit local path."""

        ...

    async def stage_files(self, uploads: list[SandboxFileUploadSpec]) -> None:
        """Stage evaluator-only inputs after the main agent has stopped."""

        ...


class SandboxLeaseScriptEvaluatorRuntime:
    """Adapt an existing NorthCUA sandbox lease to the argv-safe evaluator port.

    Provider APIs historically accept a shell command string. This adapter
    sends evaluator arguments as base64-encoded JSON to a fixed Python
    trampoline, which invokes them with ``shell=False``. User-controlled argv
    is therefore never interpreted by the provider shell.
    """

    def __init__(self, *, manager: SandboxTaskLeaseManager, lease: SandboxLease) -> None:
        self._manager = manager
        self._lease = lease

    async def run_argv(
        self,
        argv: tuple[str, ...],
        *,
        timeout_seconds: float,
        cwd: str,
        env: dict[str, str],
        allow_failure: bool,
    ) -> SandboxCommandResult:
        """Execute exact argv via a fixed ``subprocess.run(shell=False)`` trampoline."""

        if not argv or not argv[0] or any("\x00" in item for item in argv):
            raise ValueError("script evaluator argv requires a non-empty NUL-free executable")
        payload = base64.urlsafe_b64encode(
            json.dumps(
                {"argv": list(argv), "cwd": cwd, "env": env},
                ensure_ascii=False,
                separators=(",", ":"),
            ).encode("utf-8")
        ).decode("ascii")
        script = (
            "import base64,json,os,subprocess,sys;"
            "p=json.loads(base64.urlsafe_b64decode(sys.argv[1]));"
            "e=os.environ.copy();e.update(p['env']);"
            "r=subprocess.run(p['argv'],cwd=p['cwd'] or None,env=e,capture_output=True,text=True,shell=False);"
            "sys.stdout.write(r.stdout);sys.stderr.write(r.stderr);sys.exit(r.returncode)"
        )
        command = f"python3 -c {shlex.quote(script)} {shlex.quote(payload)}"
        return await self._manager.run_command(
            self._lease,
            SandboxCommandSpec(
                command=command,
                label="script-evaluator-argv",
                timeout_seconds=timeout_seconds,
                allow_failure=allow_failure,
            ),
        )

    async def download_file(self, remote_path: str, local_path: Path) -> None:
        """Download one evaluator artifact from the bound lease."""

        await self._manager.download_file(self._lease, remote=remote_path, local=local_path)

    async def stage_files(self, uploads: list[SandboxFileUploadSpec]) -> None:
        """Stage evaluator-only inputs into the bound live sandbox."""

        await self._manager.stage_evaluator_files(self._lease, uploads)


__all__ = ["SandboxLeaseScriptEvaluatorRuntime", "ScriptEvaluatorRuntimePort"]
