"""Validation helpers shared by batch runner entry points."""

from __future__ import annotations

import json
import os
from pathlib import Path

from northcua.batch.contracts import BatchManifest, BatchRunConfig, BatchRunResumeConfig, _token_fingerprint
from northcua.batch.preparation import PreparedSandboxPolicy
from northcua.sandboxes.contracts import SandboxAllocationStrategy


def validate_prepared_sandbox_allocations(*, manifest: BatchManifest, policy: PreparedSandboxPolicy) -> None:
    """Reject allocation strategies that break single-use prepared semantics.

    Args:
        manifest: Batch manifest whose default and per-task sandbox allocation
            strategies should be checked.
        policy: Prepared sandbox policy requested for the run.

    Raises:
        ValueError: If prepared queue is enabled together with
            connect-existing allocation.
    """

    if not policy.enabled:
        return
    if any((task.sandbox or manifest.defaults.sandbox).provider == "local-process" for task in manifest.tasks):
        raise ValueError("local-process batches do not support prepared sandbox queues")
    forbidden = {
        SandboxAllocationStrategy.CONNECT_EXISTING: "connect_existing",
    }
    default_strategy = manifest.defaults.sandbox.allocation_strategy
    if default_strategy in forbidden:
        raise ValueError(f"prepared_sandbox queue cannot be combined with {forbidden[default_strategy]} allocation")
    for task in manifest.tasks:
        strategy = (task.sandbox or manifest.defaults.sandbox).allocation_strategy
        if strategy in forbidden:
            raise ValueError(
                f"prepared_sandbox queue cannot be combined with {forbidden[strategy]} allocation "
                f"for task {task.task_id}"
            )


def validate_existing_manifest(*, output_dir: Path, manifest: BatchManifest) -> None:
    """Reject runs whose persisted manifest conflicts with the requested one.

    Args:
        output_dir: Concrete batch output directory.
        manifest: Requested manifest for the run.

    Raises:
        RuntimeError: If an existing SQLite-backed run belongs to a different
            manifest identity or body.
    """

    sqlite_state_path = output_dir / "batch_state.sqlite"
    manifest_path = output_dir / "manifest.json"
    if not sqlite_state_path.exists():
        return
    if not manifest_path.exists():
        raise RuntimeError(
            f"existing SQLite batch state has no manifest snapshot: {sqlite_state_path}; "
            "use a new batch_id/output directory"
        )
    existing = BatchManifest.model_validate_json(manifest_path.read_text(encoding="utf-8"))
    if existing.batch_id != manifest.batch_id:
        raise RuntimeError(
            f"existing SQLite batch_id {existing.batch_id!r} does not match requested {manifest.batch_id!r}"
        )
    existing_task_ids = [task.task_id for task in existing.tasks]
    requested_task_ids = [task.task_id for task in manifest.tasks]
    if existing_task_ids != requested_task_ids:
        raise RuntimeError(
            "existing SQLite batch state task ids differ from requested manifest: "
            f"existing={json.dumps(existing_task_ids, ensure_ascii=False)} "
            f"requested={json.dumps(requested_task_ids, ensure_ascii=False)}; "
            "use a new batch_id/output directory"
        )
    if _manifest_compare_payload(existing) != _manifest_compare_payload(manifest):
        raise RuntimeError(
            "existing SQLite batch state was created with a different manifest body; "
            "use a new batch_id/output directory"
        )


