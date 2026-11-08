"""Append-only audit log for batch maintenance commands."""

from __future__ import annotations

import json
from pathlib import Path

from northcua.batch.maintenance.contracts import MaintenanceAppliedItem, MaintenancePlan
from northcua.telemetry.contracts import utc_now_iso
from northcua_protocol.capabilities import JsonObject


class MaintenanceAuditWriter:
    """Write structured maintenance events without affecting scheduling state."""

    def __init__(self, batch_dir: Path, *, actor: str = "operator") -> None:
        self.path = batch_dir / "batch_maintenance_events.jsonl"
        self._actor = actor

    def write_event(self, *, plan: MaintenancePlan, item: MaintenanceAppliedItem) -> None:
        """Append one action result to the audit JSONL.

        Args:
            plan: Plan that produced the item.
            item: Applied, skipped, or failed action result.
        """

        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload: JsonObject = {
            "timestamp": utc_now_iso(),
            "actor": self._actor,
            "batch_id": plan.batch_id,
            "command": plan.command,
            "dry_run": plan.dry_run,
            "action": item.model_dump(mode="json"),
        }
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n")
