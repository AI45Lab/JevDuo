"""Runtime wheel bundling for sandbox-hosted device workers."""

from __future__ import annotations

import logging
import os
import shutil
import zipfile
from email.parser import Parser
import shlex
import subprocess
from dataclasses import dataclass
from pathlib import Path

from northcua.sandboxes.contracts import SandboxCommandSpec, SandboxFileUploadSpec

logger = logging.getLogger(__name__)

_REMOTE_WHEEL_DIR = "/tmp/northcua-runtime-wheels"
_PACKAGE_DIRS = (
    Path("packages/northcua-protocol"),
    Path("packages/northcua-runtime-shared"),
    Path("."),
    Path("packages/northcua-device"),
)


@dataclass(frozen=True, slots=True)
class RuntimeWheelBundle:
    """Concrete runtime bundle uploaded before device worker bootstrap.

    Attributes:
        wheel_dir: Local directory containing freshly built wheels.
        uploads: Host-to-sandbox wheel uploads.
        startup_commands: Commands that install the wheels in the sandbox.
    """

    wheel_dir: Path
    uploads: tuple[SandboxFileUploadSpec, ...]
    startup_commands: tuple[SandboxCommandSpec, ...]


class RuntimeWheelBundleBuilder:
    """Build and stage NorthCUA device-worker wheels for E2B experiments."""

    def __init__(self, *, repo_root: Path | None = None, wheelhouse: Path | None = None) -> None:
        self._repo_root = repo_root or Path(__file__).resolve().parents[3]
        configured = wheelhouse or os.environ.get("NORTHCUA_RUNTIME_WHEELHOUSE")
        self._wheelhouse = Path(configured).expanduser().resolve() if configured else None

    def build(self, *, output_dir: Path, python_executable: str = "python3") -> RuntimeWheelBundle:
        """Build local wheels and return upload/install instructions.

        Args:
            output_dir: Experiment output directory used for wheel artifacts.
            python_executable: Sandbox Python executable used for installation.

        Returns:
            Runtime wheel bundle with provider-neutral upload and startup specs.

        Raises:
            RuntimeError: If ``uv build`` fails or no wheels are produced.
        """

        wheel_dir = output_dir / "runtime-wheels"
        if self._wheelhouse is not None and wheel_dir.resolve() == self._wheelhouse:
            raise ValueError("Runtime output must differ from the input wheelhouse")
        wheel_dir.mkdir(parents=True, exist_ok=True)
        for existing in wheel_dir.glob("*.whl"):
            existing.unlink()
        if self._wheelhouse is not None:
            required = {"northcua", "northcua-protocol", "northcua-runtime-shared", "northcua-device"}
            selected = {}
            for wheel in sorted(self._wheelhouse.glob("*.whl")):
                with zipfile.ZipFile(wheel) as archive:
                    metadata_path = next(name for name in archive.namelist() if name.endswith(".dist-info/METADATA"))
                    metadata = Parser().parsestr(archive.read(metadata_path).decode())
                name = metadata["Name"].lower().replace("_", "-")
                if name not in required:
                    continue
                if name in selected:
                    raise ValueError(f"Multiple {name} wheels in {self._wheelhouse}; use a single release wheelhouse")
                selected[name] = (wheel, metadata["Version"])
            if required - selected.keys():
                raise ValueError(f"Missing runtime wheels: {sorted(required - selected.keys())}")
            for wheel, _version in selected.values():
                shutil.copy2(wheel, wheel_dir / wheel.name)
        else:
            if not (self._repo_root / "pyproject.toml").is_file():
                raise RuntimeError(
                    "An installed NorthCUA needs NORTHCUA_RUNTIME_WHEELHOUSE pointing to the four release wheels"
                )
            for package_dir in _PACKAGE_DIRS:
                self._build_package(package_dir=package_dir, wheel_dir=wheel_dir)
        wheels = tuple(sorted(wheel_dir.glob("*.whl")))
        if not wheels:
            raise RuntimeError(f"runtime wheel build produced no wheels in {wheel_dir}")
        uploads = tuple(
            SandboxFileUploadSpec(
                local_path=wheel,
                remote_path=f"{_REMOTE_WHEEL_DIR}/{wheel.name}",
                mode="0644",
            )
            for wheel in wheels
        )
        return RuntimeWheelBundle(
            wheel_dir=wheel_dir,
            uploads=uploads,
            startup_commands=(
                self._ensure_python_command(python_executable=python_executable),
                self._install_command(python_executable=python_executable),
            ),
        )

    def _build_package(self, *, package_dir: Path, wheel_dir: Path) -> None:
        """Build one package directory with uv's PEP 517 frontend."""

        source = self._repo_root / package_dir
        logger.info("building sandbox runtime wheel", extra={"package_dir": str(source)})
        completed = subprocess.run(
            [
                "uv",
                "build",
                "--wheel",
                "--out-dir",
                str(wheel_dir),
                str(source),
            ],
            cwd=self._repo_root,
            text=True,
            capture_output=True,
            check=False,
        )
        if completed.returncode != 0:
            logger.error(
                "failed to build sandbox runtime wheel",
                extra={"package_dir": str(source), "stderr": completed.stderr[-2000:]},
            )
            raise RuntimeError(f"failed to build runtime wheel for {source}: {completed.stderr.strip()}")

    @staticmethod
    def _ensure_python_command(*, python_executable: str) -> SandboxCommandSpec:
        """Return a startup command that ensures the requested Python exists."""

        if python_executable == "python3":
            return SandboxCommandSpec(command="python3 --version", label="probe-python3", timeout_seconds=30)
        python = shlex.quote(python_executable)
        python_path = "/usr/local/bin/python3.12" if python_executable == "python3.12" else python_executable
        quoted_python_path = shlex.quote(python_path)
        command = "\n".join(
            [
                "set -euxo pipefail",
                'echo "[northcua.runtime] ensuring requested sandbox python"',
                f"if ! command -v {python} >/dev/null 2>&1; then",
                '  echo "[northcua.runtime] installing uv-managed Python 3.12"',
                "  curl -LsSf https://astral.sh/uv/install.sh | sh",
                '  export PATH="/root/.local/bin:$PATH"',
                "  uv python install 3.12",
                f'  ln -sf "$(uv python find 3.12)" {quoted_python_path}',
                "fi",
                'echo "[northcua.runtime] python path: $(command -v ' + python + ')"',
                f"{python} --version",
                f"{python} -m pip --version || {python} -m ensurepip --upgrade",
            ]
        )
        return SandboxCommandSpec(
            command=f"bash -lc {shlex.quote(command)}",
            label=f"ensure-{python_executable}",
            timeout_seconds=600,
        )

    def _install_command(self, *, python_executable: str) -> SandboxCommandSpec:
        """Return the sandbox command that installs the uploaded wheels."""

        wheel_dir = shlex.quote(_REMOTE_WHEEL_DIR)
        python = shlex.quote(python_executable)
        pip_flags = "--break-system-packages" if python_executable != "python3" else ""

        command = "\n".join(
            [
                "set -euxo pipefail",
                'echo "[northcua.runtime] installing NorthCUA runtime wheels"',
                (f"{python} -m pip install {pip_flags} --upgrade --force-reinstall --no-cache-dir {wheel_dir}/*.whl"),
                'echo "[northcua.runtime] installed northcua runtime"',
                f"{python} -m northcua_device.cli --help >/tmp/northcua-device-help.txt",
            ]
        )
        return SandboxCommandSpec(
            command=f"bash -lc {shlex.quote(command)}",
            label="install-northcua-runtime-wheels",
            timeout_seconds=600,
        )
