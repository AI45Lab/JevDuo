"""Pure helpers shared by one-task Controller execution modes."""

from __future__ import annotations

from typing import cast

from northcua.evaluation.mode import EXTERNAL_SCRIPT_EVALUATION_MODE
from northcua.submitter.specs import DesktopSubmitSpec, WebSubmitSpec
from northcua_protocol.capabilities import Capability
from northcua_protocol.payloads import (
    TaskAccepted,
    TaskFinished,
    TaskRequest,
    TaskStatusSnapshot,
    task_request_fingerprint_sha256,
)


def external_script_submit_payload(
    spec: DesktopSubmitSpec | WebSubmitSpec,
    *,
    device_id: str,
    policy_route_ref: str,
    metadata: dict[str, object] | None = None,
) -> dict[str, object]:
    """Project an existing Desktop/Web spec into one external-script request."""

    payload = dict(spec.submit_payload())
    projected_metadata = dict(cast(dict[str, object], payload["metadata"]))
    projected_metadata.update(metadata or {})
    projected_metadata.update(
        {
            "evaluation_mode": EXTERNAL_SCRIPT_EVALUATION_MODE,
            "policy_route_ref": policy_route_ref,
        }
    )
    return {
        "agent_kind": cast(str, payload["agent_kind"]),
        "request": cast(str, payload["request"]),
        "target_device_id": device_id,
        "required_capabilities": cast(set[Capability], payload["required_capabilities"]),
        "metadata": projected_metadata,
    }


def task_request_for_payload(
    payload: dict[str, object],
    *,
    task_id: str,
    trace_id: str,
    token: str,
) -> TaskRequest:
    """Build the exact protocol request used for durable identity hashing."""

    return TaskRequest(
        task_id=task_id,
        trace_id=trace_id,
        agent_kind=cast(str, payload["agent_kind"]),
        request=cast(str, payload["request"]),
        token=token,
        target_device_id=cast(str, payload["target_device_id"]),
        required_capabilities=cast(set[Capability], payload["required_capabilities"]),
        metadata=cast(dict[str, object], payload["metadata"]),
    )


def acceptance_from_exact_snapshot(snapshot: TaskStatusSnapshot, request: TaskRequest) -> TaskAccepted:
    """Recover acceptance only when Controller proves the complete request identity."""

    expected = task_request_fingerprint_sha256(request)
    if snapshot.request_fingerprint_sha256 != expected or not snapshot.session_id:
        raise RuntimeError("controller task snapshot belongs to a different request")
    return TaskAccepted(
        task_id=snapshot.task_id,
        trace_id=snapshot.trace_id,
        session_id=snapshot.session_id,
        device_id=snapshot.device_id,
    )


def snapshot_to_finished(snapshot: TaskStatusSnapshot) -> TaskFinished:
    """Convert a terminal Controller status snapshot without changing facts."""

    return TaskFinished(
        task_id=snapshot.task_id,
        trace_id=snapshot.trace_id,
        session_id=snapshot.session_id,
        state=snapshot.state,
        device_id=snapshot.device_id,
        lease_id=snapshot.lease_id,
        result=snapshot.result,
        artifacts=list(snapshot.artifacts),
        error=snapshot.error or None,
        terminal_decision=dict(snapshot.terminal_decision),
    )


__all__ = [
    "acceptance_from_exact_snapshot",
    "external_script_submit_payload",
    "snapshot_to_finished",
    "task_request_for_payload",
]
