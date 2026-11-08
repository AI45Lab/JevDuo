"""Environment export helpers for desktop runtime backend selection."""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import MutableMapping

from northcua.core.config import NorthCUAAppConfig

DESKTOP_BACKEND_ENV = "NORTHCUA_DESKTOP_BACKEND"
OSWORLD_VM_IP_ENV = "NORTHCUA_OSWORLD_VM_IP"
OSWORLD_SERVER_PORT_ENV = "NORTHCUA_OSWORLD_SERVER_PORT"
OSWORLD_COMMAND_TIMEOUT_ENV = "NORTHCUA_OSWORLD_COMMAND_TIMEOUT_SECONDS"
OSWORLD_SCREENSHOT_TIMEOUT_ENV = "NORTHCUA_OSWORLD_SCREENSHOT_TIMEOUT_SECONDS"
OSWORLD_RETRY_TIMES_ENV = "NORTHCUA_OSWORLD_RETRY_TIMES"
OSWORLD_RETRY_INTERVAL_ENV = "NORTHCUA_OSWORLD_RETRY_INTERVAL_SECONDS"
OSWORLD_ENABLE_A11Y_ENV = "NORTHCUA_OSWORLD_ENABLE_A11Y"

_DESKTOP_RUNTIME_ENV_KEYS = (
    DESKTOP_BACKEND_ENV,
    OSWORLD_VM_IP_ENV,
    OSWORLD_SERVER_PORT_ENV,
    OSWORLD_COMMAND_TIMEOUT_ENV,
    OSWORLD_SCREENSHOT_TIMEOUT_ENV,
    OSWORLD_RETRY_TIMES_ENV,
    OSWORLD_RETRY_INTERVAL_ENV,
    OSWORLD_ENABLE_A11Y_ENV,
)


@dataclass(slots=True, frozen=True)
class DesktopRuntimeEnvironmentSnapshot:
    """Previous desktop-runtime environment values for later restoration.

    Attributes:
        previous_values: Prior environment values keyed by variable name.
    """

    previous_values: dict[str, str | None]


def apply_app_desktop_runtime_environment(
    app_config: NorthCUAAppConfig,
    *,
    environment: MutableMapping[str, str] | None = None,
) -> DesktopRuntimeEnvironmentSnapshot:
    """Export desktop runtime backend settings into the process environment.

    Args:
        app_config: NorthCUA application config containing desktop backend settings.
        environment: Optional mutable environment mapping for tests.

    Returns:
        Snapshot that can restore the previous environment values.
    """

    env = environment if environment is not None else os.environ
    previous = {key: env.get(key) for key in _DESKTOP_RUNTIME_ENV_KEYS}
    env[DESKTOP_BACKEND_ENV] = app_config.desktop.backend
    env[OSWORLD_VM_IP_ENV] = app_config.desktop.osworld.vm_ip
    env[OSWORLD_SERVER_PORT_ENV] = str(app_config.desktop.osworld.server_port)
    env[OSWORLD_COMMAND_TIMEOUT_ENV] = str(app_config.desktop.osworld.command_timeout_seconds)
    env[OSWORLD_SCREENSHOT_TIMEOUT_ENV] = str(app_config.desktop.osworld.screenshot_timeout_seconds)
    env[OSWORLD_RETRY_TIMES_ENV] = str(app_config.desktop.osworld.retry_times)
    env[OSWORLD_RETRY_INTERVAL_ENV] = str(app_config.desktop.osworld.retry_interval_seconds)
    env[OSWORLD_ENABLE_A11Y_ENV] = _serialize_bool(app_config.desktop.osworld.enable_accessibility)
    return DesktopRuntimeEnvironmentSnapshot(previous_values=previous)


def restore_desktop_runtime_environment(
    snapshot: DesktopRuntimeEnvironmentSnapshot,
    *,
    environment: MutableMapping[str, str] | None = None,
) -> None:
    """Restore desktop runtime environment values exported earlier.

    Args:
        snapshot: Environment snapshot returned by apply.
        environment: Optional mutable environment mapping for tests.
    """

    env = environment if environment is not None else os.environ
    for key, value in snapshot.previous_values.items():
        if value is None:
            env.pop(key, None)
        else:
            env[key] = value


def desktop_backend_from_environment(environment: MutableMapping[str, str] | None = None) -> str:
    """Return the selected desktop backend name from the environment.

    Args:
        environment: Optional mutable environment mapping for tests.

    Returns:
        Normalized backend name, defaulting to ``native``.
    """

    env = environment if environment is not None else os.environ
    return str(env.get(DESKTOP_BACKEND_ENV, "native") or "native").strip().lower()


def osworld_accessibility_enabled(environment: MutableMapping[str, str] | None = None) -> bool:
    """Return whether OSWorld remote accessibility fetching is enabled.

    Args:
        environment: Optional mutable environment mapping for tests.

    Returns:
        True when OSWorld accessibility capture should be enabled.
    """

    env = environment if environment is not None else os.environ
    return _parse_bool(env.get(OSWORLD_ENABLE_A11Y_ENV), default=True)


def _serialize_bool(value: bool) -> str:
    return "true" if value else "false"


def _parse_bool(value: str | None, *, default: bool) -> bool:
    if value is None:
        return default
    lowered = str(value).strip().lower()
    if lowered in {"1", "true", "yes", "on"}:
        return True
    if lowered in {"0", "false", "no", "off"}:
        return False
    return default
