"""Controller configuration for the NorthCUA remote harness."""

from __future__ import annotations

import os
from collections.abc import Mapping
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, Field, field_validator, model_validator

from northcua.transport.ws.keepalive import (
    DEFAULT_WEBSOCKET_PING_INTERVAL_SECONDS,
    DEFAULT_WEBSOCKET_PING_TIMEOUT_SECONDS,
    optional_positive_seconds,
)
from northcua_protocol.capabilities import Capability

DEFAULT_WEBSOCKET_OPEN_TIMEOUT_SECONDS = 120.0
DEFAULT_WEBSOCKET_BACKLOG = 4096
DEFAULT_DEVICE_DISCONNECT_GRACE_SECONDS = 120.0
DEFAULT_RUNTIME_TELEMETRY_INTERVAL_SECONDS = 10.0
DEFAULT_EVENT_WRITE_BATCH_SIZE = 128
DEFAULT_EVENT_WRITE_FLUSH_INTERVAL_SECONDS = 0.25
DEFAULT_PROCESS_WORKER_MODULE = "northcua.controller.execution.processes.worker_main"
DeviceTelemetryPersistMode = Literal["raw", "sample", "aggregate", "off"]
CommandStageEventMode = Literal["raw", "sample", "aggregate", "off"]
ControllerExecutorMode = Literal["in_process", "process"]


class ControllerSecurityConfig(BaseModel):
    """Security settings for the controller websocket boundary.

    Attributes:
        token: Shared fallback token for devices and submitters.
        device_token: Device-specific bearer token when set.
        submitter_token: Submitter-specific bearer token when set.
        insecure_dev_no_auth: Development-only switch that disables token checks.
        allowed_device_ids: Optional strict allowlist for device registration.
        allowed_tools: Optional allowlist for tool ids accepted by command bus.
        allowed_capabilities: Optional allowlist for requested task capabilities.
        max_envelope_bytes: Maximum serialized command/envelope payload size.
            This is larger than ``max_inline_artifact_bytes`` because inline
            binary artifacts expand when base64 encoded inside websocket JSON.
        max_task_metadata_bytes: Maximum task metadata JSON size.
        max_inline_artifact_bytes: Maximum inline artifact size before blob storage.
    """

    token: str = ""
    device_token: str = ""
    submitter_token: str = ""
    insecure_dev_no_auth: bool = False
    allowed_device_ids: set[str] = Field(default_factory=set)
    allowed_tools: set[str] = Field(default_factory=set)
    allowed_capabilities: set[Capability] = Field(default_factory=set)
    max_envelope_bytes: int = 32_000_000
    max_task_metadata_bytes: int = 1_000_000
    max_inline_artifact_bytes: int = 4_000_000


