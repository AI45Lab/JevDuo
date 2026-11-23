"""Private, append-only Jev evidence independent of bounded handoff summaries."""

from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path


class TrajectoryWriteError(RuntimeError):
    def __init__(self, result=None):
        super().__init__("Jev trajectory persistence failed; no operation may be replayed to repair the log")
        self.result = result


class JevTrajectory:
    def __init__(self, directory: Path, metadata: dict, *, screenshots=True, secrets=()):
        directory.mkdir(parents=True, mode=0o700, exist_ok=False)
        self.path = directory / "trajectory.jsonl"
        self.metadata, self.screenshots = metadata, screenshots
        self.secrets = tuple(sorted({str(s) for s in secrets if s}, key=len, reverse=True))
        self._lock, self._sequence, self._failed = threading.Lock(), 0, False

    def record(self, event: str, payload: dict):
        with self._lock:
            if self._failed:
                raise TrajectoryWriteError(payload.get("result"))
            record = {**self.metadata, "schema_version": 1, "sequence": self._sequence + 1,
                      "at": time.time(), "event": event, **payload}
            try:
                text = json.dumps(self._redact(record), ensure_ascii=False, allow_nan=False)
                encoded = (text + "\n").encode("utf-8")
                descriptor = os.open(self.path, os.O_RDWR | os.O_CREAT | os.O_APPEND, 0o600)
                with os.fdopen(descriptor, "ab+") as output:
                    os.fchmod(output.fileno(), 0o600)
                    output.seek(0, os.SEEK_END)
                    if output.tell():
                        output.seek(-1, os.SEEK_END)
                        if output.read(1) != b"\n":
                            raise ValueError("Truncated trajectory tail")
                    output.write(encoded)
                    output.flush()
                    os.fsync(output.fileno())
                self._sequence += 1
            except (OSError, ValueError, TypeError, UnicodeError):
                self._failed = True
                raise TrajectoryWriteError(payload.get("result")) from None

    def _redact(self, value):
        if isinstance(value, str):
            for secret in self.secrets:
                value = value.replace(secret, "[credential-redacted]")
                for ascii_only in (False, True):
                    escaped = json.dumps(secret, ensure_ascii=ascii_only)[1:-1]
                    if escaped != secret:
                        value = value.replace(escaped, "[credential-redacted]")
            return value
        if isinstance(value, dict):
            return {self._redact(str(key)): self._redact(item) for key, item in value.items()}
        if isinstance(value, (list, tuple)):
            return [self._redact(item) for item in value]
        return value
