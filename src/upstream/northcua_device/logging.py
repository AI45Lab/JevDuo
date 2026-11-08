"""Device worker logging helpers."""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path

from northcua_runtime_shared.redaction import redact_for_log

logger = logging.getLogger(__name__)


class DeviceEventLogger:
    """Append-only JSONL event logger for device-side operations."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.path = self.root / "device_runtime.jsonl"
        self._enabled = True
        try:
            self.root.mkdir(parents=True, exist_ok=True)
        except Exception:  # pragma: no cover - logging must not prevent device startup
            self._enabled = False
            logger.debug("device event log directory unavailable", exc_info=True)

    def write(self, event: str, **payload: object) -> None:
        """Append one redacted device runtime event.

        Args:
            event: Stable event name.
            **payload: JSON-serializable event metadata. Secrets and obvious
                token-like strings are redacted before writing.
        """

        if not self._enabled:
            return
        try:
            redacted_payload = redact_for_log(payload)
            record_payload = redacted_payload if isinstance(redacted_payload, dict) else {}
            record = {
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "event": event,
                **record_payload,
            }
            with self.path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(record, ensure_ascii=False) + "\n")
        except Exception:  # pragma: no cover - logging must never break command execution
            logger.debug("device event log write failed", exc_info=True)
