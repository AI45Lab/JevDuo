"""Shared controller-backed batch runner construction."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from northcua.batch.contracts import BatchRunConfig
from northcua.batch.runner import BatchRunner
from northcua.experiments.contracts import ExperimentControllerSpec
from northcua.sandboxes.device.readiness import ControllerSQLiteDeviceReadinessProbe
from northcua.telemetry.sinks import TelemetrySink

BatchRunnerFactory = Callable[[], BatchRunner]


class ControllerBackedBatchRunnerFactory:
    """Build batch runners with controller SQLite telemetry/readiness wiring."""

    def __init__(self, *, runner_factory: BatchRunnerFactory | None = None) -> None:
        self._runner_factory = runner_factory

    def build(
        self,
        *,
        controller: ExperimentControllerSpec,
        dry_run: bool,
        has_real_provider: bool,
    ) -> BatchRunner:
        """Create a runner for batch execution under an optional controller.

        Args:
            controller: Controller settings used for readiness and telemetry.
            dry_run: Whether the runner should avoid external side effects.
            has_real_provider: Whether any task uses a non-fake provider.

        Returns:
            Configured batch runner.
        """

        if self._runner_factory is not None:
            return self._runner_factory()
        if dry_run:
            return BatchRunner()
        telemetry_sink_factory = self._telemetry_sink_factory(controller)
        if controller.managed_local and controller.db_path is not None and has_real_provider:
            return BatchRunner(
                readiness_probe=ControllerSQLiteDeviceReadinessProbe(controller.db_path),
                telemetry_sink_factory=telemetry_sink_factory,
            )
        return BatchRunner(telemetry_sink_factory=telemetry_sink_factory)

    @staticmethod
    def _telemetry_sink_factory(
        controller: ExperimentControllerSpec,
    ) -> Callable[[BatchRunConfig, Path], TelemetrySink | None] | None:
        if controller.db_path is None:
            return None
        from northcua.controller.observability import ControllerTelemetrySink

        def _factory(config: BatchRunConfig, output_dir: Path) -> TelemetrySink | None:
            _ = output_dir
            if config.telemetry_controller_db_path is None:
                return None
            return ControllerTelemetrySink(config.telemetry_controller_db_path)

        return _factory
