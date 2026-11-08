"""Product-facing operations for reusable case suites."""

from __future__ import annotations

import json
import shutil
import sqlite3
import tempfile
import time
import uuid
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path

from northcua.batch.contracts import (
    BatchManifest,
    BatchRunConfig,
    BatchRunResumeConfig,
    BatchTaskState,
    BatchTaskStatus,
)
from northcua.batch.operations import (
    BatchOperations,
    _resolve_pre_resume_cleanup_provider,
    _run_pre_resume_cleanup,
)
from northcua.batch.preparation import PreparedSandboxPolicy
from northcua.batch.runner import BatchRunner
from northcua.batch.runner.validation import validate_existing_run_config, validate_prepared_sandbox_allocations
from northcua.batch.state import SQLiteBatchStateRepository
from northcua.experiments.cases.catalog import CaseCatalogLoader
from northcua.experiments.cases.contracts import (
    CaseRunMode,
    CaseRunResult,
    CaseSuiteSpec,
)
from northcua.experiments.cases.loader import load_case_suite
from northcua.experiments.cases.materialization import (
    CaseMaterializer,
    MaterializedCaseSuite,
)
from northcua.experiments.cases.operations_control import CaseRunControlMixin
from northcua.experiments.cases.operations_support import (
    CaseRunPathResolver,
    CaseRunPersistence,
    RuntimeBundleAttacher,
    apply_expected_feasibility_to_batch_result,
    apply_post_run_acceptance_to_batch_result,
    apply_trace_gate_to_batch_result,
    assert_force_run_not_active,
    rewrite_batch_summary_for_case_dispositions,
    rewrite_batch_summary_for_expected_feasibility,
    rewrite_batch_summary_for_post_run_acceptance,
    rewrite_batch_summary_for_trace_gate,
    validate_case_run_id,
)
from northcua.experiments.cases.reporting import CaseReportWriter, CaseTelemetryReportEnricher
from northcua.experiments.cases.runtime import CaseBatchRunnerFactory, CaseControllerRuntime, CaseRunPreflight
from northcua.experiments.cases.verification import CaseTraceVerifier
from northcua.experiments.materialization import RuntimeWheelBundleBuilder
from northcua.experiments.orchestration.controller import ManagedControllerProcess
from northcua.sandboxes.contracts import DEFAULT_DEVICE_READINESS_TIMEOUT_SECONDS
from northcua_protocol.capabilities import JsonObject


@dataclass(frozen=True, slots=True)
class _PreparedCaseSuite:
    """Resolved spec inputs shared by materialize and run operations."""

    spec_path: Path
    spec: CaseSuiteSpec
    suite_dir: Path
    public_controller_url: str
    submitter_controller_url: str
    run_id: str


