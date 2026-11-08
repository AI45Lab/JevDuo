"""Desktop adapter from persisted step logs to AgentKit evaluator bundles."""

from __future__ import annotations

from northcua.core.config import EvaluationBundleConfig
from northcua.core.models import StepLogRecord
from northcua.runtime.artifacts.store import NorthCUAArtifactStore

from .bundle import BundleBuilder
from .protocol import RoundEvaluationBundle


class DesktopRoundEvidenceAdapter:
    """Adapt desktop `StepLogRecord` values into a generic round bundle."""

    def __init__(
        self,
        *,
        artifact_store: NorthCUAArtifactStore,
        config: EvaluationBundleConfig,
    ) -> None:
        self.bundle_builder = BundleBuilder(artifact_store=artifact_store, config=config)

    def build_round_bundle(
        self,
        *,
        round_num: int,
        round_request: str,
        step_logs: list[StepLogRecord],
    ) -> RoundEvaluationBundle:
        """Build the generic AgentKit evaluator bundle for one desktop round.

        Args:
            round_num: One-based round number to evaluate.
            round_request: User request for the round.
            step_logs: Persisted desktop step records.

        Returns:
            Generic round evaluation bundle.
        """

        return self.bundle_builder.build(
            round_num=round_num,
            round_request=round_request,
            step_logs=step_logs,
        )


__all__ = ["DesktopRoundEvidenceAdapter"]
