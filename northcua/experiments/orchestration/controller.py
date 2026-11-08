"""Managed local controller process for experiment runs."""

from __future__ import annotations

import asyncio
import os
import sys
from dataclasses import dataclass
from pathlib import Path

from northcua.experiments.contracts import ExperimentControllerSpec


@dataclass(slots=True)
class ManagedController:
    """Handle for a controller subprocess started by an experiment.

    Attributes:
        process: Running controller subprocess.
        stdout_path: File where controller stdout is captured.
        stderr_path: File where controller stderr is captured.
    """

    process: asyncio.subprocess.Process
    stdout_path: Path
    stderr_path: Path

    async def stop(self) -> None:
        """Terminate the controller process if it is still running."""

        if self.process.returncode is not None:
            return
        self.process.terminate()
        try:
            await asyncio.wait_for(self.process.wait(), timeout=10)
        except TimeoutError:
            self.process.kill()
            await self.process.wait()


class ManagedControllerProcess:
    """Start a local NorthCUA controller for an experiment when requested."""

    async def start(self, spec: ExperimentControllerSpec, *, output_dir: Path) -> ManagedController | None:
        """Start a controller subprocess or return ``None`` when unmanaged.

        Args:
            spec: Controller settings from the experiment spec.
            output_dir: Experiment output directory for process logs.

        Returns:
            Controller handle when ``managed_local`` is true, otherwise ``None``.

        Raises:
            RuntimeError: If the controller process exits before the startup
                grace period completes.
        """

        if not spec.managed_local:
            return None
        await self._ensure_port_available(spec)
        output_dir.mkdir(parents=True, exist_ok=True)
        stdout_path = output_dir / "controller.stdout.log"
        stderr_path = output_dir / "controller.stderr.log"
        args = self.serve_args(spec)
        env = dict(os.environ)
        env.setdefault("PYTHONPATH", ".")
        if spec.token:
            env["NORTHCUA_CONTROLLER_TOKEN"] = spec.token
        if spec.command_timeout_floor_seconds > 0:
            env["NORTHCUA_CONTROLLER_COMMAND_TIMEOUT_FLOOR_SECONDS"] = str(spec.command_timeout_floor_seconds)
        with stdout_path.open("wb") as stdout_handle, stderr_path.open("wb") as stderr_handle:
            process = await asyncio.create_subprocess_exec(
                *args,
                cwd=Path.cwd(),
                stdout=stdout_handle,
                stderr=stderr_handle,
                env=env,
            )
        await self._wait_until_ready(process, spec=spec, stderr_path=stderr_path)
        return ManagedController(process=process, stdout_path=stdout_path, stderr_path=stderr_path)

    def serve_args(self, spec: ExperimentControllerSpec) -> list[str]:
        """Build the controller ``serve`` argv used by managed experiments.

        Args:
            spec: Controller settings with resolved database, blob, and
                artifact paths.

        Returns:
            Shell-free argv list for ``asyncio.create_subprocess_exec``.
        """

        args = [
            sys.executable,
            "-m",
            "northcua.cli",
            "controller",
            "serve",
            "--host",
            spec.local_host,
            "--port",
            str(spec.local_port),
            "--app-config",
            str(spec.app_config_path),
        ]
        if spec.executor_mode != "in_process":
            args.extend(["--executor-mode", spec.executor_mode])
        if spec.parallelism > 0:
            args.extend(["--parallelism", str(spec.parallelism)])
        if spec.runner_workers > 0:
            args.extend(["--runner-workers", str(spec.runner_workers)])
        if spec.worker_queue_max > 0:
            args.extend(["--worker-queue-max", str(spec.worker_queue_max)])
        if spec.worker_start_timeout_seconds > 0:
            args.extend(["--worker-start-timeout-seconds", str(spec.worker_start_timeout_seconds)])
        if spec.worker_heartbeat_timeout_seconds > 0:
            args.extend(["--worker-heartbeat-timeout-seconds", str(spec.worker_heartbeat_timeout_seconds)])
        if spec.process_worker_module:
            args.extend(["--process-worker-module", spec.process_worker_module])
        if spec.db_path is not None:
            args.extend(["--db-path", str(spec.db_path)])
        if spec.blob_dir is not None:
            args.extend(["--blob-dir", str(spec.blob_dir)])
        if spec.artifact_root_dir is not None:
            args.extend(["--artifact-root-dir", str(spec.artifact_root_dir)])
        if spec.insecure_dev_no_auth:
            args.append("--insecure-dev-no-auth")
        return args

    async def _wait_until_ready(
        self,
        process: asyncio.subprocess.Process,
        *,
        spec: ExperimentControllerSpec,
        stderr_path: Path,
    ) -> None:
        """Wait until the local controller TCP listener accepts connections.

        Args:
            process: Controller subprocess.
            spec: Controller settings containing host, port, and timeout.
            stderr_path: File used to enrich startup failure diagnostics.

        Raises:
            RuntimeError: If the process exits or the port never opens.
        """

        deadline = asyncio.get_running_loop().time() + spec.startup_timeout_seconds
        last_error = ""
        while asyncio.get_running_loop().time() < deadline:
            if process.returncode is not None:
                stderr = stderr_path.read_text(encoding="utf-8", errors="replace")
                raise RuntimeError(f"managed controller exited during startup: {stderr[-2000:]}")
            try:
                reader, writer = await asyncio.open_connection(spec.local_host, spec.local_port)
            except OSError as exc:
                last_error = str(exc)
                await asyncio.sleep(0.2)
                continue
            writer.close()
            await writer.wait_closed()
            _ = reader
            return
        raise RuntimeError(f"managed controller did not become ready: {last_error}")

    async def _ensure_port_available(self, spec: ExperimentControllerSpec) -> None:
        """Fail fast when another process already owns the managed port.

        Args:
            spec: Controller settings containing host and port.

        Raises:
            RuntimeError: If a listener already exists on the requested port.
        """

        try:
            reader, writer = await asyncio.open_connection(spec.local_host, spec.local_port)
        except OSError:
            return
        writer.close()
        await writer.wait_closed()
        _ = reader
        raise RuntimeError(
            f"managed controller port is already in use: {spec.local_host}:{spec.local_port}; "
            "choose a free port or set managed_local=false to connect to an existing controller"
        )
