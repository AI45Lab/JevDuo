"""CUA-local aliases for intentional JSON-like runtime boundaries."""

from __future__ import annotations

from typing import TypeAlias

JsonValue: TypeAlias = object
JsonObject: TypeAlias = dict[str, JsonValue]
JsonArray: TypeAlias = list[JsonValue]

ProviderToolChoice: TypeAlias = JsonObject | str | None
ProviderToolSpec: TypeAlias = JsonObject
ProviderToolSpecs: TypeAlias = list[ProviderToolSpec]
