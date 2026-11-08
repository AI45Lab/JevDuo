"""Offline artifact-based desktop evaluator entrypoints."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from northcua.agentkit.evaluator.routing import UnavailableEvaluatorClientToolDispatcher
from northcua.agentkit.evaluator.runtime import ArtifactEvaluationRequest, ArtifactEvaluationRunnerBase
from northcua.core.enums import EvaluationSchedule
from northcua.core.models import StepLogRecord

from .events import DesktopEvaluationEventCallback
from .protocol import RoundEvaluationRecord


@dataclass(frozen=True, slots=True)
class OfflineEvaluationRequest:
    """Configuration for evaluating a persisted desktop artifact directory.

    Attributes:
        app_config_path: NorthCUA app config used for evaluator LLM/tools.
        artifact_dir: Session artifact directory containing ``steps.jsonl``.
        round_num: Round to evaluate. ``0`` selects the latest round in the log.
        write_session_rollup: Whether to update ``session_evaluation.json``.
        force_enable: Whether a manual CLI replay should enable evaluation even
            when the app config has it disabled.
    """

    app_config_path: Path
    artifact_dir: Path
    round_num: int = 0
    write_session_rollup: bool = True
    force_enable: bool = True

    def to_agentkit_request(self) -> ArtifactEvaluationRequest:
        """Project this desktop request onto the reusable AgentKit contract.

        Returns:
            Generic AgentKit artifact replay request.
        """

        return ArtifactEvaluationRequest(
            app_config_path=self.app_config_path,
            artifact_dir=self.artifact_dir,
            unit_id=self.round_num,
            write_rollup=self.write_session_rollup,
            force_enable=self.force_enable,
        )


@dataclass(frozen=True, slots=True)
class OfflineEvaluationResult:
    """Result from replaying evaluator over existing artifacts.

    Attributes:
        record: Round evaluation record, or ``None`` when skipped.
        artifact_dir: Resolved source session artifact directory.
        round_num: Evaluated round number.
        step_count: Number of source steps used as input.
        final_report_path: Structured final report path.
        legacy_result_path: Compatibility per-round result path.
        round_log_path: Compatibility round JSONL path.
        session_rollup_path: Session rollup output path.
        structured_round_dir: Structured evaluator round artifact directory.
    """

    record: RoundEvaluationRecord | None
    artifact_dir: Path
    round_num: int
    step_count: int
    final_report_path: Path
    legacy_result_path: Path
    round_log_path: Path
    session_rollup_path: Path
    structured_round_dir: Path


@dataclass(frozen=True, slots=True)
class DesktopRoundArtifactInput:
    """Selected desktop round input for artifact replay.

    Attributes:
        round_num: Selected round number.
        round_request: User request associated with the selected round.
        step_logs: Step records belonging to the selected round.
    """

    round_num: int
    round_request: str
    step_logs: list[StepLogRecord]


class DesktopArtifactEvaluationRunner(
    ArtifactEvaluationRunnerBase[StepLogRecord, DesktopRoundArtifactInput, OfflineEvaluationResult]
):
    """Run the desktop evaluator directly from a persisted artifact folder."""

    primary_log_name = "steps.jsonl"

    def __init__(self, *, event_callback: DesktopEvaluationEventCallback | None = None) -> None:
        self._event_callback = event_callback

    async def run(self, request: OfflineEvaluationRequest | ArtifactEvaluationRequest) -> OfflineEvaluationResult:
        """Evaluate a stored round without rerunning the main desktop agent.

        Args:
            request: Offline evaluator replay request.

        Returns:
            Paths and persisted round record from the evaluator run.

        Raises:
            FileNotFoundError: If the artifact folder does not contain steps.
            ValueError: If no requested round exists in the step log.
        """

        agentkit_request = request.to_agentkit_request() if isinstance(request, OfflineEvaluationRequest) else request
        return await super().run(agentkit_request)

    def load_records(self, session_dir: Path) -> list[StepLogRecord]:
        """Load desktop step records from a session artifact directory.

        Args:
            session_dir: Resolved session artifact directory.

        Returns:
            Parsed desktop step records from ``steps.jsonl``.
        """

        return self._load_steps(session_dir / self.primary_log_name)

    def select_input(
        self,
        *,
        records: list[StepLogRecord],
        requested_unit_id: int,
    ) -> DesktopRoundArtifactInput:
        """Select a desktop round from loaded step records.

        Args:
            records: All loaded desktop step records.
            requested_unit_id: Requested round number, where ``0`` selects latest.

        Returns:
            Selected round input for evaluation.

        Raises:
            ValueError: If the requested round has no steps.
        """

        round_num = self._select_round_num(steps=records, requested_round=requested_unit_id)
        round_steps = [step for step in records if step.round_num == round_num]
        if not round_steps:
            raise ValueError(f"Artifact folder has no steps for round {round_num}.")
        return DesktopRoundArtifactInput(
            round_num=round_num,
            round_request=round_steps[0].request,
            step_logs=round_steps,
        )

    async def evaluate_selected(
        self,
        *,
        request: ArtifactEvaluationRequest,
        session_dir: Path,
        records: list[StepLogRecord],
        selected: DesktopRoundArtifactInput,
    ) -> OfflineEvaluationResult:
        """Evaluate the selected desktop round through RoundEvaluatorService.

        Args:
            request: Generic AgentKit artifact replay request.
            session_dir: Resolved session artifact directory.
            records: All loaded step records.
            selected: Selected round input.

        Returns:
            Persisted evaluation result and output paths.
        """

        del records
        from northcua.agents.desktop.application import NorthCUAApplication

        app = NorthCUAApplication.from_app_config(
            str(request.app_config_path),
            session_id=session_dir.name,
            artifact_root_dir_override=str(session_dir.parent),
        )
        try:
            if request.force_enable:
                app.app_config.evaluation.enabled = True
                if app.app_config.evaluation.schedule == EvaluationSchedule.DISABLED:
                    app.app_config.evaluation.schedule = EvaluationSchedule.AFTER_ROUND_SYNC

            evaluator = app.facade.round_execution_service.round_evaluator_service
            evaluator.event_callback = self._event_callback
            record = await evaluator.evaluate_round_and_maybe_rollup(
                round_request=selected.round_request,
                round_num=selected.round_num,
                step_logs=selected.step_logs,
                terminal=request.write_rollup,
                client_tool_dispatcher=UnavailableEvaluatorClientToolDispatcher(),
            )
            paths = evaluator.structured_artifact_sink.writer.layout.round_paths(selected.round_num)
            return OfflineEvaluationResult(
                record=record,
                artifact_dir=session_dir,
                round_num=selected.round_num,
                step_count=len(selected.step_logs),
                final_report_path=paths.final_report_path,
                legacy_result_path=evaluator.artifact_store.eval_round_result_path(selected.round_num),
                round_log_path=evaluator.artifact_store.round_evaluation_log_path,
                session_rollup_path=evaluator.artifact_store.session_evaluation_rollup_path,
                structured_round_dir=paths.root,
            )
        finally:
            await app.stop()

    @staticmethod
    def _load_steps(path: Path) -> list[StepLogRecord]:
        """Load typed step records from a session step log."""

        if not path.is_file():
            raise FileNotFoundError(f"Step log not found: {path}")
        steps: list[StepLogRecord] = []
        with path.open("r", encoding="utf-8") as handle:
            for line_number, line in enumerate(handle, start=1):
                stripped = line.strip()
                if not stripped:
                    continue
                try:
                    steps.append(StepLogRecord.model_validate_json(stripped))
                except ValueError as exc:
                    raise ValueError(f"Invalid step log JSON at {path}:{line_number}") from exc
        if not steps:
            raise ValueError(f"Step log contains no steps: {path}")
        return steps

    @staticmethod
    def _select_round_num(*, steps: list[StepLogRecord], requested_round: int) -> int:
        """Select an explicit round or the latest round from loaded steps."""

        available = sorted({step.round_num for step in steps})
        if requested_round > 0:
            if requested_round not in available:
                raise ValueError(f"Requested round {requested_round} not found; available rounds: {available}")
            return requested_round
        return available[-1]


__all__ = ["DesktopArtifactEvaluationRunner", "OfflineEvaluationRequest", "OfflineEvaluationResult"]
