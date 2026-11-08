"""Validated per-task retry plans used by controlled batch resumes."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path

from northcua.batch.contracts import BatchManifest, BatchTaskStatus

_PLAN_ID_RE = re.compile(r"^[A-Za-z0-9._-]{1,96}$")
_SUPPORTED_STATUSES = {BatchTaskStatus.FAILED, BatchTaskStatus.CANCELED}


@dataclass(frozen=True, slots=True)
class SelectedRetryTarget:
    """Expected source state and new attempt ceiling for one task."""

    task_id: str
    expected_status: BatchTaskStatus
    expected_attempts: int
    expected_retry_budget_remaining: int
    expected_failure_category: str
    max_attempts: int
    expected_error_sha256: str = ""

    @property
    def extra_attempts(self) -> int:
        """Return the number of additional attempts granted by this target."""

        return self.max_attempts - self.expected_attempts


@dataclass(frozen=True, slots=True)
class SelectedRetryPlan:
    """Immutable, content-addressed retry plan for one persisted batch."""

    path: Path
    sha256: str
    plan_id: str
    batch_id: str
    manifest_sha256: str
    extra_attempts: int
    targets: tuple[SelectedRetryTarget, ...]

    @property
    def targets_by_id(self) -> dict[str, SelectedRetryTarget]:
        """Return targets keyed by task id."""

        return {target.task_id: target for target in self.targets}


def load_selected_retry_plan(
    path: str | Path,
    *,
    expected_batch_id: str,
    expected_manifest_sha256: str,
) -> SelectedRetryPlan:
    """Load and strictly validate a selected-retry plan."""

    plan_path = Path(path).resolve()
    raw = plan_path.read_bytes()
    payload = json.loads(raw)
    if not isinstance(payload, dict):
        raise ValueError("selected retry plan must be a JSON object")
    if payload.get("schema_version") != 1:
        raise ValueError("selected retry plan schema_version must be 1")
    plan_id = _required_string(payload, "plan_id")
    if not _PLAN_ID_RE.fullmatch(plan_id):
        raise ValueError("selected retry plan_id contains unsupported characters")
    batch_id = _required_string(payload, "batch_id")
    if batch_id != expected_batch_id:
        raise ValueError(f"selected retry batch mismatch: {batch_id!r} != {expected_batch_id!r}")
    manifest_sha256 = _required_string(payload, "manifest_sha256")
    if manifest_sha256 != expected_manifest_sha256:
        raise ValueError("selected retry manifest checksum mismatch")
    extra_attempts = _required_positive_int(payload, "extra_attempts")
    raw_targets = payload.get("targets")
    if not isinstance(raw_targets, list) or not raw_targets:
        raise ValueError("selected retry plan targets must be a non-empty list")
    targets: list[SelectedRetryTarget] = []
    seen: set[str] = set()
    for index, item in enumerate(raw_targets):
        if not isinstance(item, dict):
            raise ValueError(f"selected retry target {index} must be an object")
        task_id = _required_string(item, "task_id")
        if task_id in seen:
            raise ValueError(f"duplicate selected retry task_id: {task_id}")
        seen.add(task_id)
        try:
            expected_status = BatchTaskStatus(_required_string(item, "expected_status"))
        except ValueError as exc:
            raise ValueError(f"invalid expected_status for {task_id}") from exc
        if expected_status not in _SUPPORTED_STATUSES:
            raise ValueError(f"unsupported selected retry status for {task_id}: {expected_status.value}")
        expected_attempts = _required_non_negative_int(item, "expected_attempts")
        expected_retry_budget = _required_non_negative_int(item, "expected_retry_budget_remaining")
        max_attempts = _required_positive_int(item, "max_attempts")
        if max_attempts - expected_attempts != extra_attempts:
            raise ValueError(
                f"selected retry target {task_id} grants {max_attempts - expected_attempts} attempts, "
                f"expected {extra_attempts}"
            )
        failure_category = item.get("expected_failure_category", "")
        if not isinstance(failure_category, str):
            raise ValueError(f"expected_failure_category for {task_id} must be a string")
        expected_error_sha256 = item.get("expected_error_sha256", "")
        if not isinstance(expected_error_sha256, str) or (
            expected_error_sha256 and not re.fullmatch(r"[0-9a-f]{64}", expected_error_sha256)
        ):
            raise ValueError(f"expected_error_sha256 for {task_id} must be empty or lowercase SHA256")
        targets.append(
            SelectedRetryTarget(
                task_id=task_id,
                expected_status=expected_status,
                expected_attempts=expected_attempts,
                expected_retry_budget_remaining=expected_retry_budget,
                expected_failure_category=failure_category,
                max_attempts=max_attempts,
                expected_error_sha256=expected_error_sha256,
            )
        )
    return SelectedRetryPlan(
        path=plan_path,
        sha256=hashlib.sha256(raw).hexdigest(),
        plan_id=plan_id,
        batch_id=batch_id,
        manifest_sha256=manifest_sha256,
        extra_attempts=extra_attempts,
        targets=tuple(targets),
    )


def apply_selected_retry_plan(manifest: BatchManifest, plan: SelectedRetryPlan) -> BatchManifest:
    """Apply plan-specific retry ceilings to an in-memory resume manifest."""

    targets = plan.targets_by_id
    manifest_ids = {task.task_id for task in manifest.tasks}
    missing = sorted(set(targets) - manifest_ids)
    if missing:
        raise ValueError(f"selected retry tasks missing from manifest: {missing[:5]}")
    tasks = [
        (
            task.model_copy(update={"max_retries": targets[task.task_id].max_attempts - 1})
            if task.task_id in targets
            else task
        )
        for task in manifest.tasks
    ]
    return manifest.model_copy(update={"tasks": tasks})


def _required_string(payload: dict[str, object], key: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"selected retry {key} must be a non-empty string")
    return value.strip()


def _required_positive_int(payload: dict[str, object], key: str) -> int:
    value = _required_non_negative_int(payload, key)
    if value <= 0:
        raise ValueError(f"selected retry {key} must be positive")
    return value


def _required_non_negative_int(payload: dict[str, object], key: str) -> int:
    value = payload.get(key)
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"selected retry {key} must be a non-negative integer")
    return value


__all__ = [
    "SelectedRetryPlan",
    "SelectedRetryTarget",
    "apply_selected_retry_plan",
    "load_selected_retry_plan",
]
