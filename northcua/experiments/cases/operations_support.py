"""Internal collaborators for high-level case-suite operations."""

from __future__ import annotations

import json
import re
import sqlite3
import time
from pathlib import Path

import yaml

from northcua.batch.contracts import BatchManifest, BatchRunResult, BatchTaskSpec, BatchTaskState, BatchTaskStatus
from northcua.batch.operations import BatchOperations
from northcua.batch.post_run import PostRunHookStatus
from northcua.experiments.cases.contracts import (
    CaseExpectedFeasibility,
    CaseSpec,
    CaseSuiteSpec,
    LargeArtifactDeliveryEvidence,
    PostRunAcceptancePolicy,
    RuntimeInjectionMode,
)
from northcua.experiments.cases.loader import default_suite_id_for_path
from northcua.experiments.cases.materialization import MaterializedCase, MaterializedCaseSuite, MaterializedFixture
from northcua.experiments.cases.reporting import CaseReportWriter, CaseTelemetryReportEnricher
from northcua.experiments.cases.verification import CaseArtifactResult, CaseTraceVerifier
from northcua.experiments.materialization import RuntimeWheelBundle, RuntimeWheelBundleBuilder
from northcua.sandboxes.contracts import SandboxFileUploadSpec
from northcua_protocol.capabilities import JsonObject

_RUN_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")
_TERMINAL_STATUSES = {
    BatchTaskStatus.SUCCEEDED,
    BatchTaskStatus.EVALUATION_PENDING,
    BatchTaskStatus.FAILED,
    BatchTaskStatus.TIMED_OUT,
    BatchTaskStatus.CANCELED,
}


def validate_case_run_id(run_id: str) -> None:
    """Reject run ids that could escape a run output directory.

    Args:
        run_id: User-supplied or generated run identifier.

    Raises:
        ValueError: If ``run_id`` is not a path-safe slug.
    """

    if not run_id or not _RUN_ID_PATTERN.fullmatch(run_id):
        raise ValueError("case run_id must be a non-empty path-safe slug")


class CaseRunPathResolver:
    """Resolve case-run paths and controller URLs for orchestration."""

    @staticmethod
    def suite_id_from_spec_path(spec_path: Path) -> str:
        """Read the suite id without expanding external case sources.

        Args:
            spec_path: Case suite YAML path.

        Returns:
            Explicit suite id or a deterministic id derived from the path.

        Raises:
            ValueError: When the YAML root is not a mapping.
        """

        raw = yaml.safe_load(spec_path.read_text(encoding="utf-8")) or {}
        if not isinstance(raw, dict):
            raise ValueError("case suite spec must be a mapping")
        suite_id = raw.get("suite_id")
        if isinstance(suite_id, str) and suite_id:
            return suite_id
        return default_suite_id_for_path(spec_path)

    @staticmethod
    def submitter_controller_url(spec: CaseSuiteSpec, *, public_url: str) -> str:
        """Return the URL used by the local batch submitter.

        Args:
            spec: Effective case suite specification.
            public_url: Public controller URL exposed to sandboxes.

        Returns:
            Local managed controller URL when available; otherwise the public URL.
        """

        if not spec.controller.managed_local:
            return public_url
        return f"ws://{spec.controller.local_host}:{spec.controller.local_port}"

    @staticmethod
    def extra_artifact_roots(spec: CaseSuiteSpec) -> list[Path]:
        """Return controller-managed roots that trace verification may scan.

        Args:
            spec: Effective case suite specification.

        Returns:
            Existing configured artifact roots that may contain trace assets.
        """

        return [
            path
            for path in (
                spec.controller.artifact_root_dir,
                spec.controller.blob_dir,
                spec.controller.db_path.parent if spec.controller.db_path else None,
            )
            if path is not None
        ]

    @staticmethod
    def run_dir(output_dir: Path, suite_id: str, run_id: str) -> Path:
        """Return the root directory for one materialized case run.

        Args:
            output_dir: User-provided case output root.
            suite_id: Suite identifier.
            run_id: Run identifier.

        Returns:
            Materialized case run directory.
        """

        return output_dir / suite_id / run_id

    @classmethod
    def batch_root(cls, output_dir: Path, suite_id: str, run_id: str) -> Path:
        """Return the batch output root below one materialized case run.

        Args:
            output_dir: User-provided case output root.
            suite_id: Suite identifier.
            run_id: Run identifier.

        Returns:
            Batch output directory for the run.
        """

        return cls.run_dir(output_dir, suite_id, run_id) / "batch"


