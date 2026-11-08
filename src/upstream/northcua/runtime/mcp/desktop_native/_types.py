"""Private type aliases for desktop-native backend boundaries."""

from __future__ import annotations

try:
    from typing import TypeAlias
except ImportError:  # pragma: no cover - compatibility for older ad-hoc test interpreters
    from typing_extensions import TypeAlias

JsonValue: TypeAlias = object
JsonObject: TypeAlias = dict[str, JsonValue]
WindowRect: TypeAlias = list[int]
