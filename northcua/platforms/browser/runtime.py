"""Browser platform runtime adapter backed by Playwright browser backends."""

from __future__ import annotations

from northcua.platforms.protocol import (
    PlatformActionRequest,
    PlatformActionResult,
    PlatformActionStatus,
    PlatformCapability,
    PlatformHealthStatus,
    PlatformKind,
    PlatformObservation,
    PlatformRuntime,
)
from northcua.runtime.browser.backend import BrowserSnapshotResult, PlaywrightBrowserBackend


class BrowserPlatformRuntime(PlatformRuntime):
    """Browser runtime adapter for browser CUA agents."""

    platform_kind = PlatformKind.BROWSER

    def __init__(self, *, browser_backend: PlaywrightBrowserBackend | None = None) -> None:
        """Initialize the browser runtime.

        Args:
            browser_backend: Optional Playwright backend.
        """

        self._browser_backend = browser_backend

    async def healthcheck(self) -> PlatformHealthStatus:
        """Return browser runtime health without forcing a page launch.

        Returns:
            Platform health status with configured capabilities.
        """

        return PlatformHealthStatus(
            platform_kind=self.platform_kind,
            ok=True,
            message="browser backend configured" if self._browser_backend is not None else "browser descriptor only",
            capabilities=(
                PlatformCapability("snapshot", enabled=self._browser_backend is not None),
                PlatformCapability("action", enabled=self._browser_backend is not None),
            ),
        )

    async def capture_observation(
        self,
        *,
        session_step: int,
        previous_steps: list[object] | None = None,
    ) -> PlatformObservation:
        """Capture a browser snapshot when a backend is configured.

        Args:
            session_step: Current step number.
            previous_steps: Reserved previous step records.

        Returns:
            Platform observation wrapping a browser snapshot.

        Raises:
            RuntimeError: If no browser backend was configured.
        """

        del session_step, previous_steps
        if self._browser_backend is None:
            raise RuntimeError("BrowserPlatformRuntime has no browser backend configured.")
        snapshot = self._browser_backend.capture_snapshot()
        return PlatformObservation(
            platform_kind=self.platform_kind,
            raw=snapshot,
            metadata=_snapshot_metadata(snapshot),
        )

    async def execute_action(self, request: PlatformActionRequest) -> PlatformActionResult:
        """Execute a minimal browser action through the configured backend.

        Args:
            request: Platform action request.

        Returns:
            Platform action result.

        Raises:
            RuntimeError: If no browser backend was configured.
            ValueError: If the requested action is not supported by this adapter.
        """

        if self._browser_backend is None:
            raise RuntimeError("BrowserPlatformRuntime has no browser backend configured.")
        selector = str(request.parameters.get("selector", ""))
        if request.name == "click":
            self._browser_backend.click(selector)
        elif request.name == "hover":
            self._browser_backend.hover(selector)
        else:
            raise ValueError(f"Unsupported browser platform action: {request.name}")
        return PlatformActionResult(call_id=request.call_id, status=PlatformActionStatus.SUCCESS)


def _snapshot_metadata(snapshot: BrowserSnapshotResult) -> dict[str, object]:
    """Build small metadata for a browser snapshot without inlining the element tree."""

    return {
        "title": snapshot.title,
        "url": snapshot.url,
        "element_count": len(snapshot.elements),
    }
