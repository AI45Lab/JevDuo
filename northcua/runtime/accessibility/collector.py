"""High-level collector facade for cross-platform accessibility capture."""

from __future__ import annotations

import json
import logging
import platform
import threading
import time
from dataclasses import dataclass

from northcua.core.accessibility import AccessibilityCapabilities, AccessibilityCaptureRequest, AccessibilitySnapshot
from northcua.core.enums import AccessibilityCaptureStatus, AccessibilityRawFormat

from .base import AccessibilityBackend
from .linux import LinuxAccessibilityBackend
from .macos import MacOSAccessibilityBackend
from .summary import AccessibilitySummaryBuilder, calculate_capture_stats
from .windows import WindowsAccessibilityBackend

logger = logging.getLogger(__name__)


class _UnsupportedAccessibilityBackend(AccessibilityBackend):
    """Fallback backend for unsupported host platforms."""

    def __init__(self, *, platform_name: str) -> None:
        super().__init__(platform_name=platform_name, source_kind=None)

    def capabilities(self) -> AccessibilityCapabilities:
        """Return an unsupported capability response.

        Returns:
            Capability metadata explaining that the host platform is unsupported.
        """

        return AccessibilityCapabilities(
            platform_name=self.platform_name,
            source=None,
            message=f"Accessibility capture is not supported on platform `{self.platform_name}`.",
        )

    def capture(self, request: AccessibilityCaptureRequest) -> AccessibilitySnapshot:
        """Return an unsupported snapshot for the requested scope.

        Args:
            request: Capture request containing the requested scope.

        Returns:
            Failed snapshot with unsupported status.
        """

        return self.unsupported_snapshot(
            scope=request.scope,
            message=f"Accessibility capture is not supported on platform `{self.platform_name}`.",
        )


@dataclass(slots=True)
class _CaptureThreadState:
    """Result carrier for one timed background capture."""

    snapshot: AccessibilitySnapshot | None = None
    error: BaseException | None = None


class AccessibilityCollector:
    """Facade selecting one backend and normalizing capture results."""

    def __init__(
        self,
        *,
        backend: AccessibilityBackend | None = None,
        platform_name: str | None = None,
        summary_builder: AccessibilitySummaryBuilder | None = None,
    ) -> None:
        self.platform_name = (platform_name or platform.system()).lower()
        self.backend = backend or self._build_backend(self.platform_name)
        self.summary_builder = summary_builder or AccessibilitySummaryBuilder()

    def capabilities(self) -> AccessibilityCapabilities:
        """Return capability metadata for the active platform/backend.

        Returns:
            Capability metadata from the backend, or a fail-open diagnostic
            response if discovery itself fails.
        """

        try:
            return self.backend.capabilities()
        except Exception as exc:
            logger.warning("Accessibility capability discovery failed.", exc_info=True)
            return AccessibilityCapabilities(
                platform_name=self.platform_name,
                source=self.backend.source_kind,
                message=f"Accessibility capability discovery failed: {exc}",
            )

    def capture(self, request: AccessibilityCaptureRequest) -> AccessibilitySnapshot:
        """Capture one normalized snapshot with timeout and summary normalization.

        Args:
            request: Capture scope, traversal limits, and timeout settings.

        Returns:
            Normalized snapshot enriched with summary and timing statistics.
        """

        started_at = time.perf_counter()
        state = _CaptureThreadState()

        def _capture() -> None:
            """Run backend capture inside the timeout boundary."""

            try:
                state.snapshot = self.backend.capture(request)
            except BaseException as exc:  # pragma: no cover - defensive capture boundary
                logger.warning("Accessibility backend capture raised across thread boundary.", exc_info=True)
                state.error = exc

        thread = threading.Thread(target=_capture, name="northcua-a11y-capture", daemon=True)
        thread.start()
        thread.join(timeout=max(request.capture_timeout_seconds, 0.1))
        if thread.is_alive():
            snapshot = AccessibilitySnapshot.failed(
                status=AccessibilityCaptureStatus.TIMEOUT,
                platform_name=self.platform_name,
                source=self.backend.source_kind,
                scope=request.scope,
                error="Accessibility capture timed out.",
                capture_ms=(time.perf_counter() - started_at) * 1000,
            )
        elif state.error is not None:
            snapshot = AccessibilitySnapshot.failed(
                status=AccessibilityCaptureStatus.ERROR,
                platform_name=self.platform_name,
                source=self.backend.source_kind,
                scope=request.scope,
                error=f"Accessibility capture failed: {state.error}",
                capture_ms=(time.perf_counter() - started_at) * 1000,
            )
        elif state.snapshot is not None:
            snapshot = state.snapshot
        else:
            snapshot = AccessibilitySnapshot.failed(
                status=AccessibilityCaptureStatus.ERROR,
                platform_name=self.platform_name,
                source=self.backend.source_kind,
                scope=request.scope,
                error="Accessibility capture produced no result.",
                capture_ms=(time.perf_counter() - started_at) * 1000,
            )

        elapsed_ms = (time.perf_counter() - started_at) * 1000
        if snapshot.raw_payload and snapshot.raw_format == AccessibilityRawFormat.NONE:
            snapshot = snapshot.model_copy(update={"raw_format": AccessibilityRawFormat.JSON})
        return snapshot.model_copy(
            update={
                "summary": snapshot.summary or self.summary_builder.build(snapshot.tree),
                "stats": calculate_capture_stats(
                    snapshot.tree,
                    capture_ms=elapsed_ms,
                    truncated=snapshot.stats.truncated,
                ),
            }
        )

    @staticmethod
    def _build_backend(platform_name: str) -> AccessibilityBackend:
        """Build the platform backend for a normalized host name.

        Args:
            platform_name: Lowercase value from ``platform.system()`` or tests.

        Returns:
            Platform backend, or an unsupported fail-open backend.
        """

        if platform_name == "windows":
            return WindowsAccessibilityBackend()
        if platform_name == "darwin":
            return MacOSAccessibilityBackend()
        if platform_name == "linux":
            return LinuxAccessibilityBackend()
        return _UnsupportedAccessibilityBackend(platform_name=platform_name)
