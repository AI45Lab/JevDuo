"""Shared CLI path and session-id helpers.

These helpers keep path and session-id behavior consistent across CLI commands.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path


def default_session_id(prefix: str) -> str:
    """Generate a timestamp-based default session id.

    Args:
        prefix: Prefix that identifies the command or runtime.

    Returns:
        Timestamp-based session identifier.
    """

    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    return f"{prefix}-{timestamp}"


def default_app_config_path() -> Path:
    """Return the preferred default app config shipped with NorthCUA.

    Returns:
        Local app config path when present, otherwise the example config path.
    """

    local_path = Path.cwd() / "config" / "app.local.yaml"
    if local_path.is_file():
        return local_path
    from importlib.resources import files

    return Path(str(files("northcua.resources.config").joinpath("app.example.yaml")))


def resolve_app_config_path(app_config_path: str | None) -> Path:
    """Resolve an explicit or default app config path.

    Args:
        app_config_path: Optional CLI-provided app config path.

    Returns:
        Resolved app config path.
    """

    if app_config_path:
        return Path(app_config_path).expanduser().resolve()
    return default_app_config_path()


def resolve_log_root(log_dir: str | None) -> Path | None:
    """Resolve a CLI log-dir override.

    Args:
        log_dir: Optional CLI-provided log directory.

    Returns:
        Resolved log root, or None when no override was provided.
    """

    if not log_dir:
        return None
    candidate = Path(log_dir).expanduser()
    if candidate.is_absolute() or len(candidate.parts) > 1 or log_dir.startswith((".", "~")):
        return candidate.resolve()
    return (Path.cwd() / "logs" / candidate.name).resolve()
