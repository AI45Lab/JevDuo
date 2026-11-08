"""Application operations backing batch CLI commands."""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import time
from collections.abc import Callable, Sequence
from pathlib import Path

from northcua.batch.contracts import (
    BatchManifest,
    BatchRunConfig,
    BatchRunResult,
    BatchRunResumeConfig,
    BatchTaskState,
    BatchTaskStatus,
)
from northcua.batch.maintenance.contracts import MaintenancePlanItem
from northcua.batch.maintenance.operations import BatchMaintenanceOperations
from northcua.batch.manifest.loader import load_batch_manifest
from northcua.batch.preparation import PreparedSandboxPolicy
from northcua.batch.queue import BatchStateStore
from northcua.batch.reporting import _merge_persisted_prepared_metrics
from northcua.batch.runner.service import BatchRunner
from northcua.batch.selected_retry import SelectedRetryPlan, apply_selected_retry_plan, load_selected_retry_plan
from northcua.batch.state import SQLiteBatchStateRepository
from northcua.sandboxes.contracts import DEFAULT_DEVICE_READINESS_TIMEOUT_SECONDS
from northcua.telemetry.report_sources import CaseSnapshotSource
from northcua.telemetry.reporting import TelemetryReportWriter
from northcua_protocol.capabilities import JsonObject

BatchRunnerFactory = Callable[[], BatchRunner]


