"""Controller-side artifact blob storage."""

from __future__ import annotations

import base64
import hashlib
import os
from pathlib import Path

from northcua_protocol.artifacts import ArtifactRef, StorageKind


class LocalControllerBlobStore:
    """Filesystem blob store keyed by artifact sha256."""

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    def materialize(self, artifact: ArtifactRef) -> ArtifactRef:
        """Persist one inline artifact and return a controller_blob ref.

        Args:
            artifact: Artifact ref to persist when it is inline.

        Returns:
            Controller blob ref for inline data, or the original non-inline ref.

        Raises:
            ValueError: If inline payload bytes do not match the declared hash
                or size, or if an existing hash-named blob fails verification.
        """

        if artifact.storage_kind == StorageKind.CONTROLLER_BLOB:
            self._verify_existing_blob(self.path_for_sha256(artifact.sha256), artifact)
            return artifact
        if artifact.storage_kind != StorageKind.INLINE or artifact.inline_data is None:
            return artifact
        data = base64.b64decode(artifact.inline_data)
        if hashlib.sha256(data).hexdigest() != artifact.sha256 or len(data) != artifact.size_bytes:
            raise ValueError("inline artifact integrity metadata does not match payload bytes")
        path = self.root / artifact.sha256
        if path.exists():
            self._verify_existing_blob(path, artifact)
        else:
            tmp_path = self.root / f".{artifact.sha256}.{os.getpid()}.tmp"
            tmp_path.write_bytes(data)
            tmp_path.replace(path)
            self._verify_existing_blob(path, artifact)
        return ArtifactRef.from_controller_blob(
            artifact_id=artifact.artifact_id,
            kind=artifact.kind,
            uri=self.uri_for_sha256(artifact.sha256),
            mime_type=artifact.mime_type,
            sha256=artifact.sha256,
            size_bytes=artifact.size_bytes,
            artifact_scope=artifact.artifact_scope,
            retention_policy=artifact.retention_policy,
        )

    def materialize_large_inline(self, artifact: ArtifactRef, *, max_inline_bytes: int) -> ArtifactRef:
        """Move oversized inline artifacts to the controller blob store.

        Args:
            artifact: Artifact ref to inspect.
            max_inline_bytes: Size threshold for controller blob materialization.

        Returns:
            Controller blob ref when the inline artifact exceeds the threshold.
        """

        if artifact.storage_kind == StorageKind.CONTROLLER_BLOB:
            return self.materialize(artifact)
        if artifact.storage_kind == StorageKind.INLINE and artifact.size_bytes > max_inline_bytes:
            return self.materialize(artifact)
        return artifact

    def uri_for_sha256(self, sha256: str) -> str:
        """Return the protocol URI for a controller-local blob.

        Args:
            sha256: Blob content hash.

        Returns:
            `controller-blob://...` URI string.
        """

        return f"controller-blob://{sha256}"

    def path_for_sha256(self, sha256: str) -> Path:
        """Return the local filesystem path backing a controller blob URI.

        Args:
            sha256: Blob content hash.

        Returns:
            Filesystem path under the controller blob root.
        """

        return self.root / sha256

    @staticmethod
    def _verify_existing_blob(path: Path, artifact: ArtifactRef) -> None:
        """Verify a hash-named blob file before trusting or reusing it."""

        if not path.exists():
            raise ValueError("controller blob is missing for declared sha256")
        data = path.read_bytes()
        if len(data) != artifact.size_bytes or hashlib.sha256(data).hexdigest() != artifact.sha256:
            raise ValueError("controller blob integrity mismatch for existing sha256 file")
