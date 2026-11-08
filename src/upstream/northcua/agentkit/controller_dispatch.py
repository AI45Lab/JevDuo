"""Domain-neutral controller remote-dispatch helpers for AgentKit agents."""

from __future__ import annotations

from northcua.core.enums import ResultStatus
from northcua.core.json_types import JsonObject
from northcua.core.models import CommandExecutionResultRecord
from northcua_protocol.artifacts import ArtifactRef, StorageKind
from northcua_protocol.payloads import CommandResult
from northcua_protocol.runtime_sidecars import split_runtime_sidecar
from northcua_protocol.state import CommandState


def command_result_record(call_id: str, result: CommandResult) -> CommandExecutionResultRecord:
    """Convert controller protocol command results to NorthCUA runtime records.

    Args:
        call_id: Runtime call id that should own the returned tool result.
        result: Protocol command result returned by the remote device.

    Returns:
        Runtime command execution record used by existing trace/replay code.
    """

    status = ResultStatus.SUCCESS if result.status == CommandState.SUCCEEDED else ResultStatus.FAILURE
    payload, _sidecar = split_runtime_sidecar(result.result)
    if isinstance(payload, dict) and result.artifacts:
        payload = _rehydrate_inline_artifacts(payload, result.artifacts)
    elif payload is None and result.artifacts:
        inline_image_url = _first_inline_image_data_url(result.artifacts)
        if inline_image_url:
            payload = {"screenshot_url": inline_image_url}
    return CommandExecutionResultRecord(
        call_id=call_id,
        status=status,
        error=result.error,
        result=normalize_remote_tool_payload(payload),
    )


def failed_command_result_record(*, call_id: str, error: str) -> CommandExecutionResultRecord:
    """Build a failed model tool result for pre-dispatch parameter rejection.

    Descriptor validation can reject model-generated arguments before a command
    reaches the device. Representing that rejection as a correlated tool result
    lets the owning agent classify the error and correct its next call.
    """

    return CommandExecutionResultRecord(
        call_id=call_id,
        status=ResultStatus.FAILURE,
        error=error,
        result={"status": "error", "error": error},
    )


def tool_error_command_result_record(*, call_id: str, payload: JsonObject) -> CommandExecutionResultRecord:
    """Build a transport-successful model-facing tool error record.

    The remote command did not run, but the agent runtime successfully handled
    the model's invalid arguments. This mirrors device-side Web validation,
    where the command result succeeds and its typed payload carries
    ``status=error`` plus retry guidance.
    """

    return CommandExecutionResultRecord(
        call_id=call_id,
        status=ResultStatus.SUCCESS,
        result=payload,
    )


def normalize_remote_tool_payload(value: object) -> JsonObject | str | None:
    """Return JSON-like remote command payloads in local runtime shape.

    Args:
        value: Raw protocol result value from a remote command.

    Returns:
        Dict/string/``None`` payload matching local tool execution records.
    """

    value, _sidecar = split_runtime_sidecar(value)
    if isinstance(value, dict):
        return {str(key): item for key, item in value.items()}
    return value if isinstance(value, str) or value is None else str(value)


def _rehydrate_inline_artifacts(value: object, artifacts: list[ArtifactRef]) -> object:
    artifacts_by_id: dict[str, ArtifactRef] = {}
    artifacts_by_sha: dict[str, ArtifactRef] = {}
    for artifact in artifacts:
        artifacts_by_id[artifact.artifact_id] = artifact
        artifacts_by_sha[artifact.sha256] = artifact

    def _visit(item: object) -> object:
        if isinstance(item, dict):
            normalized = {str(key): _visit(child) for key, child in item.items()}
            for key, artifact_id in list(normalized.items()):
                if not key.endswith("_artifact_id") or not isinstance(artifact_id, str):
                    continue
                original_key = key.removesuffix("_artifact_id")
                original_id = normalized.get(f"{key}_original")
                sha = normalized.get(f"{original_key}_artifact_sha256")
                artifact = (
                    artifacts_by_id.get(artifact_id)
                    or (artifacts_by_id.get(original_id) if isinstance(original_id, str) else None)
                    or (artifacts_by_sha.get(sha) if isinstance(sha, str) else None)
                )
                if artifact is None or not artifact.mime_type.startswith("image/"):
                    continue
                if (
                    normalized.get(original_key) in ("", None)
                    and artifact.storage_kind == StorageKind.INLINE
                    and artifact.inline_data
                ):
                    normalized[original_key] = f"data:{artifact.mime_type};base64,{artifact.inline_data}"
                normalized[f"{original_key}_artifact_sha256"] = artifact.sha256
                normalized[f"{original_key}_artifact_mime_type"] = artifact.mime_type
                normalized[f"{original_key}_artifact_storage_kind"] = artifact.storage_kind.value
            return normalized
        if isinstance(item, list):
            return [_visit(child) for child in item]
        return item

    return _visit(value)


def _first_inline_image_data_url(artifacts: list[ArtifactRef]) -> str:
    for artifact in artifacts:
        if artifact.storage_kind != StorageKind.INLINE or artifact.inline_data is None:
            continue
        if not artifact.mime_type.startswith("image/"):
            continue
        return f"data:{artifact.mime_type};base64,{artifact.inline_data}"
    return ""