class BatchOperations:
    """Product-facing batch operations independent from argparse."""

    def __init__(self, runner_factory: BatchRunnerFactory | None = None) -> None:
        self._runner_factory = runner_factory or BatchRunner

    def validate_manifest(self, manifest_path: str | Path) -> BatchManifest:
        """Load and validate a batch manifest.

        Args:
            manifest_path: YAML, JSON, or JSONL manifest path.

        Returns:
            Parsed manifest with aliases normalized and task ids validated.
        """

        return load_batch_manifest(manifest_path)

    async def run(
        self,
        *,
        manifest_path: str | Path,
        output_dir: Path,
        controller_url: str = "",
        device_controller_url: str = "",
        token: str = "",
        concurrency: int = 0,
        sandbox_create_concurrency: int = 0,
        controller_submit_concurrency: int = 0,
        controller_status_concurrency: int = 0,
        controller_submit_jitter_seconds: float = 0.0,
        device_readiness_timeout_seconds: float = 0.0,
        infra_retry_backoff_seconds: list[float] | None = None,
        prepared_sandbox_policy: PreparedSandboxPolicy | None = None,
        dry_run: bool = False,
        persist_token: bool = False,
    ) -> BatchRunResult:
        """Execute a manifest-driven batch run.

        Args:
            manifest_path: YAML, JSON, or JSONL manifest path.
            output_dir: Root directory for state, reports, and sandbox logs.
            controller_url: Optional controller websocket URL override.
            device_controller_url: Optional public Controller URL passed to sandbox device workers.
            token: Optional submitter/device token override.
            concurrency: Optional maximum number of tasks to run in parallel.
            sandbox_create_concurrency: Optional cap for concurrent sandbox create calls.
            controller_submit_concurrency: Optional cap for concurrent
                controller submit websocket openings.
            controller_status_concurrency: Optional cap for concurrent
                controller status websocket requests.
            controller_submit_jitter_seconds: Optional deterministic per-task
                delay inside the submit cap.
            device_readiness_timeout_seconds: Optional device worker readiness
                timeout override.
            infra_retry_backoff_seconds: Optional retry backoff sequence for
                retryable infra failures.
            prepared_sandbox_policy: Optional single-use prepared sandbox queue
                policy. Disabled when omitted.
            dry_run: Whether to validate and plan without creating sandboxes.

        Returns:
            Final batch summary and latest task states.
        """

        manifest = load_batch_manifest(manifest_path)
        return await self._runner_factory().run(
            BatchRunConfig(
                manifest=manifest,
                output_dir=output_dir,
                controller_url=controller_url,
                device_controller_url=device_controller_url,
                token=token,
                concurrency=concurrency,
                sandbox_create_concurrency=sandbox_create_concurrency,
                controller_submit_concurrency=controller_submit_concurrency,
                controller_status_concurrency=controller_status_concurrency,
                controller_submit_jitter_seconds=controller_submit_jitter_seconds,
                device_readiness_timeout_seconds=(
                    device_readiness_timeout_seconds or DEFAULT_DEVICE_READINESS_TIMEOUT_SECONDS
                ),
                infra_retry_backoff_seconds=list(infra_retry_backoff_seconds or []),
                prepared_sandbox_policy=prepared_sandbox_policy or PreparedSandboxPolicy(),
                dry_run=dry_run,
                persist_token=persist_token,
            )
        )

    def status(self, *, batch_id: str, output_dir: Path) -> list[BatchTaskState]:
        """Read latest local task states for a batch.

        Args:
            batch_id: Batch output namespace to inspect.
            output_dir: Root directory containing batch state directories.

        Returns:
            Latest persisted state for every task known locally.
        """

        sqlite_path = self._sqlite_path(batch_id=batch_id, output_dir=output_dir)
        if sqlite_path.exists():
            return SQLiteBatchStateRepository(sqlite_path).all()
        return BatchStateStore(self._state_path(batch_id=batch_id, output_dir=output_dir)).all()

    def report(self, *, batch_id: str, output_dir: Path) -> JsonObject:
        """Read or synthesize a batch report.

        Args:
            batch_id: Batch output namespace to inspect.
            output_dir: Root directory containing reports and JSONL state.

        Returns:
            Stored ``batch_summary.json`` content, or a minimal report derived
            from local task state when no summary has been written yet.
        """

        batch_dir = output_dir / batch_id
        sqlite_path = self._sqlite_path(batch_id=batch_id, output_dir=output_dir)
        if sqlite_path.exists():
            repository = SQLiteBatchStateRepository(sqlite_path)
            states = repository.all()
            telemetry_summary = TelemetryReportWriter(batch_dir, state_repository=repository).write()
            report_path = batch_dir / "batch_summary.json"
            existing: JsonObject = {}
            if report_path.exists():
                parsed = json.loads(report_path.read_text(encoding="utf-8"))
                existing = parsed if isinstance(parsed, dict) else {"report": parsed}
            result = _result_from_states(batch_id=batch_id, output_dir=batch_dir, states=states)
            raw_telemetry = existing.get("telemetry")
            existing_telemetry: JsonObject = dict(raw_telemetry) if isinstance(raw_telemetry, dict) else {}
            existing_metrics = _metrics_payload(existing.get("metrics"))
            payload: JsonObject = {
                **existing,
                **result.model_dump(mode="json"),
                "metrics": _merge_persisted_prepared_metrics(batch_dir, existing_metrics),
                "telemetry": {
                    **existing_telemetry,
                    "summary_path": str(batch_dir / "telemetry_summary.json"),
                    "case_metrics_path": str(batch_dir / "case_metrics.jsonl"),
                    "case_timelines_path": str(batch_dir / "case_timelines.jsonl"),
                    "failure_breakdown_path": str(batch_dir / "failure_breakdown.json"),
                    "audit_report_path": str(batch_dir / "audit_report.md"),
                    "summary": telemetry_summary.model_dump(mode="json"),
                },
            }
            return payload
        report_path = batch_dir / "batch_summary.json"
        if report_path.exists():
            parsed = json.loads(report_path.read_text(encoding="utf-8"))
            return parsed if isinstance(parsed, dict) else {"report": parsed}
        states = [state.model_dump(mode="json") for state in self.status(batch_id=batch_id, output_dir=output_dir)]
        return {"batch_id": batch_id, "task_states": states}

    def monitor(self, *, batch_id: str, output_dir: Path) -> JsonObject:
        """Read local telemetry snapshots and summary for a batch.

        Args:
            batch_id: Batch output namespace to inspect.
            output_dir: Root directory containing reports and telemetry JSONL.

        Returns:
            Operator-friendly telemetry payload for CLI or future UI adapters.
        """

        batch_dir = output_dir / batch_id
        sqlite_path = self._sqlite_path(batch_id=batch_id, output_dir=output_dir)
        repository = SQLiteBatchStateRepository(sqlite_path) if sqlite_path.exists() else None
        if repository is not None:
            TelemetryReportWriter(batch_dir, state_repository=repository).write()
        summary_path = batch_dir / "telemetry_summary.json"
        snapshots_path = batch_dir / "case_snapshots.jsonl"
        summary: object = {}
        latest_snapshots = CaseSnapshotSource(output_dir=batch_dir, state_repository=repository).latest()
        latest_by_case: dict[str, object] = {
            case_id: snapshot.model_dump(mode="json") for case_id, snapshot in latest_snapshots.items()
        }
        if not latest_by_case and snapshots_path.exists():
            for line in snapshots_path.read_text(encoding="utf-8").splitlines():
                if not line.strip():
                    continue
                snapshot = json.loads(line)
                if isinstance(snapshot, dict):
                    latest_by_case[str(snapshot.get("case_id", ""))] = snapshot
        if summary_path.exists():
            summary = json.loads(summary_path.read_text(encoding="utf-8"))
        else:
            summary = _live_summary(list(latest_by_case.values()))
        existing_metrics: dict[str, float | int | dict[str, int]] = {}
        report_path = batch_dir / "batch_summary.json"
        if report_path.exists():
            parsed = json.loads(report_path.read_text(encoding="utf-8"))
            if isinstance(parsed, dict):
                existing_metrics = _metrics_payload(parsed.get("metrics"))
        return {
            "batch_id": batch_id,
            "batch_dir": str(batch_dir),
            "summary": summary,
            "metrics": _merge_persisted_prepared_metrics(batch_dir, existing_metrics),
            "case_snapshots": list(latest_by_case.values()),
            "telemetry_paths": {
                "events": str(batch_dir / "telemetry_events.jsonl"),
                "snapshots": str(snapshots_path),
                "timelines": str(batch_dir / "case_timelines.jsonl"),
                "metrics": str(batch_dir / "case_metrics.jsonl"),
                "audit_report": str(batch_dir / "audit_report.md"),
            },
        }

    async def resume(
        self,
        *,
        batch_id: str,
        output_dir: Path,
        controller_url: str = "",
        token: str = "",
        concurrency: int = 0,
        sandbox_create_concurrency: int = 0,
        controller_submit_concurrency: int = 0,
        controller_status_concurrency: int = 0,
        controller_submit_jitter_seconds: float = 0.0,
        device_readiness_timeout_seconds: float = 0.0,
        infra_retry_backoff_seconds: list[float] | None = None,
        controller_db: Path | None = None,
        prepared_sandbox_policy: PreparedSandboxPolicy | None = None,
        pre_resume_cleanup: bool = True,
        pre_resume_cleanup_provider: str = "",
        selected_retry_plan_paths: Sequence[Path] = (),
    ) -> BatchRunResult:
        """Resume a batch from its manifest snapshot and local state.

        Args:
            batch_id: Batch output namespace to resume.
            output_dir: Root directory containing the saved manifest snapshot.
            controller_url: Optional resume-time controller URL override.
            token: Optional resume-time submitter/device token override.
            concurrency: Optional resume-time parallelism override.
            sandbox_create_concurrency: Optional resume-time sandbox create cap.
            controller_submit_concurrency: Optional resume-time controller
                submit websocket opening cap.
            controller_status_concurrency: Optional resume-time controller
                status websocket request cap.
            controller_submit_jitter_seconds: Optional resume-time deterministic
                delay inside the submit cap.
            device_readiness_timeout_seconds: Optional resume-time device
                worker readiness timeout.
            infra_retry_backoff_seconds: Optional retry backoff sequence for
                retryable infra failures.
            controller_db: Optional local controller SQLite DB for DB-first
                status reconciliation.
            prepared_sandbox_policy: Optional resume-time prepared queue policy.
            pre_resume_cleanup: Whether to run stale-row repair and controller
                residue cleanup before new submit/prepare work starts.
            pre_resume_cleanup_provider: Sandbox provider id used by cleanup
                inventory; empty uses the persisted manifest default provider.
            selected_retry_plan_paths: Explicit, content-addressed plans that
                grant additional attempts to exact terminal tasks.

        Returns:
            Final batch summary after reconciled or re-run tasks complete.

        Raises:
            FileNotFoundError: If the batch manifest snapshot is missing.
        """

        manifest_path = output_dir / batch_id / "manifest.json"
        if not manifest_path.exists():
            raise FileNotFoundError(f"cannot resume; manifest snapshot not found: {manifest_path}")
        manifest_bytes = manifest_path.read_bytes()
        identity_manifest_sha256 = hashlib.sha256(manifest_bytes).hexdigest()
        identity_manifest = BatchManifest.model_validate_json(manifest_bytes)
        selected_retry_plans = _load_selected_retry_plans(
            selected_retry_plan_paths,
            batch_id=batch_id,
            manifest_sha256=identity_manifest_sha256,
        )
        manifest = identity_manifest
        for plan in selected_retry_plans:
            manifest = apply_selected_retry_plan(manifest, plan)
        existing_resume_config = self._load_resume_config(batch_id=batch_id, output_dir=output_dir)
        resume_config = existing_resume_config.merge_overrides(
            controller_url=controller_url,
            token=token,
            concurrency=concurrency,
            sandbox_create_concurrency=sandbox_create_concurrency,
            controller_submit_concurrency=controller_submit_concurrency,
            controller_status_concurrency=controller_status_concurrency,
            controller_submit_jitter_seconds=controller_submit_jitter_seconds,
            device_readiness_timeout_seconds=device_readiness_timeout_seconds,
            infra_retry_backoff_seconds=infra_retry_backoff_seconds,
            telemetry_controller_db_path=_resume_controller_db_path(
                output_dir=output_dir,
                batch_id=batch_id,
                explicit=controller_db,
                existing=existing_resume_config.telemetry_controller_db_path,
            ),
            prepared_sandbox_policy=prepared_sandbox_policy,
        )
        sqlite_path = self._sqlite_path(batch_id=batch_id, output_dir=output_dir)
        if selected_retry_plans and not sqlite_path.exists():
            raise RuntimeError("selected retry requires an existing SQLite batch state database")
        if sqlite_path.exists():
            _validate_resume_runtime_config(existing=existing_resume_config, requested=resume_config)
            repository = SQLiteBatchStateRepository(sqlite_path)
            if pre_resume_cleanup:
                await _run_pre_resume_cleanup(
                    batch_id=batch_id,
                    output_dir=output_dir,
                    controller_db=(
                        resume_config.telemetry_controller_db_path
                        if resume_config.telemetry_controller_db_path is not None
                        else output_dir.parent / "controller.sqlite"
                    ),
                    provider=_resolve_pre_resume_cleanup_provider(
                        manifest=manifest,
                        explicit_provider=pre_resume_cleanup_provider,
                    ),
                    run_concurrency=_effective_resume_concurrency(manifest=manifest, resume_config=resume_config),
                    prepared_sandbox_policy=resume_config.prepared_sandbox_policy,
                )
            for plan in selected_retry_plans:
                reset = repository.reset_selected_for_retry(plan, actor="batch resume")
                print(
                    "[batch:resume] selected_retry_reset=PASS "
                    f"plan_id={plan.plan_id} selected={reset['selected']} "
                    f"source_failed={reset['source_failed']} source_canceled={reset['source_canceled']} "
                    f"extra_attempts={plan.extra_attempts} "
                    f"already_applied={str(reset['already_applied']).lower()}"
                )
            repository.set_control_flag(
                "paused",
                False,
                actor="batch resume",
                reason="resume clears paused flag after pre-resume cleanup",
            )
            repository.set_control_flag(
                "drain_requested",
                False,
                actor="batch resume",
                reason="resume clears drain flag after pre-resume cleanup",
            )
        return await self._runner_factory().run(
            BatchRunConfig(
                manifest=manifest,
                output_dir=output_dir,
                controller_url=resume_config.controller_url,
                device_controller_url=resume_config.device_controller_url,
                token=resume_config.token,
                concurrency=resume_config.concurrency,
                sandbox_create_concurrency=resume_config.sandbox_create_concurrency,
                controller_submit_concurrency=resume_config.controller_submit_concurrency,
                controller_status_concurrency=resume_config.controller_status_concurrency,
                controller_submit_jitter_seconds=resume_config.controller_submit_jitter_seconds,
                device_readiness_timeout_seconds=resume_config.device_readiness_timeout_seconds,
                infra_retry_backoff_seconds=resume_config.infra_retry_backoff_seconds,
                prepared_sandbox_policy=resume_config.prepared_sandbox_policy,
                persist_token=resume_config.persist_token,
                telemetry_experiment_id=resume_config.telemetry_experiment_id,
                telemetry_controller_db_path=resume_config.telemetry_controller_db_path,
                identity_manifest=identity_manifest if selected_retry_plans else None,
                identity_manifest_sha256=identity_manifest_sha256 if selected_retry_plans else "",
            )
        )

    def cancel(self, *, batch_id: str, output_dir: Path) -> Path:
        """Mark a local batch for cooperative cancellation.

        Args:
            batch_id: Batch output namespace to cancel.
            output_dir: Root directory containing the batch state directory.

        Returns:
            Path to the cancellation marker written for the scheduler.
        """

        cancel_path = output_dir / batch_id / "cancel.requested"
        cancel_path.parent.mkdir(parents=True, exist_ok=True)
        cancel_path.write_text("cancel requested\n", encoding="utf-8")
        sqlite_path = self._sqlite_path(batch_id=batch_id, output_dir=output_dir)
        if sqlite_path.exists():
            SQLiteBatchStateRepository(sqlite_path).mark_cancel_requested()
        return cancel_path

    def pause(self, *, batch_id: str, output_dir: Path, actor: str = "", reason: str = "") -> Path:
        """Pause local claiming for a batch without canceling running tasks.

        Args:
            batch_id: Batch output namespace to pause.
            output_dir: Root directory containing the batch state directory.

        Returns:
            Path to the SQLite coordination store that received the flag.

        Raises:
            FileNotFoundError: If the batch SQLite state file does not exist.
        """

        sqlite_path = self._sqlite_path(batch_id=batch_id, output_dir=output_dir)
        if not sqlite_path.exists():
            raise FileNotFoundError(f"batch SQLite state not found: {sqlite_path}")
        SQLiteBatchStateRepository(sqlite_path).set_control_flag("paused", True, actor=actor, reason=reason)
        return sqlite_path

    def drain(self, *, batch_id: str, output_dir: Path, actor: str = "", reason: str = "") -> Path:
        """Stop claiming new tasks while allowing running tasks to finish.

        Args:
            batch_id: Batch output namespace to drain.
            output_dir: Root directory containing the batch state directory.

        Returns:
            Path to the SQLite coordination store that received the flag.

        Raises:
            FileNotFoundError: If the batch SQLite state file does not exist.
        """

        sqlite_path = self._sqlite_path(batch_id=batch_id, output_dir=output_dir)
        if not sqlite_path.exists():
            raise FileNotFoundError(f"batch SQLite state not found: {sqlite_path}")
        SQLiteBatchStateRepository(sqlite_path).set_control_flag("drain_requested", True, actor=actor, reason=reason)
        return sqlite_path

    @staticmethod
    def _state_path(*, batch_id: str, output_dir: Path) -> Path:
        return output_dir / batch_id / "batch_state.jsonl"

    @staticmethod
    def _sqlite_path(*, batch_id: str, output_dir: Path) -> Path:
        return output_dir / batch_id / "batch_state.sqlite"

    @staticmethod
    def _load_resume_config(*, batch_id: str, output_dir: Path) -> BatchRunResumeConfig:
        path = output_dir / batch_id / "run_config.json"
        if not path.exists():
            return BatchRunResumeConfig()
        return BatchRunResumeConfig.model_validate_json(path.read_text(encoding="utf-8"))


