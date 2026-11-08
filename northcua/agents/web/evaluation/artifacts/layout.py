"""Thin Web wrapper around the shared AgentKit evaluator artifact layout."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from northcua.agentkit.evaluator.artifacts import EvaluatorArtifactLayout, EvaluatorArtifactWriter


@dataclass(slots=True, frozen=True)
class WebEvaluatorArtifactSink:
    """Factory for structured Web evaluator artifacts under one Web run root."""

    artifact_root: Path
    evaluator_kind: str = "web_round_evaluator"

    def layout(self) -> EvaluatorArtifactLayout:
        """Return the shared evaluator layout for this Web run."""

        return EvaluatorArtifactLayout(session_root=self.artifact_root, evaluator_kind=self.evaluator_kind)

    def writer(self) -> EvaluatorArtifactWriter:
        """Return a shared evaluator writer bound to this Web run."""

        return EvaluatorArtifactWriter(self.layout())
