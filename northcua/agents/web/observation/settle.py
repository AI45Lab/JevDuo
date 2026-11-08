"""Post-action settling helpers for web-agent observation capture."""

from __future__ import annotations

import asyncio
import hashlib
import json
import time
from dataclasses import dataclass
from typing import Any

from northcua.core.json_types import JsonObject

from ..domain.actions import WebAgentActionType, WebWaitParams
from ..domain.observations import WebElementRef, WebSnapshotObservation
from ..session.models import RegisteredWebAgentSession


@dataclass(slots=True, frozen=True)
class WebSettleResult:
    """Result of a post-action browser settle attempt."""

    settled: bool
    reason: str
    sample_count: int = 0


@dataclass(slots=True, frozen=True)
class WebStabilitySample:
    """One snapshot/bbox sample used to decide whether the page is stable."""

    snapshot: WebSnapshotObservation
    snapshot_hash: str
    bbox_hash: str

    @property
    def combined_hash(self) -> str:
        return f"{self.snapshot_hash}:{self.bbox_hash}"


@dataclass(slots=True, frozen=True)
class WebBboxProbe:
    """Batched bbox/state probe used for stability and SoM alignment."""

    refs: list[str]
    boxes: dict[str, object]
    states: dict[str, object]
    bbox_hash: str
    error: str = ""

    @property
    def ok(self) -> bool:
        """Return whether the probe can be trusted for alignment decisions."""

        return not self.error

    def require_hash(self) -> str:
        """Return the probe hash or raise a stable bbox probe error."""

        if self.error:
            raise WebBboxProbeError(self.error)
        return self.bbox_hash


class WebBboxProbeError(RuntimeError):
    """Raised when bbox/state probes cannot provide alignment signal."""


