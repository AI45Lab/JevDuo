"""Trace and evaluator artifact verification for golden experiments."""

from __future__ import annotations

import base64
import json
from pathlib import Path

from northcua.batch.contracts import BatchRunResult, BatchTaskState, BatchTaskStatus
from northcua.experiments.contracts import (
    ExperimentSpec,
    ExperimentTaskResult,
    ExperimentTaskSpec,
    TraceExpectationSpec,
)
from northcua.experiments.verification.controller_artifacts import ControllerArtifactMaterializer
from northcua.experiments.verification.trace_helpers import ExperimentTraceVerifierHelpers

_TINY_PNG = (
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
    b"\x08\x06\x00\x00\x00\x1f\x15\xc4\x89\x00\x00\x00\nIDATx\x9cc\x00"
    b"\x01\x00\x00\x05\x00\x01\r\n-\xb4\x00\x00\x00\x00IEND\xaeB`\x82"
)
_TINY_PNG_DATA_URL = "data:image/png;base64," + base64.b64encode(_TINY_PNG).decode("ascii")


class FakeTraceSeeder:
    """Create minimal trace artifacts for fake-provider integration tests."""

    def seed(self, *, output_dir: Path, spec: ExperimentSpec, batch_result: BatchRunResult) -> None:
        """Write deterministic synthetic traces for successful fake tasks.

        Args:
            output_dir: Concrete experiment output directory.
            spec: Experiment spec whose tasks should be represented.
            batch_result: Batch result containing controller/session ids.
        """

        tasks_by_id = {task.task_id: task for task in spec.tasks}
        for state in batch_result.task_states:
            if state.status != BatchTaskStatus.SUCCEEDED:
                continue
            task = tasks_by_id[state.task_id]
            session_root = output_dir / "sessions" / state.session_id
            session_root.mkdir(parents=True, exist_ok=True)
            (session_root / "steps.jsonl").write_text(
                json.dumps({"task_id": task.task_id, "app_profile": task.app_profile.value}) + "\n",
                encoding="utf-8",
            )
            (session_root / "llm_trace.jsonl").write_text(
                json.dumps(
                    _synthetic_main_llm_trace(
                        task_id=task.task_id,
                        trace_id=state.trace_id,
                        agent_kind=task.agent_kind,
                    )
                )
                + "\n",
                encoding="utf-8",
            )
            (session_root / "runtime.jsonl").write_text(
                json.dumps({"event": "fake_trace_seeded", "task_id": task.task_id}) + "\n",
                encoding="utf-8",
            )
            screenshot = session_root / "screenshot-0001.png"
            screenshot.write_bytes(_TINY_PNG)
            if task.agent_kind == "web":
                self._seed_web_artifacts(session_root=session_root, task_id=task.task_id, trace_id=state.trace_id)
            evaluator_root = session_root / "evaluations" / spec.trace_expectations.evaluator_kind / "round_0001"
            evaluator_root.mkdir(parents=True, exist_ok=True)
            (evaluator_root / "final_report.json").write_text(
                json.dumps(
                    {
                        "overall_verdict": "success",
                        "verdict": "pass",
                        "synthetic": True,
                        "safety": {
                            "risk_level": "none",
                            "findings": [],
                            "reason": "Synthetic fake-provider trace has no safety findings.",
                            "evidence_refs": [],
                        },
                        "efficiency": {
                            "rating": 5,
                            "unnecessary_step_count": 0,
                            "repeated_action_count": 0,
                            "detour_count": 0,
                            "direct_path_available": False,
                            "reason": "Synthetic fake-provider trace uses the direct path.",
                            "evidence_refs": [],
                        },
                    },
                    indent=2,
                ),
                encoding="utf-8",
            )
            (evaluator_root / "evidence_manifest.json").write_text(
                json.dumps({"evidence": ["synthetic"]}, indent=2),
                encoding="utf-8",
            )
            (evaluator_root / "llm_trace.jsonl").write_text(
                json.dumps(
                    _synthetic_evaluator_llm_trace(
                        task_id=task.task_id,
                        trace_id=state.trace_id,
                        agent_kind=task.agent_kind,
                    )
                )
                + "\n",
                encoding="utf-8",
            )

    def _seed_web_artifacts(self, *, session_root: Path, task_id: str, trace_id: str) -> None:
        screenshots = session_root / "screenshots"
        dom_snapshots = session_root / "dom_snapshots"
        screenshots.mkdir(parents=True, exist_ok=True)
        dom_snapshots.mkdir(parents=True, exist_ok=True)
        (screenshots / "step_001_raw.png").write_bytes(_TINY_PNG)
        (dom_snapshots / "step_001.txt").write_text(
            "Synthetic Web fixture DOM\nTask complete marker: success\n",
            encoding="utf-8",
        )
        (dom_snapshots / "step_001.json").write_text(
            json.dumps({"title": "Synthetic Web Fixture", "task_id": task_id}, indent=2),
            encoding="utf-8",
        )
        (session_root / "web_agent_trace.jsonl").write_text(
            json.dumps(
                {
                    "trace_id": trace_id,
                    "task_id": task_id,
                    "step": 1,
                    "observation": {
                        "url": "http://127.0.0.1/synthetic",
                        "title": "Synthetic Web Fixture",
                        "screenshot_path": "screenshots/step_001_raw.png",
                        "dom_snapshot_path": "dom_snapshots/step_001.txt",
                    },
                }
            )
            + "\n",
            encoding="utf-8",
        )
        (session_root / "result.json").write_text(
            json.dumps({"status": "success", "final_answer": "synthetic success"}, indent=2),
            encoding="utf-8",
        )


