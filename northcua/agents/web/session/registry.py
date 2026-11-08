"""In-process registry mapping NorthCUA sessions to web-agent clients."""

from __future__ import annotations

import threading

from .models import RegisteredWebAgentSession


class WebAgentSessionRegistry:
    """Thread-safe singleton registry for active web-agent sessions."""

    _instance: "WebAgentSessionRegistry | None" = None
    _instance_lock = threading.Lock()
    _sessions: dict[str, RegisteredWebAgentSession]
    _lock: threading.RLock

    def __new__(cls) -> "WebAgentSessionRegistry":
        with cls._instance_lock:
            if cls._instance is None:
                cls._instance = super().__new__(cls)
            return cls._instance

    def __init__(self) -> None:
        if hasattr(self, "_sessions"):
            return
        self._sessions = {}
        self._lock = threading.RLock()

    def register(self, session: RegisteredWebAgentSession) -> None:
        """Register one active session.

        Args:
            session: Device-side web-agent session state to make available to
                MCP tools.
        """

        with self._lock:
            self._sessions[session.session_id] = session

    def get_registered_session(self, session_id: str) -> RegisteredWebAgentSession:
        """Return an active session or raise a clear lookup error.

        Args:
            session_id: NorthCUA session id to resolve.

        Returns:
            Registered web-agent session state.

        Raises:
            LookupError: If no active session exists for ``session_id``.
        """

        with self._lock:
            session = self._sessions.get(session_id)
        if session is None:
            raise LookupError(f"No active web-agent session registered for session_id={session_id!r}")
        return session

    def unregister(self, session_id: str) -> None:
        """Remove one session from the registry.

        Args:
            session_id: NorthCUA session id to remove.
        """

        with self._lock:
            self._sessions.pop(session_id, None)

    def clear(self) -> None:
        """Clear all sessions. Primarily useful for tests."""

        with self._lock:
            self._sessions.clear()
