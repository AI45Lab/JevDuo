"""Live template doctor probes for desktop E2B templates."""

from __future__ import annotations

import json
import shlex

from pydantic import BaseModel, Field

from northcua.sandboxes.contracts import SandboxCommandResult, SandboxCommandSpec, SandboxProviderSpec
from northcua.sandboxes.provider.base import SandboxProvider
from northcua.sandboxes.tool_contracts import SandboxProviderDispatcher

CORE_DOCTOR_BINARIES = (
    "python3",
    "python3.12",
    "northcua-device",
    "xdotool",
    "wmctrl",
    "scrot",
)


class SandboxTemplateDoctorReport(BaseModel):
    """Structured result for one template doctor run.

    Fields:
        provider: Provider id used for the probe sandbox.
        template: Template alias that was probed.
        healthy: Whether the doctor command succeeded.
        result: Raw sandbox command result for diagnostics.
        checked_binaries: Binaries included in the doctor command.
    """

    provider: str
    template: str
    healthy: bool
    result: SandboxCommandResult
    checked_binaries: list[str] = Field(default_factory=list)


class SandboxTemplateDoctor:
    """Create a short-lived sandbox and verify template readiness."""

    def __init__(self, *, checked_binaries: list[str] | None = None, python_executable: str = "python3.12") -> None:
        self._python_executable = python_executable
        self._checked_binaries = _merged_checked_binaries(checked_binaries)

    async def run(self, *, provider: SandboxProvider, spec: SandboxProviderSpec) -> SandboxTemplateDoctorReport:
        """Run the doctor command in a short-lived sandbox.

        Args:
            provider: Provider instance used to create and terminate sandbox.
            spec: Sandbox provider spec containing template and desktop mode.

        Returns:
            Structured doctor report with command output and health flag.
        """

        dispatcher = SandboxProviderDispatcher(provider)
        handle = await dispatcher.create(spec)
        try:
            result = await dispatcher.run_command(
                handle,
                SandboxCommandSpec(
                    label="northcua-template-doctor",
                    command=self._doctor_command(),
                    timeout_seconds=120,
                ),
            )
        finally:
            await dispatcher.terminate(handle)
        return SandboxTemplateDoctorReport(
            provider=spec.provider,
            template=spec.template,
            healthy=result.ok,
            result=result,
            checked_binaries=self._checked_binaries,
        )

    def _doctor_command(self) -> str:
        python = shlex.quote(self._python_executable)
        return f"""bash -lc 'set -euo pipefail
export DISPLAY=${{DISPLAY:-:0}}
{python} - <<'"'"'PY'"'"'
import importlib.util, json, os, shutil, subprocess
binaries = {json.dumps(self._checked_binaries)}
mods = ["northcua", "northcua_device", "northcua_protocol", "northcua_runtime_shared", "gi", "pyatspi", "pyautogui", "PIL"]
def run(cmd):
    cp = subprocess.run(cmd, text=True, capture_output=True, timeout=10)
    return {{"rc": cp.returncode, "out": cp.stdout.strip()[:300], "err": cp.stderr.strip()[:300]}}
report = {{
    "display": os.environ.get("DISPLAY", ""),
    "binaries": {{name: shutil.which(name) or "" for name in binaries}},
    "imports": {{name: bool(importlib.util.find_spec(name)) for name in mods}},
}}
report["python3"] = run(["python3", "--version"])
if shutil.which("python3.12"):
    report["python3.12"] = run(["python3.12", "--version"])
if shutil.which("scrot"):
    report["screenshot"] = run(["bash", "-lc", "scrot /tmp/northcua-template-doctor.png && test -s /tmp/northcua-template-doctor.png"])
missing = [name for name, path in report["binaries"].items() if not path]
failed_imports = [name for name, ok in report["imports"].items() if not ok]
print(json.dumps(report, indent=2, sort_keys=True))
if missing or failed_imports:
    raise SystemExit("missing binaries=" + ",".join(missing) + " failed_imports=" + ",".join(failed_imports))
print("checked " + ",".join(binaries))
PY
'"""


def _merged_checked_binaries(checked_binaries: list[str] | None) -> list[str]:
    """Return core runtime checks plus caller-supplied app-specific binaries."""

    merged: list[str] = []
    for binary in [*CORE_DOCTOR_BINARIES, *(checked_binaries or [])]:
        if binary not in merged:
            merged.append(binary)
    return merged