def _load_selected_retry_plans(
    paths: Sequence[Path],
    *,
    batch_id: str,
    manifest_sha256: str,
) -> tuple[SelectedRetryPlan, ...]:
    """Load explicit retry plans and reject ambiguous layered selections."""

    canonical_paths = tuple(dict.fromkeys(str(Path(path).expanduser().resolve()) for path in paths))
    plans = tuple(
        load_selected_retry_plan(
            path,
            expected_batch_id=batch_id,
            expected_manifest_sha256=manifest_sha256,
        )
        for path in canonical_paths
    )
    plan_ids = [plan.plan_id for plan in plans]
    if len(plan_ids) != len(set(plan_ids)):
        raise RuntimeError("selected retry plan ids must be unique")
    task_ids = [target.task_id for plan in plans for target in plan.targets]
    if len(task_ids) != len(set(task_ids)):
        raise RuntimeError("a task may appear in only one selected retry plan per resume")
    return plans


def _live_summary(snapshots: list[object]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for snapshot in snapshots:
        if isinstance(snapshot, dict):
            status = str(snapshot.get("status", "unknown"))
            counts[status] = counts.get(status, 0) + 1
    terminal = sum(counts.get(status, 0) for status in ("succeeded", "failed", "timed_out", "canceled"))
    return {
        "total_cases": len(snapshots),
        "running": len(snapshots) - terminal,
        "succeeded": counts.get("succeeded", 0),
        "failed": counts.get("failed", 0),
        "timed_out": counts.get("timed_out", 0),
        "canceled": counts.get("canceled", 0),
    }


def _metrics_payload(value: object) -> dict[str, float | int | dict[str, int]]:
    if not isinstance(value, dict):
        return {}
    metrics: dict[str, float | int | dict[str, int]] = {}
    for key, item in value.items():
        if not isinstance(key, str):
            continue
        if isinstance(item, int | float):
            metrics[key] = item
        elif isinstance(item, dict):
            nested: dict[str, int] = {}
            for nested_key, nested_value in item.items():
                if isinstance(nested_key, str) and isinstance(nested_value, int):
                    nested[nested_key] = nested_value
            metrics[key] = nested
    return metrics


def _validate_resume_runtime_config(*, existing: BatchRunResumeConfig, requested: BatchRunResumeConfig) -> None:
    mismatches: list[str] = []
    allow_controller_url_drift = _allow_controller_url_runtime_drift()
    for field in (
        "controller_url",
        "device_controller_url",
        "telemetry_experiment_id",
    ):
        if getattr(existing, field) != getattr(requested, field):
            if allow_controller_url_drift and field in {"controller_url", "device_controller_url"}:
                continue
            mismatches.append(field)
    if existing.telemetry_controller_db_path is not None:
        if existing.telemetry_controller_db_path != requested.telemetry_controller_db_path:
            mismatches.append("telemetry_controller_db_path")
    if existing.token_fingerprint:
        if existing.token_fingerprint != requested.token_fingerprint:
            mismatches.append("token_fingerprint")
    if existing.token and requested.token:
        if existing.token != requested.token:
            mismatches.append("token")
    if _prepared_policy_identity(existing) != _prepared_policy_identity(requested):
        mismatches.append("prepared_sandbox_policy")
    if mismatches:
        raise ValueError(
            "batch output directory already has SQLite state for a different runtime configuration: "
            + ", ".join(sorted(set(mismatches)))
        )


def _prepared_policy_identity(config: BatchRunResumeConfig) -> dict[str, object]:
    policy = config.prepared_sandbox_policy
    if not policy.enabled:
        return {"enabled": False}
    return {"enabled": policy.enabled, "depth": policy.depth.value}


def _allow_controller_url_runtime_drift() -> bool:
    return os.getenv("NORTHCUA_ALLOW_CONTROLLER_URL_RUNTIME_DRIFT", "").strip().lower() in {"1", "true", "yes"}


def _resume_controller_db_path(
    *,
    output_dir: Path,
    batch_id: str,
    explicit: Path | None,
    existing: Path | None,
) -> Path | None:
    if explicit is not None:
        return explicit
    if existing is not None:
        return existing
    for candidate in (output_dir.parent / "controller.sqlite", output_dir / batch_id / "controller.sqlite"):
        if candidate.exists():
            return candidate
    return None


async def _run_pre_resume_cleanup(
    *,
    batch_id: str,
    output_dir: Path,
    controller_db: Path,
    provider: str,
    run_concurrency: int = 0,
    prepared_sandbox_policy: PreparedSandboxPolicy | None = None,
    maintenance: BatchMaintenanceOperations | None = None,
) -> None:
    batch_db = output_dir / batch_id / "batch_state.sqlite"
    if not batch_db.exists():
        return
    maintenance = maintenance or BatchMaintenanceOperations()
    summary: JsonObject = {
        "batch_id": batch_id,
        "batch_db": str(batch_db),
        "controller_db": str(controller_db),
        "provider": provider,
        "started_at_unix": time.time(),
        "passed": False,
        "errors": [],
    }

    def abort(message: str) -> None:
        summary["passed"] = False
        summary["finished_at_unix"] = time.time()
        summary["errors"] = [*_json_string_list(summary.get("errors")), message]
        _write_pre_resume_cleanup_summary(batch_db, summary)
        raise RuntimeError(message)

    try:
        _ensure_pre_resume_cleanup_indexes(batch_db)
        if controller_db.exists():
            pre_resume_repair = await maintenance.repair_pre_resume_stale(
                batch_id=batch_id,
                output_dir=output_dir,
                controller_db=controller_db,
                older_than_seconds=300.0,
                apply=True,
                include_active_leases=True,
            )
            summary["pre_resume_repair"] = _maintenance_result_summary(pre_resume_repair)
            if pre_resume_repair.errors:
                abort(f"pre-resume DB stale repair failed: {pre_resume_repair.errors}")
        repair = await maintenance.repair_stale(
            batch_id=batch_id,
            output_dir=output_dir,
            older_than_seconds=300.0,
            apply=True,
        )
        summary["stale_repair"] = _maintenance_result_summary(repair)
        if repair.errors:
            abort(f"pre-resume repair-stale failed: {repair.errors}")
        expired_prepared = _expire_unassigned_prepared_for_clean_resume(batch_db)
        summary["prepared_unassigned_expired_for_resume"] = expired_prepared
        prepared_cleanup_needed = _prepared_cleanup_needed(batch_db)
        summary["prepared_cleanup_needed"] = prepared_cleanup_needed
        if prepared_cleanup_needed:
            _ensure_prepared_indexes(batch_db)
            sandbox_cleanup = await maintenance.terminate_sandboxes(
                batch_id=batch_id,
                output_dir=output_dir,
                provider=provider,
                prepared_only=True,
                terminate_concurrency=8,
                apply=True,
            )
            summary["prepared_sandbox_cleanup"] = _maintenance_result_summary(sandbox_cleanup)
            if sandbox_cleanup.errors:
                abort(f"pre-resume prepared sandbox cleanup failed: {sandbox_cleanup.errors}")
        if controller_db.exists():
            cleanup = await maintenance.cleanup_controller_residue(
                batch_id=batch_id,
                output_dir=output_dir,
                batch_db=batch_db,
                controller_db=controller_db,
                protect_prepared=True,
                provider=provider,
                apply=True,
            )
            summary["controller_residue_cleanup"] = cleanup
            cleanup_failure = _controller_residue_cleanup_blocker(cleanup)
            if cleanup_failure:
                abort(f"pre-resume cleanup failed: {cleanup_failure}")
            post_cleanup_repair = await maintenance.repair_pre_resume_stale(
                batch_id=batch_id,
                output_dir=output_dir,
                controller_db=controller_db,
                older_than_seconds=300.0,
                apply=True,
                include_active_leases=True,
            )
            summary["post_cleanup_repair"] = _maintenance_result_summary(post_cleanup_repair)
            if post_cleanup_repair.errors:
                abort(f"post-cleanup pre-resume DB stale repair failed: {post_cleanup_repair.errors}")
        provider_sweep = await maintenance.sweep_provider_orphans(
            batch_id=batch_id,
            output_dir=output_dir,
            provider=provider,
            terminate_concurrency=8,
            apply=True,
        )
        summary["provider_orphan_sweep"] = _maintenance_result_summary(provider_sweep)
        if provider_sweep.errors:
            abort(f"pre-resume provider orphan sweep failed: {provider_sweep.errors}")
        provider_sweep_verify = await maintenance.sweep_provider_orphans(
            batch_id=batch_id,
            output_dir=output_dir,
            provider=provider,
            terminate_concurrency=8,
            apply=False,
        )
        summary["provider_orphan_sweep_verify"] = _maintenance_result_summary(provider_sweep_verify)
        if provider_sweep_verify.errors:
            abort(f"pre-resume provider orphan sweep verify failed: {provider_sweep_verify.errors}")
        if int(provider_sweep_verify.planned_count or 0) > 0:
            abort(
                "pre-resume provider orphan sweep left provider candidates after cleanup: "
                f"{summary['provider_orphan_sweep_verify']}"
            )
        if controller_db.exists() and int(provider_sweep.applied_count or 0) > 0:
            post_provider_cleanup = await maintenance.cleanup_controller_residue(
                batch_id=batch_id,
                output_dir=output_dir,
                batch_db=batch_db,
                controller_db=controller_db,
                protect_prepared=True,
                provider=provider,
                apply=True,
            )
            summary["post_provider_orphan_controller_cleanup"] = post_provider_cleanup
            provider_cleanup_failure = _controller_residue_cleanup_blocker(post_provider_cleanup)
            if provider_cleanup_failure:
                abort(f"post-provider-orphan controller cleanup failed: {provider_cleanup_failure}")
        remaining_actions: list[MaintenancePlanItem] = []
        if controller_db.exists():
            post_repair = await maintenance.repair_pre_resume_stale(
                batch_id=batch_id,
                output_dir=output_dir,
                controller_db=controller_db,
                older_than_seconds=300.0,
                apply=False,
                include_active_leases=True,
            )
            remaining_actions = _pre_resume_blocking_actions(post_repair.plan.items)
            summary["post_repair_verify"] = _maintenance_result_summary(post_repair)
        prepared_summary = _prepared_live_budget_summary(
            batch_db,
            prepared_live_limit=_prepared_live_limit(
                run_concurrency=run_concurrency,
                policy=prepared_sandbox_policy,
            ),
        )
        summary["prepared_live_budget"] = prepared_summary
        summary["blocking_actions_count"] = len(remaining_actions)
        if remaining_actions:
            sample = [
                {
                    "kind": item.kind.value,
                    "case_id": item.case_id,
                    "controller_task_id": item.controller_task_id,
                    "reason": item.reason,
                }
                for item in remaining_actions[:5]
            ]
            summary["blocking_actions_sample"] = sample
            abort(
                "pre-resume cleanup left controller/task residue that would collide on resume: "
                f"count={len(remaining_actions)} sample={sample} prepared={prepared_summary}"
            )
        active_leases = _active_lease_summary(batch_db)
        summary["active_leases_after_repair"] = active_leases
        if _json_int(active_leases.get("count")) > 0:
            abort(f"pre-resume cleanup left active runner leases after repair: {active_leases}")
        if _json_int(prepared_summary.get("cleanup_required_with_resource")) > 0:
            abort(f"pre-resume prepared sandbox cleanup debt remains: {prepared_summary}")
        if _json_int(prepared_summary.get("assigned_terminal_with_resource")) > 0:
            abort(f"pre-resume terminal prepared sandbox assignments still own resources: {prepared_summary}")
        if _json_int(prepared_summary.get("live_e2b_budget_overrun")) > 0:
            abort(f"pre-resume prepared sandbox live budget is still over limit after cleanup: {prepared_summary}")
        summary["passed"] = True
        summary["finished_at_unix"] = time.time()
        _write_pre_resume_cleanup_summary(batch_db, summary)
    except Exception as exc:
        if "finished_at_unix" not in summary:
            summary["passed"] = False
            summary["finished_at_unix"] = time.time()
            summary["errors"] = [*_json_string_list(summary.get("errors")), f"{type(exc).__name__}: {exc}"]
            try:
                _write_pre_resume_cleanup_summary(batch_db, summary)
            except Exception:
                pass
        raise


def _pre_resume_blocking_actions(items: Sequence[MaintenancePlanItem]) -> list[MaintenancePlanItem]:
    blockers: list[MaintenancePlanItem] = []
    for item in items:
        kind = item.kind.value
        if kind != "reconcile_controller_task":
            if item.apply_supported and kind != "diagnose":
                blockers.append(item)
            continue
        details = item.details
        snapshot = details.get("controller_snapshot") if isinstance(details, dict) else None
        expected_row = details.get("expected_row") if isinstance(details, dict) else None
        snapshot_state = str(snapshot.get("state", "")) if isinstance(snapshot, dict) else ""
        expected_state = expected_row.get("state", {}) if isinstance(expected_row, dict) else {}
        expected_status = str(expected_state.get("status", "")) if isinstance(expected_state, dict) else ""
        expected_active = expected_status in {"submitted", "running"}
        if snapshot_state in {"finished", "failed", "interrupted"} or not expected_active:
            blockers.append(item)
    return blockers


def _json_string_list(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item) for item in value]


