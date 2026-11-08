"""Argparse helpers for retry policy flags."""

from __future__ import annotations


def parse_retry_backoff_seconds(raw: str | None) -> list[float]:
    """Parse comma-separated retry backoff delays from CLI input.

    Args:
        raw: Comma-separated delay string from argparse.

    Returns:
        Parsed non-negative delay values in seconds.

    Raises:
        ValueError: If any parsed delay is negative or not numeric.
    """

    if raw is None or not str(raw).strip():
        return []
    values: list[float] = []
    for item in str(raw).split(","):
        item = item.strip()
        if not item:
            continue
        value = float(item)
        if value < 0:
            raise ValueError("infra retry backoff delays must be non-negative")
        values.append(value)
    return values
