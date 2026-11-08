"""Controller-driven batch runner facade."""

from __future__ import annotations

try:
    import fcntl
except ImportError:  # Windows can use local agents; batch controllers require Linux.
    fcntl = None
import hashlib
import json
import os
import sys
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from types import TracebackType

from northcua.batch.context_snapshot import freeze_batch_context_configs
from northcua.batch.contracts import (
    BatchManifest,
    BatchRunConfig,
    BatchRunResult,
    BatchRunResumeConfig,
    BatchTaskStatus,
)
from northcua.batch.metrics import BatchMetrics
from northcua.batch.planner import BatchTaskPlanner
from northcua.batch.reporting import BatchReportWriter
from northcua.batch.runner.bindings import SandboxDeviceBindingFactory
from northcua.batch.runner.controller import ControllerTaskWatcher, SubmitterFactory
from northcua.batch.runner.event_loop_watchdog import RunnerEventLoopWatchdog
from northcua.batch.runner.events import BatchEventWriter
from northcua.batch.runner.executor import BatchAttemptExecutor
from northcua.batch.runner.resume import BatchStatusReconciler
from northcua.batch.runner.scheduler import BatchScheduler
from northcua.batch.runner.validation import (
    resume_config_snapshot_for_write,
    validate_existing_manifest,
    validate_existing_run_config,
    validate_prepared_sandbox_allocations,
)
from northcua.batch.state import SQLiteBatchStateRepository
from northcua.sandboxes.device.readiness import DeviceReadinessProbe
from northcua.sandboxes.provider.registry import SandboxProviderRegistry, default_sandbox_provider_registry
from northcua.telemetry.background_jsonl import BackgroundJsonlWriter
from northcua.telemetry.sinks import BufferedLocalTelemetrySink, CompositeTelemetrySink, TelemetrySink

BatchTelemetrySinkFactory = Callable[[BatchRunConfig, Path], TelemetrySink | None]


