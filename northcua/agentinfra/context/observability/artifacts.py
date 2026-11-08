"""Concurrent, redacted JSONL persistence for context artifacts."""

from __future__ import annotations

try:
    import fcntl
except ImportError:  # Windows local agents run in one process; batch workers require Linux.
    fcntl = None
import json
import os
import re
from pathlib import Path
from threading import Lock
from weakref import WeakValueDictionary

from northcua.agentinfra.context.security import is_sensitive_key, redact_sensitive_text
from northcua.agentkernel.context.domain.enums import SummaryTracePayloadMode
from northcua.agentkernel.context.domain.models import ContextCompactionEvent, ModelInvocationTraceEnvelope
from northcua.core.json_types import JsonArray, JsonObject, JsonValue

_SENSITIVE_KEYS = frozenset(
    {
        "api_key",
        "authorization",
        "credential",
        "encrypted_content",
        "reasoning_signature",
        "reasoning_redacted_data",
        "secret",
        "thought_signature",
        "token",
    }
)
_SECRET_TEXT = re.compile(
    r"(?i)(?P<prefix>(?:\\?[\"'])?\b(?:api[_-]?key|access[_-]?token|refresh[_-]?token|client[_-]?secret|"
    r"secret[_-]?access[_-]?key|secretAccessKey|private[_-]?key|privateKey|session[_-]?cookie|"
    r"cookie[_-]?header|cookieHeader|set[_-]?cookie|proxy[_-]?authorization|authorization(?:[_-]?header|Header)?|"
    r"connection[_-]?string|connectionString|dsn|credential|password(?:[_-]?hash|Hash)?|secret|token)"
    r"\b(?:\\?[\"'])?\s*[:=]\s*(?:\\?[\"'])?)(?P<value>(?:Bearer\s+)?[^\\\s,}\"']+)|"
    r"\bBearer\s+[A-Za-z0-9._~+/=-]+"
)


class ConcurrentJsonlWriter:
    """Append exactly one locked, fsynced JSON line per record."""

    _guard = Lock()
    _locks: WeakValueDictionary[str, Lock] = WeakValueDictionary()

    def __init__(self, path: Path) -> None:
        self.path = path

    def append(self, payload: JsonObject) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        encoded = (json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n").encode("utf-8")
        lock = self._path_lock(str(self.path.resolve()))
        with lock:
            fd = os.open(self.path, os.O_APPEND | os.O_CREAT | os.O_WRONLY, 0o600)
            try:
                # The per-path thread lock serializes writers in this process;
                # flock extends that to other processes where it exists.
                if fcntl is not None:
                    fcntl.flock(fd, fcntl.LOCK_EX)
                _write_all(fd, encoded)
                os.fsync(fd)
            finally:
                if fcntl is not None:
                    fcntl.flock(fd, fcntl.LOCK_UN)
                os.close(fd)

    @classmethod
    def _path_lock(cls, key: str) -> Lock:
        with cls._guard:
            lock = cls._locks.get(key)
            if lock is None:
                lock = Lock()
                cls._locks[key] = lock
            return lock


class ArtifactContextTraceSink:
    """Persist summary calls separately from lifecycle audit events."""

    def __init__(
        self,
        *,
        summary_trace_path: Path,
        compaction_path: Path,
        payload_mode: SummaryTracePayloadMode,
    ) -> None:
        self._summary_writer = ConcurrentJsonlWriter(summary_trace_path)
        self._compaction_writer = ConcurrentJsonlWriter(compaction_path)
        self._payload_mode = payload_mode

    def record_event(self, event: ContextCompactionEvent) -> None:
        payload = event.model_dump(mode="json")
        self._compaction_writer.append(_as_json_object(_redact(payload)))

    def record_summary(self, trace: ModelInvocationTraceEnvelope) -> None:
        payload = trace.model_dump(mode="json")
        if self._payload_mode == SummaryTracePayloadMode.METADATA_ONLY:
            payload["request"] = {}
            payload["response"] = {}
        elif self._payload_mode == SummaryTracePayloadMode.SUMMARY_AND_USAGE:
            payload["request"] = {}
            response = payload.get("response")
            payload["response"] = {"summary": response.get("summary", {})} if isinstance(response, dict) else {}
        self._summary_writer.append(_as_json_object(_redact(payload)))


def _redact(value: object, *, key: str = "") -> JsonValue:
    if key.lower() in _SENSITIVE_KEYS or is_sensitive_key(key):
        return "[REDACTED]"
    if isinstance(value, str):
        if value.startswith("data:image/"):
            return "[REDACTED_IMAGE_DATA_URL]"
        return redact_sensitive_text(_SECRET_TEXT.sub(_redact_secret_match, value), "[REDACTED]")
    if isinstance(value, bool) or value is None or isinstance(value, (int, float)):
        return value
    if isinstance(value, dict):
        output: JsonObject = {}
        for raw_key, child in value.items():
            output[str(raw_key)] = _redact(child, key=str(raw_key))
        return output
    if isinstance(value, (list, tuple)):
        output_array: JsonArray = []
        output_array.extend(_redact(child) for child in value)
        return output_array
    return str(value)


def _as_json_object(value: JsonValue) -> JsonObject:
    if not isinstance(value, dict):
        raise TypeError("trace payload must be a JSON object")
    return value


def _redact_secret_match(match: re.Match[str]) -> str:
    prefix = match.groupdict().get("prefix")
    return f"{prefix}[REDACTED]" if prefix else "Bearer [REDACTED]"


def _write_all(fd: int, payload: bytes) -> None:
    view = memoryview(payload)
    while view:
        written = os.write(fd, view)
        if written <= 0:
            raise OSError("short write while appending context JSONL")
        view = view[written:]
