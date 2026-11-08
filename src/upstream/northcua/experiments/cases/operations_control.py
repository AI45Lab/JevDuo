"""Read-only and cooperative-control helpers for case-suite operations."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Protocol, cast

from northcua.batch.contracts import BatchTaskState
from northcua.batch.operations import BatchOperations
from northcua.experiments.cases.operations_support import CaseRunPathResolver, CaseRunPersistence
from northcua_protocol.capabilities import JsonObject


class _CaseRunControlHost(Protocol):
    """Structural host expected by CaseRunControlMixin."""

    _path_resolver: CaseRunPathResolver
    _persistence: CaseRunPersistence


class CaseRunControlMixin:
    """Mixin for status, report, and cooperative control commands.

    The concrete operations class provides ``_path_resolver`` and
    ``_persistence``. Keeping these commands out of the run orchestration class
    keeps the live case-run path focused on materialization and execution.
    """

    def status(self, *, suite_id: str, run_id: str, output_dir: Path) -> list[BatchTaskState]:
        """Read latest batch state for a case run.

        Args:
            suite_id: Case suite id.
            run_id: Batch run id under the suite output directory.
            output_dir: Root output directory passed to case run.

        Returns:
            Latest task states from SQLite control-plane state.
        """

        host = self._control_host()
        return BatchOperations().status(
            batch_id=run_id,
            output_dir=host._path_resolver.batch_root(output_dir, suite_id, run_id),
        )

    def monitor(self, *, suite_id: str, run_id: str, output_dir: Path) -> JsonObject:
        """Read local telemetry projections for a case run.

        Args:
            suite_id: Case suite id.
            run_id: Batch run id under the suite output directory.
            output_dir: Root output directory passed to case run.

        Returns:
            Telemetry projection for the requested run.
        """

        host = self._control_host()
        return BatchOperations().monitor(
            batch_id=run_id,
            output_dir=host._path_resolver.batch_root(output_dir, suite_id, run_id),
        )

    def report(self, *, suite_id: str, run_id: str, output_dir: Path) -> JsonObject:
        """Read the case report when present, otherwise fall back to batch report.

        Args:
            suite_id: Case suite id.
            run_id: Batch run id under the suite output directory.
            output_dir: Root output directory passed to case run.

        Returns:
            Rebuilt case report or the underlying batch report.
        """

        host = self._control_host()
        run_root = host._path_resolver.run_dir(output_dir, suite_id, run_id)
        rebuilt = host._persistence.rebuild_report_if_possible(run_dir=run_root, run_id=run_id)
        if rebuilt is not None:
            return rebuilt
        case_report = run_root / "case_report.json"
        if case_report.exists():
            parsed = json.loads(case_report.read_text(encoding="utf-8"))
            return parsed if isinstance(parsed, dict) else {"report": parsed}
        return BatchOperations().report(batch_id=run_id, output_dir=run_root / "batch")

    def cancel(self, *, suite_id: str, run_id: str, output_dir: Path) -> Path:
        """Request cancellation for a case run.

        Args:
            suite_id: Case suite id.
            run_id: Batch run id under the suite output directory.
            output_dir: Root output directory passed to case run.

        Returns:
            Path to the written cancellation marker.
        """

        host = self._control_host()
        return BatchOperations().cancel(
            batch_id=run_id,
            output_dir=host._path_resolver.batch_root(output_dir, suite_id, run_id),
        )

    def pause(self, *, suite_id: str, run_id: str, output_dir: Path) -> Path:
        """Pause new local task claims for a case run.

        Args:
            suite_id: Case suite id.
            run_id: Batch run id under the suite output directory.
            output_dir: Root output directory passed to case run.

        Returns:
            Path to the written pause marker.
        """

        host = self._control_host()
        return BatchOperations().pause(
            batch_id=run_id,
            output_dir=host._path_resolver.batch_root(output_dir, suite_id, run_id),
        )

    def drain(self, *, suite_id: str, run_id: str, output_dir: Path) -> Path:
        """Drain a case run without canceling running tasks.

        Args:
            suite_id: Case suite id.
            run_id: Batch run id under the suite output directory.
            output_dir: Root output directory passed to case run.

        Returns:
            Path to the written drain marker.
        """

        host = self._control_host()
        return BatchOperations().drain(
            batch_id=run_id,
            output_dir=host._path_resolver.batch_root(output_dir, suite_id, run_id),
        )

    def _control_host(self) -> _CaseRunControlHost:
        """Return this mixin instance narrowed to its concrete host contract."""

        return cast(_CaseRunControlHost, self)