class CaseRunPersistence:
    """Persist and rebuild case-suite control-plane projections."""

    @staticmethod
    def persist_materialized(materialized: MaterializedCaseSuite, *, persisted_spec: CaseSuiteSpec) -> None:
        """Persist the effective suite and projected batch manifest.

        Args:
            materialized: Materialized case suite with manifest projection.
            persisted_spec: Effective suite spec safe for resuming later.
        """

        materialized.output_dir.mkdir(parents=True, exist_ok=True)
        (materialized.output_dir / "case_suite.json").write_text(
            persisted_spec.model_dump_json(indent=2),
            encoding="utf-8",
        )
        (materialized.output_dir / "batch_manifest.json").write_text(
            materialized.manifest.model_dump_json(indent=2),
            encoding="utf-8",
        )

    def rebuild_report_if_possible(self, *, run_dir: Path, run_id: str) -> JsonObject | None:
        """Rebuild a report from persisted state, manifest, and artifacts.

        Args:
            run_dir: Materialized case run directory.
            run_id: Batch run identifier.

        Returns:
            Rebuilt report JSON, or ``None`` when required persisted files are absent.
        """

        spec_path = run_dir / "case_suite.json"
        manifest_path = run_dir / "batch_manifest.json"
        batch_dir = run_dir / "batch" / run_id
        if not spec_path.exists() or not manifest_path.exists() or not batch_dir.exists():
            return None
        spec = CaseSuiteSpec.model_validate_json(spec_path.read_text(encoding="utf-8"))
        manifest = BatchManifest.model_validate_json(manifest_path.read_text(encoding="utf-8"))
        states = BatchOperations().status(batch_id=run_id, output_dir=run_dir / "batch")
        materialized = self.materialized_from_persisted(spec=spec, manifest=manifest, output_dir=run_dir)
        batch_result = _scope_batch_result_to_runnable_tasks(
            materialized=materialized,
            batch_result=self.batch_result_from_states(batch_id=run_id, batch_dir=batch_dir, states=states),
        )
        rewrite_batch_summary_for_case_dispositions(materialized=materialized)
        existing = self.read_json_object(run_dir / "case_report.json")
        artifact_results = CaseTraceVerifier().verify(
            output_dir=run_dir,
            spec=spec,
            batch_result=batch_result,
            extra_roots=CaseRunPathResolver.extra_artifact_roots(spec),
        )
        batch_result = apply_trace_gate_to_batch_result(
            batch_result=batch_result,
            artifact_results=artifact_results,
            dry_run=bool(existing.get("dry_run", False)),
        )
        rewrite_batch_summary_for_trace_gate(
            batch_dir=batch_dir,
            batch_result=batch_result,
            artifact_results=artifact_results,
            dry_run=bool(existing.get("dry_run", False)),
        )
        batch_result = apply_expected_feasibility_to_batch_result(
            materialized=materialized,
            batch_result=batch_result,
        )
        rewrite_batch_summary_for_expected_feasibility(
            batch_dir=batch_dir,
            batch_result=batch_result,
        )
        batch_result = apply_post_run_acceptance_to_batch_result(
            materialized=materialized,
            batch_result=batch_result,
            dry_run=bool(existing.get("dry_run", False)),
        )
        rewrite_batch_summary_for_post_run_acceptance(
            materialized=materialized,
            batch_result=batch_result,
            dry_run=bool(existing.get("dry_run", False)),
        )
        CaseTelemetryReportEnricher(batch_dir).rebuild(artifact_results)
        case_report_path = CaseReportWriter(run_dir).write(
            materialized=materialized,
            batch_result=batch_result,
            artifact_results=artifact_results,
            dry_run=bool(existing.get("dry_run", False)),
        )
        parsed = json.loads(case_report_path.read_text(encoding="utf-8"))
        return parsed if isinstance(parsed, dict) else {"report": parsed}

    @staticmethod
    def batch_result_from_states(*, batch_id: str, batch_dir: Path, states: list[BatchTaskState]) -> BatchRunResult:
        """Project latest SQLite states into a reusable batch result.

        Args:
            batch_id: Batch run identifier.
            batch_dir: Directory where batch artifacts are stored.
            states: Latest task states read from SQLite.

        Returns:
            Batch result summary reconstructed from control-plane state.
        """

        return BatchRunResult(
            batch_id=batch_id,
            total=len(states),
            succeeded=sum(1 for state in states if state.status == BatchTaskStatus.SUCCEEDED),
            failed=sum(
                1
                for state in states
                if state.status
                in {BatchTaskStatus.FAILED, BatchTaskStatus.TIMED_OUT, BatchTaskStatus.EVALUATION_PENDING}
            ),
            canceled=sum(1 for state in states if state.status == BatchTaskStatus.CANCELED),
            incomplete=sum(1 for state in states if state.status not in _TERMINAL_STATUSES),
            output_dir=str(batch_dir),
            task_states=states,
        )

    @staticmethod
    def materialized_from_persisted(
        *,
        spec: CaseSuiteSpec,
        manifest: BatchManifest,
        output_dir: Path,
    ) -> MaterializedCaseSuite:
        """Recreate materialization metadata from persisted suite and manifest.

        Args:
            spec: Effective case suite persisted with the run.
            manifest: Projected batch manifest persisted with the run.
            output_dir: Materialized case run directory.

        Returns:
            Recreated materialized suite used by report writers and resumption.
        """

        tasks = {task.task_id: task for task in manifest.tasks}
        cases: list[MaterializedCase] = []
        for case in spec.tasks:
            task = tasks.get(case.task_id)
            sandbox = task.sandbox if task is not None and task.sandbox is not None else spec.sandbox
            startup_commands = tuple(task.startup_commands if task is not None else [])
            pre_submit_commands = tuple(task.pre_submit_commands if task is not None else case.pre_submit_commands)
            fixtures = CaseRunPersistence.materialized_fixtures_from_persisted(case=case, task=task)
            cases.append(
                MaterializedCase(
                    case=case,
                    fixtures=fixtures,
                    startup_commands=startup_commands,
                    sandbox=sandbox,
                    pre_submit_commands=pre_submit_commands,
                )
            )
        return MaterializedCaseSuite(
            spec=spec,
            output_dir=output_dir,
            fixture_dir=output_dir / "fixtures",
            cases=tuple(cases),
            manifest=manifest,
        )

    @staticmethod
    def materialized_fixtures_from_persisted(
        *,
        case: CaseSpec,
        task: BatchTaskSpec | None,
    ) -> tuple[MaterializedFixture, ...]:
        """Recreate fixture checksum metadata from a manifest task.

        Args:
            case: Case definition from the persisted suite.
            task: Optional projected batch task for the case.

        Returns:
            Materialized fixtures with persisted checksum metadata restored.
        """

        checksums = CaseRunPersistence.fixture_checksums_from_manifest_task(task)
        uploads_by_path = {
            upload.remote_path: upload
            for upload in (task.sandbox.file_uploads if task is not None and task.sandbox is not None else [])
        }
        evidence_by_path: dict[str, LargeArtifactDeliveryEvidence] = {}
        if task is not None:
            raw_evidence = task.metadata.get("large_artifact_delivery_evidence")
            if isinstance(raw_evidence, list):
                evidence_by_path = {
                    evidence.fixture_remote_path: evidence
                    for item in raw_evidence
                    if isinstance(item, dict)
                    for evidence in [LargeArtifactDeliveryEvidence.model_validate(item)]
                }
        materialized: list[MaterializedFixture] = []
        for fixture in [*case.fixtures, *case.dependency_injection.files]:
            checksum = checksums.get(fixture.remote_path) or fixture.sha256
            resolved_fixture = fixture.model_copy(update={"sha256": checksum}) if checksum else fixture
            upload = uploads_by_path.get(fixture.remote_path)
            evidence = evidence_by_path.get(fixture.remote_path)
            if (
                fixture.large_artifact is not None
                and case.disposition.is_runnable
                and (upload is None or evidence is None)
            ):
                raise ValueError(f"persisted large artifact evidence is missing for fixture {fixture.remote_path}")
            materialized.append(
                MaterializedFixture(
                    spec=resolved_fixture,
                    upload=upload
                    or SandboxFileUploadSpec(
                        local_path=Path("."),
                        remote_path=fixture.remote_path,
                        mode=fixture.mode,
                        visibility=fixture.visibility.value,
                        sha256=checksum,
                    ),
                    large_artifact_evidence=evidence,
                )
            )
        return tuple(materialized)

    @staticmethod
    def fixture_checksums_from_manifest_task(task: BatchTaskSpec | None) -> dict[str, str]:
        """Return fixture sha256 values persisted on the batch task.

        Args:
            task: Optional projected batch task.

        Returns:
            Mapping from remote fixture path to sha256 checksum.
        """

        if task is None:
            return {}
        checksums: dict[str, str] = {}
        raw_checksums = task.metadata.get("fixture_sha256")
        if isinstance(raw_checksums, dict):
            checksums.update(
                {
                    str(remote_path): checksum
                    for remote_path, checksum in raw_checksums.items()
                    if isinstance(checksum, str) and checksum
                }
            )
        if task.sandbox is not None:
            checksums.update(
                {
                    upload.remote_path: upload.sha256
                    for upload in task.sandbox.file_uploads
                    if upload.remote_path and upload.sha256
                }
            )
        return checksums

    @staticmethod
    def read_json_object(path: Path) -> JsonObject:
        """Read a JSON object from disk, returning an empty object on bad input.

        Args:
            path: JSON file path.

        Returns:
            Parsed object when the file contains a JSON object; otherwise an empty mapping.
        """

        if not path.exists():
            return {}
        try:
            parsed = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        return parsed if isinstance(parsed, dict) else {}


