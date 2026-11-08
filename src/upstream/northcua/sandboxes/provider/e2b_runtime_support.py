"""Configuration and retry helpers for the E2B provider runtime."""

from __future__ import annotations

import logging
import os
import random
import uuid

from northcua.sandboxes.contracts import SandboxProviderSpec

logger = logging.getLogger(__name__)
_EXECUTOR_TELEMETRY_DEADLINES_SECONDS = (30.0, 60.0, 120.0, 240.0, 480.0, 600.0)
_RETRYABLE_E2B_ERROR_MARKERS = (
    "context deadline exceeded",
    "deadline_exceeded",
    "deadline exceeded",
    "unexpected_eof_while_reading",
    "connection reset",
    "connection aborted",
    "connection refused",
    "connection timed out",
    "connect timeout",
    "connecterror",
    "readerror",
    "write operation timed out",
    "write timeout",
    "incomplete chunked read",
    "incomplete message body",
    "broken pipe",
    "errno 32",
    "remoteprotocolerror",
    "server disconnected",
    "temporarily unavailable",
    "tls",
    "ssl",
)
_RETRYABLE_TRANSPORT_EXCEPTION_TYPES = {
    ("httpcore", "ConnectTimeout"),
    ("httpcore", "PoolTimeout"),
    ("httpcore", "ReadTimeout"),
    ("httpcore", "RemoteProtocolError"),
    ("httpcore", "WriteTimeout"),
    ("httpx", "ConnectTimeout"),
    ("httpx", "PoolTimeout"),
    ("httpx", "ReadTimeout"),
    ("httpx", "RemoteProtocolError"),
    ("httpx", "WriteTimeout"),
}


def executor_telemetry_deadline(index: int) -> float:
    """Return an absolute wait checkpoint with bounded log frequency."""

    if index < len(_EXECUTOR_TELEMETRY_DEADLINES_SECONDS):
        return _EXECUTOR_TELEMETRY_DEADLINES_SECONDS[index]
    overflow = index - len(_EXECUTOR_TELEMETRY_DEADLINES_SECONDS) + 1
    return _EXECUTOR_TELEMETRY_DEADLINES_SECONDS[-1] + (overflow * 600.0)


def int_env(name: str, default: int) -> int:
    """Read a non-negative integer environment value."""

    value = os.getenv(name, "")
    if not value:
        return default
    try:
        return max(int(value), 0)
    except ValueError:
        logger.warning("invalid integer environment value", extra={"env_var": name, "default": default})
        return default


def control_request_timeout(name: str) -> int:
    """Resolve one control-plane SDK request timeout with a shared fallback."""

    default = max(int_env("NORTHCUA_E2B_REQUEST_TIMEOUT_SECONDS", 300), 1)
    return max(int_env(name, default), 1)


def retry_delay_seconds(backoffs: tuple[float, ...], *, attempt: int, max_jitter_seconds: float) -> float:
    """Return bounded retry delay with one-sided jitter."""

    base = float(backoffs[min(attempt - 1, len(backoffs) - 1)]) if backoffs else 0.0
    if base <= 0.0 or max_jitter_seconds <= 0.0:
        return max(base, 0.0)
    return base + random.uniform(0.0, max_jitter_seconds)


def is_retryable_e2b_error(exc: Exception) -> bool:
    """Return whether an E2B SDK exception looks like transport flakiness."""

    if type(exc) is TimeoutError:
        return False
    exception_type = type(exc)
    module_root = exception_type.__module__.split(".", maxsplit=1)[0]
    if (module_root, exception_type.__name__) in _RETRYABLE_TRANSPORT_EXCEPTION_TYPES:
        return True
    message = f"{type(exc).__module__}.{type(exc).__name__}: {exc}".lower()
    normalized = message.replace(" ", "_")
    return any(marker in message or marker in normalized for marker in _RETRYABLE_E2B_ERROR_MARKERS)


def is_file_write_unsupported(exc: Exception) -> bool:
    """Return whether SDK file write is unavailable rather than transient."""

    if isinstance(exc, (AttributeError, NotImplementedError)):
        return True
    message = f"{type(exc).__module__}.{type(exc).__name__}: {exc}".lower()
    return any(marker in message for marker in ("unsupported", "not supported", "not implemented"))


def is_unexpected_request_timeout_keyword(exc: TypeError) -> bool:
    """Return whether an E2B SDK method predates request-timeout support."""

    message = str(exc).lower()
    return "request_timeout" in message and ("unexpected keyword" in message or "keyword argument" in message)


def orphan_match_metadata(spec: SandboxProviderSpec) -> dict[str, str] | None:
    """Return exact metadata only when an allocation is safe to reconcile."""

    required_keys = (
        "northcua",
        "batch_id",
        "task_id",
        "attempt",
        "template",
        "create_token",
        "create_call_token",
    )
    metadata = {str(key): str(value) for key, value in spec.metadata.items()}
    if any(not metadata.get(key) for key in required_keys):
        return None
    if metadata["template"] != spec.template:
        return None
    return {key: metadata[key] for key in required_keys}


def create_attempt_spec(spec: SandboxProviderSpec) -> SandboxProviderSpec:
    """Return one provider-call spec with a token unique to this SDK create."""

    metadata = dict(spec.metadata)
    if not str(metadata.get("create_token") or ""):
        return spec
    metadata["create_call_token"] = uuid.uuid4().hex
    return spec.model_copy(update={"metadata": metadata})


def metadata_matches(actual: dict[str, object], expected: dict[str, str]) -> bool:
    """Return whether every expected metadata field matches exactly."""

    return all(str(actual.get(key) or "") == value for key, value in expected.items())