def _json_int(value: object, default: int = 0) -> int:
    if isinstance(value, int):
        return value
    if isinstance(value, float | str):
        try:
            return int(value)
        except (TypeError, ValueError):
            return default
    return default


def _active_lease_summary(batch_db: Path) -> JsonObject:
    active_statuses = (
        "pending",
        "failed",
        "timed_out",
        "submitted",
        "running",
        "starting_sandbox",
        "bootstrapping_device",
        "waiting_device",
    )
    now = time.time()
    with sqlite3.connect(str(batch_db)) as connection:
        rows = connection.execute(
            f"""
            SELECT status, COALESCE(lease_owner, ''), count(*)
            FROM batch_tasks
            WHERE status IN ({",".join("?" for _ in active_statuses)})
              AND lease_token<>'' AND lease_expires_at>?
            GROUP BY status, COALESCE(lease_owner, '')
            ORDER BY count(*) DESC
            """,
            (*active_statuses, now),
        ).fetchall()
    by_status_owner = [
        {"status": str(status), "lease_owner": str(owner), "count": int(count)} for status, owner, count in rows
    ]
    return {
        "count": sum(int(row["count"]) for row in by_status_owner),
        "by_status_owner": by_status_owner[:20],
    }


def _ensure_pre_resume_cleanup_indexes(batch_db: Path) -> None:
    with sqlite3.connect(str(batch_db)) as connection:
        if _sqlite_table_exists(connection, "batch_tasks"):
            connection.execute("CREATE INDEX IF NOT EXISTS idx_batch_tasks_status_task ON batch_tasks(status, task_id)")
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_batch_tasks_pre_resume_active_controller_empty_lease "
                "ON batch_tasks(status, controller_task_id, order_index) "
                "WHERE controller_task_id<>'' AND lease_token=''"
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_batch_tasks_pre_resume_active_controller_expired_lease "
                "ON batch_tasks(status, controller_task_id, lease_expires_at, order_index) "
                "WHERE controller_task_id<>'' AND lease_token<>''"
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_batch_tasks_pre_resume_active_live_lease "
                "ON batch_tasks(status, lease_expires_at, order_index) "
                "WHERE lease_token<>''"
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_batch_tasks_pre_resume_active_controller_live_lease "
                "ON batch_tasks(status, controller_task_id, lease_expires_at, order_index) "
                "WHERE controller_task_id<>'' AND lease_token<>''"
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_batch_tasks_pre_resume_pending_history "
                "ON batch_tasks(status, attempts, controller_task_id, order_index) "
                "WHERE status='pending' AND (controller_task_id<>'' OR attempts>0)"
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_batch_tasks_pre_resume_pending_history_empty_id "
                "ON batch_tasks(status, attempts, order_index) "
                "WHERE status='pending' AND controller_task_id='' AND attempts>0"
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_batch_tasks_pre_resume_pending_history_live_lease "
                "ON batch_tasks(status, attempts, lease_expires_at, order_index) "
                "WHERE status='pending' AND controller_task_id='' AND attempts>0 AND lease_token<>''"
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_batch_tasks_pre_resume_pending_live_lease "
                "ON batch_tasks(status, lease_expires_at, order_index) "
                "WHERE status='pending' AND controller_task_id='' AND lease_token<>''"
            )
        if not _sqlite_table_exists(connection, "prepared_sandboxes"):
            return
        connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_prepared_sandboxes_status ON prepared_sandboxes(status, expires_at)"
        )
        connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_prepared_sandboxes_runner ON prepared_sandboxes(runner_id, status)"
        )
        connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_prepared_sandboxes_task_status "
            "ON prepared_sandboxes(task_id, attempt, status)"
        )
        connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_prepared_sandboxes_sandbox ON prepared_sandboxes(sandbox_id)"
        )
        connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_prepared_sandboxes_resource_debt "
            "ON prepared_sandboxes(status, updated_at, task_id, attempt) "
            "WHERE sandbox_id<>'' OR device_id<>'' OR lease_id<>''"
        )


