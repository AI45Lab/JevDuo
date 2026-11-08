"""Time coercion helpers for batch health reporting."""

from __future__ import annotations

from datetime import datetime


def parse_time_to_epoch(value: str) -> float:
    """Parse a numeric or ISO timestamp into Unix epoch seconds.

    Args:
        value: Numeric epoch string or ISO timestamp.

    Returns:
        Parsed Unix epoch seconds, or ``0.0`` when parsing fails.
    """

    if not value:
        return 0.0
    try:
        return float(value)
    except ValueError:
        pass
    normalized = value.replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(normalized).timestamp()
    except ValueError:
        return 0.0


def epoch(value: object) -> float:
    """Coerce a numeric or string timestamp into Unix epoch seconds.

    Args:
        value: Numeric timestamp, string timestamp, or empty value.

    Returns:
        Parsed Unix epoch seconds, or ``0.0`` when parsing fails.
    """

    if isinstance(value, int | float):
        return float(value)
    return parse_time_to_epoch(str(value or ""))
