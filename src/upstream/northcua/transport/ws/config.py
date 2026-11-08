"""Transport-local configuration for the NorthCUA websocket transport."""

from __future__ import annotations

from pydantic import BaseModel

from northcua.core.enums import RuntimeMode


class NorthCUAWebSocketTransportConfig(BaseModel):
    """Execution-focused transport config shared by the websocket server/client.

    Fields:
        runtime_mode: Whether this transport executes locally or via remote websocket devices.
    """

    runtime_mode: RuntimeMode = RuntimeMode.LOCAL