def _ensure_prepared_indexes(batch_db: Path) -> None:
    _ensure_pre_resume_cleanup_indexes(batch_db)


def _maintenance_result_summary(result: object) -> JsonObject:
    plan = getattr(result, "plan", None)
    items = list(getattr(result, "items", []) or [])
    by_kind: dict[str, int] = {}
    for item in items:
        kind = getattr(getattr(item, "kind", ""), "value", str(getattr(item, "kind", "")))
        by_kind[str(kind)] = by_kind.get(str(kind), 0) + 1
    return {
        "command": str(getattr(result, "command", "")),
        "dry_run": bool(getattr(result, "dry_run", False)),
        "planned_count": int(getattr(result, "planned_count", 0) or 0),
        "applied_count": int(getattr(result, "applied_count", 0) or 0),
        "skipped_count": int(getattr(result, "skipped_count", 0) or 0),
        "errors": list(getattr(result, "errors", []) or []),
        "warnings": list(getattr(plan, "warnings", []) or []) if plan is not None else [],
        "metadata": dict(getattr(plan, "metadata", {}) or {}) if plan is not None else {},
        "by_kind": by_kind,
    }


_NONBLOCKING_CONTROLLER_RESIDUE_BLOCKED_REASONS = {
    "ambiguous_online_orphan_device",
    "foreign_batch_online_orphan_device",
}