def validate_existing_run_config(
    *,
    output_dir: Path,
    config: BatchRunConfig,
    sqlite_state_path: Path,
) -> None:
    """Reject accidental multi-controller ownership for an existing batch.

    Args:
        output_dir: Concrete batch output directory.
        config: Requested runtime configuration.
        sqlite_state_path: SQLite control-plane database path.

    Raises:
        ValueError: If persisted runtime identity does not match the request.
    """

    run_config_path = output_dir / "run_config.json"
    if not sqlite_state_path.exists() or not run_config_path.exists():
        return
    existing = BatchRunResumeConfig.model_validate_json(run_config_path.read_text(encoding="utf-8"))
    requested = BatchRunResumeConfig.from_run_config(config)
    mismatches: list[str] = []
    should_backfill_token_fingerprint = False
    should_backfill_controller_db_path = False
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
    elif requested.telemetry_controller_db_path is not None:
        should_backfill_controller_db_path = True
    existing_token_fingerprint = _token_fingerprint(existing.token)
    if existing.token and requested.token_fingerprint and existing_token_fingerprint != requested.token_fingerprint:
        mismatches.append("token_fingerprint")
    if existing.token_fingerprint:
        if requested.token_fingerprint and existing.token_fingerprint != requested.token_fingerprint:
            mismatches.append("token_fingerprint")
    elif requested.token_fingerprint:
        should_backfill_token_fingerprint = True
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
    if should_backfill_token_fingerprint or should_backfill_controller_db_path:
        update: dict[str, object] = {}
        if should_backfill_token_fingerprint:
            update["token_fingerprint"] = requested.token_fingerprint
        if should_backfill_controller_db_path:
            update["telemetry_controller_db_path"] = requested.telemetry_controller_db_path
        migrated = existing.model_copy(update=update)
        run_config_path.write_text(migrated.model_dump_json(indent=2), encoding="utf-8")


def resume_config_snapshot_for_write(
    *,
    output_dir: Path,
    config: BatchRunConfig,
    sqlite_state_path: Path,
) -> BatchRunResumeConfig:
    """Return the runtime snapshot that should be persisted for this run.

    Existing public-controller runs often persist only a non-secret token
    fingerprint, not the token itself. A completed-state resume may therefore
    arrive without any new token material. In that case the persisted
    fingerprint is still the authoritative runtime identity and must not be
    overwritten with an empty value.
    """

    snapshot = BatchRunResumeConfig.from_run_config(config)
    run_config_path = output_dir / "run_config.json"
    if not sqlite_state_path.exists() or not run_config_path.exists():
        return snapshot
    existing = BatchRunResumeConfig.model_validate_json(run_config_path.read_text(encoding="utf-8"))
    if existing.token and not snapshot.token and not snapshot.token_fingerprint:
        return snapshot.model_copy(
            update={
                "token": existing.token,
                "token_fingerprint": existing.token_fingerprint or _token_fingerprint(existing.token),
            }
        )
    if existing.token_fingerprint and not snapshot.token_fingerprint and not snapshot.token:
        return snapshot.model_copy(update={"token_fingerprint": existing.token_fingerprint})
    return snapshot


def _manifest_compare_payload(manifest: BatchManifest) -> object:
    payload = manifest.model_dump(mode="json")
    defaults = payload.get("defaults")
    if isinstance(defaults, dict):
        defaults["token"] = ""
    _normalize_manifest_compare_payload(payload)
    return payload


def _normalize_manifest_compare_payload(value: object) -> None:
    """Canonicalize unordered manifest fields before resume identity checks."""

    if isinstance(value, dict):
        capabilities = value.get("required_capabilities")
        if isinstance(capabilities, list):
            value["required_capabilities"] = sorted(capabilities)
        for child in value.values():
            _normalize_manifest_compare_payload(child)
    elif isinstance(value, list):
        for child in value:
            _normalize_manifest_compare_payload(child)


def _prepared_policy_identity(config: BatchRunResumeConfig) -> dict[str, object]:
    policy = config.prepared_sandbox_policy
    if not policy.enabled:
        return {"enabled": False}
    return {"enabled": policy.enabled, "depth": policy.depth.value}


def _allow_controller_url_runtime_drift() -> bool:
    return os.getenv("NORTHCUA_ALLOW_CONTROLLER_URL_RUNTIME_DRIFT", "").strip().lower() in {"1", "true", "yes"}
