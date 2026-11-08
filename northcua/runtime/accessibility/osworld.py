"""OSWorld accessibility backend using the benchmark HTTP service."""

from __future__ import annotations

import logging
import os
import time

import requests

from northcua.core.accessibility import (
    AccessibilityCapabilities,
    AccessibilityCaptureRequest,
    AccessibilityCaptureStats,
    AccessibilitySnapshot,
)
from northcua.core.desktop_runtime_environment import (
    OSWORLD_COMMAND_TIMEOUT_ENV,
    OSWORLD_ENABLE_A11Y_ENV,
    OSWORLD_SERVER_PORT_ENV,
    OSWORLD_VM_IP_ENV,
)
from northcua.core.enums import (
    AccessibilityCaptureStatus,
    AccessibilityRawFormat,
    AccessibilityScope,
    AccessibilitySourceKind,
)

from .base import AccessibilityBackend

logger = logging.getLogger(__name__)


class OSWorldAccessibilityBackend(AccessibilityBackend):
    """Accessibility backend that fetches OSWorld's raw accessibility payload."""

    def __init__(self) -> None:
        super().__init__(platform_name="osworld", source_kind=AccessibilitySourceKind.ATSPI)
        self.vm_ip = os.environ.get(OSWORLD_VM_IP_ENV, "127.0.0.1").strip() or "127.0.0.1"
        self.server_port = int(os.environ.get(OSWORLD_SERVER_PORT_ENV, "5000") or "5000")
        self.timeout_seconds = float(os.environ.get(OSWORLD_COMMAND_TIMEOUT_ENV, "120") or "120")
        self.enabled = _parse_bool(os.environ.get(OSWORLD_ENABLE_A11Y_ENV), default=True)
        self.base_url = f"http://{self.vm_ip}:{self.server_port}"
        self.session = requests.Session()
        self.session.trust_env = False

    def capabilities(self) -> AccessibilityCapabilities:
        """Return OSWorld accessibility capture capabilities.

        Returns:
            Capability metadata for remote OSWorld accessibility capture.
        """

        if not self.enabled:
            return AccessibilityCapabilities(
                platform_name=self.platform_name,
                source=self.source_kind,
                supported_scopes=[],
                requires_permission=False,
                permission_granted=True,
                raw_formats=[],
                message="OSWorld remote accessibility is disabled by configuration.",
            )
        return AccessibilityCapabilities(
            platform_name=self.platform_name,
            source=self.source_kind,
            supported_scopes=[AccessibilityScope.DESKTOP],
            requires_permission=False,
            permission_granted=True,
            raw_formats=[AccessibilityRawFormat.XML],
            message=(
                "OSWorld currently exposes a raw accessibility payload only. "
                "NorthCUA stores that payload and degrades normalized tree support."
            ),
        )

    def capture(self, request: AccessibilityCaptureRequest) -> AccessibilitySnapshot:
        """Capture OSWorld's raw accessibility payload.

        Args:
            request: Accessibility capture request.

        Returns:
            Accessibility snapshot with raw XML payload when available.
        """

        started_at = time.perf_counter()
        if not self.enabled:
            return AccessibilitySnapshot.failed(
                status=AccessibilityCaptureStatus.DISABLED,
                platform_name=self.platform_name,
                source=self.source_kind,
                scope=request.scope,
                error="OSWorld remote accessibility is disabled by configuration.",
                capture_ms=(time.perf_counter() - started_at) * 1000,
            )
        if request.scope != AccessibilityScope.DESKTOP:
            return self.unsupported_snapshot(
                scope=request.scope,
                message="OSWorld accessibility capture currently supports desktop scope only.",
            ).model_copy(
                update={
                    "stats": AccessibilityCaptureStats(capture_ms=(time.perf_counter() - started_at) * 1000),
                }
            )
        try:
            response = self.session.get(f"{self.base_url}/accessibility", timeout=self.timeout_seconds)
            response.raise_for_status()
            payload = response.json()
            raw_payload = str(payload.get("AT") or "")
            return AccessibilitySnapshot(
                status=AccessibilityCaptureStatus.SUCCESS,
                platform_name=self.platform_name,
                source=self.source_kind,
                scope=request.scope,
                stats=AccessibilityCaptureStats(capture_ms=(time.perf_counter() - started_at) * 1000),
                raw_format=AccessibilityRawFormat.XML,
                raw_payload=raw_payload,
            )
        except Exception as exc:
            logger.warning(
                "OSWorld accessibility request failed",
                extra={"base_url": self.base_url, "scope": request.scope.value},
                exc_info=True,
            )
            return self.error_snapshot(
                scope=request.scope,
                message=f"OSWorld accessibility request failed: {exc}",
            ).model_copy(
                update={
                    "stats": AccessibilityCaptureStats(capture_ms=(time.perf_counter() - started_at) * 1000),
                }
            )


def _parse_bool(value: str | None, *, default: bool) -> bool:
    if value is None:
        return default
    lowered = str(value).strip().lower()
    if lowered in {"1", "true", "yes", "on"}:
        return True
    if lowered in {"0", "false", "no", "off"}:
        return False
    return default