def _controller_residue_cleanup_blocker(cleanup: JsonObject) -> str:
    provider_status = cleanup.get("provider_status")
    if isinstance(provider_status, dict) and int(provider_status.get("terminate_failed") or 0) > 0:
        return f"provider terminate_failed={provider_status.get('terminate_failed')}"
    planned = cleanup.get("planned")
    cleaned = cleanup.get("cleaned")
    if isinstance(planned, dict) and isinstance(cleaned, dict):
        planned_sandboxes = int(planned.get("sandboxes") or 0)
        cleaned_sandboxes = int(cleaned.get("sandboxes") or 0)
        if planned_sandboxes > cleaned_sandboxes:
            return f"provider cleanup incomplete: planned_sandboxes={planned_sandboxes} cleaned_sandboxes={cleaned_sandboxes}"
    blocked_by_reason = cleanup.get("blocked_by_reason")
    if isinstance(blocked_by_reason, dict):
        blocking_reasons = {
            str(reason): int(count or 0)
            for reason, count in blocked_by_reason.items()
            if int(count or 0) > 0 and str(reason) not in _NONBLOCKING_CONTROLLER_RESIDUE_BLOCKED_REASONS
        }
        if blocking_reasons:
            return f"controller residue blocked={blocking_reasons}"
    warnings = cleanup.get("warnings")
    if not isinstance(warnings, list):
        return ""
    blocking = [
        str(item)
        for item in warnings
        if (
            "controller DB" in str(item)
            or "terminate failed" in str(item)
            or "provider inventory failed" in str(item)
            or "deterministic lookup cap exceeded" in str(item)
        )
    ]
    return "; ".join(blocking)


