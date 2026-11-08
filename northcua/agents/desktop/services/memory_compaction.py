"""Worker memory compaction policy helpers."""

from __future__ import annotations

import logging
import os

logger = logging.getLogger(__name__)


def retained_step_payload_window() -> int | None:
    """Return the recent-step payload window used only for worker memory compaction."""

    raw_value = os.getenv("NORTHCUA_WORKER_RETAIN_RECENT_STEP_PAYLOADS", "16")
    try:
        window = int(raw_value)
    except ValueError:
        logger.warning("invalid worker step payload window; using default", extra={"value": raw_value})
        window = 16
    if window <= 0:
        return None
    return max(2, window)


def compact_replay_payloads_enabled() -> bool:
    """Return whether prompt-replay payloads may be compacted from older steps."""

    return os.getenv("NORTHCUA_WORKER_COMPACT_REPLAY_PAYLOADS", "").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }
