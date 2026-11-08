"""Capability guard helpers for desktop accessibility observations."""

from __future__ import annotations

import logging
from typing import Protocol

from northcua.app.logging import log_runtime_event
from northcua.core.config import AccessibilityObservationConfig
from northcua.core.enums import AccessibilityCaptureStatus
from northcua.runtime.accessibility import AccessibilityCapabilities, AccessibilityStepRecord

logger = logging.getLogger("northcua.observation.accessibility")


def safe_exception_message(exc: Exception) -> str:
    """Return a logging-safe exception message.

    Args:
        exc: Exception raised while probing or capturing accessibility state.

    Returns:
        String message suitable for artifacts and structured runtime logs.
    """
    try:
        return str(exc)
    except Exception:
        logger.debug("Failed to stringify accessibility exception for safe logging.", exc_info=True)
        return f"<unprintable {type(exc).__name__}>"


class _CapabilityChecker(Protocol):
    """Capability checker surface used by the capture guard."""

    async def get(self, *, session_step: int) -> AccessibilityCapabilities:
        """Return accessibility capabilities for the current session.

        Args:
            session_step: Current session-level step used for logging and
                capability probe correlation.

        Returns:
            Backend capability record describing supported scopes, permission
            state, and platform metadata.
        """
        ...


class _AccessibilityCaptureGuardHost(Protocol):
    """Host attributes required by ``AccessibilityCaptureGuardMixin``."""

    @property
    def config(self) -> AccessibilityObservationConfig:
        """Accessibility observation configuration.

        Returns:
            Runtime configuration controlling scope, fail-open behavior, and
            capture limits for accessibility observations.
        """
        ...

    @property
    def capabilities_checker(self) -> _CapabilityChecker:
        """Checker used to discover accessibility backend capabilities.

        Returns:
            Capability checker bound to the current desktop observation backend.
        """
        ...

    @property
    def cache_key(self) -> tuple[str, str]:
        """Session/server scoped capability cache key.

        Returns:
            Tuple that identifies where capability probe results have already
            been logged for this process.
        """
        ...

    @property
    def _capabilities_logged(self) -> set[tuple[str, str]]:
        """Process-level set of already logged capability checks.

        Returns:
            Mutable process-local set used to avoid repeating capability logs
            for the same session/backend pair.
        """
        ...

    def _blocked_capability_record(
        self,
        *,
        capabilities: AccessibilityCapabilities,
        session_step: int,
        status: AccessibilityCaptureStatus,
        error: str,
    ) -> AccessibilityStepRecord:
        """Build a blocked accessibility capture record."""
        ...


class AccessibilityCaptureGuardMixin:
    """Capability checks and fail-open gating for accessibility capture."""

    async def _ensure_capabilities(
        self: _AccessibilityCaptureGuardHost, *, session_step: int
    ) -> AccessibilityCapabilities | None:
        """Return capabilities or ``None`` when fail-open hides probe failure."""
        try:
            capabilities = await self.capabilities_checker.get(session_step=session_step)
        except Exception as exc:
            if not self.config.fail_open:
                raise
            capabilities = None
            error_message = safe_exception_message(exc)
            if self.cache_key not in self._capabilities_logged:
                log_runtime_event(
                    logger,
                    "a11y_capabilities_checked",
                    level=logging.WARNING,
                    session_step=session_step,
                    scope=self.config.scope,
                    status=AccessibilityCaptureStatus.ERROR,
                    error=error_message,
                    error_type=type(exc).__name__,
                    fail_open=True,
                )
                self._capabilities_logged.add(self.cache_key)
        else:
            if self.cache_key not in self._capabilities_logged:
                log_runtime_event(
                    logger,
                    "a11y_capabilities_checked",
                    session_step=session_step,
                    scope=self.config.scope,
                    platform_name=capabilities.platform_name,
                    source=capabilities.source,
                    supported_scopes=capabilities.supported_scopes,
                    requires_permission=capabilities.requires_permission,
                    permission_granted=capabilities.permission_granted,
                    raw_formats=capabilities.raw_formats,
                    capability_message=capabilities.message,
                )
                self._capabilities_logged.add(self.cache_key)
        return capabilities

    def _capabilities_prevent_capture(
        self: _AccessibilityCaptureGuardHost,
        capabilities: AccessibilityCapabilities | None,
        *,
        session_step: int,
    ) -> AccessibilityStepRecord | None:
        """Return an accessibility record when capabilities block capture."""
        if capabilities is None:
            return None
        if self.config.scope not in capabilities.supported_scopes:
            return self._blocked_capability_record(
                capabilities=capabilities,
                session_step=session_step,
                status=AccessibilityCaptureStatus.UNSUPPORTED,
                error=capabilities.message or "Configured scope is not supported by the accessibility backend.",
            )
        if capabilities.requires_permission and (not capabilities.permission_granted):
            return self._blocked_capability_record(
                capabilities=capabilities,
                session_step=session_step,
                status=AccessibilityCaptureStatus.PERMISSION_DENIED,
                error=capabilities.message or "Accessibility permission not granted.",
            )
        return None

    def _blocked_capability_record(
        self: _AccessibilityCaptureGuardHost,
        *,
        capabilities: AccessibilityCapabilities,
        session_step: int,
        status: AccessibilityCaptureStatus,
        error: str,
    ) -> AccessibilityStepRecord:
        log_runtime_event(
            logger,
            "a11y_capture_failed",
            level=logging.WARNING,
            session_step=session_step,
            scope=self.config.scope,
            platform_name=capabilities.platform_name,
            source=capabilities.source,
            status=status,
            error=error,
        )
        return AccessibilityStepRecord(
            status=status,
            source=capabilities.source,
            scope=self.config.scope,
            diff_mode=self.config.diff_mode,
            error=error,
        )