class BatchRunner:
    """Run a batch by provisioning sandboxes and submitting controller tasks."""

    def __init__(
        self,
        *,
        provider_registry: SandboxProviderRegistry | None = None,
        submitter_factory: SubmitterFactory | None = None,
        planner: BatchTaskPlanner | None = None,
        readiness_probe: DeviceReadinessProbe | None = None,
        telemetry_sink_factory: BatchTelemetrySinkFactory | None = None,
    ) -> None:
        self._providers = provider_registry or default_sandbox_provider_registry()
        self._planner = planner or BatchTaskPlanner()
        self._watcher = ControllerTaskWatcher(submitter_factory)
        self._binding_factory = SandboxDeviceBindingFactory()
        self._executor = BatchAttemptExecutor(
            provider_registry=self._providers,
            planner=self._planner,
            watcher=self._watcher,
            binding_factory=self._binding_factory,
            readiness_probe=readiness_probe,
        )
        self._scheduler = BatchScheduler(
            provider_registry=self._providers,
            executor=self._executor,
            binding_factory=self._binding_factory,
            resume_reconciler=BatchStatusReconciler(self._watcher),
            readiness_probe=readiness_probe,
        )
        self._telemetry_sink_factory = telemetry_sink_factory

    async def run(self, config: BatchRunConfig) -> BatchRunResult:
        """Execute the configured batch.

        Args:
            config: Batch run configuration.

        Returns:
            Final batch summary.
        """

        manifest = config.manifest
        identity_manifest = config.identity_manifest
        output_dir = config.output_dir / manifest.batch_id
        validate_prepared_sandbox_allocations(manifest=manifest, policy=config.prepared_sandbox_policy)
        output_dir.mkdir(parents=True, exist_ok=True)
        with _BatchOutputLock(output_dir):
            manifest = freeze_batch_context_configs(manifest, batch_output_dir=output_dir)
            config = config.model_copy(update={"manifest": manifest})
            if identity_manifest is None:
                validate_existing_manifest(output_dir=output_dir, manifest=manifest)
            else:
                _validate_resume_identity_snapshot(
                    output_dir=output_dir,
                    identity_manifest=identity_manifest,
                    identity_manifest_sha256=config.identity_manifest_sha256,
                    runtime_manifest=manifest,
                )
            sqlite_state_path = output_dir / "batch_state.sqlite"
            validate_existing_run_config(
                output_dir=output_dir,
                config=config,
                sqlite_state_path=sqlite_state_path,
            )
            resume_config = resume_config_snapshot_for_write(
                output_dir=output_dir,
                config=config,
                sqlite_state_path=sqlite_state_path,
            )
            config = _config_with_persisted_resume_token(config=config, resume_config=resume_config)
            if identity_manifest is None:
                (output_dir / "manifest.json").write_text(manifest.model_dump_json(indent=2), encoding="utf-8")
            (output_dir / "run_config.json").write_text(resume_config.model_dump_json(indent=2), encoding="utf-8")
            _write_run_config_snapshots(config=config, resume_config=resume_config, batch_output_dir=output_dir)
            legacy_jsonl_path = output_dir / "batch_state.jsonl"
            migrate_legacy_jsonl = not sqlite_state_path.exists() and legacy_jsonl_path.exists()
            repository = SQLiteBatchStateRepository(sqlite_state_path)
            repository.initialize_manifest(
                manifest, legacy_jsonl_path=str(legacy_jsonl_path) if migrate_legacy_jsonl else ""
            )
        metrics = BatchMetrics()
        jsonl_writer = BackgroundJsonlWriter(name="northcua-batch-jsonl-writer")
        telemetry_sinks: list[TelemetrySink] = [BufferedLocalTelemetrySink(output_dir, writer=jsonl_writer)]
        extra_sink = self._telemetry_sink_factory(config, output_dir) if self._telemetry_sink_factory else None
        if extra_sink is not None:
            telemetry_sinks.append(extra_sink)
        telemetry_sink = CompositeTelemetrySink(telemetry_sinks)
        events = BatchEventWriter(
            repository=repository,
            output_dir=output_dir,
            batch_id=manifest.batch_id,
            telemetry_sink=telemetry_sink,
            experiment_id=config.telemetry_experiment_id or manifest.batch_id,
            jsonl_writer=jsonl_writer,
            owns_jsonl_writer=True,
        )
        try:
            async with RunnerEventLoopWatchdog(output_dir=output_dir, batch_id=manifest.batch_id):
                states = await self._scheduler.run(
                    config=config,
                    repository=repository,
                    metrics=metrics,
                    events=events,
                    output_dir=output_dir,
                )
            succeeded = sum(1 for state in states if state.status == BatchTaskStatus.SUCCEEDED)
            failed = sum(
                1
                for state in states
                if state.status
                in {BatchTaskStatus.FAILED, BatchTaskStatus.TIMED_OUT, BatchTaskStatus.EVALUATION_PENDING}
            )
            canceled = sum(1 for state in states if state.status == BatchTaskStatus.CANCELED)
            incomplete = sum(
                1
                for state in states
                if state.status
                not in {
                    BatchTaskStatus.SUCCEEDED,
                    BatchTaskStatus.EVALUATION_PENDING,
                    BatchTaskStatus.FAILED,
                    BatchTaskStatus.TIMED_OUT,
                    BatchTaskStatus.CANCELED,
                }
            )
            result = BatchRunResult(
                batch_id=manifest.batch_id,
                total=len(manifest.tasks),
                succeeded=succeeded,
                failed=failed,
                canceled=canceled,
                incomplete=incomplete,
                output_dir=str(output_dir),
                task_states=states,
            )
            events.flush()
            telemetry_sink.flush()
            BatchReportWriter(output_dir, state_repository=repository).write_summary(
                result,
                metrics.summary(completed_tasks=succeeded + failed + canceled),
            )
            return result
        finally:
            try:
                await self._watcher.aclose()
            finally:
                telemetry_sink.flush()
                events.flush()


def _validate_resume_identity_snapshot(
    *,
    output_dir: Path,
    identity_manifest: BatchManifest,
    identity_manifest_sha256: str,
    runtime_manifest: BatchManifest,
) -> None:
    """Validate an immutable manifest snapshot before applying runtime-only overrides."""

    manifest_path = output_dir / "manifest.json"
    sqlite_state_path = output_dir / "batch_state.sqlite"
    if not sqlite_state_path.exists() or not manifest_path.exists():
        raise RuntimeError("selected retry requires an existing SQLite batch and manifest snapshot")
    persisted_bytes = manifest_path.read_bytes()
    actual_sha256 = hashlib.sha256(persisted_bytes).hexdigest()
    if not identity_manifest_sha256 or actual_sha256 != identity_manifest_sha256:
        raise RuntimeError("persisted manifest changed after the selected retry plan was validated")
    persisted_manifest = BatchManifest.model_validate_json(persisted_bytes)
    if persisted_manifest != identity_manifest:
        raise RuntimeError("persisted manifest content differs from the selected retry identity snapshot")
    if runtime_manifest.batch_id != identity_manifest.batch_id:
        raise RuntimeError("selected retry runtime manifest changed the batch id")
    identity_task_ids = [task.task_id for task in identity_manifest.tasks]
    runtime_task_ids = [task.task_id for task in runtime_manifest.tasks]
    if runtime_task_ids != identity_task_ids:
        raise RuntimeError("selected retry runtime manifest changed task ids or ordering")


