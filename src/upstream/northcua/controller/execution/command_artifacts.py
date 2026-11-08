"""Command artifact materialization and metadata rewriting helpers."""

from __future__ import annotations

from northcua.controller.storage.blob_store import LocalControllerBlobStore
from northcua.controller.storage.persistence import SQLiteControllerStore, artifact_catalog_id
from northcua_protocol.artifacts import StorageKind
from northcua_protocol.payloads import CommandResult


class CommandArtifactMaterializer:
    """Validate command artifact ownership before catalog persistence."""

    def __init__(
        self,
        *,
        store: SQLiteControllerStore,
        blob_store: LocalControllerBlobStore | None,
        max_inline_artifact_bytes: int,
    ) -> None:
        self.store = store
        self.blob_store = blob_store
        self.max_inline_artifact_bytes = max_inline_artifact_bytes

    def materialize_result(self, result: CommandResult, *, task_id: str) -> CommandResult:
        """Validate command output artifact ownership without duplicating bytes.

        Args:
            result: Command result received from a device or adapter.
            task_id: Task id associated with the command session.

        Returns:
            The live result. Inline command artifacts are kept in the in-memory
            response so controller-owned runners can persist them under
            agent-artifacts; SQLite/catalog persistence compacts those payloads
            separately and must not copy screenshots into controller-blobs.

        Raises:
            ValueError: If a controller blob ref is not owned by the command scope.
        """

        if self.blob_store is None:
            return result
        for artifact in result.artifacts:
            if artifact.storage_kind != StorageKind.CONTROLLER_BLOB:
                continue
            existing = self.store.artifact_row_by_ref(
                artifact,
                task_id=task_id,
                session_id=result.session_id,
                command_id=result.command_id,
            )
            if existing is None:
                raise ValueError("inbound controller_blob artifact is not owned by this command scope")
        return result

    @staticmethod
    def rewrite_result_metadata(
        result: CommandResult,
        *,
        trace_id: str,
        task_id: str,
    ) -> tuple[CommandResult, dict[str, str]]:
        """Rewrite payload-facing artifact ids to controller catalog ids.

        Args:
            result: Command result whose payload may reference artifact ids.
            trace_id: Trace id used to derive catalog ids.
            task_id: Task id associated with the command session.

        Returns:
            Updated result and a map of original artifact id to catalog id.
        """

        artifact_catalog_ids = {
            artifact.artifact_id: artifact_catalog_id(
                artifact,
                trace_id=trace_id,
                task_id=task_id,
                session_id=result.session_id,
                command_id=result.command_id,
            )
            for artifact in result.artifacts
        }
        payload = result.result
        if not isinstance(payload, dict) or not result.artifacts:
            return result, artifact_catalog_ids
        rewritten = dict(payload)
        for key, value in list(payload.items()):
            if not key.endswith("_artifact_id") or not isinstance(value, str):
                continue
            catalog_id = artifact_catalog_ids.get(value)
            if catalog_id is None:
                continue
            rewritten[key] = catalog_id
            rewritten[f"{key}_original"] = value
        return result.model_copy(update={"result": rewritten}), artifact_catalog_ids
