"""SQLite-safe result compaction for controller terminal payloads."""

from __future__ import annotations

import hashlib
import json
import re

from northcua_protocol.artifacts import ArtifactRef, StorageKind
from northcua_protocol.payloads import CommandResult, TaskFinished

_DATA_IMAGE_RE = re.compile(r"^data:(image/[-+.a-zA-Z0-9]+);base64,(.*)$", re.DOTALL)
_IMAGE_URL_KEYS = {
    "annotated_screenshot_url",
    "image_url",
    "overlay_image_url",
    "raw_image_url",
    "screenshot_url",
}
_HEAVY_TREE_KEYS = {
    "accessibility_tree",
    "children",
    "raw_accessibility_tree",
    "raw_tree",
    "tree",
}
_HEAVY_TEXT_KEYS = {
    "accessibility_tree_markdown",
    "raw_tree_markdown",
    "tree_markdown",
}
_MAX_SQLITE_TEXT_CHARS = 16_384
_MAX_SQLITE_CONTAINER_BYTES = 64_000
_PREVIEW_CHARS = 256


def compact_command_result_for_sqlite(result: CommandResult) -> CommandResult:
    """Return a command result whose JSON payload is safe to persist in SQLite.

    The live controller response still carries the full device payload. This
    helper is only for the durable controller DB copy, where large screenshots
    and accessibility trees are duplicated by agent-artifact files.

    Args:
        result: Full command result payload received from a device.

    Returns:
        Copy of the command result safe for controller SQLite persistence.
    """

    compacted_result = compact_sqlite_json_value(result.result)
    artifacts, omitted_artifacts = _compact_artifacts_for_result_json(result.artifacts)
    return result.model_copy(
        update={
            "result": _attach_omitted_artifact_metadata(compacted_result, omitted_artifacts),
            "artifacts": artifacts,
        }
    )


def compact_task_finished_for_sqlite(payload: TaskFinished) -> TaskFinished:
    """Return a task terminal payload whose SQLite copy omits large result blobs.

    Args:
        payload: Full task terminal payload before durable persistence.

    Returns:
        Copy of the terminal payload with oversized result fields compacted.
    """

    artifacts, omitted_artifacts = _compact_artifacts_for_result_json(payload.artifacts)
    compacted_result = compact_sqlite_json_value(payload.result)
    return payload.model_copy(
        update={
            "result": _attach_omitted_artifact_metadata(compacted_result, omitted_artifacts),
            "artifacts": artifacts,
            "terminal_decision": compact_sqlite_json_value(payload.terminal_decision),
        }
    )


def compact_artifact_ref_json_for_sqlite(artifact: ArtifactRef) -> str:
    """Serialize one artifact catalog row without inline payload bytes.

    Args:
        artifact: Artifact reference to persist in the controller catalog.

    Returns:
        Compact JSON string for the artifact row.
    """

    payload = artifact.model_dump(mode="json")
    if artifact.storage_kind == StorageKind.INLINE and artifact.inline_data is not None:
        payload.pop("inline_data", None)
        payload["inline_data_omitted"] = True
        payload["omitted_reason"] = "full artifact payload is stored under agent-artifacts"
    elif artifact.inline_data is None:
        payload.pop("inline_data", None)
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def compact_sqlite_json_value(value: object) -> object:
    """Compact one JSON-like value while preserving useful status and refs.

    Args:
        value: JSON-like payload value.

    Returns:
        Payload with large image, tree, and text values replaced by metadata.
    """

    return _compact_value(value, key="")


def _compact_artifacts_for_result_json(
    artifacts: list[ArtifactRef],
) -> tuple[list[ArtifactRef], list[dict[str, object]]]:
    retained: list[ArtifactRef] = []
    omitted: list[dict[str, object]] = []
    for artifact in artifacts:
        if artifact.storage_kind == StorageKind.INLINE and artifact.inline_data is not None:
            omitted.append(_artifact_metadata(artifact))
            retained.append(
                artifact.model_copy(
                    update={
                        "storage_kind": StorageKind.DEVICE_LOCAL,
                        "uri": f"agent-artifacts://sha256/{artifact.sha256}/{artifact.artifact_id}",
                        "inline_data": None,
                    }
                )
            )
            continue
        retained.append(artifact)
    return retained, omitted


def _attach_omitted_artifact_metadata(value: object, artifacts: list[dict[str, object]]) -> object:
    if not artifacts:
        return value
    if isinstance(value, dict):
        return {**value, "sqlite_omitted_artifacts": artifacts}
    if value is None:
        return {"sqlite_omitted_artifacts": artifacts}
    return {"value": value, "sqlite_omitted_artifacts": artifacts}


def _artifact_metadata(artifact: ArtifactRef) -> dict[str, object]:
    return {
        "artifact_id": artifact.artifact_id,
        "kind": artifact.kind.value,
        "storage_kind": artifact.storage_kind.value,
        "mime_type": artifact.mime_type,
        "sha256": artifact.sha256,
        "size_bytes": artifact.size_bytes,
        "uri": artifact.uri,
        "inline_data_omitted": True,
        "omitted_reason": "full artifact payload is stored under agent-artifacts",
    }


def _compact_value(value: object, *, key: str) -> object:
    if isinstance(value, dict):
        return _compact_dict(value, key=key)
    if isinstance(value, list):
        return _compact_list(value, key=key)
    if isinstance(value, str):
        return _compact_string(value, key=key)
    return value


def _compact_dict(value: dict[object, object], *, key: str) -> dict[str, object]:
    if _is_heavy_tree_key(key):
        return _omitted_metadata(value, kind=_omitted_kind_for_key(key))
    compacted: dict[str, object] = {}
    for raw_key, child in value.items():
        child_key = str(raw_key)
        if child_key in _IMAGE_URL_KEYS and isinstance(child, str):
            replacement, metadata = _compact_image_url(child)
            compacted[child_key] = replacement
            if metadata is not None:
                compacted[f"{child_key}_omitted"] = metadata
            continue
        if _is_heavy_tree_key(child_key):
            compacted[child_key] = _omitted_metadata(child, kind=_omitted_kind_for_key(child_key))
            continue
        compacted[child_key] = _compact_value(child, key=child_key)
    if key and _encoded_size(compacted) > _MAX_SQLITE_CONTAINER_BYTES and _looks_like_accessibility_payload(compacted):
        return _omitted_metadata(compacted, kind="accessibility_payload")
    return compacted


def _compact_list(value: list[object], *, key: str) -> object:
    if _is_heavy_tree_key(key):
        return _omitted_metadata(value, kind=_omitted_kind_for_key(key))
    compacted = [_compact_value(child, key=key) for child in value]
    if key and _encoded_size(compacted) > _MAX_SQLITE_CONTAINER_BYTES:
        return _omitted_metadata(compacted, kind="large_list")
    return compacted


def _compact_string(value: str, *, key: str) -> object:
    if key in _IMAGE_URL_KEYS:
        replacement, metadata = _compact_image_url(value)
        if metadata is not None:
            return replacement
    match = _DATA_IMAGE_RE.match(value)
    if match is not None:
        mime_type, encoded = match.groups()
        return {
            "omitted": True,
            "kind": "image_data_url",
            "mime_type": mime_type,
            "sha256": hashlib.sha256(value.encode("utf-8")).hexdigest(),
            "size_bytes": len(value.encode("utf-8")),
            "encoded_chars": len(encoded),
        }
    if key in _HEAVY_TEXT_KEYS or len(value) > _MAX_SQLITE_TEXT_CHARS:
        return {
            "omitted": True,
            "kind": _omitted_kind_for_key(key) if key else "large_text",
            "sha256": hashlib.sha256(value.encode("utf-8")).hexdigest(),
            "size_bytes": len(value.encode("utf-8")),
            "preview": value[:_PREVIEW_CHARS],
        }
    return value


def _compact_image_url(value: str) -> tuple[str, dict[str, object] | None]:
    match = _DATA_IMAGE_RE.match(value)
    if match is None:
        return value, None
    mime_type, encoded = match.groups()
    metadata: dict[str, object] = {
        "kind": "image_data_url",
        "mime_type": mime_type,
        "sha256": hashlib.sha256(value.encode("utf-8")).hexdigest(),
        "size_bytes": len(value.encode("utf-8")),
        "encoded_chars": len(encoded),
    }
    return "", metadata


def _omitted_metadata(value: object, *, kind: str) -> dict[str, object]:
    encoded = _encoded_bytes(value)
    metadata: dict[str, object] = {
        "omitted": True,
        "kind": kind,
        "sha256": hashlib.sha256(encoded).hexdigest(),
        "size_bytes": len(encoded),
    }
    if kind in {"accessibility_tree", "accessibility_children", "accessibility_payload"}:
        stats = _accessibility_stats(value)
        metadata.update(
            {
                "node_count": stats["node_count"],
                "max_depth": stats["max_depth"],
            }
        )
    return metadata


def _omitted_kind_for_key(key: str) -> str:
    if key == "children":
        return "accessibility_children"
    if key in _HEAVY_TEXT_KEYS:
        return "accessibility_text"
    if key in _HEAVY_TREE_KEYS:
        return "accessibility_tree"
    return "large_payload"


def _is_heavy_tree_key(key: str) -> bool:
    return key in _HEAVY_TREE_KEYS


def _looks_like_accessibility_payload(value: dict[str, object]) -> bool:
    keys = set(value)
    return bool(keys & {"node_count", "max_depth", "focused_path", "platform_name", "scope", "source", "status"})


def _accessibility_stats(value: object) -> dict[str, int]:
    """Return approximate accessibility tree node and depth stats.

    Args:
        value: Compacted or raw accessibility payload.

    Returns:
        Mapping with ``node_count`` and ``max_depth``.
    """

    def _visit(item: object, depth: int) -> tuple[int, int]:
        if isinstance(item, dict):
            count = 1
            max_depth = depth
            children = item.get("children")
            if isinstance(children, list):
                for child in children:
                    child_count, child_depth = _visit(child, depth + 1)
                    count += child_count
                    max_depth = max(max_depth, child_depth)
            return count, max_depth
        if isinstance(item, list):
            count = 0
            max_depth = depth
            for child in item:
                child_count, child_depth = _visit(child, depth + 1)
                count += child_count
                max_depth = max(max_depth, child_depth)
            return count, max_depth
        return 0, depth

    node_count, max_depth = _visit(value, 0)
    return {"node_count": node_count, "max_depth": max_depth}


def _encoded_size(value: object) -> int:
    return len(_encoded_bytes(value))


def _encoded_bytes(value: object) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")
