"""Observation collection pipeline for web-agent snapshots and SoM images."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from time import time_ns
from typing import Protocol

from northcua.core.json_types import JsonObject

from ..backend import WebActionCommandResult
from ..domain.observations import WebObservationContext, WebScreenshotObservation, WebSnapshotObservation
from ..session.models import RegisteredWebAgentSession
from .settle import WebBboxProbe, WebPostActionSettleService, WebSettleResult

ObservationMode = str
ScreenshotPersister = Callable[[RegisteredWebAgentSession, WebScreenshotObservation], WebScreenshotObservation]


class WebObservationQualityError(RuntimeError):
    """Raised when a captured observation is unsafe to expose as fresh state."""


class ScreenshotAnnotator(Protocol):
    """Annotate a screenshot, optionally reusing a collector capture probe."""

    def __call__(
        self,
        registered: RegisteredWebAgentSession,
        screenshot: WebScreenshotObservation,
        snapshot: WebSnapshotObservation,
        *,
        viewport_payload: JsonObject | None = None,
        box_payloads: Mapping[str, object] | None = None,
        state_payloads: Mapping[str, object] | None = None,
    ) -> Awaitable[WebScreenshotObservation]:
        """Return a marked screenshot."""
        ...


@dataclass(slots=True)
class WebObservationCollector:
    """Collect aligned web observations for initial and post-action prompts."""

    settle_service: WebPostActionSettleService
    persist_screenshot: ScreenshotPersister
    annotate_screenshot: ScreenshotAnnotator

    async def collect(
        self,
        *,
        registered: RegisteredWebAgentSession,
        mode: ObservationMode,
        observation_id: str | None = None,
        settle_result: WebSettleResult | None = None,
    ) -> WebObservationContext:
        """Collect a fresh observation in snapshot or SoM mode."""

        normalized_mode = mode if mode in {"snapshot", "som"} else "snapshot"
        snapshot = await registered.client.snapshot()
        screenshot: WebScreenshotObservation | None = None
        aligned_snapshot = snapshot
        settled = settle_result.settled if settle_result is not None else True
        settle_reason = settle_result.reason if settle_result is not None else "captured"
        if normalized_mode == "som":
            screenshot, aligned, align_reason, aligned_snapshot = await self._capture_aligned_som(registered, snapshot)
            if settle_result is not None and self._is_empty_som_quality_failure(screenshot, aligned_snapshot):
                screenshot, aligned, align_reason, aligned_snapshot = await self._retry_empty_som_observation(
                    registered=registered,
                    screenshot=screenshot,
                    snapshot=aligned_snapshot,
                    aligned=aligned,
                    align_reason=align_reason,
                )
            if not aligned:
                settled = False
                settle_reason = self._append_reason(settle_reason, align_reason)
        return WebObservationContext(
            snapshot=aligned_snapshot,
            screenshot=screenshot,
            observation_id=observation_id or f"web-obs-{time_ns()}",
            settled=settled,
            settle_reason=settle_reason,
        )

    async def _retry_empty_som_observation(
        self,
        *,
        registered: RegisteredWebAgentSession,
        screenshot: WebScreenshotObservation | None,
        snapshot: WebSnapshotObservation,
        aligned: bool,
        align_reason: str,
    ) -> tuple[WebScreenshotObservation | None, bool, str, WebSnapshotObservation]:
        max_attempts = int(registered.settings.som_empty_ref_retry_attempts)
        current_screenshot = screenshot
        current_snapshot = snapshot
        current_aligned = aligned
        current_reason = align_reason
        for attempt in range(max_attempts):
            retry_snapshot = await registered.client.snapshot()
            retry_screenshot, retry_aligned, retry_reason, aligned_snapshot = await self._capture_aligned_som(
                registered,
                retry_snapshot,
            )
            current_screenshot = retry_screenshot
            current_snapshot = aligned_snapshot
            current_aligned = retry_aligned
            current_reason = self._append_reason(
                retry_reason,
                f"empty_som_quality_retry_attempt_{attempt + 1}",
            )
            if not self._is_empty_som_quality_failure(current_screenshot, current_snapshot):
                return current_screenshot, current_aligned, current_reason, current_snapshot
        raise WebObservationQualityError(
            "post-action SoM observation quality failed: screenshot was captured for a non-blank page, "
            "but model-visible refs and SoM marks were empty after retry. Call web_snapshot or web_screenshot "
            "before reusing old refs."
        )

    async def _capture_aligned_som(
        self,
        registered: RegisteredWebAgentSession,
        snapshot: WebSnapshotObservation,
    ) -> tuple[WebScreenshotObservation | None, bool, str, WebSnapshotObservation]:
        config = registered.settings.post_action_settle
        if not config.final_bbox_verify:
            screenshot = await self._capture_som(registered, snapshot)
            capture_error = self._capture_error_reason(screenshot)
            if capture_error:
                return screenshot, False, capture_error, snapshot
            return screenshot, True, "final_bbox_verify_disabled", snapshot

        first = await self._capture_with_bbox_check(registered, snapshot)
        if first[1]:
            return first[0], first[1], first[2], snapshot
        if self._is_screenshot_capture_failure(first[2]):
            return first[0], False, first[2], snapshot
        retry_snapshot = await registered.client.snapshot()
        retry = await self._capture_with_bbox_check(registered, retry_snapshot)
        if retry[1]:
            return retry[0], True, "bbox_changed_during_capture_retry_succeeded", retry_snapshot
        return retry[0], False, self._append_reason("bbox_changed_during_capture_best_effort", retry[2]), retry_snapshot

    async def _capture_with_bbox_check(
        self,
        registered: RegisteredWebAgentSession,
        snapshot: WebSnapshotObservation,
    ) -> tuple[WebScreenshotObservation | None, bool, str]:
        before_probe = await self._bbox_probe(registered, snapshot)
        try:
            screenshot = await registered.client.screenshot(annotate=False)
        except Exception as exc:
            return self._capture_failure_screenshot(exc), False, self._capture_failure_reason(exc)
        viewport_payload, after_probe = await asyncio.gather(
            self._viewport_payload(registered),
            self._bbox_probe(registered, snapshot),
        )
        captured = await self._materialize_and_annotate(
            registered,
            snapshot,
            screenshot,
            viewport_payload=viewport_payload,
            bbox_probe=after_probe if after_probe.ok else None,
        )
        if before_probe.error or after_probe.error:
            details = "; ".join(part for part in (before_probe.error, after_probe.error) if part)
            return captured, False, f"bbox_probe_failed_during_capture: {details}"
        if before_probe.bbox_hash == after_probe.bbox_hash:
            return captured, True, "bbox_aligned"
        return captured, False, "bbox_changed_during_capture"

    async def _capture_som(
        self,
        registered: RegisteredWebAgentSession,
        snapshot: WebSnapshotObservation,
    ) -> WebScreenshotObservation | None:
        try:
            screenshot = await registered.client.screenshot(annotate=False)
        except Exception as exc:
            return self._capture_failure_screenshot(exc)
        viewport_payload, bbox_probe = await asyncio.gather(
            self._viewport_payload(registered),
            self._bbox_probe(registered, snapshot),
        )
        return await self._materialize_and_annotate(
            registered,
            snapshot,
            screenshot,
            viewport_payload=viewport_payload,
            bbox_probe=bbox_probe if bbox_probe.ok else None,
        )

    async def _materialize_and_annotate(
        self,
        registered: RegisteredWebAgentSession,
        snapshot: WebSnapshotObservation,
        screenshot: WebScreenshotObservation,
        *,
        viewport_payload: JsonObject | None = None,
        bbox_probe: WebBboxProbe | None = None,
    ) -> WebScreenshotObservation | None:
        try:
            persisted = self.persist_screenshot(registered, screenshot)
        except Exception as exc:
            raw = dict(screenshot.raw)
            raw["persist_error"] = f"{type(exc).__name__}: {exc}"
            screenshot.raw = raw
            screenshot.data_url = ""
            return screenshot
        try:
            return await self.annotate_screenshot(
                registered,
                persisted,
                snapshot,
                viewport_payload=viewport_payload,
                box_payloads=bbox_probe.boxes if bbox_probe is not None else None,
                state_payloads=bbox_probe.states if bbox_probe is not None else None,
            )
        except Exception as exc:
            raw = dict(persisted.raw)
            raw["annotation_error"] = f"{type(exc).__name__}: {exc}"
            persisted.raw = raw
            return persisted

    async def _bbox_probe(
        self,
        registered: RegisteredWebAgentSession,
        snapshot: WebSnapshotObservation,
    ) -> WebBboxProbe:
        try:
            return await self.settle_service.bbox_probe(registered, snapshot.elements)
        except Exception as exc:
            return WebBboxProbe(
                refs=[],
                boxes={},
                states={},
                bbox_hash="",
                error=f"{type(exc).__name__}: {exc}",
            )

    async def _viewport_payload(self, registered: RegisteredWebAgentSession) -> JsonObject:
        try:
            result: WebActionCommandResult = await registered.client.get_viewport_size()
            payload = result.require_success().data
        except Exception:
            return {}
        if isinstance(payload, dict):
            nested = payload.get("result")
            return dict(nested) if isinstance(nested, dict) else dict(payload)
        return {}

    def _capture_failure_screenshot(self, exc: Exception) -> WebScreenshotObservation:
        return WebScreenshotObservation(raw={"capture_error": self._capture_failure_reason(exc)})

    def _capture_failure_reason(self, exc: Exception) -> str:
        return f"screenshot_capture_failed: {type(exc).__name__}: {exc}"

    def _capture_error_reason(self, screenshot: WebScreenshotObservation | None) -> str:
        if screenshot is None:
            return ""
        return str(screenshot.raw.get("capture_error") or "")

    def _is_empty_som_quality_failure(
        self,
        screenshot: WebScreenshotObservation | None,
        snapshot: WebSnapshotObservation,
    ) -> bool:
        if screenshot is None:
            return False
        if not (screenshot.path or screenshot.data_url):
            return False
        if screenshot.raw.get("capture_error"):
            return False
        annotation = screenshot.annotation
        if annotation is None:
            return bool(snapshot.elements and screenshot.raw.get("annotation_error"))
        if annotation.marks:
            return False
        url = (snapshot.url or snapshot.origin or "").strip()
        if not url or url == "about:blank":
            return False
        if not snapshot.elements:
            return "[ref=" in (snapshot.snapshot or "")
        if annotation.visible_interactive_count > 0:
            return True
        omitted_reason_counts = annotation.omitted_reason_counts or {}
        missing_bbox_count = int(omitted_reason_counts.get("missing_bbox") or 0)
        probed_ref_count = min(len(snapshot.elements), int(annotation.max_box_lookup))
        if probed_ref_count > 0 and missing_bbox_count >= probed_ref_count:
            return True
        return not omitted_reason_counts

    def _is_screenshot_capture_failure(self, reason: str) -> bool:
        return reason.startswith("screenshot_capture_failed:")

    def _append_reason(self, existing: str, extra: str) -> str:
        if not existing:
            return extra
        if not extra:
            return existing
        if extra in existing:
            return existing
        return f"{existing}; {extra}"
