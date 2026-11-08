"""Provider response payload normalization helpers."""

from __future__ import annotations

from typing import cast

from northcua.agentkernel.json_types import JsonValue

from .state import ProviderResponsePayload


def _safe_model_dump(payload: JsonValue) -> ProviderResponsePayload:
    """Best-effort conversion of provider payloads into dictionaries.

    Args:
        payload: Raw provider payload or Pydantic-like model.

    Returns:
        JSON-like dictionary suitable for trace persistence.
    """

    if isinstance(payload, dict):
        return cast(ProviderResponsePayload, payload)
    model_dump = getattr(payload, "model_dump", None)
    if callable(model_dump):
        try:
            return cast(ProviderResponsePayload, model_dump(mode="json"))
        except TypeError:
            return cast(ProviderResponsePayload, model_dump())
    return {"value": str(payload)}


__all__ = ["_safe_model_dump"]
