"""Small value coercion helpers for batch health reports."""

from __future__ import annotations

JsonDict = dict[str, object]


def report_value(report: JsonDict | None, key: str) -> str:
    """Read a report field as a string.

    Args:
        report: Optional nested report dictionary.
        key: Field name to read.

    Returns:
        String value for the field, or an empty string when absent.
    """

    if not isinstance(report, dict):
        return ""
    return str(report.get(key) or "").strip().lower()


def to_float(value: object) -> float:
    """Coerce a SQLite/JSON value to float for best-effort metrics.

    Args:
        value: Raw value read from SQLite, JSON, or telemetry payloads.

    Returns:
        Float value, or ``0.0`` when conversion fails.
    """

    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def to_int(value: object) -> int:
    """Coerce a SQLite/JSON value to int for best-effort metrics.

    Args:
        value: Raw value read from SQLite, JSON, or telemetry payloads.

    Returns:
        Integer value, or zero when conversion fails.
    """

    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def shorten(value: str, limit: int) -> str:
    """Truncate long error samples while preserving a visible suffix marker.

    Args:
        value: Error or sample text to truncate.
        limit: Maximum returned string length.

    Returns:
        Original value when short enough, otherwise a shortened value ending in
        an ellipsis marker.
    """

    if len(value) <= limit:
        return value
    return value[: max(limit - 3, 0)] + "..."