def _write_pre_resume_cleanup_summary(batch_db: Path, summary: JsonObject) -> Path:
    path = batch_db.parent / "pre_resume_cleanup_summary.json"
    temp_path = path.with_suffix(".json.tmp")
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path.write_text(json.dumps(summary, indent=2, sort_keys=True), encoding="utf-8")
    temp_path.replace(path)
    return path


def _expire_unassigned_prepared_for_clean_resume(batch_db: Path) -> JsonObject:
    """Discard speculative prepared capacity after the previous runner is quiesced."""

    if not batch_db.exists():
        return {"count": 0}
    with sqlite3.connect(str(batch_db)) as connection:
        connection.row_factory = sqlite3.Row
        if not _sqlite_table_exists(connection, "prepared_sandboxes"):
            return {"count": 0}
        rows = connection.execute("""
            SELECT task_id, attempt, status, sandbox_id, device_id, lease_id, runner_id
            FROM prepared_sandboxes
            WHERE status IN ('reserved','preparing','prepared')
            ORDER BY updated_at ASC
            LIMIT 5
            """).fetchall()
        count = _sqlite_count(
            connection,
            "SELECT COUNT(*) FROM prepared_sandboxes WHERE status IN ('reserved','preparing','prepared')",
            (),
        )
        with_resource = _sqlite_count(
            connection,
            """
            SELECT COUNT(*) FROM prepared_sandboxes
            WHERE status IN ('reserved','preparing','prepared')
              AND (sandbox_id<>'' OR device_id<>'' OR lease_id<>'')
            """,
            (),
        )
        if count:
            now = time.time()
            connection.execute(
                """
                UPDATE prepared_sandboxes
                SET status='expired',
                    updated_at=?,
                    error=CASE
                        WHEN error='' THEN 'prepared sandbox discarded before clean resume'
                        ELSE error
                    END
                WHERE status IN ('reserved','preparing','prepared')
                """,
                (now,),
            )
    return {
        "count": count,
        "with_resource": with_resource,
        "sample": [
            {
                "task_id": str(row["task_id"] or ""),
                "attempt": int(row["attempt"] or 0),
                "status": str(row["status"] or ""),
                "runner_id": str(row["runner_id"] or ""),
                "sandbox_id_present": bool(row["sandbox_id"]),
                "device_id_present": bool(row["device_id"]),
                "lease_id_present": bool(row["lease_id"]),
            }
            for row in rows
        ],
    }


