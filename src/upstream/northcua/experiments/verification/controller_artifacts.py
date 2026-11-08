"""Materialize controller-stored artifacts into the experiment file layout."""

from __future__ import annotations

import logging
from pathlib import Path

from northcua.batch.contracts import BatchTaskState
from northcua.experiments.verification.controller_artifact_helpers import (
    ControllerArtifactMaterializerHelpers,
    ControllerArtifactRow,
)

logger = logging.getLogger(__name__)


class ControllerArtifactMaterializer(ControllerArtifactMaterializerHelpers):
    """Write inline controller artifacts back to session-like local paths."""

    def materialize(
        self,
        *,
        output_dir: Path,
        states: list[BatchTaskState],
        evaluator_kind: str,
        db_path: Path | None = None,
        canonical_roots: list[Path] | None = None,
        blob_roots: list[Path] | None = None,
        allow_writes: bool = True,
    ) -> Path | None:
        """Decode legacy controller artifacts for experiment verification.

        Modern runs keep full agent screenshots, traces, and accessibility
        payloads under ``agent-artifacts`` and store only hashes/refs in
        controller SQLite. This materializer is therefore a legacy fallback:
        it can recover rows that still contain inline bytes or controller blob
        refs, but intentionally compacted rows require an existing canonical
        artifact root.

        Args:
            output_dir: Concrete experiment output directory containing
                ``controller.sqlite``.
            states: Batch task states whose controller/session ids should be
                materialized.
            evaluator_kind: Preferred evaluator directory for legacy evaluator
                aggregate artifacts.
            db_path: Optional explicit controller SQLite path for experiments
                that store the controller catalog outside ``output_dir``.
            canonical_roots: Existing artifact roots that should be preferred
                over SQLite fallback materialization.
            blob_roots: Controller blob store roots that may contain
                ``controller_blob`` payloads referenced by the catalog.
            allow_writes: Whether missing artifacts may be materialized into
                ``output_dir/materialized``. Set to ``False`` for dry-run
                recovery planning.

        Returns:
            Root directory containing fallback artifacts, or ``None`` when the
            controller SQLite database is absent or every session already has
            a complete canonical artifact root.
        """

        db_path = db_path or output_dir / "controller.sqlite"
        if not db_path.exists():
            return None
        materialized_root = output_dir / "materialized"
        roots = self._dedupe_roots([*(canonical_roots or []), output_dir / "agent-artifacts"])
        blob_roots = self._dedupe_roots([*(blob_roots or []), output_dir / "controller-blobs"])
        total_written = 0
        for state in states:
            if not state.controller_task_id or not state.session_id:
                continue
            canonical_session_root = self._find_canonical_session_root(
                roots=roots,
                state=state,
                evaluator_kind=evaluator_kind,
            )
            if canonical_session_root is not None:
                logger.info(
                    "Skipping controller artifact materialization because session artifacts already exist",
                    extra={
                        "task_id": state.task_id,
                        "controller_task_id": state.controller_task_id,
                        "session_id": state.session_id,
                        "root": str(canonical_session_root),
                    },
                )
                continue
            if not allow_writes:
                continue
            task_root = materialized_root / self._safe_name(state.task_id)
            count = self._materialize_state(
                db_path=db_path,
                state=state,
                task_root=task_root,
                evaluator_kind=evaluator_kind,
                blob_roots=blob_roots,
            )
            if count:
                total_written += count
                logger.info(
                    "Materialized controller artifacts for experiment task",
                    extra={
                        "task_id": state.task_id,
                        "controller_task_id": state.controller_task_id,
                        "session_id": state.session_id,
                        "artifact_count": count,
                        "root": str(task_root),
                    },
                )
        return materialized_root if total_written else None

    def _materialize_state(
        self,
        *,
        db_path: Path,
        state: BatchTaskState,
        task_root: Path,
        evaluator_kind: str,
        blob_roots: list[Path],
    ) -> int:
        rows: list[ControllerArtifactRow] = self._load_rows(db_path=db_path, state=state, blob_roots=blob_roots)
        written = 0
        for row in rows:
            target = self._target_path(row=row, state=state, task_root=task_root, evaluator_kind=evaluator_kind)
            if target is None:
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(row.payload)
            written += 1
        return written


__all__ = ["ControllerArtifactMaterializer"]
