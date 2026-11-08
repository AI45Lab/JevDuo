"""Telemetry-specific redaction helpers for safe monitor payloads.

The telemetry stream is designed for dashboards, CLI monitors, and audit
reports, so diagnostic text may be viewed outside the process that produced it.
These helpers apply shared NorthCUA log redaction and an additional pass for
common ``key=value`` secret fragments before events are written to controller or
local JSONL sinks.
"""

from __future__ import annotations

import re

from northcua_runtime_shared.redaction import redact_for_log

_SECRET_ASSIGNMENT = re.compile(r"(?i)\b(token|api[_-]?key|secret|password|bearer)\s*[:=]\s*([^\s,;]+)")


def redact_message(message: str) -> str:
    """Redact secret-looking substrings in one telemetry message.

    Args:
        message: Error or diagnostic string.

    Returns:
        Message with simple ``key=value`` secret fragments removed.
    """

    redacted = redact_for_log(message)
    text = redacted if isinstance(redacted, str) else str(redacted)
    return _SECRET_ASSIGNMENT.sub(lambda match: f"{match.group(1)}=<redacted>", text)


def redact_details(details: dict[str, object]) -> dict[str, object]:
    """Redact structured telemetry details.

    Args:
        details: Small diagnostic mapping.

    Returns:
        Redacted details mapping.
    """

    redacted = redact_for_log(details)
    if not isinstance(redacted, dict):
        return {}
    return {str(key): redact_message(value) if isinstance(value, str) else value for key, value in redacted.items()}
