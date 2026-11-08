"""Best-effort device runtime telemetry sinks."""

from __future__ import annotations

import asyncio
import hashlib
import logging
import os
from collections.abc import Callable
from datetime import datetime, timezone

from northcua_device.connection import DeviceConnection
from northcua_device.logging import DeviceEventLogger
from northcua_protocol.envelope import Envelope
from northcua_protocol.enums import MessageType
from northcua_protocol.payloads import DeviceTelemetryEvent

logger = logging.getLogger(__name__)

_DEFAULT_QUEUE_MAXSIZE = 2048
_DEFAULT_STREAM_MODE = "terminal_sampled"
_DEFAULT_SAMPLE_RATE = 0.02
_VALID_STREAM_MODES = {"raw", "terminal", "terminal_sampled", "off"}
_RAW_STREAM_MODE_ALIASES = {"all", "full"}
_TERMINAL_SAMPLED_ALIASES = {"sample", "sampled"}
_DEFAULT_ALWAYS_STREAM_EVENTS = {
    "device_command_received",
    "command_started",
    "command_result_sent",
    "command_result_send_failed",
    "device_command_prepare_failed",
    "device_command_timeout_capped",
    "device_ordered_queue_full",
}
_REPORTABLE_EVENTS = {
    "device_command_received",
    "device_command_dequeued",
    "device_command_execution_started",
    "device_command_prepare_started",
    "device_command_prepare_finished",
    "device_command_prepare_failed",
    "device_command_timeout_capped",
    "device_driver_call_started",
    "device_driver_call_finished",
    "device_result_json_serialize_started",
    "device_result_json_serialize_finished",
    "device_websocket_send_started",
    "device_websocket_send_finished",
    "command_started",
    "command_result_ready",
    "command_result_sent",
    "command_result_send_failed",
    "device_ordered_queue_full",
}