class RuntimeBundleAttacher:
    """Attach runtime wheel bundles to materialized sandbox specs."""

    def __init__(self, bundle_builder: RuntimeWheelBundleBuilder) -> None:
        self._bundle_builder = bundle_builder

    def attach(
        self,
        materialized: MaterializedCaseSuite,
        *,
        dry_run: bool,
        has_real_provider: bool,
    ) -> MaterializedCaseSuite:
        """Attach local runtime wheels when upload injection is requested.

        Args:
            materialized: Materialized case suite.
            dry_run: Whether this run should avoid provider-side mutation.
            has_real_provider: Whether the projected manifest targets a real sandbox provider.

        Returns:
            Materialized suite with runtime uploads and startup commands prepended when needed.
        """

        if dry_run or materialized.spec.runtime.mode != RuntimeInjectionMode.UPLOAD_WHEELS:
            return materialized
        if not has_real_provider:
            return materialized
        bundles: dict[str, RuntimeWheelBundle] = {}
        runnable_task_ids = {task.task_id for task in materialized.manifest.tasks}
        for case in materialized.cases:
            if case.case.task_id not in runnable_task_ids:
                continue
            python_executable = case.sandbox.device_python
            if python_executable not in bundles:
                bundles[python_executable] = self._bundle_builder.build(
                    output_dir=materialized.output_dir
                    / "runtime-bundles"
                    / _safe_runtime_bundle_key(python_executable),
                    python_executable=python_executable,
                )
        return self._attach_runtime_bundles(materialized, bundles)

    @staticmethod
    def _attach_runtime_bundles(
        materialized: MaterializedCaseSuite,
        bundles: dict[str, RuntimeWheelBundle],
    ) -> MaterializedCaseSuite:
        cases: list[MaterializedCase] = []
        tasks_by_id = {task.task_id: task for task in materialized.manifest.tasks}
        updated_tasks: dict[str, BatchTaskSpec] = {}
        for case in materialized.cases:
            task = tasks_by_id.get(case.case.task_id)
            if task is None:
                cases.append(case)
                continue
            bundle = bundles[case.sandbox.device_python]
            sandbox = case.sandbox.model_copy(update={"file_uploads": [*case.sandbox.file_uploads, *bundle.uploads]})
            startup_commands = [*bundle.startup_commands, *case.startup_commands]
            cases.append(
                MaterializedCase(
                    case=case.case,
                    fixtures=case.fixtures,
                    startup_commands=tuple(startup_commands),
                    sandbox=sandbox,
                    pre_submit_commands=case.pre_submit_commands,
                )
            )
            updated_tasks[task.task_id] = task.model_copy(
                update={
                    "sandbox": sandbox,
                    "startup_commands": startup_commands,
                }
            )
        tasks = [updated_tasks.get(task.task_id, task) for task in materialized.manifest.tasks]
        return MaterializedCaseSuite(
            spec=materialized.spec,
            output_dir=materialized.output_dir,
            fixture_dir=materialized.fixture_dir,
            cases=tuple(cases),
            manifest=materialized.manifest.model_copy(update={"tasks": tasks}),
        )


