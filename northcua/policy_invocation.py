"""Task-local policy invocation identity propagated to provider transports."""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from urllib.parse import quote

from northcua.core.json_types import JsonObject

# Retain the established wire header for existing policy endpoints.
POLICY_STEP_HEADER = "X-NexCUA-Controller-Step-Ref"

_CURRENT_STEP_REF: ContextVar[str] = ContextVar("northcua_policy_step_ref", default="")


def policy_step_ref(context: Mapping[str, object] | None) -> str:
    """Derive one stable model-call identity from existing Controller step facts."""

    values = dict(context or {})
    session_id = str(values.get("northcua_session_id") or values.get("session_id") or "").strip()
    agent_kind = str(values.get("agent_kind") or "agent").strip()
    round_number = values.get("round_num") or values.get("query_index") or 1
    step_number = values.get("session_step") or values.get("step_index") or values.get("round_step")
    if not session_id or not isinstance(step_number, int) or isinstance(step_number, bool) or step_number < 1:
        return ""
    return f"{session_id}:{agent_kind}:{round_number}:{step_number}"


@contextmanager
def use_policy_step_ref(step_ref: str) -> Iterator[None]:
    """Bind one step reference across the asynchronous provider call only."""

    token = _CURRENT_STEP_REF.set(step_ref)
    try:
        yield
    finally:
        _CURRENT_STEP_REF.reset(token)


def policy_step_extra_headers() -> JsonObject:
    """Return SDK-compatible extra headers without exposing credentials."""

    step_ref = _CURRENT_STEP_REF.get()
    # Preserve logical IDs internally; HTTP header values must be printable ASCII.
    return {} if not step_ref else {POLICY_STEP_HEADER: quote(step_ref, safe=":-._~")}


__all__ = ["POLICY_STEP_HEADER", "policy_step_extra_headers", "policy_step_ref", "use_policy_step_ref"]