def _prepared_cleanup_needed(batch_db: Path) -> bool:
    if not batch_db.exists():
        return False
    with sqlite3.connect(str(batch_db)) as connection:
        if not _sqlite_table_exists(connection, "prepared_sandboxes"):
            return False
        now = time.time()
        return (
            _sqlite_count(
                connection,
                """
                SELECT COUNT(*) FROM prepared_sandboxes
                WHERE status IN (
                    'assigned','cleanup_deferred','cleanup_failed',
                    'expired','failed','canceled'
                )
                AND (sandbox_id<>'' OR device_id<>'' OR lease_id<>'')
                """,
                (),
            )
            + _sqlite_count(
                connection,
                """
                SELECT COUNT(*) FROM prepared_sandboxes
                WHERE status IN ('reserved','preparing','prepared')
                  AND expires_at<=?
                  AND (sandbox_id<>'' OR device_id<>'' OR lease_id<>'')
                """,
                (now,),
            )
            > 0
        )


def _prepared_live_limit(*, run_concurrency: int, policy: PreparedSandboxPolicy | None) -> int:
    if policy is None or not policy.enabled or run_concurrency <= 0:
        return 0
    return run_concurrency + policy.effective_buffer(run_concurrency=run_concurrency)


def _effective_resume_concurrency(*, manifest: BatchManifest, resume_config: BatchRunResumeConfig) -> int:
    return int(resume_config.concurrency or manifest.defaults.concurrency or 0)


def _resolve_pre_resume_cleanup_provider(*, manifest: BatchManifest, explicit_provider: str = "") -> str:
    provider = str(explicit_provider or "").strip()
    if provider:
        return provider
    manifest_provider = str(manifest.defaults.sandbox.provider or "").strip()
    return manifest_provider or "e2b"


def _prepared_live_budget_summary(batch_db: Path, *, prepared_live_limit: int = 0) -> JsonObject:
    if not batch_db.exists():
        return {}
    with sqlite3.connect(str(batch_db)) as connection:
        connection.row_factory = sqlite3.Row
        if not _sqlite_table_exists(connection, "prepared_sandboxes"):
            return {}
        now = time.time()
        status_rows = connection.execute(
            "SELECT status, COUNT(*) AS count FROM prepared_sandboxes GROUP BY status"
        ).fetchall()
        status_counts = {str(row["status"]): int(row["count"] or 0) for row in status_rows}
        active_unassigned = _sqlite_count(
            connection,
            """
            SELECT COUNT(*) FROM prepared_sandboxes
            WHERE status IN ('reserved','preparing','prepared') AND expires_at>?
            """,
            (now,),
        )
        assigned_total = _sqlite_count(
            connection,
            "SELECT COUNT(*) FROM prepared_sandboxes WHERE status IN ('assigned','cleanup_deferred')",
            (),
        )
        assigned_terminal = _sqlite_count(
            connection,
            """
            SELECT COUNT(*)
            FROM prepared_sandboxes p
            JOIN batch_tasks t ON t.task_id=p.task_id
            WHERE p.status IN ('assigned','cleanup_deferred')
              AND t.status IN ('succeeded','evaluation_pending','failed','timed_out','canceled')
            """,
            (),
        )
        assigned_terminal_with_resource = _sqlite_count(
            connection,
            """
            SELECT COUNT(*)
            FROM prepared_sandboxes p
            JOIN batch_tasks t ON t.task_id=p.task_id
            WHERE p.status IN ('assigned','cleanup_deferred')
              AND t.status IN ('succeeded','evaluation_pending','failed','timed_out','canceled')
              AND (p.sandbox_id<>'' OR p.device_id<>'' OR p.lease_id<>'')
            """,
            (),
        )
        cleanup_failed = _sqlite_count(
            connection,
            "SELECT COUNT(*) FROM prepared_sandboxes WHERE status='cleanup_failed'",
            (),
        )
        cleanup_required_with_resource = _sqlite_count(
            connection,
            """
            SELECT COUNT(*) FROM prepared_sandboxes
            WHERE status='cleanup_failed' AND (sandbox_id<>'' OR device_id<>'' OR lease_id<>'')
            """,
            (),
        )
    assigned_live = max(assigned_total - assigned_terminal, 0)
    live_total = active_unassigned + assigned_live + assigned_terminal_with_resource + cleanup_required_with_resource
    overrun = max(live_total - prepared_live_limit, 0) if prepared_live_limit > 0 else 0
    return {
        "prepared_live_limit": prepared_live_limit,
        "active_unassigned": active_unassigned,
        "assigned_total": assigned_total,
        "assigned_live": assigned_live,
        "assigned_terminal": assigned_terminal,
        "assigned_terminal_with_resource": assigned_terminal_with_resource,
        "cleanup_deferred": status_counts.get("cleanup_deferred", 0),
        "cleanup_failed": cleanup_failed,
        "cleanup_required_with_resource": cleanup_required_with_resource,
        "live_total": live_total,
        "live_e2b_budget_overrun": overrun,
        "prepared_status_counts": status_counts,
    }


def _sqlite_table_exists(connection: sqlite3.Connection, name: str) -> bool:
    row = connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)).fetchone()
    return row is not None


def _sqlite_count(connection: sqlite3.Connection, query: str, params: tuple[object, ...]) -> int:
    row = connection.execute(query, params).fetchone()
    return int(row[0] if row is not None else 0)


def _result_from_states(*, batch_id: str, output_dir: Path, states: list[BatchTaskState]) -> BatchRunResult:
    terminal_statuses = {
        BatchTaskStatus.SUCCEEDED,
        BatchTaskStatus.EVALUATION_PENDING,
        BatchTaskStatus.FAILED,
        BatchTaskStatus.TIMED_OUT,
        BatchTaskStatus.CANCELED,
    }
    return BatchRunResult(
        batch_id=batch_id,
        total=len(states),
        succeeded=sum(1 for state in states if state.status == BatchTaskStatus.SUCCEEDED),
        failed=sum(
            1
            for state in states
            if state.status in {BatchTaskStatus.FAILED, BatchTaskStatus.TIMED_OUT, BatchTaskStatus.EVALUATION_PENDING}
        ),
        canceled=sum(1 for state in states if state.status == BatchTaskStatus.CANCELED),
        incomplete=sum(1 for state in states if state.status not in terminal_statuses),
        output_dir=str(output_dir),
        task_states=states,
    )