class WebPostActionSettleService:
    """Wait for a web page to become stable enough for aligned SoM capture."""

    async def settle_after_action(
        self,
        *,
        registered: RegisteredWebAgentSession,
        action: WebAgentActionType,
    ) -> WebSettleResult:
        """Run debounce and stability sampling after a mutating web action."""

        config = registered.settings.post_action_settle
        if not config.enabled:
            return WebSettleResult(settled=True, reason="settle_disabled", sample_count=0)
        await self._wait_duration(registered, config.debounce_ms)
        if action in {WebAgentActionType.OPEN, WebAgentActionType.NEW_TAB, WebAgentActionType.FOCUS_TAB}:
            await self._best_effort_load_wait(registered)
        required_stable = max(config.snapshot_stable_samples, config.bbox_stable_samples)
        deadline = time.monotonic() + (config.max_wait_ms / 1000)
        previous_hash = ""
        consecutive = 0
        sample_count = 0
        last_error = ""
        while True:
            try:
                sample = await self.capture_sample(registered)
                sample_count += 1
                sample_hash = sample.combined_hash
                consecutive = consecutive + 1 if sample_hash == previous_hash else 1
                previous_hash = sample_hash
                if consecutive >= required_stable:
                    return WebSettleResult(
                        settled=True,
                        reason=f"stable_{consecutive}_samples",
                        sample_count=sample_count,
                    )
            except Exception as exc:  # best-effort observation stability; action success remains authoritative
                last_error = f"{type(exc).__name__}: {exc}"
                sample_count += 1
            if time.monotonic() >= deadline:
                reason = "timeout_best_effort"
                if last_error:
                    reason = f"{reason}: {last_error}"
                return WebSettleResult(settled=False, reason=reason, sample_count=sample_count)
            await self._wait_duration(registered, min(config.sample_interval_ms, config.max_wait_ms))

    async def capture_sample(self, registered: RegisteredWebAgentSession) -> WebStabilitySample:
        """Capture one snapshot/bbox stability sample."""

        snapshot = await registered.client.snapshot()
        return WebStabilitySample(
            snapshot=snapshot,
            snapshot_hash=self.snapshot_hash(snapshot),
            bbox_hash=await self.bbox_hash(registered, snapshot.elements),
        )

    def snapshot_hash(self, snapshot: WebSnapshotObservation) -> str:
        """Return a stable hash for URL/title/snapshot/ref state."""

        top_refs = [
            {
                "ref": element.model_ref,
                "role": element.role,
                "name": element.name,
                "tag": element.tag,
            }
            for element in snapshot.elements[:32]
        ]
        return self._hash_json(
            {
                "url": snapshot.url or snapshot.origin,
                "title": snapshot.title,
                "snapshot": snapshot.snapshot,
                "ref_count": len(snapshot.elements),
                "top_refs": top_refs,
            }
        )

    async def bbox_hash(
        self,
        registered: RegisteredWebAgentSession,
        elements: list[WebElementRef],
    ) -> str:
        """Return a stable hash for current visible candidate bounding boxes."""

        return (await self.bbox_probe(registered, elements)).require_hash()

    async def bbox_probe(
        self,
        registered: RegisteredWebAgentSession,
        elements: list[WebElementRef],
    ) -> WebBboxProbe:
        """Return bbox/state payloads and a stable hash in one batched read.

        Boxes and ref states are independent browser probes, so they are read
        concurrently. This keeps the stability signal identical while avoiding
        a full extra CLI round trip for every sample.
        """

        refs = self._candidate_refs(elements, limit=registered.settings.post_action_settle.bbox_candidate_limit)
        if not refs:
            return WebBboxProbe(refs=[], boxes={}, states={}, bbox_hash="no-refs")
        boxes, states = await asyncio.gather(
            self._box_payloads(registered, refs),
            self._state_payloads(registered, refs),
        )
        box_probe_error = boxes.get("__error__")
        if box_probe_error is not None:
            return WebBboxProbe(
                refs=refs,
                boxes=boxes,
                states=states,
                bbox_hash="",
                error=f"box probe failed: {box_probe_error}",
            )
        state_probe_error = states.get("__error__")
        if state_probe_error is not None:
            return WebBboxProbe(
                refs=refs,
                boxes=boxes,
                states=states,
                bbox_hash="",
                error=f"state probe failed: {state_probe_error}",
            )
        normalized: JsonObject = {}
        missing_box_count = 0
        for ref in refs:
            box = boxes.get(ref)
            if box is None:
                missing_box_count += 1
            raw_state = states.get(ref, {})
            state = raw_state if isinstance(raw_state, dict) else {}
            normalized[ref] = {
                "box": self._normalize_box(box),
                "visible": state.get("visible"),
                "enabled": state.get("enabled"),
            }
        if missing_box_count == len(refs):
            return WebBboxProbe(
                refs=refs,
                boxes=boxes,
                states=states,
                bbox_hash="",
                error=f"box probe returned no boxes for {len(refs)} candidate refs",
            )
        return WebBboxProbe(
            refs=refs,
            boxes=boxes,
            states=states,
            bbox_hash=self._hash_json(normalized),
        )

    def _candidate_refs(self, elements: list[WebElementRef], *, limit: int) -> list[str]:
        refs: list[str] = []
        seen: set[str] = set()
        for element in elements:
            ref = element.model_ref
            if ref in seen:
                continue
            refs.append(ref)
            seen.add(ref)
            if len(refs) >= limit:
                break
        return refs

    async def _box_payloads(self, registered: RegisteredWebAgentSession, refs: list[str]) -> dict[str, object]:
        try:
            result = await registered.client.get_boxes(refs)
            payload = result.require_success().data
        except Exception as exc:
            return {"__error__": f"{type(exc).__name__}: {exc}"}
        if not isinstance(payload, dict):
            return {}
        errors = self._per_ref_errors(payload.get("errors"), refs)
        if errors:
            return {"__error__": f"per-ref box errors: {errors}"}
        boxes = payload.get("boxes")
        if isinstance(boxes, dict):
            return {str(key): value for key, value in boxes.items()}
        return {str(key): value for key, value in payload.items()}

    async def _state_payloads(self, registered: RegisteredWebAgentSession, refs: list[str]) -> dict[str, object]:
        try:
            result = await registered.client.get_ref_states(refs)
            payload = result.require_success().data
        except Exception as exc:
            return {"__error__": f"{type(exc).__name__}: {exc}"}
        if not isinstance(payload, dict):
            return {}
        errors = self._per_ref_errors(payload.get("errors"), refs)
        if errors:
            return {"__error__": f"per-ref state errors: {errors}"}
        states = payload.get("states")
        if not isinstance(states, dict):
            return {}
        normalized: dict[str, object] = {}
        for ref, state in states.items():
            if not isinstance(state, dict):
                continue
            normalized[str(ref)] = {
                key: bool(value)
                for key, value in state.items()
                if key in {"visible", "enabled"} and isinstance(value, bool)
            }
        return normalized

    def _per_ref_errors(self, value: object, candidate_refs: list[str]) -> dict[str, str]:
        if not isinstance(value, dict):
            return {}
        candidate_ref_set = set(candidate_refs)
        errors: dict[str, str] = {}
        for raw_ref, raw_error in value.items():
            ref = str(raw_ref)
            error = str(raw_error or "").strip()
            if ref in candidate_ref_set and error:
                errors[ref] = error
        return errors

    def _normalize_box(self, value: object) -> list[float] | None | str:
        if isinstance(value, str):
            return value
        if not isinstance(value, dict):
            return None
        parsed: list[float] = []
        for key in ("x", "y", "width", "height"):
            item = value.get(key)
            if isinstance(item, bool) or not isinstance(item, (int, float)):
                return None
            parsed.append(round(float(item), 2))
        return parsed

    async def _best_effort_load_wait(self, registered: RegisteredWebAgentSession) -> None:
        timeout_ms = registered.settings.post_action_settle.load_wait_timeout_ms
        if timeout_ms <= 0:
            return
        try:
            result = await registered.client.wait(
                WebWaitParams(load_state="domcontentloaded"),
                timeout_seconds=timeout_ms / 1000,
            )
            result.require_success()
        except Exception:
            return

    async def _wait_duration(self, registered: RegisteredWebAgentSession, duration_ms: int) -> None:
        if duration_ms <= 0:
            return
        try:
            result = await registered.client.wait(WebWaitParams(duration_ms=duration_ms))
            result.require_success()
        except Exception:
            await asyncio.sleep(duration_ms / 1000)

    def _hash_json(self, value: Any) -> str:
        payload = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()