class DeviceTelemetrySink:
    """Write local JSONL logs and optionally stream selected events upstream.

    ``write`` is synchronous and must never block command execution. Controller
    sends happen on a bounded background queue; when the queue is full, events
    are dropped after still being written to the local JSONL log.
    """

    def __init__(
        self,
        *,
        device_id: str,
        local_events: DeviceEventLogger,
        queue_maxsize: int | None = None,
        stream_mode: str | None = None,
        sample_rate: float | None = None,
        always_events: set[str] | None = None,
    ) -> None:
        self.device_id = device_id
        self._local_events = local_events
        self._queue: asyncio.Queue[DeviceTelemetryEvent] | None = None
        self._task: asyncio.Task[None] | None = None
        self._connection: DeviceConnection | None = None
        self._trace_id = ""
        self._dropped = 0
        self._filtered_by_policy = 0
        self._queue_maxsize = queue_maxsize or _queue_maxsize_from_env()
        self._stream_mode = _stream_mode_from_value(stream_mode)
        self._sample_rate = _sample_rate_from_value(sample_rate)
        self._always_stream_events = frozenset(
            _DEFAULT_ALWAYS_STREAM_EVENTS | set(always_events or _always_events_from_env())
        )

    async def start(self, *, connection: DeviceConnection, trace_id: str) -> None:
        """Start live controller telemetry for the active websocket.

        Args:
            connection: Active controller websocket connection.
            trace_id: Trace id attached to streamed telemetry envelopes.
        """

        if self._task is not None and not self._task.done():
            return
        self._connection = connection
        self._trace_id = trace_id
        self._queue = asyncio.Queue(maxsize=self._queue_maxsize)
        self._task = asyncio.create_task(self._run(), name="northcua-device-telemetry")
        self.write(
            "device_telemetry_started",
            device_id=self.device_id,
            queue_maxsize=self._queue_maxsize,
            stream_mode=self._stream_mode,
            sample_rate=self._sample_rate,
            always_events=sorted(self._always_stream_events),
        )

    async def stop(self) -> None:
        """Stop the background sender without delaying device shutdown."""

        task = self._task
        queue = self._queue
        if queue is not None and task is not None and not task.done():
            try:
                await asyncio.wait_for(queue.join(), timeout=0.5)
            except TimeoutError:
                pass
        self._task = None
        self._queue = None
        if task is None:
            return
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        self._connection = None
        self._trace_id = ""

    def write(self, event: str, **payload: object) -> None:
        """Write one local event and enqueue controller telemetry if enabled.

        Args:
            event: Device telemetry event name.
            **payload: JSON-like telemetry payload values.
        """

        payload = {**payload, "device_id": self.device_id}
        try:
            self._local_events.write(event, **payload)
        except Exception:  # pragma: no cover - local telemetry is best-effort
            logger.debug("device local telemetry write failed", exc_info=True)
        if event not in _REPORTABLE_EVENTS:
            return
        queue = self._queue
        if queue is None:
            return
        if not self._should_stream_upstream(event, payload):
            self._filtered_by_policy += 1
            if self._filtered_by_policy in {1, 10, 100} or self._filtered_by_policy % 1000 == 0:
                self._safe_local_write(
                    "device_telemetry_stream_filtered",
                    event_type=event,
                    filtered_count=self._filtered_by_policy,
                    stream_mode=self._stream_mode,
                    sample_rate=self._sample_rate,
                )
            return
        try:
            queue.put_nowait(self._event_from_payload(event, payload))
        except asyncio.QueueFull:
            self._dropped += 1
            if self._dropped in {1, 10, 100} or self._dropped % 1000 == 0:
                try:
                    self._local_events.write(
                        "device_telemetry_dropped",
                        device_id=self.device_id,
                        dropped_count=self._dropped,
                        queue_maxsize=self._queue_maxsize,
                    )
                except Exception:  # pragma: no cover - local telemetry is best-effort
                    logger.debug("device telemetry drop log failed", exc_info=True)

    async def _run(self) -> None:
        while True:
            queue = self._queue
            connection = self._connection
            if queue is None or connection is None:
                await asyncio.sleep(0)
                continue
            event = await queue.get()
            try:
                await connection.send(
                    Envelope.wrap(
                        MessageType.DEVICE_TELEMETRY,
                        event,
                        trace_id=self._trace_id or "device-telemetry",
                    )
                )
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # pragma: no cover - network boundary
                logger.debug("device telemetry send failed", exc_info=True)
                self._safe_local_write(
                    "device_telemetry_send_failed",
                    event_type=event.event_type,
                    command_id=event.command_id,
                    session_id=event.session_id,
                    tool_id=event.tool_id,
                    error=str(exc),
                )
            finally:
                queue.task_done()

    def _event_from_payload(self, event: str, payload: dict[str, object]) -> DeviceTelemetryEvent:
        command_id = _string_value(payload.get("command_id"))
        session_id = _string_value(payload.get("session_id"))
        lease_id = _string_value(payload.get("lease_id"))
        tool_id = _string_value(payload.get("tool_id"))
        entity_type = "command" if command_id else ("session" if session_id else "device")
        entity_id = command_id or session_id or self.device_id
        telemetry_payload = _safe_payload(payload)
        telemetry_payload.setdefault("device_event_timestamp", datetime.now(timezone.utc).isoformat())
        return DeviceTelemetryEvent(
            device_id=self.device_id,
            event_type=event,
            entity_type=entity_type,
            entity_id=entity_id,
            command_id=command_id,
            session_id=session_id,
            lease_id=lease_id,
            tool_id=tool_id,
            stage=_stage_for_event(event),
            payload=telemetry_payload,
        )

    def _should_stream_upstream(self, event: str, payload: dict[str, object]) -> bool:
        mode = self._stream_mode
        if mode == "raw":
            return True
        if mode == "off":
            return False
        if event in self._always_stream_events or _payload_indicates_failure(payload):
            return True
        if mode == "terminal":
            return False
        command_id = _string_value(payload.get("command_id"))
        sample_key = command_id or _string_value(payload.get("session_id")) or event
        return _sample_key_included(sample_key=f"{self.device_id}:{sample_key}", sample_rate=self._sample_rate)

    def _safe_local_write(self, event: str, **payload: object) -> None:
        try:
            self._local_events.write(event, device_id=self.device_id, **payload)
        except Exception:  # pragma: no cover - local telemetry is best-effort
            logger.debug("device telemetry local write failed", exc_info=True)