def _synthetic_main_llm_trace(*, task_id: str, trace_id: str, agent_kind: str) -> dict[str, object]:
    return {
        "source": "web" if agent_kind == "web" else "desktop",
        "trace_id": trace_id,
        "session_step": 1,
        "llm_trace": {
            "request": {
                "tools": [],
                "input": [
                    {
                        "type": "message",
                        "role": "user",
                        "content": f"Synthetic trace for {task_id}.",
                    }
                ],
            },
            "assistant_text": "Synthetic task completed successfully.",
        },
        "synthetic": True,
    }


def _synthetic_evaluator_llm_trace(*, task_id: str, trace_id: str, agent_kind: str) -> dict[str, object]:
    return {
        "source": "web_evaluator" if agent_kind == "web" else "desktop_evaluator",
        "trace_id": trace_id,
        "session_step": 1,
        "llm_trace": {
            "request": {
                "tools": [],
                "messages": [
                    {"role": "system", "content": "Evaluate the completed round."},
                    {
                        "role": "user",
                        "content": [
                            {"type": "text", "text": f"Evidence for {task_id}."},
                            {"type": "image_url", "image_url": {"url": _TINY_PNG_DATA_URL}},
                        ],
                    },
                ],
            },
            "assistant_text": "The synthetic evaluator report is successful.",
        },
        "synthetic": True,
    }


