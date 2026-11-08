"""Runtime, status, and recovery helpers for experiment operations."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path

from northcua.batch.contracts import (
    BatchManifest,
    BatchRunConfig,
    BatchRunResult,
    BatchRunResumeConfig,
    BatchTaskState,
    BatchTaskStatus,
)
from northcua.batch.preparation import PreparedSandboxPolicy
from northcua.batch.runner.validation import validate_existing_run_config
from northcua.batch.state import SQLiteBatchStateRepository
from northcua.experiments.contracts import (
    ExperimentRunConfig,
    ExperimentRunResult,
    ExperimentSpec,
    ExperimentTaskResult,
)
from northcua.experiments.orchestration.operations_batch_runtime import ExperimentOperationsBatchRuntimeMixin
from northcua.experiments.orchestration.operations_status import ExperimentOperationsStatusMixin
from northcua.experiments.recovery import RecoveryPlan
from northcua.experiments.reporting import ExperimentReportWriter
from northcua.experiments.verification import FakeTraceSeeder
from northcua.sandboxes.contracts import SandboxAllocationStrategy
from northcua.sandboxes.device.readiness import ControllerSQLiteDeviceReadinessProbe


@dataclass(frozen=True)
class _PersistedExperimentRun:
    """Files and state required to resume or inspect one experiment."""

    experiment_dir: Path
    batch_dir: Path
    sqlite_path: Path
    spec: ExperimentSpec
    manifest: BatchManifest
    repository: SQLiteBatchStateRepository
    states: list[BatchTaskState]


class ExperimentOperationsRuntimeMixin(ExperimentOperationsStatusMixin, ExperimentOperationsBatchRuntimeMixin):
    """Status, recovery, and runtime helpers for ``ExperimentOperations``."""

    def recover_plan(self, *, experiment_id: str, output_dir: Path) -> RecoveryPlan:
        """Build a dry-run recovery plan for a persisted experiment.

        Args:
            experiment_id: Experiment output namespace.
            output_dir: Root directory containing experiment outputs.

        Returns:
            Typed recovery plan with per-case actions.

        Raises:
            FileNotFoundError: If the persisted experiment or batch manifest is
                missing.
        """

        persisted = self._load_persisted_run(experiment_id=experiment_id, output_dir=output_dir)
        verification = self._verify_existing(persisted)
        return self._recovery_planner.plan(
            spec=persisted.spec,
            manifest=persisted.manifest,
            output_dir=persisted.experiment_dir,
            states=persisted.states,
            verification=verification,
        )

    async def resume(
        self,
        *,
        experiment_id: str,
        output_dir: Path,
        concurrency: int = 0,
        sandbox_create_concurrency: int = 0,
        controller_submit_concurrency: int = 0,
        controller_status_concurrency: int = 0,
        controller_submit_jitter_seconds: float = 0.0,
        device_readiness_timeout_seconds: float = 0.0,
        infra_retry_backoff_seconds: list[float] | None = None,
        prepared_sandbox_policy: PreparedSandboxPolicy | None = None,
        public_controller_url: str = "",
    ) -> ExperimentRunResult:
        """Resume an interrupted experiment using local SQLite state.

        Args:
            experiment_id: Experiment output namespace.
            output_dir: Root directory containing experiment outputs.
            concurrency: Optional resume-time concurrency override.
            sandbox_create_concurrency: Optional resume-time sandbox create cap.
            controller_submit_concurrency: Optional resume-time controller
                submit handshake cap.
            controller_status_concurrency: Optional resume-time controller
                status request cap.
            controller_submit_jitter_seconds: Optional per-task submit jitter.
            device_readiness_timeout_seconds: Optional device readiness timeout.
            infra_retry_backoff_seconds: Optional retry backoff sequence for
                retryable infra failures.
            prepared_sandbox_policy: Optional resume-time prepared queue policy.
            public_controller_url: Optional public controller URL override.

        Returns:
            Final experiment result after skipped, retried, or verified cases.

        Raises:
            FileNotFoundError: If persisted experiment inputs are missing.
        """

        persisted = self._load_persisted_run(experiment_id=experiment_id, output_dir=output_dir)
        spec = persisted.spec
        if public_controller_url:
            spec = spec.model_copy(
                update={"controller": spec.controller.model_copy(update={"public_url": public_controller_url})}
            )
        spec = self._with_effective_controller_token(spec)
        plan = self.recover_plan(experiment_id=experiment_id, output_dir=output_dir)
        (persisted.experiment_dir / "recovery_plan.json").write_text(
            plan.model_dump_json(indent=2),
            encoding="utf-8",
        )
        self._stamp_recovery_plan(persisted.repository, plan)
        controller_url = public_controller_url or spec.controller.public_url
        submitter_controller_url = (
            f"ws://{spec.controller.local_host}:{spec.controller.local_port}"
            if spec.controller.managed_local
            else controller_url
        )
        existing_resume_config = self._load_batch_resume_config(persisted.batch_dir)
        resume_config = existing_resume_config.merge_overrides(
            controller_url=submitter_controller_url if public_controller_url else "",
            device_controller_url=controller_url if public_controller_url else "",
            token=spec.controller.token,
            concurrency=concurrency,
            sandbox_create_concurrency=sandbox_create_concurrency,
            controller_submit_concurrency=controller_submit_concurrency,
            controller_status_concurrency=controller_status_concurrency,
            controller_submit_jitter_seconds=controller_submit_jitter_seconds,
            device_readiness_timeout_seconds=device_readiness_timeout_seconds,
            infra_retry_backoff_seconds=infra_retry_backoff_seconds,
            telemetry_controller_db_path=existing_resume_config.telemetry_controller_db_path or spec.controller.db_path,
            prepared_sandbox_policy=prepared_sandbox_policy,
        )
        resume_config = resume_config.model_copy(
            update={"telemetry_experiment_id": resume_config.telemetry_experiment_id or spec.experiment_id}
        )
        batch_config = BatchRunConfig(
            manifest=persisted.manifest,
            output_dir=persisted.experiment_dir / "batch",
            controller_url=resume_config.controller_url or submitter_controller_url,
            device_controller_url=resume_config.device_controller_url or controller_url,
            token=resume_config.token or spec.controller.token,
            concurrency=resume_config.concurrency or spec.concurrency,
            sandbox_create_concurrency=resume_config.sandbox_create_concurrency,
            controller_submit_concurrency=resume_config.controller_submit_concurrency,
            controller_status_concurrency=resume_config.controller_status_concurrency,
            controller_submit_jitter_seconds=resume_config.controller_submit_jitter_seconds,
            device_readiness_timeout_seconds=resume_config.device_readiness_timeout_seconds,
            infra_retry_backoff_seconds=list(resume_config.infra_retry_backoff_seconds),
            prepared_sandbox_policy=resume_config.prepared_sandbox_policy,
            persist_token=spec.sandbox.provider == "fake" and not spec.controller.token,
            telemetry_experiment_id=resume_config.telemetry_experiment_id,
            telemetry_controller_db_path=resume_config.telemetry_controller_db_path,
        )
        validate_existing_run_config(
            output_dir=persisted.batch_dir,
            config=batch_config,
            sqlite_state_path=persisted.sqlite_path,
        )
        if plan.runnable_count:
            persisted.repository.set_control_flag("paused", False)
            persisted.repository.set_control_flag("drain_requested", False)
        managed_controller = (
            None
            if not plan.runnable_count or spec.sandbox.provider == "fake" or not spec.controller.managed_local
            else await self._controller_process.start(spec.controller, output_dir=persisted.experiment_dir)
        )
        try:
            batch_result = await self._build_batch_runner(spec).run(batch_config)
            if spec.sandbox.provider == "fake":
                FakeTraceSeeder().seed(output_dir=persisted.experiment_dir, spec=spec, batch_result=batch_result)
            task_results = self._verifier.verify(
                output_dir=persisted.experiment_dir,
                spec=spec,
                batch_result=batch_result,
                extra_roots=self._extra_artifact_roots(spec),
            )
            return ExperimentReportWriter(persisted.experiment_dir).write(
                spec=spec,
                batch_result=batch_result,
                task_results=task_results,
            )
        finally:
            if managed_controller is not None:
                await managed_controller.stop()

    def pause(self, *, experiment_id: str, output_dir: Path) -> Path:
        """Pause new case claims for an experiment.

        Args:
            experiment_id: Experiment output namespace.
            output_dir: Root directory containing experiment outputs.

        Returns:
            Path to the SQLite coordination store that was updated.
        """

        persisted = self._load_persisted_run(experiment_id=experiment_id, output_dir=output_dir)
        persisted.repository.set_control_flag("paused", True)
        return persisted.sqlite_path

    def drain(self, *, experiment_id: str, output_dir: Path) -> Path:
        """Drain an experiment by stopping new claims while running cases finish.

        Args:
            experiment_id: Experiment output namespace.
            output_dir: Root directory containing experiment outputs.

        Returns:
            Path to the SQLite coordination store that was updated.
        """

        persisted = self._load_persisted_run(experiment_id=experiment_id, output_dir=output_dir)
        persisted.repository.set_control_flag("drain_requested", True)
        return persisted.sqlite_path

    def cancel(self, *, experiment_id: str, output_dir: Path) -> Path:
        """Request cooperative cancellation for an experiment's batch.

        Args:
            experiment_id: Experiment output namespace.
            output_dir: Root directory containing experiment outputs.

        Returns:
            Path to the cancellation marker written beside batch state.
        """

        persisted = self._load_persisted_run(experiment_id=experiment_id, output_dir=output_dir)
        cancel_path = persisted.batch_dir / "cancel.requested"
        cancel_path.write_text("cancel requested\n", encoding="utf-8")
        persisted.repository.mark_cancel_requested()
        return cancel_path

    def _load_persisted_run(self, *, experiment_id: str, output_dir: Path) -> _PersistedExperimentRun:
        experiment_dir = output_dir / experiment_id
        spec_path = experiment_dir / "experiment_spec.json"
        manifest_path = experiment_dir / "batch_manifest.json"
        batch_dir = experiment_dir / "batch" / experiment_id
        sqlite_path = batch_dir / "batch_state.sqlite"
        if not spec_path.exists():
            raise FileNotFoundError(f"experiment spec not found: {spec_path}")
        if not manifest_path.exists():
            raise FileNotFoundError(f"batch manifest not found: {manifest_path}")
        if not sqlite_path.exists():
            raise FileNotFoundError(f"batch SQLite state not found: {sqlite_path}")
        repository = SQLiteBatchStateRepository(sqlite_path)
        spec = ExperimentSpec.model_validate_json(spec_path.read_text(encoding="utf-8"))
        manifest = BatchManifest.model_validate_json(manifest_path.read_text(encoding="utf-8"))
        states = repository.all()
        return _PersistedExperimentRun(
            experiment_dir=experiment_dir,
            batch_dir=batch_dir,
            sqlite_path=sqlite_path,
            spec=spec,
            manifest=manifest,
            repository=repository,
            states=states,
        )

    @staticmethod
    def _load_batch_resume_config(batch_dir: Path) -> BatchRunResumeConfig:
        path = batch_dir / "run_config.json"
        if not path.exists():
            return BatchRunResumeConfig()
        return BatchRunResumeConfig.model_validate_json(path.read_text(encoding="utf-8"))

    def _verify_existing(self, persisted: _PersistedExperimentRun) -> list[ExperimentTaskResult]:
        batch_result = self._batch_result_from_states(
            persisted.manifest.batch_id, persisted.batch_dir, persisted.states
        )
        return self._verifier.verify(
            output_dir=persisted.experiment_dir,
            spec=persisted.spec,
            batch_result=batch_result,
            extra_roots=self._extra_artifact_roots(persisted.spec),
            materialize_controller_artifacts=False,
        )

    @staticmethod
    def _batch_result_from_states(batch_id: str, batch_dir: Path, states: list[BatchTaskState]) -> BatchRunResult:
        succeeded = sum(1 for state in states if state.status == BatchTaskStatus.SUCCEEDED)
        failed = sum(
            1
            for state in states
            if state.status in {BatchTaskStatus.FAILED, BatchTaskStatus.TIMED_OUT, BatchTaskStatus.EVALUATION_PENDING}
        )
        canceled = sum(1 for state in states if state.status == BatchTaskStatus.CANCELED)
        terminal_statuses = {
            BatchTaskStatus.SUCCEEDED,
            BatchTaskStatus.EVALUATION_PENDING,
            BatchTaskStatus.FAILED,
            BatchTaskStatus.TIMED_OUT,
            BatchTaskStatus.CANCELED,
        }
        return BatchRunResult(
            batch_id=batch_id,
            total=len(states),
            succeeded=succeeded,
            failed=failed,
            canceled=canceled,
            incomplete=sum(1 for state in states if state.status not in terminal_statuses),
            output_dir=str(batch_dir),
            task_states=states,
        )

    @staticmethod
    def _extra_artifact_roots(spec: ExperimentSpec) -> list[Path]:
        return [
            path
            for path in (
                spec.controller.artifact_root_dir,
                spec.controller.blob_dir,
                spec.controller.db_path.parent if spec.controller.db_path else None,
            )
            if path is not None
        ]

    @staticmethod
    def _stamp_recovery_plan(repository: SQLiteBatchStateRepository, plan: RecoveryPlan) -> None:
        for item in plan.items:
            state = repository.get(item.task_id)
            if state is None:
                continue
            repository.upsert_state(
                state.model_copy(
                    update={
                        "recovery_action": item.action,
                        "last_failure_category": item.failure_category,
                        "retry_budget_remaining": item.retry_budget_remaining,
                    }
                )
            )

    @staticmethod
    def _effective_spec(config: ExperimentRunConfig) -> ExperimentSpec:
        if not config.public_controller_url:
            return config.spec
        controller = config.spec.controller.model_copy(update={"public_url": config.public_controller_url})
        return config.spec.model_copy(update={"controller": controller})

    @staticmethod
    def _with_managed_controller_paths(spec: ExperimentSpec, *, experiment_dir: Path) -> ExperimentSpec:
        if not spec.controller.managed_local:
            return spec
        controller = spec.controller.model_copy(
            update={
                "db_path": spec.controller.db_path or experiment_dir / "controller.sqlite",
                "blob_dir": spec.controller.blob_dir or experiment_dir / "controller-blobs",
                "artifact_root_dir": spec.controller.artifact_root_dir or experiment_dir / "agent-artifacts",
            }
        )
        return spec.model_copy(update={"controller": controller})

    @staticmethod
    def _with_effective_controller_token(spec: ExperimentSpec) -> ExperimentSpec:
        if spec.controller.token:
            return spec
        token = os.getenv("NORTHCUA_CONTROLLER_TOKEN", "")
        if not token:
            return spec
        return spec.model_copy(update={"controller": spec.controller.model_copy(update={"token": token})})

    @staticmethod
    def _spec_for_persistence(spec: ExperimentSpec) -> ExperimentSpec:
        """Return a report-safe copy of an experiment spec.

        The runtime spec may contain a controller token resolved from a local
        secret environment variable. Reports still need the effective paths and
        public URL for debugging, but must never persist the shared controller
        credential into experiment artifacts.
        """

        if not spec.controller.token:
            return spec
        controller = spec.controller.model_copy(update={"token": ""})
        return spec.model_copy(update={"controller": controller})
