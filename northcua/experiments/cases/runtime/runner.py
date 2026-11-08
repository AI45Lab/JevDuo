"""Batch-runner factory for case-suite execution."""

from __future__ import annotations

from northcua.batch.runner import BatchRunner
from northcua.experiments.cases.contracts import CaseSuiteSpec
from northcua.experiments.orchestration.batch_runner_factory import (
    BatchRunnerFactory,
    ControllerBackedBatchRunnerFactory,
)


class CaseBatchRunnerFactory:
    """Build batch runners with case-suite telemetry and readiness wiring."""

    def __init__(self, *, runner_factory: BatchRunnerFactory | None = None) -> None:
        self._factory = ControllerBackedBatchRunnerFactory(runner_factory=runner_factory)

    def build(self, spec: CaseSuiteSpec, *, dry_run: bool = False, has_real_provider: bool = False) -> BatchRunner:
        """Create a configured batch runner for a case suite.

        Args:
            spec: Case suite spec containing controller settings.
            dry_run: Whether the runner should avoid live provider side effects.
            has_real_provider: Whether any task uses a non-fake provider.

        Returns:
            Batch runner configured for the case suite.
        """

        return self._factory.build(
            controller=spec.controller,
            dry_run=dry_run,
            has_real_provider=has_real_provider,
        )
