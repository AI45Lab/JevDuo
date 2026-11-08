"""Local browser workers for trusted small batches; no OS sandbox isolation."""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import time
from urllib.parse import urlsplit
import uuid

from northcua.sandboxes.contracts import (
    SandboxAllocationStrategy,
    SandboxCommandResult,
    SandboxDeviceBinding,
    SandboxHandle,
    SandboxLogRef,
    SandboxProviderCapabilities,
    SandboxProviderFeature,
    SandboxProviderSpec,
    SandboxReadinessState,
)
from northcua.sandboxes.device.readiness import ControllerSQLiteDeviceReadinessProbe
from northcua.sandboxes.capacity import build_capacity_probe_result
from northcua.sandboxes.capacity_contracts import SandboxInventoryItem, SandboxInventoryState
from northcua_protocol.capabilities import Capability, PlatformKind


class LocalProcessSandboxProvider:
    """Fresh browser workers on this machine, using the current Python install.

    Only browser capability and a local controller are supported. Guest shell,
    uploads, templates and resource isolation intentionally are not emulated.
    A supervisor bounds orphan lifetime after runner death, including SIGKILL.
    """

    provider_id = "local-process"

    def __init__(self, root: Path | None = None):
        self.root = root or Path(tempfile.gettempdir()) / f"northcua-local-{os.getuid()}"
        self._processes: dict[str, subprocess.Popen] = {}

    def _root(self) -> Path:
        if os.name != "posix":
            raise RuntimeError("local-process batches require macOS or Linux")
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        info = self.root.lstat()
        if self.root.is_symlink() or info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise RuntimeError("local-process state directory must be owned by this user with mode 0700")
        return self.root

    def _directory(self, handle: SandboxHandle) -> Path:
        if handle.provider != self.provider_id or not re.fullmatch(r"local-[0-9a-f]{32}", handle.sandbox_id):
            raise ValueError("Invalid local-process handle")
        directory = self._root() / handle.sandbox_id
        if directory.is_symlink():
            raise ValueError("Local allocation must not be a symlink")
        return directory

    def capabilities(self) -> SandboxProviderCapabilities:
        return SandboxProviderCapabilities(
            features={
                SandboxProviderFeature.CONNECT_EXISTING,
                SandboxProviderFeature.COLLECT_LOGS,
                SandboxProviderFeature.EXTEND_TIMEOUT,
                SandboxProviderFeature.CAPACITY_PROBE,
            }
        )

    async def preflight(self, spec: SandboxProviderSpec) -> None:
        self._root()
        if spec.provider != self.provider_id or spec.desktop or spec.platform != PlatformKind.BROWSER:
            raise ValueError("local-process supports only desktop: false and platform: browser")
        if spec.template != "browser" or spec.file_uploads:
            raise ValueError("local-process requires template: browser and does not support file uploads")
        if spec.metadata.get("agent_kind", "web") != "web":
            raise ValueError("local-process supports only Web tasks")
        if any((spec.resources.cpu_count, spec.resources.memory_mb, spec.resources.disk_mb)):
            raise ValueError("local-process cannot enforce CPU, memory or disk limits")
        if spec.resources.timeout_seconds <= 0:
            raise ValueError("local-process requires a positive lifetime")
        db = Path(str(spec.metadata.get("controller_db", ""))).expanduser()
        if not db.is_absolute() or not db.is_file():
            raise ValueError(
                "Set sandbox.metadata.controller_db to the running local controller's absolute SQLite path"
            )

    async def create(self, spec: SandboxProviderSpec) -> SandboxHandle:
        await self.preflight(spec)
        if spec.allocation_strategy == SandboxAllocationStrategy.CONNECT_EXISTING:
            return await self.connect(spec)
        handle = SandboxHandle(
            sandbox_id=f"local-{uuid.uuid4().hex}",
            provider=self.provider_id,
            template="browser",
            metadata=spec.metadata,
        )
        directory = self._directory(handle)
        directory.mkdir(mode=0o700)
        (directory / "handle.json").write_text(handle.model_dump_json())
        (directory / "deadline").write_text(str(time.time() + spec.resources.timeout_seconds))
        return handle

    async def connect(self, spec: SandboxProviderSpec) -> SandboxHandle:
        handle = SandboxHandle(sandbox_id=spec.sandbox_id, provider=self.provider_id, template="browser")
        stored = SandboxHandle.model_validate_json((self._directory(handle) / "handle.json").read_text())
        if stored.sandbox_id != spec.sandbox_id or spec.template != "browser":
            raise ValueError("Local allocation identity mismatch")
        return stored

    async def start_device_worker(self, handle: SandboxHandle, binding: SandboxDeviceBinding) -> None:
        from northcua.sandboxes.device.bootstrap import _worker_argv

        if binding.platform != PlatformKind.BROWSER or binding.agentkit_agent_kinds != {"web"}:
            raise ValueError("local-process can start only a Web browser worker")
        if binding.capabilities != {Capability.BROWSER_PAGE}:
            raise ValueError("local-process permits only browser.page capability")
        url = urlsplit(binding.server_url)
        if url.scheme not in {"ws", "wss"} or url.hostname not in {"127.0.0.1", "localhost", "::1"}:
            raise ValueError("local-process requires a loopback controller")
        directory = self._directory(handle)
        if any((directory / name).exists() for name in ("worker.json", "started.json", "stop", "finished.json")):
            raise RuntimeError("Local allocation has already started or been retired")
        local_binding = binding.model_copy(
            update={"python_executable": sys.executable, "log_dir": str(directory / "device")}
        )
        argv = _worker_argv(local_binding, include_token=False, include_config=False)
        if not binding.token:
            argv.append("--insecure-dev-no-auth")
        # Workers need OS/browser settings, not model or cloud credentials.
        names = (
            "PATH",
            "HOME",
            "USER",
            "TMPDIR",
            "LANG",
            "DISPLAY",
            "XAUTHORITY",
            "SYSTEMROOT",
            "PLAYWRIGHT_BROWSERS_PATH",
            "NORTHCUA_BROWSER_EXECUTABLE",
        )
        environment = {key: os.environ[key] for key in names if key in os.environ}
        environment.update({"NO_PROXY": "localhost,127.0.0.1,::1", "NORTHCUA_DEVICE_TOKEN": binding.token})
        with (directory / "worker.json").open("x") as target:
            os.chmod(target.name, 0o600)
            json.dump({"argv": argv, "environment": environment}, target)
        try:
            with (directory / "supervisor.log").open("ab") as log:
                process = subprocess.Popen(
                    [
                        sys.executable,
                        "-m",
                        "northcua.sandboxes.provider.local_supervisor",
                        str(directory),
                        str(os.getpid()),
                    ],
                    stdin=subprocess.DEVNULL,
                    stdout=log,
                    stderr=subprocess.STDOUT,
                    start_new_session=True,
                )
        except OSError:
            (directory / "worker.json").unlink(missing_ok=True)
            (directory / "finished.json").write_text('{"reason":"launch_failed","cleanup_confirmed":true}')
            raise
        self._processes[handle.sandbox_id] = process
        probe = ControllerSQLiteDeviceReadinessProbe(str(handle.metadata["controller_db"]), poll_interval_seconds=0.1)
        try:
            result = await probe.wait_for_ready(binding.device_id, timeout_seconds=30)
            if result.state != SandboxReadinessState.READY:
                raise RuntimeError("Local browser worker did not register; inspect sandbox-logs")
        except BaseException:
            await asyncio.shield(self.terminate(handle))
            raise

    async def terminate(self, handle: SandboxHandle) -> None:
        directory = self._directory(handle)
        if not directory.exists():
            return
        (directory / "stop").touch()
        process = self._processes.get(handle.sandbox_id)
        if (directory / "worker.json").exists() and not (directory / "finished.json").exists():
            deadline = time.monotonic() + 15
            while not (directory / "finished.json").exists():
                if process is not None and process.poll() is not None:
                    raise RuntimeError(
                        "Local supervisor exited without a cleanup receipt; worker cleanup is unconfirmed"
                    )
                if time.monotonic() > deadline:
                    raise TimeoutError("Local worker cleanup not confirmed; allocation retained for inspection")
                await asyncio.sleep(0.1)
        if process is not None:
            await asyncio.to_thread(process.wait, timeout=5)
        receipt = directory / "finished.json"
        if receipt.exists() and not json.loads(receipt.read_text()).get("cleanup_confirmed", False):
            raise RuntimeError("Local supervisor could not confirm complete worker cleanup; inspect finished.json")
        # Leave a small identity/cleanup receipt for idempotent maintenance.
        (directory / "worker.json").unlink(missing_ok=True)
        if not (directory / "finished.json").exists():
            if (directory / "started.json").exists():
                raise RuntimeError("A worker started but cleanup is unconfirmed; allocation retained")
            (directory / "finished.json").write_text('{"reason":"not_started","cleanup_confirmed":true}')

    async def extend_timeout(self, handle: SandboxHandle, timeout_seconds: int) -> None:
        directory = self._directory(handle)
        path = directory / f"deadline-{uuid.uuid4().hex}"
        path.write_text(str(time.time() + timeout_seconds))
        path.replace(directory / "deadline")

    async def collect_logs(self, handle: SandboxHandle, destination: Path) -> list[SandboxLogRef]:
        destination.mkdir(parents=True, exist_ok=True)
        results = []
        for name in ("worker.log", "supervisor.log", "finished.json"):
            source = self._directory(handle) / name
            if source.is_file():
                target = destination / name
                shutil.copyfile(source, target)
                results.append(SandboxLogRef(name=name, local_path=target, size_bytes=target.stat().st_size))
        return results

    async def run_command(self, handle, command):
        raise ValueError("local-process does not run guest shell commands; serve local fixtures separately")

    async def start_process(self, handle, command):
        raise ValueError("local-process uses typed device bootstrap only")

    async def upload(self, handle, local, remote):
        raise ValueError("local-process does not support guest uploads")

    async def download(self, handle, remote, local):
        raise ValueError("local-process does not support guest downloads")

    async def build_template(self, spec):
        raise ValueError("local-process uses the installed browser; no template build is needed")

    async def probe(self, spec):
        await self.preflight(spec)
        return SandboxCommandResult(stdout="Local browser provider configuration valid; no worker started")

    async def capacity_probe(self, request):
        if request.provider != self.provider_id:
            raise ValueError("Wrong provider for local allocation inventory")
        items = []
        for path in self._root().glob("local-*/handle.json"):
            handle = SandboxHandle.model_validate_json(path.read_text())
            directory = self._directory(handle)
            if directory != path.parent:
                raise ValueError("Local allocation inventory identity mismatch")
            items.append(
                SandboxInventoryItem(
                    sandbox_id=handle.sandbox_id,
                    provider=self.provider_id,
                    template=handle.template,
                    state=(
                        SandboxInventoryState.TERMINATED
                        if (
                            (directory / "finished.json").exists()
                            and json.loads((directory / "finished.json").read_text()).get("cleanup_confirmed", False)
                        )
                        else SandboxInventoryState.UNKNOWN
                    ),
                    metadata=handle.metadata,
                )
            )
        return build_capacity_probe_result(
            request=request,
            source="local_allocation_receipts",
            items=items,
            warnings=["Local processes share the host; inventory is not an OS resource capacity guarantee"],
        )
