"""Projection from experiment suites to batch manifests."""

from __future__ import annotations

from northcua.batch.contracts import BatchDefaults, BatchManifest, BatchTaskSpec
from northcua.batch.runner.bindings import SandboxDeviceBindingFactory
from northcua.experiments.materialization import MaterializedExperiment
from northcua.experiments.planner.evaluator_capabilities import EvaluatorCapabilityPlanner


def _metadata_string(metadata: dict[str, object], key: str) -> str:
    value = metadata.get(key)
    if isinstance(value, str):
        return value.strip()
    return ""


class ExperimentBatchPlanner:
    """Convert high-level experiment intent into the existing batch runner IR."""

    def __init__(
        self,
        *,
        binding_factory: SandboxDeviceBindingFactory | None = None,
        evaluator_capabilities: EvaluatorCapabilityPlanner | None = None,
    ) -> None:
        self._binding_factory = binding_factory or SandboxDeviceBindingFactory()
        self._evaluator_capabilities = evaluator_capabilities or EvaluatorCapabilityPlanner()

    def plan(self, experiment: MaterializedExperiment, *, controller_url: str) -> BatchManifest:
        """Build a batch manifest for one materialized experiment.

        Args:
            experiment: Experiment with generated fixture uploads.
            controller_url: Public controller websocket URL used by sandbox
                device workers and submitters.

        Returns:
            Batch manifest ready for ``BatchRunner``.
        """

        spec = experiment.spec
        defaults = BatchDefaults(
            controller_url=controller_url,
            device_controller_url=controller_url,
            concurrency=spec.concurrency,
            sandbox=spec.sandbox,
        )
        tasks: list[BatchTaskSpec] = []
        evaluator_capabilities = self._evaluator_capabilities.from_app_config_path(spec.controller.app_config_path)
        controller_metadata = {
            "controller_app_config_path": str(spec.controller.app_config_path),
            **self._binding_factory.desktop_coordinate_metadata(
                {"controller_app_config_path": str(spec.controller.app_config_path)}
            ),
        }
        for materialized in experiment.tasks:
            task = materialized.task
            sandbox = spec.sandbox.model_copy(
                update={
                    "file_uploads": [*spec.sandbox.file_uploads, *materialized.uploads],
                    "metadata": {
                        **spec.sandbox.metadata,
                        "northcua": "true",
                        "experiment_id": spec.experiment_id,
                        "batch_id": spec.experiment_id,
                        "task_id": task.task_id,
                        "app_profile": task.app_profile.value,
                    },
                }
            )
            tasks.append(
                BatchTaskSpec(
                    task_id=task.task_id,
                    agent_kind=task.agent_kind,
                    query=task.query,
                    url=_metadata_string(task.metadata, "url") or _metadata_string(task.metadata, "web_url"),
                    max_steps=task.max_steps,
                    max_session_steps=task.max_session_steps,
                    metadata={
                        **task.metadata,
                        **controller_metadata,
                        "experiment_id": spec.experiment_id,
                        "app_profile": task.app_profile.value,
                    },
                    required_capabilities=(
                        self._binding_factory.default_capabilities(task.agent_kind)
                        | set(task.required_capabilities)
                        | evaluator_capabilities
                    ),
                    sandbox=sandbox,
                    startup_commands=task.startup_commands,
                    timeout_seconds=task.timeout_seconds,
                    max_retries=task.max_retries,
                )
            )
        return BatchManifest(batch_id=spec.experiment_id, defaults=defaults, tasks=tasks)
