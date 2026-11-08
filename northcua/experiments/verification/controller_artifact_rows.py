"""Controller artifact SQLite row decoding helpers."""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import sqlite3
from pathlib import Path

from northcua.batch.contracts import BatchTaskState
from northcua.experiments.verification.controller_artifact_models import ControllerArtifactRow

logger = logging.getLogger(__name__)


class ControllerArtifactRowDecodeMixin:
    """Decode controller artifact rows and blob payloads."""

    def _load_rows(
        self,
        *,
        db_path: Path,
        state: BatchTaskState,
        blob_roots: list[Path],
    ) -> list[ControllerArtifactRow]:
        identifiers = [value for value in (state.controller_task_id, state.trace_id, state.session_id) if value]
        if not identifiers:
            return []
        placeholders = ",".join("?" for _ in identifiers)
        query = (
            "select original_artifact_id, source, kind, mime_type, artifact_json "
            "from artifacts where task_id in ({ids}) or trace_id in ({ids}) or session_id in ({ids}) "
            "order by created_at, original_artifact_id"
        ).format(ids=placeholders)
        parameters = [*identifiers, *identifiers, *identifiers]
        with sqlite3.connect(db_path) as connection:
            connection.row_factory = sqlite3.Row
            records = connection.execute(query, parameters).fetchall()
        rows: list[ControllerArtifactRow] = []
        for record in records:
            row = self._decode_row(record, blob_roots=blob_roots)
            if row is not None:
                rows.append(row)
        return rows

    def _decode_row(self, record: sqlite3.Row, *, blob_roots: list[Path]) -> ControllerArtifactRow | None:
        try:
            artifact = json.loads(str(record["artifact_json"] or "{}"))
        except json.JSONDecodeError:
            logger.warning(
                "Skipping malformed controller artifact JSON",
                extra={"original_artifact_id": record["original_artifact_id"]},
                exc_info=True,
            )
            return None
        inline_data = artifact.get("inline_data")
        if isinstance(inline_data, str) and inline_data:
            try:
                payload = base64.b64decode(inline_data)
            except ValueError:
                logger.warning(
                    "Skipping controller artifact with invalid inline data",
                    extra={"original_artifact_id": record["original_artifact_id"]},
                    exc_info=True,
                )
                return None
        elif artifact.get("inline_data_omitted"):
            logger.info(
                "Skipping controller artifact row whose inline payload was intentionally omitted",
                extra={
                    "original_artifact_id": record["original_artifact_id"],
                    "kind": record["kind"],
                    "source": record["source"],
                    "storage_kind": artifact.get("storage_kind"),
                    "sha256": artifact.get("sha256") or record["sha256"],
                },
            )
            return None
        else:
            payload = self._read_controller_blob(record=record, artifact=artifact, blob_roots=blob_roots)
            if payload is None:
                return None
        return ControllerArtifactRow(
            original_artifact_id=str(record["original_artifact_id"] or ""),
            source=str(record["source"] or ""),
            kind=str(record["kind"] or ""),
            mime_type=str(record["mime_type"] or "application/octet-stream"),
            payload=payload,
        )

    def _read_controller_blob(
        self,
        *,
        record: sqlite3.Row,
        artifact: dict[str, object],
        blob_roots: list[Path],
    ) -> bytes | None:
        if str(artifact.get("storage_kind") or "") != "controller_blob":
            return None
        sha256 = str(artifact.get("sha256") or record["sha256"] or "")
        if not sha256:
            return None
        expected_size = self._coerce_int(artifact.get("size_bytes") or record["size_bytes"])
        for root in blob_roots:
            path = root / sha256
            if not path.is_file():
                continue
            data = path.read_bytes()
            if expected_size and len(data) != expected_size:
                logger.warning(
                    "Skipping controller blob with unexpected size",
                    extra={"original_artifact_id": record["original_artifact_id"], "blob": str(path)},
                )
                continue
            if hashlib.sha256(data).hexdigest() != sha256:
                logger.warning(
                    "Skipping controller blob with sha256 mismatch",
                    extra={"original_artifact_id": record["original_artifact_id"], "blob": str(path)},
                )
                continue
            return data
        logger.warning(
            "Skipping controller blob artifact because payload file is missing",
            extra={"original_artifact_id": record["original_artifact_id"], "sha256": sha256},
        )
        return None

    @staticmethod
    def _coerce_int(value: object) -> int:
        """Convert SQLite/JSON scalar values into an optional positive size."""

        if isinstance(value, bool):
            return int(value)
        if isinstance(value, int):
            return value
        if isinstance(value, float):
            return int(value)
        if isinstance(value, str) and value.strip():
            return int(value)
        return 0
