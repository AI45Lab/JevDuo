"""Device readiness probes for sandbox-hosted workers."""

from __future__ import annotations

import asyncio
import os
import sqlite3
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from northcua.sandboxes.contracts import SandboxReadinessState
from northcua_protocol.state import DeviceState


@dataclass(frozen=True, slots=True)
class SandboxReadinessResult:
    """Per-call readiness diagnostics while preserving enum comparison.

    Attributes:
        state: Final readiness state.
        probe_count: Number of probe iterations performed.
        last_status: Last controller/device status observed.
        last_error: Last probe error message.
    """

    state: SandboxReadinessState
    probe_count: int = 0
    last_status: str = ""
    last_error: str = ""

    def __eq__(self, other: object) -> bool:
        if isinstance(other, SandboxReadinessState):
            return self.state == other
        if isinstance(other, SandboxReadinessResult):
            return self.state == other.state
        return False

    @property
    def value(self) -> str:
        """Return the underlying readiness enum value for compatibility.

        Returns:
            Serialized readiness enum value.
        """

        return self.state.value


class DeviceReadinessProbe(Protocol):
    """Protocol for waiting until a sandbox device is visible to controller."""

    async def wait_for_ready(
        self,
        device_id: str,
        *,
        timeout_seconds: float,
    ) -> SandboxReadinessState | SandboxReadinessResult:
        """Wait for a device to become usable.

        Args:
            device_id: Controller device id expected to register.
            timeout_seconds: Maximum wait duration in seconds.

        Returns:
            Final readiness state observed by the probe.
        """
        ...


class NoopDeviceReadinessProbe:
    """Default readiness probe for providers without controller device reads."""

    async def wait_for_ready(self, device_id: str, *, timeout_seconds: float) -> SandboxReadinessState:
        """Return ready after yielding control once.

        Args:
            device_id: Controller device id; ignored by the no-op probe.
            timeout_seconds: Maximum wait duration; ignored by the no-op probe.

        Returns:
            ``READY`` after one event-loop yield.
        """

        _ = device_id
        _ = timeout_seconds
        await asyncio.sleep(0)
        return SandboxReadinessState.READY


class DelayedDeviceReadinessProbe:
    """Deterministic probe used by tests."""

    def __init__(self, state: SandboxReadinessState = SandboxReadinessState.READY, delay_seconds: float = 0) -> None:
        self.state = state
        self.delay_seconds = delay_seconds

    async def wait_for_ready(self, device_id: str, *, timeout_seconds: float) -> SandboxReadinessState:
        """Return configured readiness after a bounded delay.

        Args:
            device_id: Controller device id; retained for protocol parity.
            timeout_seconds: Maximum wait duration in seconds.

        Returns:
            Configured readiness state, or ``TIMED_OUT`` when the configured
            delay exceeds the caller's timeout.
        """

        _ = device_id
        if self.delay_seconds > timeout_seconds > 0:
            await asyncio.sleep(timeout_seconds)
            return SandboxReadinessState.TIMED_OUT
        await asyncio.sleep(self.delay_seconds)
        return self.state


class ControllerSQLiteDeviceReadinessProbe:
    """Poll a managed local controller database until a device is online.

    This probe is intentionally local-only. It is used by experiment suites
    that start the controller in the same process tree and therefore know the
    SQLite path. Generic distributed batches should replace it with a
    controller status API once that API exposes device rows.
    """

    def __init__(
        self,
        db_path: str | Path,
        *,
        poll_interval_seconds: float = 0.5,
        max_poll_interval_seconds: float | None = None,
    ) -> None:
        self.db_path = Path(db_path)
        self.poll_interval_seconds = max(poll_interval_seconds, 0.001)
        self.max_poll_interval_seconds = max(
            max_poll_interval_seconds or self.poll_interval_seconds, self.poll_interval_seconds
        )
        self.last_probe_count = 0
        self.last_error = ""
        self.last_status = ""
        self.progress_interval_seconds = _readiness_progress_interval_seconds()

    async def wait_for_ready(self, device_id: str, *, timeout_seconds: float) -> SandboxReadinessResult:
        """Wait until the controller stores the target device as online.

        Args:
            device_id: Controller device id expected to register.
            timeout_seconds: Maximum wait duration in seconds.

        Returns:
            ``READY`` when the device is online, otherwise ``TIMED_OUT``.
        """

        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout_seconds
        next_log_at = loop.time()
        probe_count = 0
        last_error = ""
        last_status = ""
        sleep_seconds = self.poll_interval_seconds
        self._publish_last(probe_count=0, last_error="", last_status="")
        self._progress("device readiness: waiting", device_id=device_id, timeout_seconds=timeout_seconds)
        while loop.time() < deadline:
            online, last_error, last_status = await asyncio.to_thread(self._probe_once, device_id)
            probe_count += 1
            self._publish_last(probe_count=probe_count, last_error=last_error, last_status=last_status)
            if online:
                self._progress("device readiness: ready", device_id=device_id)
                return SandboxReadinessResult(
                    state=SandboxReadinessState.READY,
                    probe_count=probe_count,
                    last_status=last_status,
                    last_error=last_error,
                )
            if loop.time() >= next_log_at:
                remaining = max(0, int(deadline - loop.time()))
                self._progress("device readiness: still waiting", device_id=device_id, remaining_seconds=remaining)
                next_log_at = loop.time() + self.progress_interval_seconds
            await asyncio.sleep(min(sleep_seconds, max(0.0, deadline - loop.time())))
            sleep_seconds = min(self.max_poll_interval_seconds, sleep_seconds * 1.5)
        self._progress("device readiness: timed out", device_id=device_id, timeout_seconds=timeout_seconds)
        return SandboxReadinessResult(
            state=SandboxReadinessState.TIMED_OUT,
            probe_count=probe_count,
            last_status=last_status,
            last_error=last_error,
        )

    def _probe_once(self, device_id: str) -> tuple[bool, str, str]:
        if not self.db_path.exists():
            return False, "controller_db_missing", ""
        try:
            with sqlite3.connect(self.db_path) as connection:
                row = connection.execute("SELECT status FROM devices WHERE device_id=?", (device_id,)).fetchone()
        except sqlite3.Error as exc:
            return False, type(exc).__name__, ""
        last_status = str(row[0]) if row else "missing"
        return bool(row and row[0] == DeviceState.ONLINE.value), "", last_status

    def _publish_last(self, *, probe_count: int, last_error: str, last_status: str) -> None:
        self.last_probe_count = probe_count
        self.last_error = last_error
        self.last_status = last_status

    @staticmethod
    def _progress(message: str, **fields: object) -> None:
        """Emit an operator-facing readiness line for live sandbox runs."""

        rendered_fields = " ".join(f"{key}={value}" for key, value in fields.items())
        line = f"[northcua.sandbox.readiness] {message}"
        if rendered_fields:
            line = f"{line} {rendered_fields}"
        print(line, file=sys.stderr, flush=True)


def _readiness_progress_interval_seconds() -> float:
    raw = os.getenv("NORTHCUA_SANDBOX_READINESS_PROGRESS_INTERVAL_SECONDS", "").strip()
    if not raw:
        return 10.0
    try:
        value = float(raw)
    except ValueError:
        return 10.0
    return max(1.0, value)