class ExperimentTraceVerifier(ExperimentTraceVerifierHelpers):
    """Verify that main-agent and evaluator traces exist for each task."""

    def __init__(self, *, controller_artifacts: ControllerArtifactMaterializer | None = None) -> None:
        """Create a verifier with optional controller artifact support.

        Args:
            controller_artifacts: Materializer used to decode inline controller
                artifacts into the experiment output tree before verification.
        """

        self._controller_artifacts = controller_artifacts or ControllerArtifactMaterializer()

    def verify(
        self,
        *,
        output_dir: Path,
        spec: ExperimentSpec,
        batch_result: BatchRunResult,
        extra_roots: list[Path] | None = None,
        materialize_controller_artifacts: bool = True,
    ) -> list[ExperimentTaskResult]:
        """Verify every task and return dataset-oriented rows.

        Args:
            output_dir: Concrete experiment output directory.
            spec: Experiment spec containing task and expectation metadata.
            batch_result: Underlying batch result.
            extra_roots: Optional controller blob/log roots to include in the
                artifact search.
            materialize_controller_artifacts: Whether controller SQLite/blob
                fallback artifacts may be written into ``output_dir``.

        Returns:
            Verification rows, one per experiment task.
        """

        canonical_roots = self._dedupe_roots([output_dir / "agent-artifacts", *(extra_roots or [])])
        materialized = self._controller_artifacts.materialize(
            output_dir=output_dir,
            states=batch_result.task_states,
            evaluator_kind=spec.trace_expectations.evaluator_kind,
            db_path=spec.controller.db_path,
            canonical_roots=canonical_roots,
            blob_roots=extra_roots,
            allow_writes=materialize_controller_artifacts,
        )
        roots = self._dedupe_roots([*canonical_roots, output_dir])
        if materialized is not None:
            roots = self._dedupe_roots([*canonical_roots, materialized, output_dir])
        states_by_id = {state.task_id: state for state in batch_result.task_states}
        return [
            self._verify_task(
                task=task,
                state=states_by_id.get(task.task_id),
                expectations=spec.trace_expectations,
                roots=roots,
            )
            for task in spec.tasks
        ]

    def _verify_task(
        self,
        *,
        task: ExperimentTaskSpec,
        state: BatchTaskState | None,
        expectations: TraceExpectationSpec,
        roots: list[Path],
    ) -> ExperimentTaskResult:
        errors: list[str] = []
        if state is None:
            return ExperimentTaskResult(
                task_id=task.task_id,
                app_profile=task.app_profile,
                status="missing",
                query=task.query,
                fixture_remote_paths=[fixture.remote_path for fixture in task.fixtures],
                fixture_sha256={fixture.remote_path: fixture.sha256 for fixture in task.fixtures if fixture.sha256},
                errors=["batch task state missing"],
            )
        if state.status != BatchTaskStatus.SUCCEEDED:
            errors.append(f"batch task did not succeed: {state.status.value}")
        main_steps = self._find_main_step_trace(task=task, state=state, roots=roots)
        main_llm = self._find_first("llm_trace.jsonl", state=state, roots=roots, exclude_evaluations=True)
        screenshots = self._find_screenshots(state=state, roots=roots)
        valid_screenshots = [path for path in screenshots if self._is_valid_image(path)]
        evaluator_report = self._find_evaluator_file(
            "final_report.json", evaluator_kind=expectations.evaluator_kind, state=state, roots=roots
        )
        evaluator_evidence = self._find_evaluator_file(
            "evidence_manifest.json", evaluator_kind=expectations.evaluator_kind, state=state, roots=roots
        )
        evaluator_llm = self._find_evaluator_file(
            "llm_trace.jsonl", evaluator_kind=expectations.evaluator_kind, state=state, roots=roots
        )
        evaluator_report_payload = self._evaluator_report_payload(evaluator_report)
        evaluator_verdict = self._evaluator_verdict(evaluator_report_payload)
        main_trace_dir = self._artifact_parent(main_steps) or self._artifact_parent(main_llm)
        evaluator_trace_dir = self._artifact_parent(evaluator_report) or self._artifact_parent(evaluator_llm)
        if expectations.require_main_steps and not self._non_empty(main_steps):
            errors.append(_main_steps_missing_error(task))
        if expectations.require_main_llm_trace and not self._non_empty(main_llm):
            errors.append("main-agent llm_trace.jsonl missing or empty")
        if expectations.require_screenshot and not valid_screenshots:
            errors.append("main-agent screenshot missing")
        if expectations.require_evaluator_report and not self._non_empty(evaluator_report):
            errors.append("evaluator final_report.json missing or empty")
        if self._non_empty(evaluator_report) and evaluator_verdict != "success":
            errors.append(f"evaluator overall_verdict is not success: {evaluator_verdict or 'missing'}")
        if expectations.require_evaluator_report and not self._non_empty(evaluator_evidence):
            errors.append("evaluator evidence_manifest.json missing or empty")
        if expectations.require_evaluator_llm_trace and not self._non_empty(evaluator_llm):
            errors.append("evaluator llm_trace.jsonl missing or empty")
        return ExperimentTaskResult(
            task_id=task.task_id,
            app_profile=task.app_profile,
            status=state.status.value,
            controller_task_id=state.controller_task_id,
            trace_id=state.trace_id,
            session_id=state.session_id,
            sandbox_id=state.sandbox_id,
            query=task.query,
            fixture_remote_paths=[fixture.remote_path for fixture in task.fixtures],
            fixture_sha256={fixture.remote_path: fixture.sha256 for fixture in task.fixtures if fixture.sha256},
            main_trace_dir=main_trace_dir,
            main_steps_path=str(main_steps) if main_steps else "",
            main_llm_trace_path=str(main_llm) if main_llm else "",
            screenshot_paths=[str(path) for path in valid_screenshots],
            evaluator_trace_dir=evaluator_trace_dir,
            evaluator_report_path=str(evaluator_report) if evaluator_report else "",
            evaluator_evidence_manifest_path=str(evaluator_evidence) if evaluator_evidence else "",
            evaluator_llm_trace_path=str(evaluator_llm) if evaluator_llm else "",
            evaluator_verdict=evaluator_verdict,
            evaluator_safety_risk_level=self._evaluator_safety_risk_level(evaluator_report_payload),
            evaluator_efficiency_rating=self._evaluator_efficiency_rating(evaluator_report_payload),
            evaluator_diagnosis_category_breakdown=self._evaluator_diagnosis_category_breakdown(
                evaluator_report_payload
            ),
            passed=not errors,
            errors=errors,
        )

    def _find_main_step_trace(
        self,
        *,
        task: ExperimentTaskSpec,
        state: BatchTaskState,
        roots: list[Path],
    ) -> Path | None:
        if task.agent_kind == "web":
            web_steps = self._find_first("web_agent_trace.jsonl", state=state, roots=roots)
            if web_steps is not None:
                return web_steps
        return self._find_first("steps.jsonl", state=state, roots=roots)


def _main_steps_missing_error(task: ExperimentTaskSpec) -> str:
    if task.agent_kind == "web":
        return "main-agent web_agent_trace.jsonl missing or empty"
    return "main-agent steps.jsonl missing or empty"
