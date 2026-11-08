"""Shared recursive redaction for controller and device operator logs."""

from __future__ import annotations

import base64
import binascii
import re
from collections.abc import Mapping, Sequence

SECRET_KEY_MARKERS = ("token", "api_key", "authorization", "password", "secret", "credential")
INLINE_PAYLOAD_KEYS = ("inline_data", "base64", "base64_data")
_DATA_URL_RE = re.compile(r"^data:([^;,]+)?;base64,(.+)$", re.IGNORECASE | re.DOTALL)
_AUTH_VALUE_RE = re.compile(r"^(bearer|basic)\s+.+$", re.IGNORECASE)
_OPENAI_KEY_RE = re.compile(r"\bsk-[A-Za-z0-9_-]{12,}\b")
_BASE64ISH_RE = re.compile(r"^[A-Za-z0-9+/=\s]{256,}$")


def redact_for_log(value: object) -> object:
    """Recursively redact secrets and large inline payloads.

    Args:
        value: JSON-like value destined for an operator log, event, or CLI
            projection.

    Returns:
        Redacted value with shape preserved where possible.
    """

    return _redact(value, key="")


def _redact(value: object, *, key: str) -> object:
    if _is_secret_key(key):
        return "<redacted>"
    if _is_inline_payload_key(key) and isinstance(value, str):
        return f"<base64:{_decoded_base64_length(value)} bytes>"
    if isinstance(value, Mapping):
        return {str(item_key): _redact(item_value, key=str(item_key)) for item_key, item_value in value.items()}
    if isinstance(value, Sequence) and not isinstance(value, str | bytes | bytearray):
        return [_redact(item, key="") for item in value]
    if isinstance(value, str):
        return _redact_string(value)
    return value


def _is_secret_key(key: str) -> bool:
    lowered = key.lower()
    return any(marker in lowered for marker in SECRET_KEY_MARKERS)


def _is_inline_payload_key(key: str) -> bool:
    lowered = key.lower()
    return any(marker in lowered for marker in INLINE_PAYLOAD_KEYS)


def _redact_string(value: str) -> str:
    if _AUTH_VALUE_RE.match(value) or _OPENAI_KEY_RE.search(value):
        return "<redacted>"
    data_url = _DATA_URL_RE.match(value)
    if data_url:
        mime_type = data_url.group(1) or "application/octet-stream"
        return f"<data-url:{mime_type}:{_decoded_base64_length(data_url.group(2))} bytes>"
    if _BASE64ISH_RE.match(value):
        return f"<base64:{_decoded_base64_length(value)} bytes>"
    return value


def _decoded_base64_length(value: str) -> int:
    try:
        return len(base64.b64decode(value.encode("ascii"), validate=False))
    except (binascii.Error, ValueError):
        return len(value)
