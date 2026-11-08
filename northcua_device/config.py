"""Device worker configuration and loading helpers."""

from __future__ import annotations

import os
import socket
from pathlib import Path

import yaml
from pydantic import BaseModel, Field, field_validator

from northcua_protocol.capabilities import Capability, PlatformKind


class DeviceClientConfig(BaseModel):
    """Configuration for a standalone NorthCUA device worker.

    Attributes:
        server_url: Controller websocket URL.
        token: Device bearer token.
        device_id: Stable worker id used for strict targeting.
        display_name: Optional operator-facing label.
        platform: Platform family used to select runtime adapters.
        max_sessions: Maximum active sessions this worker accepts.
        max_gui_sessions: GUI session capacity for foreground-focus runtimes.
        gui_resource_id: Shared GUI resource id for workers controlling the
            same desktop/display. Leave blank to derive a local host id.
        max_concurrent_commands: Maximum concurrently executing commands.
        supports_parallel_sessions: Whether scheduler may place multiple sessions.
        requires_foreground_focus: Whether GUI focus makes parallel sessions unsafe.
        allowed_tools: Optional tool-id allowlist.
        allowed_capabilities: Optional capability allowlist for submitted tasks.
        agentkit_agent_kinds: AgentKit agent kinds explicitly exposed by this worker.
        heartbeat_interval_seconds: Heartbeat cadence.
        telemetry_stream_mode: Upstream telemetry mode. Local JSONL remains full.
        telemetry_sample_rate: Deterministic command-level full-stage sample
            rate used by ``terminal_sampled``.
        telemetry_always_events: Extra device telemetry event names to stream
            upstream in addition to safe terminal/default events.
        reconnect_initial_delay_seconds: Initial reconnect backoff.
        reconnect_max_delay_seconds: Maximum reconnect backoff.
        insecure_dev_no_auth: Whether to omit token auth for local dev only.
        log_dir: Directory for device runtime JSONL events.
    """

    server_url: str = "ws://127.0.0.1:8765"
    token: str = ""
    device_id: str = "local-device"
    display_name: str = ""
    platform: PlatformKind = PlatformKind.MACOS
    max_sessions: int = 1
    max_gui_sessions: int = 1
    gui_resource_id: str = ""
    max_concurrent_commands: int = 1
    supports_parallel_sessions: bool = False
    requires_foreground_focus: bool = True
    allowed_tools: set[str] = Field(default_factory=set)
    allowed_capabilities: set[Capability] = Field(default_factory=set)
    agentkit_agent_kinds: set[str] = Field(default_factory=set)
    heartbeat_interval_seconds: float = 30.0
    telemetry_stream_mode: str = "terminal_sampled"
    telemetry_sample_rate: float = 0.02
    telemetry_always_events: set[str] = Field(default_factory=set)
    reconnect_initial_delay_seconds: float = 1.0
    reconnect_max_delay_seconds: float = 30.0
    insecure_dev_no_auth: bool = False
    log_dir: str = "logs/device"

    @classmethod
    def from_sources(
        cls,
        *,
        config_path: str | None = None,
        overrides: dict[str, object] | None = None,
    ) -> "DeviceClientConfig":
        """Load config using YAML, environment variables, then explicit overrides.

        Args:
            config_path: Optional YAML config path.
            overrides: CLI-level values that take final precedence.

        Returns:
            Validated device client configuration.
        """

        data: dict[str, object] = {}
        if config_path:
            path = Path(config_path).expanduser()
            if path.exists():
                loaded = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
                if isinstance(loaded, dict):
                    data.update(loaded)
        env_map = {
            "server_url": "NORTHCUA_DEVICE_SERVER",
            "token": "NORTHCUA_DEVICE_TOKEN",
            "device_id": "NORTHCUA_DEVICE_ID",
            "display_name": "NORTHCUA_DEVICE_NAME",
            "gui_resource_id": "NORTHCUA_DEVICE_GUI_RESOURCE_ID",
            "heartbeat_interval_seconds": "NORTHCUA_DEVICE_HEARTBEAT_INTERVAL_SECONDS",
            "telemetry_stream_mode": "NORTHCUA_DEVICE_TELEMETRY_STREAM_MODE",
            "telemetry_sample_rate": "NORTHCUA_DEVICE_TELEMETRY_SAMPLE_RATE",
            "telemetry_always_events": "NORTHCUA_DEVICE_TELEMETRY_ALWAYS_EVENTS",
        }
        for key, env_name in env_map.items():
            value = os.getenv(env_name)
            if value:
                data[key] = value
        if overrides:
            data.update({key: value for key, value in overrides.items() if value not in (None, "", set())})
        return cls.model_validate(data)

    @field_validator("telemetry_stream_mode", mode="before")
    @classmethod
    def _normalize_telemetry_stream_mode(cls, value: object) -> str:
        if value is None or value == "":
            return "terminal_sampled"
        normalized = str(value).strip().lower().replace("-", "_")
        if normalized in {"all", "full"}:
            return "raw"
        if normalized in {"sample", "sampled"}:
            return "terminal_sampled"
        return normalized

    @field_validator("telemetry_sample_rate", mode="before")
    @classmethod
    def _normalize_telemetry_sample_rate(cls, value: object) -> float:
        if value is None or value == "":
            return 0.02
        try:
            parsed = float(str(value))
        except (TypeError, ValueError):
            return 0.02
        return min(1.0, max(0.0, parsed))

    @field_validator("telemetry_always_events", mode="before")
    @classmethod
    def _normalize_telemetry_always_events(cls, value: object) -> set[str]:
        if value in (None, ""):
            return set()
        if isinstance(value, str):
            return {part.strip() for part in value.split(",") if part.strip()}
        return set(value) if isinstance(value, (list, tuple, set)) else set()

    def resolved_gui_resource_id(self) -> str:
        """Return the GUI resource id advertised to the controller.

        Returns:
            Explicit configured resource id, or a stable local host/display id
            so multiple local workers are scheduled as one shared GUI surface.
        """

        if self.gui_resource_id:
            return self.gui_resource_id
        return f"{self.platform.value}:{socket.gethostname()}:gui"
