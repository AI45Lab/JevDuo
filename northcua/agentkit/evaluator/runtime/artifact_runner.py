"""Reusable artifact-based evaluator runner skeletons."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path
from typing import Generic, TypeVar

SourceRecordT = TypeVar("SourceRecordT")
SelectedInputT = TypeVar("SelectedInputT")
ResultT = TypeVar("ResultT")


@dataclass(frozen=True, slots=True)
class ArtifactEvaluationRequest:
    """Generic request for evaluating an existing artifact directory.

    Attributes:
        app_config_path: Agent/runtime config used by the evaluator.
        artifact_dir: Session artifact directory, or a parent containing one
            session directory.
        unit_id: Agent-specific unit to evaluate, such as a round number.
            ``0`` means the runner should select the latest available unit.
        write_rollup: Whether the agent should update aggregate/session output.
        force_enable: Whether a manual replay should enable evaluation even if
            the runtime config disabled scheduled evaluation.
    """

    app_config_path: Path
    artifact_dir: Path
    unit_id: int = 0
    write_rollup: bool = True
    force_enable: bool = True


class ArtifactEvaluationRunnerBase(Generic[SourceRecordT, SelectedInputT, ResultT], ABC):
    """Template-method skeleton for artifact-only evaluator replay.

    Subclasses provide the agent-specific application bootstrap, trace parsing,
    unit selection, and evaluation call. The shared base owns path resolution and
    the high-level replay sequence so new agents do not each invent a separate
    artifact-evaluation command shape.
    """

    primary_log_name = "steps.jsonl"

    async def run(self, request: ArtifactEvaluationRequest) -> ResultT:
        """Evaluate one persisted artifact unit through subclass hooks.

        Args:
            request: Generic artifact replay request.

        Returns:
            Agent-specific evaluation result returned by ``evaluate_selected``.

        Raises:
            FileNotFoundError: If no primary log can be found.
            ValueError: If artifact resolution or unit selection is ambiguous.
        """

        session_dir = self.resolve_session_dir(request.artifact_dir)
        records = self.load_records(session_dir)
        selected = self.select_input(records=records, requested_unit_id=request.unit_id)
        return await self.evaluate_selected(
            request=request,
            session_dir=session_dir,
            records=records,
            selected=selected,
        )

    def resolve_session_dir(self, artifact_dir: Path) -> Path:
        """Resolve a session directory from a direct path or one-child root.

        Args:
            artifact_dir: Direct session directory or parent directory.

        Returns:
            Concrete session directory containing ``primary_log_name``.

        Raises:
            FileNotFoundError: If no child session contains the primary log.
            ValueError: If multiple child sessions are present.
        """

        candidate = artifact_dir.expanduser().resolve()
        if (candidate / self.primary_log_name).is_file():
            return candidate
        child_sessions = sorted(
            path for path in candidate.iterdir() if path.is_dir() and (path / self.primary_log_name).is_file()
        )
        if len(child_sessions) == 1:
            return child_sessions[0]
        if not child_sessions:
            raise FileNotFoundError(f"No {self.primary_log_name} found under artifact directory: {candidate}")
        names = ", ".join(path.name for path in child_sessions[:8])
        raise ValueError(
            f"Artifact directory contains multiple sessions with {self.primary_log_name}; choose one: {names}"
        )

    @abstractmethod
    def load_records(self, session_dir: Path) -> list[SourceRecordT]:
        """Load agent-specific persisted records from a session directory.

        Args:
            session_dir: Resolved session artifact directory.

        Returns:
            Typed source records parsed from the agent's primary log.
        """

    @abstractmethod
    def select_input(self, *, records: list[SourceRecordT], requested_unit_id: int) -> SelectedInputT:
        """Select the agent-specific evaluation unit from loaded records.

        Args:
            records: Source records loaded from the artifact directory.
            requested_unit_id: Agent-specific unit id, where ``0`` means latest.

        Returns:
            Typed selected input for ``evaluate_selected``.

        Raises:
            ValueError: If the requested unit cannot be selected.
        """

    @abstractmethod
    async def evaluate_selected(
        self,
        *,
        request: ArtifactEvaluationRequest,
        session_dir: Path,
        records: list[SourceRecordT],
        selected: SelectedInputT,
    ) -> ResultT:
        """Evaluate the selected unit and return a typed result.

        Args:
            request: Generic artifact replay request.
            session_dir: Resolved session artifact directory.
            records: All loaded source records.
            selected: Agent-specific selected unit.

        Returns:
            Agent-specific evaluation result.
        """


__all__ = ["ArtifactEvaluationRequest", "ArtifactEvaluationRunnerBase"]
