"""Websocket transport layer.

This package intentionally avoids eager importing of the heavier server/transport
modules so submodule imports do not create circular dependencies during bootstrap.
"""

__all__ = [
    "NorthCUAWebSocketClient",
    "NorthCUAWebSocketTransport",
    "NorthCUAWebSocketTransportConfig",
]


def __getattr__(name: str):
    """Lazily import public websocket transport exports.

    Args:
        name: Requested attribute name.

    Returns:
        Requested websocket class or config type.

    Raises:
        AttributeError: If the name is not exported by this package.
    """

    if name == "NorthCUAWebSocketClient":
        from .client import NorthCUAWebSocketClient

        return NorthCUAWebSocketClient
    if name == "NorthCUAWebSocketTransport":
        from .transport import NorthCUAWebSocketTransport

        return NorthCUAWebSocketTransport
    if name == "NorthCUAWebSocketTransportConfig":
        from .config import NorthCUAWebSocketTransportConfig

        return NorthCUAWebSocketTransportConfig
    raise AttributeError(name)
