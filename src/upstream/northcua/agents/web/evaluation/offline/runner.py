"""Offline artifact-based Web evaluator entrypoints."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from northcua.agentkernel.tools.surface import ToolSurfaceService
from northcua.agentkit.model_step import AgentKitModelStepComponents, AgentKitModelStepExecutor


@dataclass(frozen=True, slots=True)
class WebOfflineEvaluationRequest:
    """Configuration for evaluating a persisted Web artifact directory.

    Attributes:
        app_config_path: NorthCUA app config used for evaluator LLM/tools.
        artifact_dir: Web session artifact directory containing ``result.json``.
        round_num: Round to evaluate.
    """

    app_config_path: Path
    artifact_dir: Path
    round_num: int = 1


@dataclass(frozen=True, slots=True)
class WebOfflineEvaluationResult:
    """Result from replaying the Web evaluator over existing artifacts."""

    report: dict[str, object]
    artifact_dir: Path
    round_num: int
    final_report_path: Path
    evidence_manifest_path: Path
    llm_trace_path: Path
    tool_trace_path: Path
    runtime_log_path: Path
    structured_round_dir: Path


class WebArtifactEvaluationRunner:
    """Run the Web evaluator directly from a persisted artifact folder."""

    async def run(self, request: WebOfflineEvaluationRequest) -> WebOfflineEvaluationResult:
        """Evaluate a stored Web run without rerunning the browser agent.

        Args:
            request: Offline evaluator replay request.

        Returns:
            Paths and parsed final report from the evaluator run.

        Raises:
            FileNotFoundError: If required Web artifacts are missing.
        """

        artifact_dir = request.artifact_dir.expanduser().resolve()
        self._validate_artifact_dir(artifact_dir)

        from northcua.agents.web import WebAgentApplication
        from northcua.agents.web.evaluation.service import WebRoundEvaluatorService

        app = WebAgentApplication.from_app_config(
            str(request.app_config_path),
            session_id=artifact_dir.name,
            artifact_root_dir_override=str(artifact_dir.parent),
        )
        try:
            service = WebRoundEvaluatorService(model_step_executor=self._build_model_step_executor(app))
            await service.evaluate_artifact_root(
                artifact_dir,
                round_num=request.round_num,
                remote=False,
                max_steps=app.app_config.evaluation.max_steps,
                force_finalizer_after_steps=app.app_config.evaluation.force_finalizer_after_steps,
            )
            round_dir = artifact_dir / "evaluations" / "web_round_evaluator" / f"round_{request.round_num:04d}"
            final_report_path = round_dir / "final_report.json"
            return WebOfflineEvaluationResult(
                report=self._read_json_object(final_report_path),
                artifact_dir=artifact_dir,
                round_num=request.round_num,
                final_report_path=final_report_path,
                evidence_manifest_path=round_dir / "evidence_manifest.json",
                llm_trace_path=round_dir / "llm_trace.jsonl",
                tool_trace_path=round_dir / "tool_trace.jsonl",
                runtime_log_path=round_dir / "runtime.jsonl",
                structured_round_dir=round_dir,
            )
        finally:
            await app.stop()

    @staticmethod
    def _validate_artifact_dir(artifact_dir: Path) -> None:
        result_path = artifact_dir / "result.json"
        trace_path = artifact_dir / "web_agent_trace.jsonl"
        if not result_path.is_file():
            raise FileNotFoundError(f"Web result artifact not found: {result_path}")
        if not trace_path.is_file():
            raise FileNotFoundError(f"Web trace artifact not found: {trace_path}")

    @staticmethod
    def _build_model_step_executor(app: object) -> AgentKitModelStepExecutor:
        return AgentKitModelStepExecutor(
            AgentKitModelStepComponents(
                app_config=getattr(app, "app_config"),
                agent_config=getattr(app, "agent_config"),
                execution_transport=getattr(app, "execution_transport"),
                user_id=str(getattr(getattr(app, "app_config"), "execution").user_id),
                transport_capabilities=getattr(app, "transport_capabilities"),
                tool_surface_service=ToolSurfaceService(),
            )
        )

    @staticmethod
    def _read_json_object(path: Path) -> dict[str, object]:
        if not path.is_file():
            return {}
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return {}
        return payload if isinstance(payload, dict) else {}


__all__ = [
    "WebArtifactEvaluationRunner",
    "WebOfflineEvaluationRequest",
    "WebOfflineEvaluationResult",
]