class _BatchOutputLock:
    """Process-local filesystem lock around manifest and SQLite initialization."""

    def __init__(self, output_dir: Path) -> None:
        self._path = output_dir / ".batch.lock"
        self._handle = None

    def __enter__(self) -> "_BatchOutputLock":
        if fcntl is None:
            raise RuntimeError("Batch/controller execution requires Linux; local agents support Windows.")
        self._handle = self._path.open("a+", encoding="utf-8")
        fcntl.flock(self._handle.fileno(), fcntl.LOCK_EX)
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        if self._handle is None:
            return
        fcntl.flock(self._handle.fileno(), fcntl.LOCK_UN)
        self._handle.close()
        self._handle = None


def _write_run_config_snapshots(
    *,
    config: BatchRunConfig,
    resume_config: BatchRunResumeConfig,
    batch_output_dir: Path,
) -> None:
    """Persist timestamped run config and redacted environment snapshots."""

    now = datetime.now(UTC)
    timestamp = now.strftime("%Y%m%dT%H%M%SZ")
    payload = {
        "schema_version": 1,
        "captured_at": now.isoformat(),
        "batch_id": config.manifest.batch_id,
        "argv": _redacted_argv(sys.argv),
        "cwd": os.getcwd(),
        "runtime_config": _redacted_runtime_config(resume_config),
        "environment": _safe_environment_snapshot(),
    }
    text = json.dumps(payload, indent=2, ensure_ascii=False, sort_keys=True)

    local_snapshot_dir = batch_output_dir / "run_config_snapshots"
    local_snapshot_dir.mkdir(parents=True, exist_ok=True)
    (local_snapshot_dir / f"run_config.{timestamp}.json").write_text(text, encoding="utf-8")
    (local_snapshot_dir / "run_config.latest.json").write_text(text, encoding="utf-8")

    external_snapshot_dir = batch_output_dir.parent.parent / "run_config_snapshots"
    external_snapshot_dir.mkdir(parents=True, exist_ok=True)
    batch_id = config.manifest.batch_id
    (external_snapshot_dir / f"{batch_id}.{timestamp}.json").write_text(text, encoding="utf-8")
    (external_snapshot_dir / f"{batch_id}.latest.json").write_text(text, encoding="utf-8")


def _config_with_persisted_resume_token(
    *,
    config: BatchRunConfig,
    resume_config: BatchRunResumeConfig,
) -> BatchRunConfig:
    """Use a persisted token for shared-runner resume when no token was supplied."""

    if config.token or not resume_config.token:
        return config
    return config.model_copy(update={"token": resume_config.token})


def _safe_environment_snapshot() -> dict[str, str]:
    prefixes = ("NORTHCUA_", "OPENAI_", "AZURE_", "ANTHROPIC_", "E2B_")
    exact_keys = {"PATH", "PYTHONPATH", "HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY"}
    redacted_terms = ("TOKEN", "KEY", "SECRET", "PASSWORD", "CREDENTIAL", "AUTH")
    snapshot: dict[str, str] = {}
    for key, value in os.environ.items():
        if key in exact_keys or key.startswith(prefixes):
            snapshot[key] = (
                "<redacted>"
                if key in {"HTTP_PROXY", "HTTPS_PROXY"} or any(term in key.upper() for term in redacted_terms)
                else value
            )
    return dict(sorted(snapshot.items()))


def _redacted_runtime_config(resume_config: BatchRunResumeConfig) -> dict[str, object]:
    payload = resume_config.model_dump(mode="json")
    token = str(payload.get("token") or "")
    if token:
        payload["token"] = "<redacted>"
        payload["token_fingerprint"] = payload.get("token_fingerprint") or _secret_fingerprint(token)
    return payload


def _redacted_argv(argv: list[str]) -> list[str]:
    redacted: list[str] = []
    redact_next = False
    secret_flags = ("token", "key", "secret", "password", "credential", "auth")
    for item in argv:
        flag_name = item.lstrip("-").split("=", 1)[0].replace("-", "_").upper()
        if redact_next:
            redacted.append("<redacted>")
            redact_next = False
            continue
        if any(term.upper() in flag_name for term in secret_flags):
            if "=" in item:
                redacted.append(item.split("=", 1)[0] + "=<redacted>")
            else:
                redacted.append(item)
                redact_next = True
            continue
        redacted.append(item)
    return redacted


def _secret_fingerprint(secret: str) -> str:
    if not secret:
        return ""
    return hashlib.sha256(f"northcua-config-secret-v1:{secret}".encode("utf-8")).hexdigest()
