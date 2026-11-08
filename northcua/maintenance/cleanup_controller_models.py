"""Typed records for controller residue cleanup planning."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

JsonDict = dict[str, object]


@dataclass(frozen=True, slots=True)
class CleanupPaths:
    """Resolved cleanup output paths.

    Attributes:
        output_dir: Directory where cleanup reports are written.
        summary: JSON summary output path.
        devices_tsv: TSV report for device rows.
        sessions_tsv: TSV report for session rows.
        leases_tsv: TSV report for lease rows.
        blocked_tsv: TSV report for blocked cleanup candidates.
        backup: Optional controller DB backup path used during apply.
    """

    output_dir: Path
    summary: Path
    devices_tsv: Path
    sessions_tsv: Path
    leases_tsv: Path
    blocked_tsv: Path
    backup: Path | None = None


@dataclass(slots=True)
class CleanupCandidate:
    """One stale controller task and its associated resources.

    Attributes:
        controller_task_id: Controller task id to clean.
        case_id: Batch case/task id.
        reason: Human-readable cleanup reason.
        task_status: Controller task status.
        trace_id: Associated trace id.
        session_id: Associated controller session id.
        session_status: Session row status.
        lease_id: Associated lease id.
        lease_status: Lease row status.
        device_id: Associated device id.
        device_status: Device row status.
        sandbox_id: Sandbox id inferred from task metadata or rows.
        provider_status: Provider cleanup status.
        metadata: JSON-like controller task metadata.
    """

    controller_task_id: str
    case_id: str
    reason: str
    task_status: str
    trace_id: str = ""
    session_id: str = ""
    session_status: str = ""
    lease_id: str = ""
    lease_status: str = ""
    device_id: str = ""
    device_status: str = ""
    sandbox_id: str = ""
    provider_status: str = ""
    metadata: JsonDict = field(default_factory=dict)


@dataclass(slots=True)
class CleanupPlan:
    """Planned controller residue cleanup.

    Attributes:
        batch_id: Batch id being cleaned.
        apply: Whether cleanup will mutate controller/provider state.
        batch_db: Batch SQLite path.
        controller_db: Controller SQLite path.
        paths: Output paths for reports and backups.
        protected: JSON-like summary of protected resources.
        candidates: Cleanup candidates eligible for action.
        blocked: Candidates intentionally blocked from cleanup.
        warnings: Non-fatal discovery/apply warnings.
        applied_counts: Counts of mutated rows by table.
    """

    batch_id: str
    apply: bool
    batch_db: Path
    controller_db: Path
    paths: CleanupPaths
    protected: JsonDict
    candidates: list[CleanupCandidate]
    blocked: list[JsonDict]
    warnings: list[str]
    applied_counts: JsonDict = field(default_factory=dict)
