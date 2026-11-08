"""Deterministic structured-key classification for credentials and cookies."""

from __future__ import annotations

import re

_SEPARATOR = re.compile(r"[^a-z0-9]+")
_CAMEL_BOUNDARY = re.compile(r"(?<=[a-z0-9])(?=[A-Z])")
_EXACT = frozenset(
    {
        "api_key",
        "apikey",
        "authorization",
        "client_secret",
        "cookie",
        "credential",
        "credentials",
        "encrypted_content",
        "password",
        "passwd",
        "proxy_authorization",
        "reasoning_redacted_data",
        "reasoning_signature",
        "refresh_token",
        "secret",
        "set_cookie",
        "thought_signature",
        "token",
        "access_token",
    }
)
_SENSITIVE_SUFFIXES = (
    "_api_key",
    "_authorization",
    "_cookie",
    "_credential",
    "_password",
    "_secret",
    "_token",
)
_URI_CREDENTIAL = re.compile(r"\b[a-z][a-z0-9+.-]*://[^\s/@:]+:[^\s/@]+@", re.IGNORECASE)
_BEARER_CREDENTIAL = re.compile(r"\bBearer\s+[A-Za-z0-9._~+/=-]+", re.IGNORECASE)
_QUOTED_ASSIGNED_VALUE = re.compile(
    r"(?P<label>[A-Za-z][A-Za-z0-9_-]{1,80})(?P<separator>[\"']?\s*[:=]\s*)"
    r"(?P<quote>[\"'])(?P<value>(?:\\.|(?!(?P=quote))[\s\S])*)(?P=quote)",
)
_BARE_ASSIGNED_VALUE = re.compile(
    r"(?P<label>[A-Za-z][A-Za-z0-9_-]{1,80})(?P<separator>[\"']?\s*[:=]\s*)(?![\"'])" r"(?P<value>[^\s,}]+)",
)
_PEM_PRIVATE_KEY = re.compile(
    r"-----BEGIN(?: [A-Z0-9]+)* PRIVATE KEY-----[\s\S]*?-----END(?: [A-Z0-9]+)* PRIVATE KEY-----",
    re.IGNORECASE,
)


def is_sensitive_key(key: str) -> bool:
    """Recognize common header/config credential spellings after normalization."""

    snake_case = _CAMEL_BOUNDARY.sub("_", key.strip())
    normalized = _SEPARATOR.sub("_", snake_case.lower()).strip("_")
    if normalized in _EXACT or normalized.endswith(_SENSITIVE_SUFFIXES):
        return True
    parts = tuple(part for part in normalized.split("_") if part)
    if "password" in parts:
        return True
    if "cookie" in parts and {"session", "set", "header"}.intersection(parts):
        return True
    if "authorization" in parts and {"proxy", "header"}.intersection(parts):
        return True
    if "secret" in parts and "count" not in parts:
        return True
    if "private" in parts and "key" in parts:
        return True
    if "connection" in parts and "string" in parts:
        return True
    if "dsn" in parts:
        return True
    if "token" in parts and not {"count", "counter", "usage", "limit"}.intersection(parts):
        return True
    return False


def contains_sensitive_text(value: str) -> bool:
    """Detect assigned credential values, Bearer tokens and credential URIs."""

    return bool(
        contains_embedded_credential_text(value)
        or any(
            is_sensitive_key(match.group("label"))
            for pattern in (_QUOTED_ASSIGNED_VALUE, _BARE_ASSIGNED_VALUE)
            for match in pattern.finditer(value)
        )
    )


def contains_embedded_credential_text(value: str) -> bool:
    """Detect credential material independent of an enclosing config key."""

    return bool(_URI_CREDENTIAL.search(value) or _BEARER_CREDENTIAL.search(value) or _PEM_PRIVATE_KEY.search(value))


def redact_sensitive_uris(value: str, replacement: str = "[credential URI omitted]") -> str:
    """Remove URI userinfo credentials while retaining surrounding text."""

    return _URI_CREDENTIAL.sub(replacement, value)


def redact_sensitive_text(value: str, replacement: str = "[credential omitted]") -> str:
    """Redact structured-looking credentials embedded in plain or JSON text."""

    def replace_quoted(match: re.Match[str]) -> str:
        if not is_sensitive_key(match.group("label")):
            return match.group(0)
        quote = match.group("quote")
        return f"{match.group('label')}{match.group('separator')}{quote}{replacement}{quote}"

    def replace_bare(match: re.Match[str]) -> str:
        if not is_sensitive_key(match.group("label")):
            return match.group(0)
        return f"{match.group('label')}{match.group('separator')}{replacement}"

    redacted = _PEM_PRIVATE_KEY.sub("[private key omitted]", value)
    redacted = _QUOTED_ASSIGNED_VALUE.sub(replace_quoted, redacted)
    redacted = _BARE_ASSIGNED_VALUE.sub(replace_bare, redacted)
    redacted = _BEARER_CREDENTIAL.sub(f"Bearer {replacement}", redacted)
    return _URI_CREDENTIAL.sub(replacement, redacted)
