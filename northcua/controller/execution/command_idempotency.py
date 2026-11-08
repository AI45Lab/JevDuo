"""Command idempotency helpers for controller/device replay protection."""

from __future__ import annotations

import hashlib
import json


def command_idempotency_key(*, session_id: str, lease_id: str, command_id: str, payload: dict[str, object]) -> str:
    """Build a deterministic command idempotency key.

    Args:
        session_id: Controller session id.
        lease_id: Active lease id.
        command_id: Command id assigned by the caller.
        payload: Stable command payload used for conflict detection.

    Returns:
        SHA-256 idempotency key.
    """

    raw = json.dumps(
        {
            "session_id": session_id,
            "lease_id": lease_id,
            "command_id": command_id,
            "payload": payload,
        },
        sort_keys=True,
        ensure_ascii=False,
    )
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


__all__ = ["command_idempotency_key"]
