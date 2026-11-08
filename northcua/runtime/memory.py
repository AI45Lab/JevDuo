"""Best-effort process memory release helpers."""

from __future__ import annotations

import ctypes
import gc
import logging
import os
import sys

logger = logging.getLogger(__name__)

_TRIM_ENV = "NORTHCUA_WORKER_MEMORY_TRIM"


def worker_memory_trim_enabled() -> bool:
    """Return whether worker-side post-step memory trimming is enabled.

    Returns:
        ``True`` unless ``NORTHCUA_WORKER_MEMORY_TRIM`` is explicitly falsey.
    """

    raw = os.getenv(_TRIM_ENV, "1").strip().lower()
    return raw not in {"0", "false", "no", "off"}


def release_unused_process_memory() -> bool:
    """Collect Python cycles and ask glibc to return free arenas to the OS.

    Returns:
        ``True`` when ``malloc_trim`` ran and reported success.
    """

    gc.collect()
    if not sys.platform.startswith("linux"):
        return False
    try:
        libc = ctypes.CDLL("libc.so.6")
        trim = getattr(libc, "malloc_trim")
        trim.argtypes = [ctypes.c_size_t]
        trim.restype = ctypes.c_int
        return bool(trim(0))
    except Exception:
        logger.debug("process_memory_trim_failed", exc_info=True)
        return False


__all__ = ["release_unused_process_memory", "worker_memory_trim_enabled"]
