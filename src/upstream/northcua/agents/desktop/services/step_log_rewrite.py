"""Helpers for safely rewriting step JSONL artifacts."""

from __future__ import annotations

import json
import logging
import os
from collections.abc import Sequence
from pathlib import Path

from northcua.core.models import StepLogRecord

logger = logging.getLogger(__name__)


def rewrite_step_logs_preserving_existing(
    records: Sequence[StepLogRecord],
    step_log_path: Path,
) -> None:
    """Rewrite step JSONL without persisting compacted in-memory records.

    Older steps may have heavyweight in-memory fields released after the full
    row has already been written to ``steps.jsonl``. Rewrite paths are rare but
    can happen for verification and terminal overrides, so they must write the
    current status/metadata while preserving heavy fields from existing rows.
    """

    step_log_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = step_log_path.with_name(f"{step_log_path.name}.rewrite.tmp")
    existing_handle = step_log_path.open("r", encoding="utf-8") if step_log_path.exists() else None
    try:
        with tmp_path.open("w", encoding="utf-8") as output:
            for record in records:
                existing_line, existing_payload = _next_existing_payload(existing_handle)
                payload = record.model_dump(mode="json")
                if (
                    existing_line is not None
                    and existing_payload is not None
                    and _existing_payload_same_step(
                        existing_payload,
                        record,
                    )
                ):
                    payload = _merge_missing_heavy_payloads(existing_payload, payload)
                json.dump(payload, output, ensure_ascii=False)
                output.write("\n")
        os.replace(tmp_path, step_log_path)
    finally:
        if existing_handle is not None:
            existing_handle.close()
        if tmp_path.exists():
            tmp_path.unlink()


def _next_existing_payload(handle) -> tuple[str | None, dict[str, object] | None]:
    """Read the next non-empty JSONL row from ``handle`` using one-row memory."""

    if handle is None:
        return None, None
    for line in handle:
        if not line.strip():
            continue
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            logger.warning("ignoring malformed step log row during rewrite preservation")
            return line, None
        if isinstance(payload, dict):
            return line, payload
        return line, None
    return None, None


def _merge_missing_heavy_payloads(
    existing_payload: dict[str, object],
    current_payload: dict[str, object],
) -> dict[str, object]:
    """Merge old heavy payload fields into the current step row only when missing."""

    for key in ("llm_reasoning", "llm_thinking", "llm_reasoning_encrypted_content"):
        if existing_payload.get(key) and not current_payload.get(key):
            current_payload[key] = existing_payload[key]

    _merge_result_image_payloads(existing_payload.get("tool_results"), current_payload.get("tool_results"))
    _merge_artifact_image_payloads(
        existing_payload.get("observation_artifacts"),
        current_payload.get("observation_artifacts"),
    )
    return current_payload


def _merge_result_image_payloads(existing_results: object, current_results: object) -> None:
    """Merge released image URLs for matching tool result identities."""

    if not isinstance(existing_results, list) or not isinstance(current_results, list):
        return
    existing_by_call_id = {
        str(existing.get("tool_call_id") or ""): existing
        for existing in existing_results
        if isinstance(existing, dict) and existing.get("tool_call_id")
    }
    for index, current in enumerate(current_results):
        if not isinstance(current, dict):
            continue
        call_id = str(current.get("tool_call_id") or "")
        existing = existing_by_call_id.get(call_id) if call_id else None
        if (
            existing is None
            and not call_id
            and index < len(existing_results)
            and isinstance(existing_results[index], dict)
            and not existing_results[index].get("tool_call_id")
        ):
            existing = existing_results[index]
        if existing is None:
            continue
        for key in ("raw_image_url", "overlay_image_url", "image_url"):
            if existing.get(key) and not current.get(key):
                current[key] = existing[key]


def _merge_artifact_image_payloads(existing_artifacts: object, current_artifacts: object) -> None:
    """Merge released observation artifact URLs for matching artifact identities."""

    if not isinstance(existing_artifacts, list) or not isinstance(current_artifacts, list):
        return
    existing_by_identity = {
        _artifact_identity(existing): existing
        for existing in existing_artifacts
        if isinstance(existing, dict) and _artifact_identity(existing) is not None
    }
    for index, current in enumerate(current_artifacts):
        if not isinstance(current, dict):
            continue
        identity = _artifact_identity(current)
        existing = existing_by_identity.get(identity) if identity is not None else None
        if (
            existing is None
            and identity is None
            and index < len(existing_artifacts)
            and isinstance(existing_artifacts[index], dict)
            and _artifact_identity(existing_artifacts[index]) is None
        ):
            existing = existing_artifacts[index]
        if existing is None:
            continue
        if existing.get("url") and not current.get("url"):
            current["url"] = existing["url"]


def _artifact_identity(artifact: dict[str, object]) -> tuple[str, str, str] | None:
    """Return a stable identity for an observation artifact when available."""

    path = str(artifact.get("path") or "")
    role = str(artifact.get("role") or "")
    source = str(artifact.get("source") or "")
    if not path and not role and not source:
        return None
    return path, role, source


def _existing_payload_same_step(payload: dict[str, object], record: StepLogRecord) -> bool:
    """Return whether an existing disk row still represents the live step."""

    return (
        str(payload.get("session_id") or "") == record.session_id
        and payload.get("session_step") == record.session_step
        and str(payload.get("request") or "") == record.request
    )
