"""Runtime bundle and batch-runner helpers for experiment operations."""

from __future__ import annotations

from typing import TYPE_CHECKING

from northcua.batch.runner import BatchRunner
from northcua.experiments.contracts import ExperimentRuntimeMode, ExperimentSpec
from northcua.experiments.materialization import MaterializedExperiment, MaterializedTask, RuntimeWheelBundle
from northcua.experiments.orchestration.batch_runner_factory import ControllerBackedBatchRunnerFactory

if TYPE_CHECKING:
    from northcua.experiments.materialization import RuntimeWheelBundleBuilder
    from northcua.experiments.orchestration.batch_runner_factory import BatchRunnerFactory


class ExperimentOperationsBatchRuntimeMixin:
    """Attach runtime bundles and build configured batch runners."""

    _runtime_bundle_builder: "RuntimeWheelBundleBuilder"
    _batch_runner_factory: "BatchRunnerFactory | None"

    def _with_runtime_bundle(
        self,
        materialized: MaterializedExperiment,
        *,
        dry_run: bool,
    ) -> MaterializedExperiment:
        """Attach local device-worker wheels to real-provider experiments."""

        spec = materialized.spec
        if dry_run or spec.sandbox.provider == "fake":
            return materialized
        if spec.runtime.mode == ExperimentRuntimeMode.BAKED_IN:
            return materialized
        bundle = self._runtime_bundle_builder.build(
            output_dir=materialized.output_dir,
            python_executable=spec.sandbox.device_python,
        )
        return self._attach_runtime_bundle(materialized, bundle)

    @staticmethod
    def _attach_runtime_bundle(
        materialized: MaterializedExperiment,
        bundle: RuntimeWheelBundle,
    ) -> MaterializedExperiment:
        sandbox = materialized.spec.sandbox.model_copy(
            update={"file_uploads": [*materialized.spec.sandbox.file_uploads, *bundle.uploads]}
        )
        spec = materialized.spec.model_copy(update={"sandbox": sandbox})
        tasks = tuple(
            MaterializedTask(
                task=item.task.model_copy(
                    update={"startup_commands": [*bundle.startup_commands, *item.task.startup_commands]}
                ),
                uploads=item.uploads,
            )
            for item in materialized.tasks
        )
        return MaterializedExperiment(
            spec=spec,
            output_dir=materialized.output_dir,
            fixture_dir=materialized.fixture_dir,
            tasks=tasks,
        )

    def _build_batch_runner(self, spec: ExperimentSpec) -> BatchRunner:
        return ControllerBackedBatchRunnerFactory(runner_factory=self._batch_runner_factory).build(
            controller=spec.controller,
            dry_run=False,
            has_real_provider=spec.sandbox.provider != "fake",
        )
