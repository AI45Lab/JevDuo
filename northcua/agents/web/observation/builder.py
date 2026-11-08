"""Observation builder for web-agent steps."""

from __future__ import annotations

from ..backend import WebActionBackend
from ..domain.observations import WebObservationContext, WebScreenshotObservation


class WebObservationBuilder:
    """Collect prompt-facing page state from a browser backend."""

    async def build(
        self,
        *,
        client: WebActionBackend,
        include_screenshot: bool,
    ) -> WebObservationContext:
        """Build the current observation context for one step.

        Args:
            client: Browser client that owns the current web session.
            include_screenshot: Whether to capture a screenshot for prompt
                projection in addition to the default snapshot.

        Returns:
            Prompt-facing observation context for the next model step.
        """

        snapshot = await client.snapshot()
        screenshot: WebScreenshotObservation | None = None
        if include_screenshot:
            screenshot = await client.screenshot()
        return WebObservationContext(snapshot=snapshot, screenshot=screenshot)
