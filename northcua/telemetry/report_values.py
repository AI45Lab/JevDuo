"""Shared value coercion helpers for telemetry reporting."""

from __future__ import annotations

from datetime import datetime


class MetricValue:
    """Small coercion helpers for loose JSON metric payloads."""

    @staticmethod
    def as_int(value: object) -> int:
        """Convert a loose metric value to a non-negative integer-like value.

        Args:
            value: JSON value from traces, snapshots, or reports.

        Returns:
            Integer value when the input is an int or digit string; otherwise 0.
        """

        if isinstance(value, int):
            return value
        if isinstance(value, str) and value.isdigit():
            return int(value)
        return 0

    @staticmethod
    def as_float(value: object) -> float:
        """Convert a loose metric value to a non-negative float.

        Args:
            value: JSON value from traces, snapshots, reports, or event details.

        Returns:
            Float value when the input is numeric or numeric text; otherwise 0.0.
        """

        if isinstance(value, int | float):
            return max(float(value), 0.0)
        if isinstance(value, str):
            try:
                return max(float(value), 0.0)
            except ValueError:
                return 0.0
        return 0.0

    @staticmethod
    def as_int_map(value: object) -> dict[str, int]:
        """Convert a loose JSON object into a string-to-int metric map.

        Args:
            value: Potential JSON object from evaluator or snapshot metrics.

        Returns:
            Positive integer counts keyed by string.
        """

        if not isinstance(value, dict):
            return {}
        result: dict[str, int] = {}
        for key, item in value.items():
            metric = MetricValue.as_int(item)
            if metric > 0:
                result[str(key)] = metric
        return result

    @staticmethod
    def diagnosis_summary(value: object) -> str:
        """Render a diagnosis-count map for the markdown audit table.

        Args:
            value: Loose diagnosis category breakdown.

        Returns:
            Stable comma-separated summary, or an empty string when no counts exist.
        """

        counts = MetricValue.as_int_map(value)
        if not counts:
            return ""
        return ", ".join(f"{category}:{count}" for category, count in sorted(counts.items()))


class TimeValue:
    """Timestamp parsing used by elapsed-time metrics."""

    @staticmethod
    def parse(value: str) -> datetime | None:
        """Parse an ISO timestamp into a datetime.

        Args:
            value: ISO timestamp string.

        Returns:
            Parsed datetime, or ``None`` for invalid values.
        """

        try:
            return datetime.fromisoformat(value)
        except ValueError:
            return None