def _queue_maxsize_from_env() -> int:
    raw_value = os.getenv("NORTHCUA_DEVICE_TELEMETRY_QUEUE_MAXSIZE")
    if not raw_value:
        return _DEFAULT_QUEUE_MAXSIZE
    try:
        return max(1, int(raw_value))
    except ValueError:
        return _DEFAULT_QUEUE_MAXSIZE


def _stream_mode_from_value(value: str | None) -> str:
    raw_value = value if value is not None else os.getenv("NORTHCUA_DEVICE_TELEMETRY_STREAM_MODE")
    if not raw_value:
        return _DEFAULT_STREAM_MODE
    normalized = str(raw_value).strip().lower().replace("-", "_")
    if normalized in _RAW_STREAM_MODE_ALIASES:
        return "raw"
    if normalized in _TERMINAL_SAMPLED_ALIASES:
        return "terminal_sampled"
    if normalized in _VALID_STREAM_MODES:
        return normalized
    logger.warning("unknown device telemetry stream mode %r; falling back to raw", raw_value)
    return "raw"


def _sample_rate_from_value(value: float | str | None) -> float:
    raw_value = value if value is not None else os.getenv("NORTHCUA_DEVICE_TELEMETRY_SAMPLE_RATE")
    if raw_value in (None, ""):
        return _DEFAULT_SAMPLE_RATE
    try:
        parsed = float(raw_value)
    except (TypeError, ValueError):
        return _DEFAULT_SAMPLE_RATE
    if parsed <= 0:
        return 0.0
    if parsed >= 1:
        return 1.0
    return parsed


def _always_events_from_env() -> set[str]:
    raw_value = os.getenv("NORTHCUA_DEVICE_TELEMETRY_ALWAYS_EVENTS", "")
    return {part.strip() for part in raw_value.split(",") if part.strip()}


def _sample_key_included(*, sample_key: str, sample_rate: float) -> bool:
    if sample_rate <= 0:
        return False
    if sample_rate >= 1:
        return True
    digest = hashlib.blake2b(sample_key.encode("utf-8"), digest_size=8).digest()
    bucket = int.from_bytes(digest, byteorder="big") / float(1 << 64)
    return bucket < sample_rate


def _payload_indicates_failure(payload: dict[str, object]) -> bool:
    error = payload.get("error")
    if isinstance(error, str) and error:
        return True
    status = payload.get("status")
    if status is None:
        return False
    value = getattr(status, "value", status)
    normalized = str(value).strip().lower()
    return normalized not in {"", "success", "succeeded", "commandstate.succeeded"}


def _stage_for_event(event: str) -> str:
    if event.startswith("device_result_json_"):
        return "result_json"
    if event.startswith("device_websocket_send_") or event.startswith("command_result_send"):
        return "websocket_send"
    if event.startswith("device_driver_"):
        return "driver"
    if event.startswith("device_command_prepare") or event == "device_command_timeout_capped":
        return "prepare"
    if event in {"device_command_received", "device_command_dequeued", "command_started"}:
        return "queue"
    if event == "command_result_ready":
        return "result_ready"
    if event == "command_result_sent":
        return "result_sent"
    return event


def _string_value(value: object) -> str:
    return value if isinstance(value, str) else ""


def _safe_payload(payload: dict[str, object]) -> dict[str, object]:
    safe: dict[str, object] = {}
    for key, value in payload.items():
        if isinstance(value, (str, int, float, bool)) or value is None:
            safe[key] = value
        elif isinstance(value, dict):
            safe[key] = _safe_payload(value)
        elif isinstance(value, (list, tuple)):
            safe[key] = [item for item in value if isinstance(item, (str, int, float, bool)) or item is None][:64]
        else:
            safe[key] = str(value)
    return safe


EventWriter = Callable[..., None]


__all__ = ["DeviceTelemetrySink", "EventWriter"]
