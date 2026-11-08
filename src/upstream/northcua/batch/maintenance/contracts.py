"""Typed contracts for batch maintenance plans and audit results."""

from __future__ import annotations

from enum import Enum
from pathlib import Path

from pydantic import BaseModel, Field

from northcua_protocol.capabilities import JsonObject


class MaintenanceActionKind(str, Enum):
    """Operations that the maintenance executor can apply safely."""

    DIAGNOSE = "diagnose"
    RESET_STALE_ROW = "reset_stale_row"
    RESET_STALE_IN_FLIGHT_ROW = "reset_stale_in_flight_row"
    CLEAR_ACTIVE_LEASE = "clear_active_lease"
    RECONCILE_CONTROLLER_TASK = "reconcile_controller_task"
    CANCEL_CONTROLLER_TASK = "cancel_controller_task"
    MARK_CASE_CANCELED = "mark_case_canceled"
    TERMINATE_SANDBOX = "terminate_sandbox"


class ControllerTaskRepairMode(str, Enum):
    """How a controller task was identified for maintenance."""

    EXPLICIT_ID = "explicit_id"
    DETERMINISTIC_ID = "deterministic_id"
    BATCH_LIST_MATCH = "batch_list_match"
    UNAVAILABLE = "unavailable"


class SandboxTerminationScope(str, Enum):
    """Scope used when selecting sandboxes for termination."""

    BATCH = "batch"
    CASE = "case"


class MaintenancePlanItem(BaseModel):
    """One proposed maintenance action.

    Attributes:
        action_id: Stable id within one plan.
        kind: Operation kind understood by the executor.
        case_id: Optional batch task id affected by the action.
        controller_task_id: Optional controller task id affected by the action.
        sandbox_id: Optional provider sandbox id affected by the action.
        provider: Optional sandbox provider id.
        reason: Operator-facing reason for the action.
        apply_supported: Whether this item can be executed by ``--apply``.
        details: Small typed/JSON details needed by the executor or audit.
    """

    action_id: str
    kind: MaintenanceActionKind
    case_id: str = ""
    controller_task_id: str = ""
    sandbox_id: str = ""
    provider: str = ""
    reason: str = ""
    apply_supported: bool = True
    details: JsonObject = Field(default_factory=dict)


class MaintenancePlan(BaseModel):
    """Dry-run or apply-ready set of batch maintenance actions.

    Attributes:
        batch_id: Batch namespace being inspected or repaired.
        batch_dir: Directory containing SQLite state and audit outputs.
        command: Maintenance subcommand that produced the plan.
        dry_run: Whether the plan/result should avoid side effects.
        items: Ordered proposed actions.
        warnings: Non-fatal planning caveats.
    """

    batch_id: str
    batch_dir: Path
    command: str
    dry_run: bool = True
    items: list[MaintenancePlanItem] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    metadata: JsonObject = Field(default_factory=dict)

    @property
    def planned_count(self) -> int:
        """Return the number of items in the plan.

        Returns:
            Count of planned actions and diagnostics.
        """

        return len(self.items)


class MaintenanceAppliedItem(BaseModel):
    """Result for one planned maintenance item.

    Attributes:
        action_id: Action id copied from the plan item.
        kind: Operation kind attempted.
        case_id: Optional affected batch task id.
        controller_task_id: Optional affected controller task id.
        sandbox_id: Optional affected provider sandbox id.
        applied: Whether the action executed successfully.
        skipped: Whether the action was skipped because this was dry-run or
            the item is diagnostic only.
        error: Error summary when execution failed.
        before: Small before snapshot for audit.
        after: Small after snapshot for audit.
    """

    action_id: str
    kind: MaintenanceActionKind
    case_id: str = ""
    controller_task_id: str = ""
    sandbox_id: str = ""
    applied: bool = False
    skipped: bool = False
    error: str = ""
    before: JsonObject = Field(default_factory=dict)
    after: JsonObject = Field(default_factory=dict)


class MaintenanceApplyResult(BaseModel):
    """Machine-readable output for a maintenance command.

    Attributes:
        batch_id: Batch namespace affected by the command.
        command: Maintenance subcommand name.
        dry_run: Whether any mutations were intentionally skipped.
        planned_count: Number of plan items.
        applied_count: Number of successfully applied items.
        skipped_count: Number of skipped dry-run/diagnostic items.
        errors: Execution errors, if any.
        audit_path: Append-only audit JSONL path.
        plan: Original plan with dry-run/apply flag resolved.
        items: Per-item execution results.
    """

    batch_id: str
    command: str
    dry_run: bool
    planned_count: int = 0
    applied_count: int = 0
    skipped_count: int = 0
    errors: list[str] = Field(default_factory=list)
    audit_path: str = ""
    plan: MaintenancePlan
    items: list[MaintenanceAppliedItem] = Field(default_factory=list)