class CaseOperations(CaseRunControlMixin):
    """High-level case-suite operations independent from argparse."""

    def __init__(
        self,
        *,
        materializer: CaseMaterializer | None = None,
        batch_runner: BatchRunner | None = None,
        controller_process: ManagedControllerProcess | None = None,
        runtime_bundle_builder: RuntimeWheelBundleBuilder | None = None,
    ) -> None:
        self._materializer = materializer
        self._batch_runner = batch_runner
        self._controller_runtime = CaseControllerRuntime(controller_process=controller_process)
        self._preflight = CaseRunPreflight()
        self._runtime_bundle_builder = runtime_bundle_builder or RuntimeWheelBundleBuilder()
        self._path_resolver = CaseRunPathResolver()
        self._persistence = CaseRunPersistence()
        self._runtime_attacher = RuntimeBundleAttacher(self._runtime_bundle_builder)

    def validate(self, spec_path: str | Path, *, catalog_path: str | Path | None = None) -> CaseSuiteSpec:
        """Load and validate a case suite plus catalog compatibility.

        Args:
            spec_path: Suite YAML path.
            catalog_path: Optional catalog YAML override.

        Returns:
            Validated suite with case sources expanded.
        """

        suite = load_case_suite(spec_path)
        self._materializer_for(catalog_path).validate_suite(suite)
        return suite

    def materialize_file(
        self,
        *,
        spec_path: str | Path,
        output_dir: Path,
        catalog_path: str | Path | None = None,
        public_controller_url: str = "",
        run_id: str = "",
    ) -> MaterializedCaseSuite:
        """Materialize a spec file and persist its manifest snapshot.

        Args:
            spec_path: Suite YAML path.
            output_dir: Root directory for materialized output.
            catalog_path: Optional catalog YAML override.
            public_controller_url: Controller URL to project into the manifest.
            run_id: Optional run id for resumable output paths.

        Returns:
            Materialized suite and batch manifest.
        """

        prepared = self._prepare_spec(
            spec_path=spec_path,
            output_dir=output_dir,
            public_controller_url=public_controller_url,
            run_id=run_id,
        )
        materialized = self._materializer_for(catalog_path).materialize(
            prepared.spec,
            output_dir=output_dir,
            spec_dir=prepared.spec_path.parent,
            controller_url=prepared.public_controller_url,
            run_id=prepared.run_id,
        )
        self._persistence.persist_materialized(
            materialized,
            persisted_spec=self._controller_runtime.spec_for_persistence(materialized.spec),
        )
        return materialized

    async def run_file(
        self,
        *,
        spec_path: str | Path,
        output_dir: Path,
        catalog_path: str | Path | None = None,
        public_controller_url: str = "",
        dry_run: bool = False,
        run_id: str = "",
        resume: bool = False,
        force: bool = False,
        concurrency: int = 0,
        controller_parallelism: int = 0,
        sandbox_create_concurrency: int = 0,
        controller_submit_concurrency: int = 0,
        controller_status_concurrency: int = 0,
        controller_submit_jitter_seconds: float = 0.0,
        device_readiness_timeout_seconds: float = 0.0,
        lease_timeout_seconds: float = 0.0,
        infra_retry_backoff_seconds: list[float] | None = None,
        prepared_sandbox_policy: PreparedSandboxPolicy | None = None,
        controller_token: str = "",
    ) -> CaseRunResult:
        """Materialize and run a case suite through the existing batch runner.

        Args:
            spec_path: Suite YAML path.
            output_dir: Root directory for run artifacts.
            catalog_path: Optional catalog YAML override.
            public_controller_url: Public controller URL used by sandboxes.
            dry_run: Whether to plan without creating live sandboxes.
            run_id: Optional explicit run id.
            resume: Whether to resume from persisted SQLite/manifest state.
            force: Whether to replace an inactive existing run directory.
            concurrency: Optional task concurrency override.
            controller_parallelism: Optional one-knob controller process
                executor parallelism override.
            sandbox_create_concurrency: Optional sandbox creation throttle.
            controller_submit_concurrency: Optional controller submit handshake
                throttle.
            controller_status_concurrency: Optional controller status
                websocket request throttle.
            controller_submit_jitter_seconds: Optional deterministic submit
                jitter inside the handshake throttle.
            device_readiness_timeout_seconds: Optional device worker readiness
                timeout override.
            lease_timeout_seconds: Optional local task lease timeout override.
            infra_retry_backoff_seconds: Optional retry backoff sequence for
                retryable infra failures.
            prepared_sandbox_policy: Optional single-use prepared sandbox queue policy.
            controller_token: Optional runtime-only controller token. It is
                used for this execution but never persisted to case artifacts.

        Returns:
            Case run summary with report and manifest paths.

        Raises:
            ValueError: When resume is requested without a run id.
            FileExistsError: When the run directory exists without force/resume.
        """

        resolved = Path(spec_path).expanduser().resolve()
        if resume:
            if not run_id:
                raise ValueError("case run --resume requires --run-id")
            return await self._resume_file(
                spec_path=resolved,
                output_dir=output_dir,
                public_controller_url=public_controller_url,
                dry_run=dry_run,
                run_id=run_id,
                concurrency=concurrency,
                controller_parallelism=controller_parallelism,
                sandbox_create_concurrency=sandbox_create_concurrency,
                controller_submit_concurrency=controller_submit_concurrency,
                controller_status_concurrency=controller_status_concurrency,
                controller_submit_jitter_seconds=controller_submit_jitter_seconds,
                device_readiness_timeout_seconds=device_readiness_timeout_seconds,
                lease_timeout_seconds=lease_timeout_seconds,
                infra_retry_backoff_seconds=infra_retry_backoff_seconds,
                prepared_sandbox_policy=prepared_sandbox_policy,
                controller_token=controller_token,
            )
        suite = load_case_suite(resolved)
        effective_run_id = run_id or self._new_run_id(suite.suite_id)
        self._validate_run_id(effective_run_id)
        prepared = self._prepare_spec(
            spec_path=resolved,
            output_dir=output_dir,
            public_controller_url=public_controller_url,
            run_id=effective_run_id,
            controller_token=controller_token,
        )
        prepared = replace(
            prepared,
            spec=self._with_runtime_parallelism(
                prepared.spec,
                concurrency=concurrency,
                controller_parallelism=controller_parallelism,
            ),
        )
        self._check_run_dir_available(prepared.suite_dir, force=force)
        materializer = self._materializer_for(catalog_path)
        if force and prepared.suite_dir.exists():
            self._preflight_force_run(
                materializer=materializer,
                prepared=prepared,
                dry_run=dry_run,
            )
            assert_force_run_not_active(suite_dir=prepared.suite_dir, run_id=prepared.run_id)
            shutil.rmtree(prepared.suite_dir)
        materialized = materializer.materialize(
            prepared.spec,
            output_dir=output_dir,
            spec_dir=prepared.spec_path.parent,
            controller_url=prepared.public_controller_url,
            submitter_controller_url=prepared.submitter_controller_url,
            run_id=prepared.run_id,
        )
        has_real_provider = self._preflight.has_real_provider(materialized.manifest)
        self._preflight.validate(
            prepared.spec,
            manifest=materialized.manifest,
            public_url=prepared.public_controller_url,
            dry_run=dry_run,
        )
        materialized = self._runtime_attacher.attach(
            materialized,
            dry_run=dry_run,
            has_real_provider=has_real_provider,
        )
        return await self._execute_materialized_run(
            materialized=materialized,
            public_controller_url=prepared.public_controller_url,
            submitter_controller_url=prepared.submitter_controller_url,
            dry_run=dry_run,
            has_real_provider=has_real_provider,
            concurrency=concurrency,
            sandbox_create_concurrency=sandbox_create_concurrency,
            controller_submit_concurrency=controller_submit_concurrency,
            controller_status_concurrency=controller_status_concurrency,
            controller_submit_jitter_seconds=controller_submit_jitter_seconds,
            device_readiness_timeout_seconds=device_readiness_timeout_seconds,
            lease_timeout_seconds=lease_timeout_seconds,
            infra_retry_backoff_seconds=infra_retry_backoff_seconds,
            prepared_sandbox_policy=prepared_sandbox_policy,
            persist_materialized=True,
        )

    async def _resume_file(
        self,
        *,
        spec_path: Path,
        output_dir: Path,
        public_controller_url: str,
        dry_run: bool,
        run_id: str,
        concurrency: int,
        controller_parallelism: int,
        sandbox_create_concurrency: int,
        controller_submit_concurrency: int,
        controller_status_concurrency: int,
        controller_submit_jitter_seconds: float,
        device_readiness_timeout_seconds: float,
        lease_timeout_seconds: float,
        infra_retry_backoff_seconds: list[float] | None,
        prepared_sandbox_policy: PreparedSandboxPolicy | None,
        controller_token: str = "",
    ) -> CaseRunResult:
        suite_id = self._path_resolver.suite_id_from_spec_path(spec_path)
        self._validate_run_id(run_id)
        run_root = self._path_resolver.run_dir(output_dir, suite_id, run_id)
        sqlite_path = run_root / "batch" / run_id / "batch_state.sqlite"
        if not sqlite_path.exists():
            raise FileNotFoundError(f"cannot resume; batch SQLite state not found: {sqlite_path}")
        spec_path_persisted = run_root / "case_suite.json"
        manifest_path = run_root / "batch_manifest.json"
        if not spec_path_persisted.exists() or not manifest_path.exists():
            raise FileNotFoundError(f"cannot resume; persisted case suite or manifest is missing under {run_root}")
        spec = CaseSuiteSpec.model_validate_json(spec_path_persisted.read_text(encoding="utf-8"))
        manifest = BatchManifest.model_validate_json(manifest_path.read_text(encoding="utf-8"))
        spec = self._controller_runtime.effective_spec(
            spec,
            public_controller_url=public_controller_url,
            controller_token=controller_token,
        )
        spec = self._controller_runtime.with_managed_controller_paths(spec, suite_dir=run_root)
        spec = self._with_runtime_parallelism(
            spec,
            concurrency=concurrency,
            controller_parallelism=controller_parallelism,
        )
        resolved_public_url = public_controller_url or spec.controller.public_url
        submitter_url = self._path_resolver.submitter_controller_url(spec, public_url=resolved_public_url)
        materialized = self._persistence.materialized_from_persisted(spec=spec, manifest=manifest, output_dir=run_root)
        has_real_provider = self._preflight.has_real_provider(manifest)
        self._preflight.validate(spec, manifest=manifest, public_url=resolved_public_url, dry_run=dry_run)
        effective_prepared_policy = prepared_sandbox_policy or self._persisted_prepared_policy(
            batch_dir=run_root / "batch" / run_id
        )
        self._validate_resume_runtime_config(
            materialized=materialized,
            public_controller_url=resolved_public_url,
            submitter_controller_url=submitter_url,
            dry_run=dry_run,
            concurrency=concurrency,
            sandbox_create_concurrency=sandbox_create_concurrency,
            controller_submit_concurrency=controller_submit_concurrency,
            controller_status_concurrency=controller_status_concurrency,
            controller_submit_jitter_seconds=controller_submit_jitter_seconds,
            device_readiness_timeout_seconds=device_readiness_timeout_seconds,
            lease_timeout_seconds=lease_timeout_seconds,
            infra_retry_backoff_seconds=infra_retry_backoff_seconds,
            prepared_sandbox_policy=effective_prepared_policy,
            sqlite_path=sqlite_path,
        )
        if not dry_run:
            cleanup_started_at = time.time()
            await _run_pre_resume_cleanup(
                batch_id=run_id,
                output_dir=run_root / "batch",
                controller_db=materialized.spec.controller.db_path or run_root / "controller.sqlite",
                provider=_resolve_pre_resume_cleanup_provider(manifest=manifest),
                run_concurrency=concurrency or materialized.spec.concurrency,
                prepared_sandbox_policy=effective_prepared_policy,
            )
            self._assert_fresh_pre_resume_cleanup_summary(
                batch_dir=run_root / "batch" / run_id,
                started_at_unix=cleanup_started_at,
            )
            self._assert_clean_case_resume_barrier(sqlite_path)
        self._clear_resume_control_flags(sqlite_path)
        return await self._execute_materialized_run(
            materialized=materialized,
            public_controller_url=resolved_public_url,
            submitter_controller_url=submitter_url,
            dry_run=dry_run,
            has_real_provider=has_real_provider,
            concurrency=concurrency,
            sandbox_create_concurrency=sandbox_create_concurrency,
            controller_submit_concurrency=controller_submit_concurrency,
            controller_status_concurrency=controller_status_concurrency,
            controller_submit_jitter_seconds=controller_submit_jitter_seconds,
            device_readiness_timeout_seconds=device_readiness_timeout_seconds,
            lease_timeout_seconds=lease_timeout_seconds,
            infra_retry_backoff_seconds=infra_retry_backoff_seconds,
            prepared_sandbox_policy=effective_prepared_policy,
            persist_materialized=False,
        )

    @staticmethod
    def _validate_resume_runtime_config(
        *,
        materialized: MaterializedCaseSuite,
        public_controller_url: str,
        submitter_controller_url: str,
        dry_run: bool,
        concurrency: int,
        sandbox_create_concurrency: int,
        controller_submit_concurrency: int,
        controller_status_concurrency: int,
        controller_submit_jitter_seconds: float,
        device_readiness_timeout_seconds: float,
        lease_timeout_seconds: float,
        infra_retry_backoff_seconds: list[float] | None,
        prepared_sandbox_policy: PreparedSandboxPolicy | None,
        sqlite_path: Path,
    ) -> None:
        """Validate persisted case-run runtime identity before clearing pause.

        Args:
            materialized: Persisted case suite and batch manifest.
            public_controller_url: Public URL passed to device workers.
            submitter_controller_url: URL used by the local submitter.
            dry_run: Whether the resume is dry-run only.
            concurrency: Task concurrency override requested.
            sandbox_create_concurrency: Sandbox create throttle requested.
            controller_submit_concurrency: Controller submit throttle requested.
            controller_status_concurrency: Controller status throttle requested.
            controller_submit_jitter_seconds: Submit jitter requested.
            device_readiness_timeout_seconds: Device worker readiness timeout
                requested.
            lease_timeout_seconds: Local task lease timeout requested.
            infra_retry_backoff_seconds: Retry backoff sequence requested for
                retryable infra failures.
            prepared_sandbox_policy: CLI override for prepared queue policy.
            sqlite_path: Existing SQLite control-plane database path.

        Raises:
            ValueError: If the requested resume changes persisted runtime
                identity such as controller URLs or prepared queue depth.
        """

        config = BatchRunConfig(
            manifest=materialized.manifest,
            output_dir=materialized.output_dir / "batch",
            controller_url=submitter_controller_url,
            device_controller_url=public_controller_url,
            token=materialized.spec.controller.token,
            concurrency=concurrency or materialized.spec.concurrency,
            sandbox_create_concurrency=sandbox_create_concurrency,
            controller_submit_concurrency=controller_submit_concurrency,
            controller_status_concurrency=controller_status_concurrency,
            controller_submit_jitter_seconds=controller_submit_jitter_seconds,
            device_readiness_timeout_seconds=(
                device_readiness_timeout_seconds or DEFAULT_DEVICE_READINESS_TIMEOUT_SECONDS
            ),
            lease_timeout_seconds=lease_timeout_seconds or BatchRunConfig.model_fields["lease_timeout_seconds"].default,
            infra_retry_backoff_seconds=list(infra_retry_backoff_seconds or []),
            prepared_sandbox_policy=prepared_sandbox_policy or materialized.spec.prepared_sandbox_policy,
            dry_run=dry_run,
            persist_token=False,
            telemetry_experiment_id=materialized.spec.suite_id,
            telemetry_controller_db_path=materialized.spec.controller.db_path,
        )
        validate_existing_run_config(
            output_dir=materialized.output_dir / "batch" / materialized.manifest.batch_id,
            config=config,
            sqlite_state_path=sqlite_path,
        )

    @staticmethod
    def _persisted_prepared_policy(*, batch_dir: Path) -> PreparedSandboxPolicy | None:
        run_config_path = batch_dir / "run_config.json"
        if not run_config_path.exists():
            return None
        return BatchRunResumeConfig.model_validate_json(
            run_config_path.read_text(encoding="utf-8")
        ).prepared_sandbox_policy

    @staticmethod
    def _clear_resume_control_flags(sqlite_path: Path) -> None:
        """Allow case-suite resumes to claim work after a prior pause or drain."""

        repository = SQLiteBatchStateRepository(sqlite_path)
        repository.set_control_flag("paused", False)
        repository.set_control_flag("drain_requested", False)

    @staticmethod
    def _assert_fresh_pre_resume_cleanup_summary(*, batch_dir: Path, started_at_unix: float) -> None:
        summary_path = batch_dir / "pre_resume_cleanup_summary.json"
        if not summary_path.exists():
            raise RuntimeError(f"case resume pre-clean did not write summary: {summary_path}")
        try:
            summary = json.loads(summary_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"case resume pre-clean summary is not valid JSON: {summary_path}") from exc
        if not isinstance(summary, dict):
            raise RuntimeError(f"case resume pre-clean summary is not an object: {summary_path}")
        summary_started = float(summary.get("started_at_unix") or 0.0)
        summary_finished = float(summary.get("finished_at_unix") or 0.0)
        if summary_started + 1.0 < started_at_unix or summary_finished + 1.0 < started_at_unix:
            raise RuntimeError(
                f"case resume pre-clean summary is stale; refusing to start runner: summary={summary_path}"
            )
        if summary.get("passed") is not True:
            raise RuntimeError(
                "case resume pre-clean did not pass; refusing to start runner: "
                f"summary={summary_path} errors={summary.get('errors')}"
            )

    @staticmethod
    def _assert_clean_case_resume_barrier(sqlite_path: Path) -> None:
        active_statuses = (
            BatchTaskStatus.SUBMITTED.value,
            BatchTaskStatus.RUNNING.value,
            BatchTaskStatus.STARTING_SANDBOX.value,
            BatchTaskStatus.BOOTSTRAPPING_DEVICE.value,
            BatchTaskStatus.WAITING_DEVICE.value,
        )
        placeholders = ",".join("?" for _ in active_statuses)
        with sqlite3.connect(str(sqlite_path)) as connection:
            rows = connection.execute(
                f"""
                SELECT
                    status,
                    COUNT(*) AS count,
                    SUM(CASE WHEN COALESCE(lease_token, '')<>'' THEN 1 ELSE 0 END) AS lease_count,
                    SUM(CASE WHEN COALESCE(controller_task_id, '')<>'' THEN 1 ELSE 0 END) AS controller_count
                FROM batch_tasks
                WHERE status IN ({placeholders})
                GROUP BY status
                ORDER BY status
                """,
                active_statuses,
            ).fetchall()
            if not rows:
                return
            sample = connection.execute(
                f"""
                SELECT task_id, status, attempts, COALESCE(lease_owner, '') AS lease_owner,
                       CASE WHEN COALESCE(lease_token, '')<>'' THEN 1 ELSE 0 END AS has_lease,
                       COALESCE(controller_task_id, '') AS controller_task_id
                FROM batch_tasks
                WHERE status IN ({placeholders})
                ORDER BY updated_at DESC, order_index ASC
                LIMIT 5
                """,
                active_statuses,
            ).fetchall()
        counts = [
            {
                "status": str(status),
                "count": int(count or 0),
                "lease_count": int(lease_count or 0),
                "controller_count": int(controller_count or 0),
            }
            for status, count, lease_count, controller_count in rows
        ]
        sample_rows = [
            {
                "task_id": str(task_id),
                "status": str(status),
                "attempts": int(attempts or 0),
                "lease_owner": str(lease_owner)[:64],
                "has_lease": bool(has_lease),
                "controller_task_id": str(controller_task_id)[:64],
            }
            for task_id, status, attempts, lease_owner, has_lease, controller_task_id in sample
        ]
        raise RuntimeError(
            "case resume pre-clean left active batch rows; refusing to start runner: "
            f"counts={counts} sample={sample_rows}"
        )

    async def _execute_materialized_run(
        self,
        *,
        materialized: MaterializedCaseSuite,
        public_controller_url: str,
        submitter_controller_url: str,
        dry_run: bool,
        has_real_provider: bool,
        concurrency: int,
        sandbox_create_concurrency: int,
        controller_submit_concurrency: int,
        controller_status_concurrency: int,
        controller_submit_jitter_seconds: float,
        device_readiness_timeout_seconds: float,
        lease_timeout_seconds: float,
        infra_retry_backoff_seconds: list[float] | None,
        prepared_sandbox_policy: PreparedSandboxPolicy | None,
        persist_materialized: bool,
    ) -> CaseRunResult:
        effective_concurrency = concurrency or materialized.spec.concurrency
        effective_prepared_policy = prepared_sandbox_policy or materialized.spec.prepared_sandbox_policy
        validate_prepared_sandbox_allocations(
            manifest=materialized.manifest,
            policy=effective_prepared_policy,
        )
        managed_controller = await self._controller_runtime.start(
            materialized.spec,
            suite_dir=materialized.output_dir,
            dry_run=dry_run,
            has_real_provider=has_real_provider,
        )
        try:
            if persist_materialized:
                persisted_spec = materialized.spec
                if prepared_sandbox_policy is not None:
                    persisted_spec = materialized.spec.model_copy(
                        update={"prepared_sandbox_policy": effective_prepared_policy}
                    )
                self._persistence.persist_materialized(
                    materialized,
                    persisted_spec=self._controller_runtime.spec_for_persistence(persisted_spec),
                )
            config = BatchRunConfig(
                manifest=materialized.manifest,
                output_dir=materialized.output_dir / "batch",
                controller_url=submitter_controller_url,
                device_controller_url=public_controller_url,
                token=materialized.spec.controller.token,
                concurrency=effective_concurrency,
                sandbox_create_concurrency=sandbox_create_concurrency,
                controller_submit_concurrency=controller_submit_concurrency,
                controller_status_concurrency=controller_status_concurrency,
                controller_submit_jitter_seconds=controller_submit_jitter_seconds,
                device_readiness_timeout_seconds=(
                    device_readiness_timeout_seconds or DEFAULT_DEVICE_READINESS_TIMEOUT_SECONDS
                ),
                lease_timeout_seconds=lease_timeout_seconds
                or BatchRunConfig.model_fields["lease_timeout_seconds"].default,
                infra_retry_backoff_seconds=list(infra_retry_backoff_seconds or []),
                prepared_sandbox_policy=effective_prepared_policy,
                dry_run=dry_run,
                persist_token=False,
                telemetry_experiment_id=materialized.spec.suite_id,
                telemetry_controller_db_path=materialized.spec.controller.db_path,
            )
            runner = self._batch_runner or CaseBatchRunnerFactory().build(
                materialized.spec,
                dry_run=dry_run,
                has_real_provider=has_real_provider,
            )
            batch_result = await runner.run(config)
            rewrite_batch_summary_for_case_dispositions(materialized=materialized)
            CaseReportWriter(materialized.output_dir).write(
                materialized=materialized,
                batch_result=batch_result,
                artifact_results=[],
                dry_run=dry_run,
            )
            artifact_results = CaseTraceVerifier().verify(
                output_dir=materialized.output_dir,
                spec=materialized.spec,
                batch_result=batch_result,
                extra_roots=self._path_resolver.extra_artifact_roots(materialized.spec),
            )
            batch_result = apply_trace_gate_to_batch_result(
                batch_result=batch_result,
                artifact_results=artifact_results,
                dry_run=dry_run,
            )
            rewrite_batch_summary_for_trace_gate(
                batch_dir=Path(batch_result.output_dir),
                batch_result=batch_result,
                artifact_results=artifact_results,
                dry_run=dry_run,
            )
            batch_result = apply_expected_feasibility_to_batch_result(
                materialized=materialized,
                batch_result=batch_result,
            )
            rewrite_batch_summary_for_expected_feasibility(
                batch_dir=Path(batch_result.output_dir),
                batch_result=batch_result,
            )
            batch_result = apply_post_run_acceptance_to_batch_result(
                materialized=materialized,
                batch_result=batch_result,
                dry_run=dry_run,
            )
            rewrite_batch_summary_for_post_run_acceptance(
                materialized=materialized,
                batch_result=batch_result,
                dry_run=dry_run,
            )
            CaseTelemetryReportEnricher(Path(batch_result.output_dir)).rebuild(artifact_results)
            case_report_path = CaseReportWriter(materialized.output_dir).write(
                materialized=materialized,
                batch_result=batch_result,
                artifact_results=artifact_results,
                dry_run=dry_run,
            )
            return CaseRunResult(
                suite_id=materialized.spec.suite_id,
                run_id=batch_result.batch_id,
                run_mode=CaseRunMode.DRY_RUN if dry_run else CaseRunMode.LIVE,
                dry_run=dry_run,
                total=len(materialized.cases),
                succeeded=batch_result.succeeded,
                failed=batch_result.failed,
                canceled=batch_result.canceled,
                incomplete=batch_result.incomplete,
                output_dir=str(materialized.output_dir),
                batch_result=batch_result,
                case_report_path=str(case_report_path),
                batch_manifest_path=str(materialized.output_dir / "batch_manifest.json"),
                runnable=sum(1 for item in materialized.cases if item.case.disposition.is_runnable),
                blocked=sum(1 for item in materialized.cases if not item.case.disposition.is_runnable),
            )
        finally:
            if managed_controller is not None:
                await managed_controller.stop()

    @staticmethod
    def _with_runtime_parallelism(
        spec: CaseSuiteSpec,
        *,
        concurrency: int,
        controller_parallelism: int,
    ) -> CaseSuiteSpec:
        """Apply resume/new-run parallelism overrides to a case suite spec."""

        updates: dict[str, object] = {}
        if concurrency > 0:
            updates["concurrency"] = concurrency
        if controller_parallelism > 0:
            updates["controller"] = spec.controller.model_copy(
                update={
                    "parallelism": controller_parallelism,
                    "runner_workers": controller_parallelism,
                    "worker_queue_max": controller_parallelism,
                }
            )
        if not updates:
            return spec
        return spec.model_copy(update=updates)

    def _materializer_for(self, catalog_path: str | Path | None) -> CaseMaterializer:
        if self._materializer is not None and catalog_path is None:
            return self._materializer
        return CaseMaterializer(catalog=CaseCatalogLoader().load(catalog_path))

    def _prepare_spec(
        self,
        *,
        spec_path: str | Path,
        output_dir: Path,
        public_controller_url: str,
        run_id: str = "",
        controller_token: str = "",
    ) -> _PreparedCaseSuite:
        resolved = Path(spec_path).expanduser().resolve()
        spec = load_case_suite(resolved)
        if run_id:
            self._validate_run_id(run_id)
        suite_dir = output_dir / spec.suite_id / run_id if run_id else output_dir / spec.suite_id
        spec = self._controller_runtime.effective_spec(
            spec,
            public_controller_url=public_controller_url,
            controller_token=controller_token,
        )
        spec = self._controller_runtime.with_managed_controller_paths(spec, suite_dir=suite_dir)
        public_url = public_controller_url or spec.controller.public_url
        submitter_url = self._path_resolver.submitter_controller_url(spec, public_url=public_url)
        return _PreparedCaseSuite(
            spec_path=resolved,
            spec=spec,
            suite_dir=suite_dir,
            public_controller_url=public_url,
            submitter_controller_url=submitter_url,
            run_id=run_id,
        )

    @staticmethod
    def _new_run_id(suite_id: str) -> str:
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        return f"{suite_id}-{timestamp}-{uuid.uuid4().hex[:8]}"

    @staticmethod
    def _validate_run_id(run_id: str) -> None:
        validate_case_run_id(run_id)

    @staticmethod
    def _check_run_dir_available(run_dir: Path, *, force: bool) -> None:
        if not run_dir.exists():
            return
        if force:
            return
        raise FileExistsError(f"case run directory already exists: {run_dir}; pass --resume or --force")

    def _preflight_force_run(
        self,
        *,
        materializer: CaseMaterializer,
        prepared: _PreparedCaseSuite,
        dry_run: bool,
    ) -> None:
        """Validate a forced run in a temporary output tree before deletion."""

        with tempfile.TemporaryDirectory(prefix="northcua-case-force-preflight-") as temp_root:
            preview = materializer.materialize(
                prepared.spec,
                output_dir=Path(temp_root),
                spec_dir=prepared.spec_path.parent,
                controller_url=prepared.public_controller_url,
                submitter_controller_url=prepared.submitter_controller_url,
                run_id=prepared.run_id,
            )
            self._preflight.validate(
                prepared.spec,
                manifest=preview.manifest,
                public_url=prepared.public_controller_url,
                dry_run=dry_run,
            )
