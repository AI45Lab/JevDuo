"""Task artifact validation and materialization helpers."""

from __future__ import annotations

from northcua.controller.auth import ControllerAuth
from northcua.controller.storage.blob_store import LocalControllerBlobStore
from northcua_protocol.artifacts import ArtifactRef, StorageKind


class TaskArtifactMaterializer:
    """Move controller-bound task artifacts into safe storage."""

    def __init__(self, *, auth: ControllerAuth, blob_store: LocalControllerBlobStore) -> None:
        self.auth = auth
        self.blob_store = blob_store

    def materialize(self, artifacts: list[ArtifactRef], *, allow_controller_blob: bool = False) -> list[ArtifactRef]:
        """Validate task artifact provenance and spill large inline payloads.

        Args:
            artifacts: Task input or task terminal artifacts from a client/device.
            allow_controller_blob: Whether already-controller-owned blobs are
                permitted. This must stay false for external task/device input,
                and is only enabled for controller-owned runner completions.

        Returns:
            Artifact refs with oversized inline data stored in controller blobs.

        Raises:
            ValueError: If an inbound task artifact claims controller-owned storage.
        """

        materialized: list[ArtifactRef] = []
        for artifact in artifacts:
            if artifact.storage_kind == StorageKind.CONTROLLER_BLOB and not allow_controller_blob:
                raise ValueError("inbound task artifacts cannot claim controller_blob storage")
            materialized.append(
                self.blob_store.materialize(artifact)
                if allow_controller_blob and artifact.storage_kind == StorageKind.CONTROLLER_BLOB
                else self.blob_store.materialize_large_inline(
                    artifact,
                    max_inline_bytes=self.auth.config.max_inline_artifact_bytes,
                )
            )
        return materialized
