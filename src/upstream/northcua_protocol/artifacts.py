"""Artifact references returned by device workers and controller storage."""

from __future__ import annotations

import base64
import hashlib
import re
from enum import Enum

from pydantic import Field, model_validator

from northcua_protocol.base import ProtocolModel

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


class ArtifactKind(str, Enum):
    """Supported artifact categories used by task, session, and command traces."""

    SCREENSHOT = "screenshot"
    ACCESSIBILITY_TREE = "accessibility_tree"
    TOOL_RESULT = "tool_result"
    TEXT = "text"
    JSON = "json"
    OTHER = "other"


class StorageKind(str, Enum):
    """Artifact storage location strategy."""

    INLINE = "inline"
    CONTROLLER_BLOB = "controller_blob"
    DEVICE_LOCAL = "device_local"
    FUTURE_S3 = "future_s3"


class ArtifactScope(str, Enum):
    """Lifecycle scope associated with one artifact."""

    COMMAND = "command"
    SESSION = "session"
    TASK = "task"


class RetentionPolicy(str, Enum):
    """Retention hint for artifact stores."""

    EPHEMERAL = "ephemeral"
    SESSION = "session"
    TASK = "task"
    LONG_TERM = "long_term"


class ArtifactRef(ProtocolModel):
    """Portable artifact reference with integrity and retention metadata.

    Attributes:
        artifact_id: Stable id used to correlate events, traces, and blob-store
            records.
        kind: High-level category of the artifact payload.
        storage_kind: Where the bytes are stored or how they can be fetched.
        uri: Storage-specific URI for non-inline artifacts.
        mime_type: MIME type used by consumers when materializing the payload.
        sha256: Lowercase SHA-256 digest of the artifact bytes.
        size_bytes: Raw byte length before base64 encoding.
        inline_data: Base64 payload for small inline artifacts.
        artifact_scope: Entity lifecycle that owns the artifact.
        retention_policy: Retention hint for controller/device stores.
    """

    artifact_id: str
    kind: ArtifactKind
    storage_kind: StorageKind
    uri: str = ""
    mime_type: str = "application/octet-stream"
    sha256: str
    size_bytes: int
    inline_data: str | None = None
    artifact_scope: ArtifactScope = ArtifactScope.COMMAND
    retention_policy: RetentionPolicy = RetentionPolicy.TASK

    @model_validator(mode="after")
    def _validate_inline_hash(self) -> "ArtifactRef":
        """Validate integrity metadata for every storage strategy."""

        if self.size_bytes < 0:
            raise ValueError("ArtifactRef size_bytes must be non-negative")
        if not _SHA256_RE.match(self.sha256):
            raise ValueError("ArtifactRef sha256 must be a 64-character lowercase hex digest")
        if self.storage_kind == StorageKind.CONTROLLER_BLOB:
            expected_uri = f"controller-blob://{self.sha256}"
            if self.uri != expected_uri:
                raise ValueError("ArtifactRef controller_blob uri must match sha256")
        elif self.storage_kind in {StorageKind.DEVICE_LOCAL, StorageKind.FUTURE_S3} and not self.uri:
            raise ValueError(f"ArtifactRef {self.storage_kind.value} uri is required")
        if self.storage_kind != StorageKind.INLINE:
            if self.inline_data is not None:
                raise ValueError("ArtifactRef inline_data is only valid for inline storage")
            return self
        if self.inline_data is None:
            raise ValueError("ArtifactRef inline storage requires inline_data")
        raw = base64.b64decode(self.inline_data)
        actual = hashlib.sha256(raw).hexdigest()
        if actual != self.sha256:
            raise ValueError("ArtifactRef inline_data sha256 mismatch")
        if len(raw) != self.size_bytes:
            raise ValueError("ArtifactRef inline_data size mismatch")
        return self

    @classmethod
    def from_inline_bytes(
        cls,
        *,
        artifact_id: str,
        kind: ArtifactKind,
        data: bytes,
        mime_type: str,
        artifact_scope: ArtifactScope = ArtifactScope.COMMAND,
        retention_policy: RetentionPolicy = RetentionPolicy.TASK,
    ) -> "ArtifactRef":
        """Build an inline artifact reference from raw bytes.

        Args:
            artifact_id: Stable id for trace/event correlation.
            kind: Artifact category.
            data: Raw artifact bytes to encode inline.
            mime_type: MIME type associated with ``data``.
            artifact_scope: Owning protocol entity scope.
            retention_policy: Retention hint for storage backends.

        Returns:
            Artifact reference carrying base64 inline bytes and integrity data.
        """

        return cls(
            artifact_id=artifact_id,
            kind=kind,
            storage_kind=StorageKind.INLINE,
            mime_type=mime_type,
            sha256=hashlib.sha256(data).hexdigest(),
            size_bytes=len(data),
            inline_data=base64.b64encode(data).decode("ascii"),
            artifact_scope=artifact_scope,
            retention_policy=retention_policy,
        )

    @classmethod
    def from_controller_blob(
        cls,
        *,
        artifact_id: str,
        kind: ArtifactKind,
        uri: str,
        mime_type: str,
        sha256: str,
        size_bytes: int,
        artifact_scope: ArtifactScope = ArtifactScope.COMMAND,
        retention_policy: RetentionPolicy = RetentionPolicy.TASK,
    ) -> "ArtifactRef":
        """Build an artifact reference for bytes stored by the controller.

        Args:
            artifact_id: Stable id for trace/event correlation.
            kind: Artifact category.
            uri: Controller blob URI, normally ``controller-blob://<sha256>``.
            mime_type: MIME type associated with the stored bytes.
            sha256: Lowercase SHA-256 digest of the stored bytes.
            size_bytes: Raw byte length.
            artifact_scope: Owning protocol entity scope.
            retention_policy: Retention hint for storage backends.

        Returns:
            Artifact reference pointing at controller-managed blob storage.
        """

        return cls(
            artifact_id=artifact_id,
            kind=kind,
            storage_kind=StorageKind.CONTROLLER_BLOB,
            uri=uri,
            mime_type=mime_type,
            sha256=sha256,
            size_bytes=size_bytes,
            artifact_scope=artifact_scope,
            retention_policy=retention_policy,
        )
