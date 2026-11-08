"""Neutral parsing for structured guest-capability command evidence."""

from __future__ import annotations

import json
import re

from northcua_protocol.capabilities import JsonObject

GUEST_CAPABILITY_GATE_LABEL = "guest-capability-gate"
GUEST_CAPABILITY_EVIDENCE_PREFIX = "NORTHCUA_GUEST_CAPABILITY_EVIDENCE="
_EVIDENCE_PATTERN = re.compile(rf"{GUEST_CAPABILITY_EVIDENCE_PREFIX}(\{{[^{{}}\r\n]*\}})")


def guest_capability_evidence_payloads(value: str) -> list[JsonObject]:
    """Extract validated JSON records from guest-capability command output."""

    evidence: list[JsonObject] = []
    seen: set[tuple[str, str, str, int]] = set()
    for match in _EVIDENCE_PATTERN.finditer(value):
        try:
            payload = json.loads(match.group(1))
        except json.JSONDecodeError:
            continue
        if not isinstance(payload, dict):
            continue
        capability = payload.get("capability")
        status = payload.get("status")
        probe = payload.get("probe")
        exit_code = payload.get("exit_code")
        if (
            not isinstance(capability, str)
            or status not in {"available", "unavailable"}
            or not isinstance(probe, str)
            or not isinstance(exit_code, int)
            or isinstance(exit_code, bool)
        ):
            continue
        key = (capability, status, probe, exit_code)
        if key in seen:
            continue
        evidence.append(
            {
                "capability": capability,
                "status": status,
                "probe": probe,
                "exit_code": exit_code,
            }
        )
        seen.add(key)
    return evidence
