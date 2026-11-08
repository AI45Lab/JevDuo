"""Identifier and timestamp helpers for controller persistence."""

from __future__ import annotations

from datetime import datetime, timezone

from northcua_protocol.artifacts import ArtifactRef


def utc_now_iso() -> str:
    """Return a timezone-aware UTC timestamp string.

    Returns:
        ISO-8601 UTC timestamp.
    """

    return datetime.now(timezone.utc).isoformat()


def artifact_catalog_id(
    artifact: ArtifactRef,
    *,
    trace_id: str,
    task_id: str = "",
    session_id: str = "",
    command_id: str = "",
) -> str:
    """Build a stable controller-owned artifact catalog identifier.

    Args:
        artifact: Artifact reference being cataloged.
        trace_id: Trace id associated with the artifact.
        task_id: Optional task id for task-scoped artifacts.
        session_id: Optional session id for session-scoped artifacts.
        command_id: Optional command id for command output artifacts.

    Returns:
        Stable artifact catalog id derived from trace/entity ids and hash.
    """

    parts = [trace_id, task_id, session_id, command_id, artifact.artifact_id, artifact.sha256]
    return "::".join(part for part in parts if part)


def command_storage_key(*, session_id: str, lease_id: str, command_id: str) -> str:
    """Build the controller-private key for one command row.

    Args:
        session_id: Session that owns the command.
        lease_id: Lease generation that authorized the command.
        command_id: Protocol-visible command id.

    Returns:
        Stable storage key that isolates same-named commands across sandboxes.
    """

    return "::".join((session_id, lease_id, command_id))
