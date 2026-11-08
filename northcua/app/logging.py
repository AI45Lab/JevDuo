"""Structured runtime logging for NorthCUA.

The helpers in this module bind runtime log files through context variables and
serialize NorthCUA logger records as JSONL without changing normal logging APIs.
"""

from __future__ import annotations

import contextvars
import dataclasses
import json
import logging
from contextlib import contextmanager
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Iterator, cast

from northcua.runtime.artifacts.store import NorthCUAArtifactStore

_runtime_log_path_var: contextvars.ContextVar[Path | None] = contextvars.ContextVar(
    "northcua_runtime_log_path",
    default=None,
)
_runtime_session_id_var: contextvars.ContextVar[str] = contextvars.ContextVar(
    "northcua_runtime_session_id",
    default="",
)
_LOG_RECORD_RESERVED_ATTRS = {
    "args",
    "asctime",
    "created",
    "exc_info",
    "exc_text",
    "filename",
    "funcName",
    "levelname",
    "levelno",
    "lineno",
    "message",
    "module",
    "msecs",
    "msg",
    "name",
    "pathname",
    "process",
    "processName",
    "relativeCreated",
    "stack_info",
    "thread",
    "threadName",
    "taskName",
}


def _json_safe(value: object) -> object:
    """Convert runtime logging payloads into JSON-serializable primitives."""

    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, Enum):
        return value.value
    if dataclasses.is_dataclass(value):
        return _json_safe(dataclasses.asdict(cast(Any, value)))
    if isinstance(value, dict):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_json_safe(item) for item in value]
    model_dump = getattr(value, "model_dump", None)
    if callable(model_dump):
        try:
            return _json_safe(model_dump(mode="json"))
        except TypeError:
            return _json_safe(model_dump())
    return str(value)


def log_runtime_event(
    logger: logging.Logger,
    event: str,
    *,
    level: int = logging.INFO,
    **fields: object,
) -> None:
    """Emit one structured runtime event into `runtime.jsonl`.

    Args:
        logger: Logger used to emit the event.
        event: Stable runtime event name.
        level: Logging level for the event.
        **fields: Additional JSON-serializable event fields.
    """

    safe_fields = {}
    for key, value in fields.items():
        safe_key = f"field_{key}" if key in _LOG_RECORD_RESERVED_ATTRS or key.startswith("_") else key
        safe_fields[safe_key] = value
    logger.log(level, event, extra={"event": event, **safe_fields})


@contextmanager
def runtime_logging_context(*, log_path: Path, session_id: str) -> Iterator[None]:
    """Bind the current coroutine/task to a session-scoped runtime log path.

    Args:
        log_path: Runtime JSONL log path.
        session_id: Active session identifier.

    Yields:
        None while runtime logging context variables are bound.

    Returns:
        Iterator context manager that restores the previous logging bindings on
        exit.
    """

    path_token = _runtime_log_path_var.set(log_path)
    session_token = _runtime_session_id_var.set(session_id)
    try:
        yield
    finally:
        _runtime_log_path_var.reset(path_token)
        _runtime_session_id_var.reset(session_token)


class JsonlRuntimeLogHandler(logging.Handler):
    """Write northcua logger records as structured JSON lines."""

    def emit(self, record: logging.LogRecord) -> None:
        """Serialize one log record into the active runtime log file.

        Args:
            record: Log record emitted under the NorthCUA logger tree.
        """

        log_path = _runtime_log_path_var.get()
        if log_path is None:
            return

        payload = {
            "ts": datetime.fromtimestamp(record.created, tz=timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
            "session_id": _runtime_session_id_var.get(),
            "module": record.module,
            "line": record.lineno,
        }
        extras = {
            key: value
            for key, value in record.__dict__.items()
            if key not in _LOG_RECORD_RESERVED_ATTRS and not key.startswith("_")
        }
        if extras:
            payload["extra"] = _json_safe(extras)

        log_path.parent.mkdir(parents=True, exist_ok=True)
        with log_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(payload, ensure_ascii=False) + "\n")


def configure_runtime_logging(artifact_store: NorthCUAArtifactStore) -> None:
    """Attach one structured JSONL handler to the NorthCUA logger tree.

    Args:
        artifact_store: Artifact store used to ensure runtime logging is configured once.
    """

    logger = logging.getLogger("northcua")
    handler_key = "northcua-runtime-jsonl"

    for existing in logger.handlers:
        if getattr(existing, "_northcua_handler_key", "") == handler_key:
            return

    handler = JsonlRuntimeLogHandler()
    handler._northcua_handler_key = handler_key  # type: ignore[attr-defined]
    handler.setLevel(logging.DEBUG)
    logger.addHandler(handler)
    logger.setLevel(min(logger.level or logging.INFO, logging.DEBUG))
