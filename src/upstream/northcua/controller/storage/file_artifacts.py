"""Helpers for cataloging existing artifact files without copying payloads."""

from __future__ import annotations

import hashlib
from pathlib import Path
from urllib.parse import quote

from northcua_protocol.artifacts import ArtifactKind, ArtifactRef, ArtifactScope, RetentionPolicy, StorageKind

_HASH_CHUNK_BYTES = 1024 * 1024


def file_artifact_ref(
    *,
    artifact_id: str,
    kind: ArtifactKind,
    path: Path,
    mime_type: str,
    artifact_root: Path | None = None,
    artifact_scope: ArtifactScope = ArtifactScope.TASK,
    retention_policy: RetentionPolicy = RetentionPolicy.TASK,
) -> ArtifactRef:
    """Return a metadata-only ref for an artifact already stored on disk.

    Controller-owned runners write their full traces, screenshots, and
    accessibility payloads under ``agent-artifacts``. The controller catalog only
    needs integrity metadata plus a path-like reference; copying those bytes into
    ``controller-blobs`` creates a second source of truth and large duplicate
    storage growth.

    Args:
        artifact_id: Stable artifact id to expose in controller results.
        kind: Artifact kind, such as trace, screenshot, or log.
        path: Existing artifact file path.
        mime_type: MIME type of the artifact payload.
        artifact_root: Optional root used to make the artifact URI relative.
        artifact_scope: Scope used by artifact retention/cataloging.
        retention_policy: Retention policy for the artifact.

    Returns:
        Artifact reference containing URI, size, and checksum metadata.
    """

    resolved = path.resolve()
    return ArtifactRef(
        artifact_id=artifact_id,
        kind=kind,
        storage_kind=StorageKind.DEVICE_LOCAL,
        uri=_agent_artifact_uri(path=resolved, artifact_root=artifact_root, artifact_id=artifact_id),
        mime_type=mime_type,
        sha256=_sha256_file(resolved),
        size_bytes=resolved.stat().st_size,
        artifact_scope=artifact_scope,
        retention_policy=retention_policy,
    )


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(_HASH_CHUNK_BYTES), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _agent_artifact_uri(*, path: Path, artifact_root: Path | None, artifact_id: str) -> str:
    relative = _relative_artifact_path(path=path, artifact_root=artifact_root)
    uri_path = quote(relative or artifact_id, safe="/._-")
    return f"agent-artifacts://path/{uri_path}"


def _relative_artifact_path(*, path: Path, artifact_root: Path | None) -> str:
    if artifact_root is None:
        return path.name
    try:
        return path.relative_to(artifact_root.resolve()).as_posix()
    except ValueError:
        return path.name


__all__ = ["file_artifact_ref"]
