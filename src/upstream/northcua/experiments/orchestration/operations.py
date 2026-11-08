"""Product-facing experiment operations used by CLI and tests."""

from __future__ import annotations

import json
import os
from collections.abc import Callable
from pathlib import Path

from northcua.batch.contracts import BatchManifest, BatchRunConfig, BatchRunResult, BatchTaskState, BatchTaskStatus
from northcua.batch.runner import BatchRunner
from northcua.batch.state import SQLiteBatchStateRepository
from northcua.controller.storage import SQLiteControllerStore
from northcua.experiments.contracts import (
    ExperimentRunConfig,
    ExperimentRunResult,
    ExperimentSpec,
    ExperimentTaskResult,
)
from northcua.experiments.materialization import (
    ExperimentMaterializer,
    MaterializedExperiment,
    MaterializedTask,
    RuntimeWheelBundle,
    RuntimeWheelBundleBuilder,
    load_experiment_spec,
)
from northcua.experiments.orchestration.controller import ManagedControllerProcess
from northcua.experiments.orchestration.operations_runtime import ExperimentOperationsRuntimeMixin
from northcua.experiments.orchestration.preflight import ExperimentPreflight
from northcua.experiments.planner import ExperimentBatchPlanner
from northcua.experiments.recovery import ExperimentRecoveryPlanner, RecoveryPlan
from northcua.experiments.reporting import ExperimentReportWriter
from northcua.experiments.verification import ExperimentTraceVerifier, FakeTraceSeeder
from northcua.reliability.contracts import FailureAction
from northcua.sandboxes.contracts import SandboxAllocationStrategy
from northcua.sandboxes.device.readiness import ControllerSQLiteDeviceReadinessProbe
from northcua.telemetry.sinks import TelemetrySink

BatchRunnerFactory = Callable[[], BatchRunner]


class ExperimentOperations(ExperimentOperationsRuntimeMixin):
    """High-level experiment suite operations independent from argparse."""

    def __init__(
        self,
        *,
        batch_runner_factory: BatchRunnerFactory | None = None,
        controller_process: ManagedControllerProcess | None = None,
        materializer: ExperimentMaterializer | None = None,
        runtime_bundle_builder: RuntimeWheelBundleBuilder | None = None,
        planner: ExperimentBatchPlanner | None = None,
        verifier: ExperimentTraceVerifier | None = None,
        recovery_planner: ExperimentRecoveryPlanner | None = None,
    ) -> None:
        self._batch_runner_factory = batch_runner_factory
        self._controller_process = controller_process or ManagedControllerProcess()
        self._materializer = materializer or ExperimentMaterializer()
        self._runtime_bundle_builder = runtime_bundle_builder or RuntimeWheelBundleBuilder()
        self._planner = planner or ExperimentBatchPlanner()
        self._verifier = verifier or ExperimentTraceVerifier()
        self._recovery_planner = recovery_planner or ExperimentRecoveryPlanner()
        self._preflight = ExperimentPreflight()

    def validate(self, spec_path: str | Path) -> ExperimentSpec:
        """Load and validate an experiment spec.

        Args:
            spec_path: YAML or JSON experiment spec path.

        Returns:
            Validated experiment spec.
        """

        return load_experiment_spec(spec_path)

    async def run_file(
        self,
        *,
        spec_path: str | Path,
        output_dir: Path,
        public_controller_url: str = "",
        dry_run: bool = False,
        require_real_e2b: bool = True,
    ) -> ExperimentRunResult:
        """Run an experiment spec loaded from disk.

        Args:
            spec_path: YAML or JSON experiment spec path.
            output_dir: Root directory for experiment outputs.
            public_controller_url: Optional public controller URL override.
            dry_run: Whether to project the batch without external side effects.
            require_real_e2b: Whether to enforce the live E2B environment gate.

        Returns:
            Experiment run summary with report paths.
        """

        resolved = Path(spec_path).expanduser().resolve()
        spec = load_experiment_spec(resolved)
        return await self.run(
            ExperimentRunConfig(
                spec=spec,
                output_dir=output_dir,
                public_controller_url=public_controller_url,
                dry_run=dry_run,
                require_real_e2b=require_real_e2b and not dry_run,
            ),
            spec_dir=resolved.parent,
        )

    async def run(self, config: ExperimentRunConfig, *, spec_dir: Path | None = None) -> ExperimentRunResult:
        """Run a validated experiment spec.

        Args:
            config: Runtime experiment configuration.
            spec_dir: Directory for resolving source fixtures from file-backed
                specs.

        Returns:
            Experiment run summary with verification rows.
        """

        spec = self._effective_spec(config)
        experiment_dir = config.output_dir / spec.experiment_id
        experiment_dir.mkdir(parents=True, exist_ok=True)
        spec = self._with_managed_controller_paths(spec, experiment_dir=experiment_dir)
        spec = self._with_effective_controller_token(spec)
        controller_url = config.public_controller_url or spec.controller.public_url
        self._preflight.validate_public_controller_url(spec, public_url=controller_url)
        self._preflight.validate_live_gate(spec, require_real_e2b=config.require_real_e2b and not config.dry_run)
        self._preflight.validate_controller_auth(spec)
        submitter_controller_url = (
            f"ws://{spec.controller.local_host}:{spec.controller.local_port}"
            if spec.controller.managed_local
            else controller_url
        )
        managed_controller = (
            None
            if config.dry_run or spec.sandbox.provider == "fake"
            else await self._controller_process.start(spec.controller, output_dir=experiment_dir)
        )
        try:
            materialized = self._materializer.materialize(spec, output_dir=experiment_dir, spec_dir=spec_dir)
            materialized = self._with_runtime_bundle(materialized, dry_run=config.dry_run)
            spec = materialized.spec
            manifest = self._planner.plan(materialized, controller_url=controller_url)
            (experiment_dir / "experiment_spec.json").write_text(
                self._spec_for_persistence(spec).model_dump_json(indent=2),
                encoding="utf-8",
            )
            (experiment_dir / "batch_manifest.json").write_text(manifest.model_dump_json(indent=2), encoding="utf-8")
            batch_result = await self._build_batch_runner(spec).run(
                BatchRunConfig(
                    manifest=manifest,
                    output_dir=experiment_dir / "batch",
                    controller_url=submitter_controller_url,
                    device_controller_url=controller_url,
                    token=spec.controller.token,
                    concurrency=spec.concurrency,
                    dry_run=config.dry_run,
                    persist_token=spec.sandbox.provider == "fake" and not spec.controller.token,
                    telemetry_experiment_id=spec.experiment_id,
                    telemetry_controller_db_path=spec.controller.db_path,
                )
            )
            if spec.sandbox.provider == "fake" and not config.dry_run:
                FakeTraceSeeder().seed(output_dir=experiment_dir, spec=spec, batch_result=batch_result)
            extra_roots = [
                path
                for path in (
                    spec.controller.artifact_root_dir,
                    spec.controller.blob_dir,
                    spec.controller.db_path.parent if spec.controller.db_path else None,
                )
                if path is not None
            ]
            task_results = self._verifier.verify(
                output_dir=experiment_dir,
                spec=spec,
                batch_result=batch_result,
                extra_roots=extra_roots,
            )
            return ExperimentReportWriter(experiment_dir).write(
                spec=spec,
                batch_result=batch_result,
                task_results=task_results,
            )
        finally:
            if managed_controller is not None:
                await managed_controller.stop()
