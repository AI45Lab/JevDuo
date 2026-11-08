"""Runtime-only protocol payload sidecars.

Sidecars carry controller/device synchronization data alongside command
results. They are not model-visible tool output and should be stripped before
trace replay or ordinary command-result persistence.
"""

from __future__ import annotations

from collections.abc import Mapping

from northcua_protocol.capabilities import JsonObject

RUNTIME_SIDECAR_KEY = "_northcua_runtime"


def split_runtime_sidecar(payload: object) -> tuple[object, JsonObject]:
    """Return ``payload`` without runtime sidecar plus the sidecar payload."""

    if not isinstance(payload, Mapping):
        return payload, {}
    sidecar = payload.get(RUNTIME_SIDECAR_KEY)
    clean = {str(key): value for key, value in payload.items() if key != RUNTIME_SIDECAR_KEY}
    return clean, _json_object(sidecar)


def attach_runtime_sidecar(payload: object, sidecar: Mapping[str, object]) -> object:
    """Attach a runtime-only sidecar to a dict payload when possible."""

    if not isinstance(payload, Mapping):
        return payload
    clean_sidecar = _json_object(sidecar)
    if not clean_sidecar:
        return payload
    result = {str(key): value for key, value in payload.items() if key != RUNTIME_SIDECAR_KEY}
    result[RUNTIME_SIDECAR_KEY] = clean_sidecar
    return result


def _json_object(value: object) -> JsonObject:
    if not isinstance(value, Mapping):
        return {}
    return {str(key): item for key, item in value.items()}
