"""Factories for Playwright web-agent browser sessions."""

from __future__ import annotations

from northcua.core.config import WebAgentConfig

from .playwright import PlaywrightWebActionBackend
from .protocol import WebActionBackend


def build_playwright_web_backend(*, config: WebAgentConfig, session_id: str) -> WebActionBackend:
    """Create the Playwright web action backend for one session."""

    return PlaywrightWebActionBackend(config=config, session_id=session_id)
