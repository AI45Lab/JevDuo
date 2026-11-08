"""Normalize browser observation payloads into typed NorthCUA contracts."""

from __future__ import annotations

import re
from collections.abc import Mapping

from northcua.core.json_types import JsonObject

from ..domain.observations import (
    WebElementRef,
    WebScreenshotAnnotation,
    WebScreenshotObservation,
    WebSnapshotObservation,
)

_BARE_REF_PATTERN = re.compile(r"(?P<prefix>\bref=)(?P<ref>(?!@)e\d+\b)")


class WebSnapshotNormalizer:
    """Normalize the loose browser snapshot JSON shape."""

    def normalize(self, data: object) -> WebSnapshotObservation:
        """Return a typed snapshot observation from raw command data.

        Args:
            data: Raw browser snapshot payload.

        Returns:
            Normalized snapshot observation with refs in typed form.
        """

        if not isinstance(data, Mapping):
            return WebSnapshotObservation(snapshot=_canonicalize_snapshot_refs(str(data or "")), raw={})
        raw = dict(data)
        refs_payload = raw.get("refs") or raw.get("elements") or {}
        elements = self._normalize_refs(refs_payload)
        origin = str(raw.get("origin") or "")
        snapshot = _canonicalize_snapshot_refs(str(raw.get("snapshot") or raw.get("text") or raw.get("dom") or ""))
        for key in ("snapshot", "text", "dom"):
            if key in raw and isinstance(raw[key], str):
                raw[key] = _canonicalize_snapshot_refs(raw[key])
        return WebSnapshotObservation(
            url=str(raw.get("url") or raw.get("page_url") or origin),
            title=str(raw.get("title") or raw.get("page_title") or ""),
            origin=origin,
            snapshot=snapshot,
            elements=elements,
            raw=raw,
        )

    def _normalize_refs(self, refs_payload: object) -> list[WebElementRef]:
        if isinstance(refs_payload, Mapping):
            return [
                self._normalize_ref(ref_id=str(ref_id), payload=payload) for ref_id, payload in refs_payload.items()
            ]
        if isinstance(refs_payload, list):
            elements: list[WebElementRef] = []
            for index, payload in enumerate(refs_payload, start=1):
                if isinstance(payload, Mapping):
                    raw_ref = payload.get("ref") or payload.get("ref_id") or payload.get("id") or f"e{index}"
                    elements.append(self._normalize_ref(ref_id=str(raw_ref), payload=payload))
                else:
                    elements.append(WebElementRef(ref_id=f"e{index}", text=str(payload or "")))
            return elements
        return []

    def _normalize_ref(self, *, ref_id: str, payload: object) -> WebElementRef:
        if not isinstance(payload, Mapping):
            return WebElementRef(ref_id=ref_id, text=str(payload or ""))
        attributes = payload.get("attributes")
        return WebElementRef(
            ref_id=ref_id[1:] if ref_id.startswith("@") else ref_id,
            role=str(payload.get("role") or ""),
            name=str(payload.get("name") or payload.get("label") or ""),
            text=str(payload.get("text") or ""),
            tag=str(payload.get("tag") or payload.get("tag_name") or ""),
            attributes=dict(attributes) if isinstance(attributes, Mapping) else {},
        )


def _canonicalize_snapshot_refs(snapshot: str) -> str:
    """Return snapshot text with bare ``ref=eN`` rewritten as ``ref=@eN``."""

    return _BARE_REF_PATTERN.sub(lambda match: f"{match.group('prefix')}@{match.group('ref')}", snapshot)


class WebScreenshotNormalizer:
    """Normalize browser screenshot JSON output."""

    def normalize(self, data: object) -> WebScreenshotObservation:
        """Return typed screenshot metadata from raw command data.

        Args:
            data: Raw browser screenshot payload.

        Returns:
            Normalized screenshot metadata with optional dimensions.
        """

        if not isinstance(data, Mapping):
            return WebScreenshotObservation(path=str(data or ""), raw={})
        raw: JsonObject = dict(data)
        width = self._optional_int(raw.get("width"))
        height = self._optional_int(raw.get("height"))
        annotation_payload = raw.get("annotation")
        annotation = (
            WebScreenshotAnnotation.model_validate(annotation_payload)
            if isinstance(annotation_payload, Mapping)
            else None
        )
        return WebScreenshotObservation(
            path=str(raw.get("path") or raw.get("file") or ""),
            data_url=str(raw.get("data_url") or raw.get("screenshot_url") or ""),
            width=width,
            height=height,
            annotation=annotation,
            raw=raw,
        )

    def _optional_int(self, value: object) -> int | None:
        if isinstance(value, bool):
            return None
        if isinstance(value, int):
            return value
        if isinstance(value, float):
            return int(value)
        if isinstance(value, str) and value.isdigit():
            return int(value)
        return None