class ControllerConfig(BaseModel):
    """Configuration for the controller control plane.

    Attributes:
        host: Controller websocket bind host.
        port: Controller websocket bind port.
        db_path: SQLite persistence path.
        blob_dir: Controller blob storage directory.
        offline_after_seconds: Heartbeat age before a device is considered stale.
        device_disconnect_grace_seconds: Seconds to wait before interrupting
            active sessions after a device websocket disconnect. A short grace
            lets remote devices survive transient tunnel reconnects.
        websocket_open_timeout_seconds: Seconds allowed for an incoming
            websocket opening handshake before the controller closes it.
        websocket_ping_interval_seconds: Seconds between protocol-level
            websocket ping frames. Set to 0 to disable keepalive pings.
        websocket_ping_timeout_seconds: Seconds to wait for a pong response
            after sending a ping. Set to 0 to disable ping timeouts.
        websocket_backlog: TCP listen backlog for controller websocket accepts.
        runtime_telemetry_interval_seconds: Seconds between best-effort
            controller runtime telemetry samples. Set to 0 to disable.
        device_telemetry_persist_mode: Raw device stage telemetry persistence
            policy. ``raw`` writes every device event, ``sample`` writes a
            deterministic sample, and ``aggregate``/``off`` rely on runtime
            telemetry histograms without appending per-event rows.
        device_telemetry_sample_rate: Deterministic raw persistence rate used
            when device_telemetry_persist_mode is ``sample``.
        heartbeat_persist_interval_seconds: Minimum per-device interval for
            persisting heartbeat state/events. In-memory scheduler state is
            still updated on every heartbeat.
        command_stage_event_mode: Persistence policy for high-volume command
            lifecycle events. ``aggregate`` writes interval summaries and
            optional samples instead of every per-command stage row.
            This policy is applied after the device telemetry persistence gate,
            so full raw device command-stage telemetry requires both
            device_telemetry_persist_mode=raw and command_stage_event_mode=raw.
        command_stage_event_sample_rate: Deterministic sample rate for command
            stage raw event rows when command_stage_event_mode samples.
        event_write_batch_size: Maximum buffered controller events before a
            single SQLite batch commit.
        event_write_flush_interval_seconds: Best-effort flush interval for
            buffered controller events.
        command_timeout_seconds: Default command timeout used by runners.
        command_timeout_floor_seconds: Minimum command timeout accepted by
            descriptor-backed remote dispatch. This keeps real remote devices
            from being marked timed out only because a conservative descriptor
            budget was too short for websocket/tunnel result delivery.
        executor_mode: Whether controller-owned agent execution runs in the
            controller event loop or isolated worker processes.
        runner_workers: Maximum concurrent process-executor workers.
        worker_queue_max: Maximum scheduled plus queued process-executor tasks.
        worker_start_timeout_seconds: Seconds to wait for a worker process to
            connect and accept its run request.
        worker_heartbeat_timeout_seconds: Seconds without heartbeat before a
            worker is treated as failed.
        process_worker_module: Python module used as the process worker
            entrypoint. The default runs the real desktop worker; overriding is
            intended for no-LLM simulation and development diagnostics.
        security: Controller auth and payload limit settings.
    """

    host: str = "127.0.0.1"
    port: int = 8765
    db_path: str = "logs/controller.sqlite"
    blob_dir: str = "logs/controller_blobs"
    offline_after_seconds: float = 240.0
    device_disconnect_grace_seconds: float = DEFAULT_DEVICE_DISCONNECT_GRACE_SECONDS
    websocket_open_timeout_seconds: float | None = DEFAULT_WEBSOCKET_OPEN_TIMEOUT_SECONDS
    websocket_ping_interval_seconds: float | None = DEFAULT_WEBSOCKET_PING_INTERVAL_SECONDS
    websocket_ping_timeout_seconds: float | None = DEFAULT_WEBSOCKET_PING_TIMEOUT_SECONDS
    websocket_backlog: int = DEFAULT_WEBSOCKET_BACKLOG
    runtime_telemetry_interval_seconds: float = DEFAULT_RUNTIME_TELEMETRY_INTERVAL_SECONDS
    device_telemetry_persist_mode: DeviceTelemetryPersistMode = "aggregate"
    device_telemetry_sample_rate: float = 1.0
    heartbeat_persist_interval_seconds: float = 30.0
    command_stage_event_mode: CommandStageEventMode = "aggregate"
    command_stage_event_sample_rate: float = 0.02
    event_write_batch_size: int = DEFAULT_EVENT_WRITE_BATCH_SIZE
    event_write_flush_interval_seconds: float = DEFAULT_EVENT_WRITE_FLUSH_INTERVAL_SECONDS
    command_timeout_seconds: float = 60.0
    command_timeout_floor_seconds: float = 0.0
    executor_mode: ControllerExecutorMode = "in_process"
    runner_workers: int = 4
    worker_queue_max: int = 0
    worker_start_timeout_seconds: float = 60.0
    worker_heartbeat_timeout_seconds: float = 60.0
    process_worker_module: str = DEFAULT_PROCESS_WORKER_MODULE
    security: ControllerSecurityConfig = Field(default_factory=ControllerSecurityConfig)

    @field_validator("websocket_open_timeout_seconds", mode="before")
    @classmethod
    def _normalize_websocket_open_timeout(cls, value: object) -> object:
        if value in (None, ""):
            return None
        timeout = float(str(value))
        return None if timeout <= 0 else timeout

    @field_validator("websocket_ping_interval_seconds", "websocket_ping_timeout_seconds", mode="before")
    @classmethod
    def _normalize_websocket_ping_setting(cls, value: object) -> float | None:
        return optional_positive_seconds(value, default=None)

    @field_validator("websocket_backlog", mode="before")
    @classmethod
    def _normalize_websocket_backlog(cls, value: object) -> int:
        if value in (None, ""):
            return DEFAULT_WEBSOCKET_BACKLOG
        backlog = int(str(value))
        return DEFAULT_WEBSOCKET_BACKLOG if backlog <= 0 else backlog

    @field_validator("runtime_telemetry_interval_seconds", mode="before")
    @classmethod
    def _normalize_runtime_telemetry_interval(cls, value: object) -> float:
        if value in (None, ""):
            return DEFAULT_RUNTIME_TELEMETRY_INTERVAL_SECONDS
        return max(0.0, float(str(value)))

    @field_validator("device_telemetry_persist_mode", mode="before")
    @classmethod
    def _normalize_device_telemetry_persist_mode(cls, value: object) -> str:
        if value in (None, ""):
            return "raw"
        mode = str(value).strip().lower()
        if mode not in {"raw", "sample", "aggregate", "off"}:
            raise ValueError("device_telemetry_persist_mode must be raw, sample, aggregate, or off")
        return mode

    @field_validator("device_telemetry_sample_rate", mode="before")
    @classmethod
    def _normalize_device_telemetry_sample_rate(cls, value: object) -> float:
        if value in (None, ""):
            return 1.0
        return min(1.0, max(0.0, float(str(value))))

    @field_validator("command_stage_event_mode", mode="before")
    @classmethod
    def _normalize_command_stage_event_mode(cls, value: object) -> str:
        if value in (None, ""):
            return "aggregate"
        mode = str(value).strip().lower()
        if mode not in {"raw", "sample", "aggregate", "off"}:
            raise ValueError("command_stage_event_mode must be raw, sample, aggregate, or off")
        return mode

    @field_validator("command_stage_event_sample_rate", mode="before")
    @classmethod
    def _normalize_command_stage_event_sample_rate(cls, value: object) -> float:
        if value in (None, ""):
            return 0.02
        return min(1.0, max(0.0, float(str(value))))

    @field_validator("event_write_batch_size", mode="before")
    @classmethod
    def _normalize_event_write_batch_size(cls, value: object) -> int:
        if value in (None, ""):
            return DEFAULT_EVENT_WRITE_BATCH_SIZE
        return max(1, int(str(value)))

    @field_validator("event_write_flush_interval_seconds", mode="before")
    @classmethod
    def _normalize_event_write_flush_interval(cls, value: object) -> float:
        if value in (None, ""):
            return DEFAULT_EVENT_WRITE_FLUSH_INTERVAL_SECONDS
        return max(0.0, float(str(value)))

    @field_validator("executor_mode", mode="before")
    @classmethod
    def _normalize_executor_mode(cls, value: object) -> str:
        if value in (None, ""):
            return "in_process"
        mode = str(value).strip().lower().replace("-", "_")
        if mode not in {"in_process", "process"}:
            raise ValueError("executor_mode must be in_process or process")
        return mode

    @field_validator("runner_workers", "worker_queue_max", mode="before")
    @classmethod
    def _normalize_positive_int(cls, value: object) -> int:
        if value in (None, ""):
            return 0
        return max(0, int(str(value)))

    @field_validator("worker_start_timeout_seconds", "worker_heartbeat_timeout_seconds", mode="before")
    @classmethod
    def _normalize_positive_float(cls, value: object) -> float:
        if value in (None, ""):
            return 60.0
        return max(1.0, float(str(value)))

    @model_validator(mode="after")
    def _validate_device_telemetry_aggregate_sink(self) -> "ControllerConfig":
        if self.device_telemetry_persist_mode == "aggregate" and self.runtime_telemetry_interval_seconds <= 0:
            raise ValueError("device_telemetry_persist_mode=aggregate requires runtime_telemetry_interval_seconds > 0")
        return self

    @classmethod
    def from_sources(
        cls,
        *,
        config_path: str | None = None,
        overrides: dict[str, object] | None = None,
    ) -> "ControllerConfig":
        """Load controller config from YAML, environment, and explicit overrides.

        Args:
            config_path: Optional controller YAML path.
            overrides: Explicit values from CLI flags; empty values are ignored.

        Returns:
            Validated controller configuration.
        """

        data: dict[str, object] = {}
        if config_path:
            path = Path(config_path).expanduser()
            if path.exists():
                loaded = yaml.safe_load(path.read_text(encoding="utf-8"))
                if isinstance(loaded, Mapping):
                    data.update({str(key): value for key, value in loaded.items()})
        device_telemetry_mode_explicit = "device_telemetry_persist_mode" in data
        security_raw = data.get("security")
        security: dict[str, object] = (
            {str(key): value for key, value in security_raw.items()} if isinstance(security_raw, Mapping) else {}
        )
        for env_name, key in (
            ("NORTHCUA_CONTROLLER_TOKEN", "token"),
            ("NORTHCUA_CONTROLLER_DEVICE_TOKEN", "device_token"),
            ("NORTHCUA_CONTROLLER_SUBMITTER_TOKEN", "submitter_token"),
        ):
            token = os.getenv(env_name)
            if token:
                security[key] = token
        for env_name, key in (
            ("NORTHCUA_CONTROLLER_COMMAND_TIMEOUT_SECONDS", "command_timeout_seconds"),
            ("NORTHCUA_CONTROLLER_OFFLINE_AFTER_SECONDS", "offline_after_seconds"),
            ("NORTHCUA_CONTROLLER_DEVICE_DISCONNECT_GRACE_SECONDS", "device_disconnect_grace_seconds"),
            ("NORTHCUA_CONTROLLER_WEBSOCKET_OPEN_TIMEOUT_SECONDS", "websocket_open_timeout_seconds"),
            ("NORTHCUA_CONTROLLER_WEBSOCKET_PING_INTERVAL_SECONDS", "websocket_ping_interval_seconds"),
            ("NORTHCUA_CONTROLLER_WEBSOCKET_PING_TIMEOUT_SECONDS", "websocket_ping_timeout_seconds"),
            ("NORTHCUA_CONTROLLER_WEBSOCKET_BACKLOG", "websocket_backlog"),
            ("NORTHCUA_CONTROLLER_RUNTIME_TELEMETRY_INTERVAL_SECONDS", "runtime_telemetry_interval_seconds"),
            ("NORTHCUA_CONTROLLER_DEVICE_TELEMETRY_PERSIST_MODE", "device_telemetry_persist_mode"),
            ("NORTHCUA_CONTROLLER_DEVICE_TELEMETRY_SAMPLE_RATE", "device_telemetry_sample_rate"),
            ("NORTHCUA_CONTROLLER_HEARTBEAT_PERSIST_INTERVAL_SECONDS", "heartbeat_persist_interval_seconds"),
            ("NORTHCUA_CONTROLLER_COMMAND_TIMEOUT_FLOOR_SECONDS", "command_timeout_floor_seconds"),
            ("NORTHCUA_CONTROLLER_COMMAND_STAGE_EVENT_MODE", "command_stage_event_mode"),
            ("NORTHCUA_CONTROLLER_COMMAND_STAGE_EVENT_SAMPLE_RATE", "command_stage_event_sample_rate"),
            ("NORTHCUA_CONTROLLER_EVENT_WRITE_BATCH_SIZE", "event_write_batch_size"),
            ("NORTHCUA_CONTROLLER_EVENT_WRITE_FLUSH_INTERVAL_SECONDS", "event_write_flush_interval_seconds"),
            ("NORTHCUA_CONTROLLER_EXECUTOR_MODE", "executor_mode"),
            ("NORTHCUA_CONTROLLER_RUNNER_WORKERS", "runner_workers"),
            ("NORTHCUA_CONTROLLER_WORKER_QUEUE_MAX", "worker_queue_max"),
            ("NORTHCUA_CONTROLLER_WORKER_START_TIMEOUT_SECONDS", "worker_start_timeout_seconds"),
            ("NORTHCUA_CONTROLLER_WORKER_HEARTBEAT_TIMEOUT_SECONDS", "worker_heartbeat_timeout_seconds"),
            ("NORTHCUA_CONTROLLER_PROCESS_WORKER_MODULE", "process_worker_module"),
        ):
            value = os.getenv(env_name)
            if value:
                data[key] = value
                if key == "device_telemetry_persist_mode":
                    device_telemetry_mode_explicit = True
        legacy_timeout_floor = os.getenv("NORTHCUA_ACTION_COMMAND_TIMEOUT_SECONDS")
        if legacy_timeout_floor and "command_timeout_floor_seconds" not in data:
            data["command_timeout_floor_seconds"] = legacy_timeout_floor
        if security:
            data["security"] = security
        if overrides:
            clean_overrides = {key: value for key, value in overrides.items() if value not in (None, "", set())}
            override_security = clean_overrides.get("security")
            if isinstance(override_security, Mapping):
                clean_overrides.pop("security")
                security_raw = data.get("security")
                security = (
                    {str(key): value for key, value in security_raw.items()}
                    if isinstance(security_raw, Mapping)
                    else {}
                )
                security.update({str(key): value for key, value in override_security.items()})
                data["security"] = security
            if "device_telemetry_persist_mode" in clean_overrides:
                device_telemetry_mode_explicit = True
            data.update(clean_overrides)
        runtime_telemetry_interval = data.get("runtime_telemetry_interval_seconds")
        if runtime_telemetry_interval is not None and not device_telemetry_mode_explicit:
            runtime_telemetry_interval_seconds = _coerce_float(
                runtime_telemetry_interval,
                DEFAULT_RUNTIME_TELEMETRY_INTERVAL_SECONDS,
            )
            if runtime_telemetry_interval_seconds <= 0:
                data["device_telemetry_persist_mode"] = "off"
        return cls.model_validate(data)


def _coerce_float(value: object, default: float) -> float:
    if not isinstance(value, str | int | float):
        return default
    try:
        return float(value)
    except ValueError:
        return default