def rewrite_batch_summary_for_case_dispositions(*, materialized: MaterializedCaseSuite) -> None:
    """Record suite-level pre-allocation dispositions beside the batch summary."""

    summary_path = materialized.output_dir / "batch" / materialized.manifest.batch_id / "batch_summary.json"
    if not summary_path.exists():
        return
    try:
        parsed = json.loads(summary_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return
    if not isinstance(parsed, dict):
        return
    blocked = [item.case for item in materialized.cases if not item.case.disposition.is_runnable]
    parsed["case_dispositions"] = {
        "suite_total": len(materialized.cases),
        "runnable": len(materialized.cases) - len(blocked),
        "blocked": len(blocked),
        "blocked_cases": [
            {
                "task_id": case.task_id,
                **case.disposition.model_dump(mode="json"),
            }
            for case in blocked
        ],
    }
    summary_path.write_text(json.dumps(parsed, indent=2), encoding="utf-8")


def apply_trace_gate_to_batch_result(
    *,
    batch_result: BatchRunResult,
    artifact_results: list[CaseArtifactResult],
    dry_run: bool,
) -> BatchRunResult:
    """Treat missing trace/evaluator evidence as incomplete for live successes.

    SQLite remains the source of truth for task execution state. This projection
    only prevents a live case run from looking fully green when a task succeeded
    but its required trace, screenshot, or evaluator artifacts are missing.

    Args:
        batch_result: Batch execution result reconstructed from SQLite.
        artifact_results: Per-case trace verification rows.
        dry_run: Whether the run intentionally skipped live artifacts.

    Returns:
        The original batch result, or a copy with additional incomplete rows
        accounting for successful tasks that failed artifact verification.
    """

    if dry_run:
        return batch_result
    trace_failures = {
        result.task_id: result
        for result in artifact_results
        if result.status == BatchTaskStatus.SUCCEEDED.value and not result.passed
    }
    if not trace_failures:
        return batch_result
    states = []
    for state in batch_result.task_states:
        failure = trace_failures.get(state.task_id)
        if failure is None or state.status != BatchTaskStatus.SUCCEEDED:
            states.append(state)
            continue
        terminal_decision = dict(state.terminal_decision)
        terminal_decision["trace_gate"] = {
            "passed": False,
            "errors": list(failure.errors),
        }
        states.append(
            state.model_copy(
                update={
                    "status": BatchTaskStatus.INCOMPLETE,
                    "terminal_decision": terminal_decision,
                }
            )
        )
    return _batch_result_with_states(batch_result=batch_result, states=states)


def _scope_batch_result_to_runnable_tasks(
    *,
    materialized: MaterializedCaseSuite,
    batch_result: BatchRunResult,
) -> BatchRunResult:
    runnable_task_ids = [task.task_id for task in materialized.manifest.tasks]
    runnable_task_id_set = set(runnable_task_ids)
    states_by_id: dict[str, BatchTaskState] = {}
    for state in batch_result.task_states:
        if state.task_id not in runnable_task_id_set:
            continue
        if state.task_id in states_by_id:
            raise ValueError(f"duplicate runnable task state: {state.task_id}")
        states_by_id[state.task_id] = state
    states = [states_by_id[task_id] for task_id in runnable_task_ids if task_id in states_by_id]
    return _batch_result_with_states(
        batch_result=batch_result,
        states=states,
        total=len(runnable_task_ids),
    )


def _batch_result_with_states(
    *,
    batch_result: BatchRunResult,
    states: list[BatchTaskState],
    total: int | None = None,
) -> BatchRunResult:
    dispatched_total = batch_result.total if total is None else total
    if len(states) > dispatched_total:
        raise ValueError("batch task states cannot exceed runnable task total")
    succeeded = sum(1 for state in states if state.status == BatchTaskStatus.SUCCEEDED)
    failed = sum(
        1
        for state in states
        if state.status in {BatchTaskStatus.FAILED, BatchTaskStatus.TIMED_OUT, BatchTaskStatus.EVALUATION_PENDING}
    )
    canceled = sum(1 for state in states if state.status == BatchTaskStatus.CANCELED)
    nonterminal = sum(1 for state in states if state.status not in _TERMINAL_STATUSES)
    incomplete = nonterminal + dispatched_total - len(states)
    if (
        dispatched_total == batch_result.total
        and succeeded == batch_result.succeeded
        and failed == batch_result.failed
        and canceled == batch_result.canceled
        and incomplete == batch_result.incomplete
        and states == batch_result.task_states
    ):
        return batch_result
    return batch_result.model_copy(
        update={
            "total": dispatched_total,
            "succeeded": succeeded,
            "failed": failed,
            "canceled": canceled,
            "incomplete": incomplete,
            "task_states": states,
        }
    )


def apply_expected_feasibility_to_batch_result(
    *,
    materialized: MaterializedCaseSuite,
    batch_result: BatchRunResult,
) -> BatchRunResult:
    """Project expected-infeasible terminal decisions as successful cases.

    The raw SQLite state remains unchanged. This projection only aligns the
    product-facing case result/report with benchmark semantics: when a case is
    marked expected-infeasible and the agent terminates with the infeasible
    reason, the case satisfied its oracle even though the controller state is a
    failure-like terminal state.

    Args:
        materialized: Materialized case suite containing expected feasibility.
        batch_result: Batch result reconstructed from the control plane.

    Returns:
        Batch result with expected-infeasible rows counted as succeeded.
    """

    scoped_result = _scope_batch_result_to_runnable_tasks(
        materialized=materialized,
        batch_result=batch_result,
    )
    cases_by_id = {item.case.task_id: item.case for item in materialized.cases}
    states = [
        _project_expected_feasibility_state(state, cases_by_id.get(state.task_id))
        for state in scoped_result.task_states
    ]
    return _batch_result_with_states(batch_result=scoped_result, states=states)


def rewrite_batch_summary_for_expected_feasibility(*, batch_dir: Path, batch_result: BatchRunResult) -> None:
    """Mirror expected-feasibility projected counts into batch_summary.json.

    Args:
        batch_dir: Batch output directory containing ``batch_summary.json``.
        batch_result: Expected-feasibility-projected batch result.
    """

    summary_path = batch_dir / "batch_summary.json"
    if not summary_path.exists():
        return
    try:
        parsed = json.loads(summary_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return
    if not isinstance(parsed, dict):
        return
    projected_ids = [
        state.task_id
        for state in batch_result.task_states
        if state.status == BatchTaskStatus.SUCCEEDED
        and isinstance(state.terminal_decision, dict)
        and state.terminal_decision.get("expected_feasibility_projection") is True
    ]
    if not projected_ids:
        return
    parsed.update(
        {
            "total": batch_result.total,
            "succeeded": batch_result.succeeded,
            "failed": batch_result.failed,
            "canceled": batch_result.canceled,
            "incomplete": batch_result.incomplete,
            "expected_feasibility_projection": {
                "expected_infeasible_success_task_ids": projected_ids,
                "expected_infeasible_success_count": len(projected_ids),
            },
        }
    )
    summary_path.write_text(json.dumps(parsed, indent=2), encoding="utf-8")


def apply_post_run_acceptance_to_batch_result(
    *,
    materialized: MaterializedCaseSuite,
    batch_result: BatchRunResult,
    dry_run: bool,
) -> BatchRunResult:
    """Project required primary post-run oracle failures into case status."""

    scoped_result = _scope_batch_result_to_runnable_tasks(
        materialized=materialized,
        batch_result=batch_result,
    )
    policy = materialized.spec.post_run_acceptance_policy
    if dry_run or policy == PostRunAcceptancePolicy.REPORT_ONLY:
        return scoped_result
    cases_by_id = {item.case.task_id: item.case for item in materialized.cases}
    states = [
        _project_post_run_acceptance_state(state, cases_by_id.get(state.task_id), policy=policy)
        for state in scoped_result.task_states
    ]
    return _batch_result_with_states(batch_result=scoped_result, states=states)


def rewrite_batch_summary_for_post_run_acceptance(
    *,
    materialized: MaterializedCaseSuite,
    batch_result: BatchRunResult,
    dry_run: bool,
) -> None:
    """Mirror runnable-only counts and strict acceptance into ``batch_summary.json``."""

    policy = materialized.spec.post_run_acceptance_policy
    summary_path = Path(batch_result.output_dir) / "batch_summary.json"
    if not summary_path.exists():
        return
    try:
        parsed = json.loads(summary_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return
    if not isinstance(parsed, dict):
        return
    parsed.update(
        {
            "total": batch_result.total,
            "succeeded": batch_result.succeeded,
            "failed": batch_result.failed,
            "canceled": batch_result.canceled,
            "incomplete": batch_result.incomplete,
        }
    )
    if dry_run or policy == PostRunAcceptancePolicy.REPORT_ONLY:
        parsed.pop("post_run_acceptance", None)
    else:
        failures = {
            state.task_id: projection
            for state in batch_result.task_states
            if isinstance(state.terminal_decision, dict)
            and isinstance((projection := state.terminal_decision.get("post_run_acceptance")), dict)
            and projection.get("passed") is False
        }
        parsed["post_run_acceptance"] = {
            "policy": policy.value,
            "passed": not failures,
            "failed_task_ids": sorted(failures),
            "failures": failures,
        }
    summary_path.write_text(json.dumps(parsed, indent=2), encoding="utf-8")


def rewrite_batch_summary_for_trace_gate(
    *,
    batch_dir: Path,
    batch_result: BatchRunResult,
    artifact_results: list[CaseArtifactResult],
    dry_run: bool,
) -> None:
    """Mirror trace-gated counts into batch_summary.json when it exists.

    Args:
        batch_dir: Batch output directory containing ``batch_summary.json``.
        batch_result: Trace-gated batch result.
        artifact_results: Per-case trace verification rows.
        dry_run: Whether live evidence was intentionally skipped.
    """

    if dry_run:
        return
    gated_task_ids = [
        result.task_id
        for result in artifact_results
        if result.status == BatchTaskStatus.SUCCEEDED.value and not result.passed
    ]
    if not gated_task_ids:
        return
    summary_path = batch_dir / "batch_summary.json"
    if not summary_path.exists():
        return
    try:
        parsed = json.loads(summary_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return
    if not isinstance(parsed, dict):
        return
    parsed.update(
        {
            "total": batch_result.total,
            "succeeded": batch_result.succeeded,
            "failed": batch_result.failed,
            "canceled": batch_result.canceled,
            "incomplete": batch_result.incomplete,
            "trace_gate": {
                "missing_artifact_success_task_ids": gated_task_ids,
                "missing_artifact_success_count": len(gated_task_ids),
            },
        }
    )
    summary_path.write_text(json.dumps(parsed, indent=2), encoding="utf-8")


def _project_expected_feasibility_state(state: BatchTaskState, case: CaseSpec | None) -> BatchTaskState:
    if case is None or not case.disposition.is_runnable:
        return state
    if case.expected_feasibility != CaseExpectedFeasibility.INFEASIBLE:
        return state
    if _terminal_reason(state) != "infeasible":
        return state
    if isinstance(state.terminal_decision.get("trace_gate"), dict):
        return state
    if state.status == BatchTaskStatus.SUCCEEDED:
        return state
    terminal_decision = dict(state.terminal_decision)
    terminal_decision["expected_feasibility_projection"] = True
    return state.model_copy(
        update={
            "status": BatchTaskStatus.SUCCEEDED,
            "error": "",
            "last_failure_category": None,
            "terminal_decision": terminal_decision,
        }
    )


def _project_post_run_acceptance_state(
    state: BatchTaskState,
    case: CaseSpec | None,
    *,
    policy: PostRunAcceptancePolicy,
) -> BatchTaskState:
    if case is None or not case.disposition.is_runnable:
        return state
    failure = _primary_post_run_acceptance_failure(case, state)
    if failure is None:
        return state
    terminal_decision = dict(state.terminal_decision)
    terminal_decision["post_run_acceptance"] = {
        "policy": policy.value,
        "passed": False,
        **failure,
    }
    updates: dict[str, object] = {"terminal_decision": terminal_decision}
    if state.status == BatchTaskStatus.SUCCEEDED:
        reason = str(failure["reason"])
        updates.update(
            {
                "status": BatchTaskStatus.FAILED,
                "error": f"post-run acceptance failed: {reason}",
            }
        )
    return state.model_copy(update=updates)


def _primary_post_run_acceptance_failure(case: CaseSpec, state: BatchTaskState) -> JsonObject | None:
    if not case.post_run_hooks:
        return {
            "code": "primary_hook_missing",
            "hook_id": "",
            "reason": "required primary post-run hook is not declared",
        }
    primary = case.post_run_hooks[0]
    result = next((item for item in state.post_run_results if item.hook_id == primary.hook_id), None)
    if result is None:
        return {
            "code": "primary_result_missing",
            "hook_id": primary.hook_id,
            "reason": f"required primary post-run hook result is missing: {primary.hook_id}",
        }
    if result.error or result.status in {PostRunHookStatus.ERROR, PostRunHookStatus.TIMED_OUT}:
        detail = result.error or result.status.value
        return {
            "code": "primary_hook_error",
            "hook_id": primary.hook_id,
            "status": result.status.value,
            "reason": f"required primary post-run hook errored: {primary.hook_id}: {detail}",
        }
    if result.passed is False:
        return {
            "code": "primary_verdict_failed",
            "hook_id": primary.hook_id,
            "status": result.status.value,
            "reason": f"required primary post-run hook returned passed=false: {primary.hook_id}",
        }
    if result.status != PostRunHookStatus.PASSED:
        return {
            "code": "primary_hook_failed",
            "hook_id": primary.hook_id,
            "status": result.status.value,
            "reason": f"required primary post-run hook did not pass: {primary.hook_id}",
        }
    if result.passed is not True:
        return {
            "code": "primary_verdict_missing",
            "hook_id": primary.hook_id,
            "status": result.status.value,
            "reason": f"required primary post-run hook did not return passed=true: {primary.hook_id}",
        }
    return None


def _terminal_reason(state: BatchTaskState) -> str:
    decision = state.terminal_decision
    if not isinstance(decision, dict):
        return ""
    reason = decision.get("reason")
    return reason if isinstance(reason, str) else ""


def assert_force_run_not_active(*, suite_dir: Path, run_id: str) -> None:
    """Prevent forced cleanup from deleting a batch with active task leases.

    Args:
        suite_dir: Materialized case suite run directory.
        run_id: Batch run identifier below ``suite_dir``.

    Raises:
        RuntimeError: When the existing SQLite state cannot be inspected or
            still contains nonterminal or actively leased tasks.
    """

    sqlite_path = suite_dir / "batch" / run_id / "batch_state.sqlite"
    if not sqlite_path.exists():
        return
    terminal = {
        BatchTaskStatus.SUCCEEDED.value,
        BatchTaskStatus.FAILED.value,
        BatchTaskStatus.TIMED_OUT.value,
        BatchTaskStatus.CANCELED.value,
    }
    try:
        with sqlite3.connect(str(sqlite_path)) as connection:
            rows = connection.execute(
                "SELECT task_id, status, lease_token, lease_expires_at FROM batch_tasks"
            ).fetchall()
    except sqlite3.DatabaseError as exc:
        raise RuntimeError(f"cannot safely inspect existing case run before --force delete: {sqlite_path}") from exc
    now = time.time()
    active: list[tuple[str, str, str]] = []
    for task_id, status, lease_token, lease_expires_at in rows:
        task_status = str(status)
        if task_status not in terminal:
            active.append((str(task_id), task_status, "nonterminal"))
            continue
        if str(lease_token or "") and float(lease_expires_at or 0.0) > now:
            active.append((str(task_id), task_status, "leased"))
    if active:
        preview = ", ".join(f"{task_id}:{status}:{reason}" for task_id, status, reason in active[:5])
        raise RuntimeError(f"refusing to force-delete active case run {run_id}; active tasks: {preview}")


def _safe_runtime_bundle_key(python_executable: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", python_executable).strip("_") or "python"
