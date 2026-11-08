"""Websocket keepalive configuration helpers."""

from __future__ import annotations

import os

DEFAULT_WEBSOCKET_PING_INTERVAL_SECONDS: float | None = None
DEFAULT_WEBSOCKET_PING_TIMEOUT_SECONDS: float | None = None

CLIENT_PING_INTERVAL_ENV = "NORTHCUA_WEBSOCKET_CLIENT_PING_INTERVAL_SECONDS"
CLIENT_PING_TIMEOUT_ENV = "NORTHCUA_WEBSOCKET_CLIENT_PING_TIMEOUT_SECONDS"


def optional_positive_seconds(value: object, *, default: float | None) -> float | None:
    """Normalize optional websocket timeout values.

    Empty values keep the caller-provided default. Non-positive values disable
    the corresponding websockets ping setting by returning ``None``.
    """

    if value in (None, ""):
        return default
    seconds = float(str(value))
    return None if seconds <= 0 else seconds


def client_ping_interval_seconds() -> float | None:
    """Return client-side websocket ping interval from environment."""

    return optional_positive_seconds(
        os.getenv(CLIENT_PING_INTERVAL_ENV),
        default=DEFAULT_WEBSOCKET_PING_INTERVAL_SECONDS,
    )


def client_ping_timeout_seconds() -> float | None:
    """Return client-side websocket ping timeout from environment."""

    return optional_positive_seconds(
        os.getenv(CLIENT_PING_TIMEOUT_ENV),
        default=DEFAULT_WEBSOCKET_PING_TIMEOUT_SECONDS,
    )
