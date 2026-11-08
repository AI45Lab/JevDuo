"""Projection helpers for model-visible web observations."""

from __future__ import annotations

from collections.abc import Mapping
from urllib.parse import urlparse

from northcua.core.json_types import JsonObject

from ..domain.observations import (
    WebBoundingBox,
    WebElementRef,
    WebObservationBundle,
    WebObservationContext,
    WebObservationPagePayload,
    WebObservationScreenshotPayload,
    WebObservationSnapshotPayload,
    WebScreenshotAnnotation,
    WebScreenshotMark,
    WebScreenshotObservation,
    WebSnapshotObservation,
)
from .ref_alias import WebRefAliasService, canonical_source_ref


class WebObservationProjector:
    """Project internal web observations into prompt-safe JSON payloads."""

    def __init__(self) -> None:
        self.ref_alias_service = WebRefAliasService()

    def build_bundle(self, observation: WebObservationContext) -> WebObservationBundle:
        """Build the model-visible observation bundle.

        Args:
            observation: Internal observation context with optional screenshot
                and Set-of-Mark annotation metadata.

        Returns:
            Path-free, JSON-safe observation bundle for tool results and
            prompt observation text.
        """

        snapshot = observation.snapshot
        projected_refs = self.snapshot_refs(
            snapshot, annotation=observation.screenshot.annotation if observation.screenshot else None
        )
        projected_text = self.snapshot_text(
            snapshot,
            annotation=observation.screenshot.annotation if observation.screenshot else None,
        )
        return WebObservationBundle(
            observation_id=observation.observation_id or "web-observation",
            settled=observation.settled,
            settle_reason=observation.settle_reason,
            page=WebObservationPagePayload(url=snapshot.url or snapshot.origin, title=snapshot.title),
            snapshot=WebObservationSnapshotPayload(
                text=projected_text,
                refs=projected_refs,
                ref_count=len(projected_refs),
            ),
            screenshot=self.screenshot_payload(observation.screenshot),
        )

    def bundle_dict(self, observation: WebObservationContext) -> JsonObject:
        """Return the model-visible observation bundle as a JSON object."""

        return self.build_bundle(observation).model_dump(mode="json")

    def snapshot_refs(
        self,
        snapshot: WebSnapshotObservation,
        *,
        annotation: WebScreenshotAnnotation | None = None,
    ) -> list[JsonObject]:
        """Return prompt-safe ref metadata for one snapshot."""

        if annotation is not None:
            elements_by_ref = {element.model_ref: element for element in snapshot.elements}
            refs: list[JsonObject] = []
            for mark in annotation.marks:
                ref = str(mark.ref or "").strip()
                element = self._element_for_mark(mark, elements_by_ref)
                label = self._best_label(element, mark)
                payload: JsonObject = {
                    "ref": ref,
                    "role": element.role if element is not None else mark.role,
                    "label": label,
                }
                state = self._state_text(element)
                if state:
                    payload["state"] = state
                refs.append(payload)
            return refs

        refs: list[JsonObject] = []
        for projected in self.ref_alias_service.projected_refs_for_observation(snapshot):
            element = projected.element
            alias = projected.alias
            payload = {
                "ref": alias.ref,
                "role": element.role,
                "label": self._best_label(element, None),
            }
            state = self._state_text(element)
            if state:
                payload["state"] = state
            refs.append(payload)
        return refs

    def snapshot_text(
        self,
        snapshot: WebSnapshotObservation,
        *,
        annotation: WebScreenshotAnnotation | None = None,
    ) -> str:
        """Return model-visible snapshot text aligned with SoM marks when present."""

        if annotation is None:
            lines = [
                (
                    f"{projected.alias.ref} {self._best_role(projected.element, None)} "
                    f'"{self._best_label(projected.element, None)}"{self._state_suffix(projected.element)}'
                )
                for projected in self.ref_alias_service.projected_refs_for_observation(snapshot)
            ]
            return "\n".join(lines)
        mark_refs = [str(mark.ref or "").strip() for mark in annotation.marks if mark.ref]
        if not mark_refs:
            return ""
        lines: list[str] = []
        elements_by_ref = {element.model_ref: element for element in snapshot.elements}
        marks_by_ref = {str(mark.ref or "").strip(): mark for mark in annotation.marks}
        for ref in mark_refs:
            mark = marks_by_ref.get(ref)
            element = self._element_for_mark(mark, elements_by_ref) if mark is not None else None
            label = self._best_label(element, mark)
            role = self._best_role(element, mark)
            lines.append(f'{ref} {role} "{label}"{self._state_suffix(element)}')
        return "\n".join(lines)

    def _element_for_mark(
        self,
        mark: WebScreenshotMark,
        elements_by_ref: Mapping[str, WebElementRef],
    ) -> WebElementRef | None:
        """Resolve a SoM mark against source refs or replayed model aliases."""

        if mark.source_ref:
            return elements_by_ref.get(canonical_source_ref(mark.source_ref))
        ref = str(mark.ref or "").strip()
        if not ref:
            return None
        return elements_by_ref.get(canonical_source_ref(ref)) or elements_by_ref.get(
            ref if ref.startswith("@") else f"@{ref}"
        )

    def _best_label(self, element: WebElementRef | None, mark: WebScreenshotMark | None) -> str:
        label = (
            element.name
            if element is not None and element.name
            else (
                element.text
                if element is not None and element.text
                else mark.name
                if mark is not None and mark.name
                else ""
            )
        )
        return " ".join(str(label).split())

    def _state_suffix(self, element: WebElementRef | None) -> str:
        state_text = self._state_text(element)
        return f" {state_text}" if state_text else ""

    def _state_text(self, element: WebElementRef | None) -> str:
        if element is None:
            return ""
        attrs = {str(key).lower(): value for key, value in element.attributes.items()}
        parts: list[str] = []
        value = str(attrs.get("value") or "").strip()
        label = self._best_label(element, None)
        if value and value != label:
            parts.append(f'value="{value}"')
        if "checked" in attrs:
            parts.append(f"checked={self._bool_text(attrs.get('checked'))}")
        return " ".join(parts)

    def _bool_text(self, value: object) -> str:
        if isinstance(value, bool):
            return str(value).lower()
        return "true" if str(value).strip().lower() in {"1", "true", "yes", "checked"} else "false"

    def _best_role(self, element: WebElementRef | None, mark: WebScreenshotMark | None) -> str:
        role = (
            element.role
            if element is not None and element.role
            else (
                element.tag
                if element is not None and element.tag
                else mark.role
                if mark is not None and mark.role
                else "element"
            )
        )
        return str(role or "element").strip() or "element"

    def screenshot_payload(self, screenshot: WebScreenshotObservation | None) -> WebObservationScreenshotPayload:
        """Return path-free screenshot metadata for a model-visible bundle."""

        if screenshot is None:
            return WebObservationScreenshotPayload(
                kind="none",
                image_attached=False,
                image_omitted_reason="no screenshot captured for this observation",
            )
        return WebObservationScreenshotPayload(
            kind=screenshot.annotation.kind if screenshot.annotation is not None else "viewport",
            width=screenshot.width,
            height=screenshot.height,
            image_attached=bool(screenshot.data_url),
            image_omitted_reason="" if screenshot.data_url else self._image_omitted_reason(screenshot),
            annotation=self.annotation_payload(screenshot.annotation),
        )

    def screenshot_result(self, screenshot: WebScreenshotObservation) -> JsonObject:
        """Return path-free payload for explicit screenshot tool results."""

        payload: JsonObject = {
            "width": screenshot.width,
            "height": screenshot.height,
            "annotation": self.annotation_payload(screenshot.annotation),
        }
        payload["kind"] = screenshot.annotation.kind if screenshot.annotation is not None else "viewport"
        payload["image_attached"] = bool(screenshot.data_url)
        if not screenshot.data_url:
            payload["image_omitted_reason"] = self._image_omitted_reason(screenshot)
        raw = self._strip_path_fields(screenshot.raw)
        if raw:
            payload["raw"] = raw
        return payload

    def context_from_payload(
        self,
        observation_payload: object,
        *,
        screenshot_url: str = "",
    ) -> WebObservationContext | None:
        """Rehydrate a prompt observation from a model-visible bundle payload."""

        if not isinstance(observation_payload, Mapping):
            return None
        try:
            bundle = WebObservationBundle.model_validate(observation_payload)
        except Exception:
            return None
        snapshot = WebSnapshotObservation(
            url=bundle.page.url,
            title=bundle.page.title,
            origin=self._origin_from_url(bundle.page.url),
            snapshot=bundle.snapshot.text,
            elements=[
                WebElementRef(
                    ref_id=str(ref.get("ref") or "").strip().removeprefix("@"),
                    role=str(ref.get("role") or ""),
                    name=str(ref.get("label") or ref.get("name") or ""),
                    text=str(ref.get("text") or ""),
                    tag=str(ref.get("tag") or ""),
                    attributes=self._attributes_from_ref_state(ref),
                )
                for ref in bundle.snapshot.refs
                if str(ref.get("ref") or "").strip()
            ],
            raw={"source": "post_action_observation_bundle", "observation_id": bundle.observation_id},
        )
        screenshot = self._screenshot_from_bundle(bundle, screenshot_url=screenshot_url)
        return WebObservationContext(
            snapshot=snapshot,
            screenshot=screenshot,
            observation_id=bundle.observation_id,
            settled=bundle.settled,
            settle_reason=bundle.settle_reason,
        )

    def _attributes_from_ref_state(self, ref: Mapping[str, object]) -> JsonObject:
        state = str(ref.get("state") or "").strip()
        if not state:
            return {}
        attrs: JsonObject = {}
        if 'value="' in state:
            value_part = state.split('value="', 1)[1]
            attrs["value"] = value_part.split('"', 1)[0]
        if "checked=" in state:
            checked_part = state.split("checked=", 1)[1].split(None, 1)[0].strip().lower()
            attrs["checked"] = checked_part in {"1", "true", "yes", "checked"}
        return attrs

    def annotation_payload(self, annotation: WebScreenshotAnnotation | None) -> JsonObject:
        """Return annotation metadata with filesystem/device paths removed."""

        if annotation is None:
            return {}
        payload = annotation.model_dump(mode="json")
        payload.pop("source_screenshot_path", None)
        payload.pop("overlay_screenshot_path", None)
        self._normalize_annotation_payload(payload)
        return self._strip_path_fields(payload)

    def _screenshot_from_bundle(
        self,
        bundle: WebObservationBundle,
        *,
        screenshot_url: str,
    ) -> WebScreenshotObservation | None:
        screenshot_payload = bundle.screenshot
        if screenshot_payload.kind == "none":
            return None
        annotation = self._annotation_from_payload(screenshot_payload.annotation)
        return WebScreenshotObservation(
            data_url=screenshot_url,
            width=screenshot_payload.width,
            height=screenshot_payload.height,
            annotation=annotation,
            raw={
                "source": "post_action_observation_bundle",
                "observation_id": bundle.observation_id,
                "image_omitted_reason": screenshot_payload.image_omitted_reason,
            },
        )

    def _annotation_from_payload(self, payload: Mapping[str, object]) -> WebScreenshotAnnotation | None:
        if not payload:
            return None
        payload = self._strip_path_fields(payload)
        try:
            return self._normalize_annotation(WebScreenshotAnnotation.model_validate(payload))
        except Exception:
            marks: list[WebScreenshotMark] = []
            raw_marks = payload.get("marks")
            if isinstance(raw_marks, list):
                for raw_mark in raw_marks:
                    mark = self._mark_from_payload(raw_mark)
                    if mark is not None:
                        marks.append(mark)
            return WebScreenshotAnnotation(
                kind=str(payload.get("kind") or "set_of_mark"),
                coordinate_frame=str(payload.get("coordinate_frame") or "viewport_css"),
                image_coordinate_frame=str(payload.get("image_coordinate_frame") or "screenshot_pixels"),
                marks=[self._normalize_mark(mark) for mark in marks],
                truncated=bool(payload.get("truncated", False)),
                omitted_count=self._int_value(payload.get("omitted_count")),
                max_marks=self._int_value(payload.get("max_marks"), default=150),
                max_box_lookup=self._int_value(payload.get("max_box_lookup"), default=512),
                marked_count=self._int_value(payload.get("marked_count"), default=len(marks)),
                visible_interactive_count=self._int_value(payload.get("visible_interactive_count"), default=len(marks)),
                omitted_reason_counts=(
                    {
                        str(key): self._int_value(value)
                        for key, value in (payload.get("omitted_reason_counts") or {}).items()
                    }
                    if isinstance(payload.get("omitted_reason_counts"), Mapping)
                    else {}
                ),
            )

    def _mark_from_payload(self, payload: object) -> WebScreenshotMark | None:
        if not isinstance(payload, Mapping):
            return None
        try:
            return WebScreenshotMark.model_validate(payload)
        except Exception:
            css = self._box_from_payload(payload.get("bbox_css"))
            image = self._box_from_payload(payload.get("bbox_image"))
            ref = str(payload.get("ref") or "").strip()
            if not ref or css is None or image is None:
                return None
            return WebScreenshotMark(
                ref=ref,
                label=ref,
                source_ref=str(payload.get("source_ref") or ""),
                role=str(payload.get("role") or ""),
                name=str(payload.get("name") or ""),
                bbox_css=css,
                bbox_image=image,
                visible=bool(payload.get("visible", True)),
                enabled=bool(payload.get("enabled", True)),
            )

    def _canonical_ref(self, ref: str) -> str:
        value = str(ref or "").strip()
        return value

    def _normalize_mark(self, mark: WebScreenshotMark) -> WebScreenshotMark:
        ref = str(mark.ref or "").strip()
        if mark.ref == ref and mark.label == ref:
            return mark
        return mark.model_copy(update={"ref": ref, "label": ref})

    def _normalize_annotation(self, annotation: WebScreenshotAnnotation) -> WebScreenshotAnnotation:
        marks = [self._normalize_mark(mark) for mark in annotation.marks]
        return annotation.model_copy(update={"marks": marks})

    def _normalize_annotation_payload(self, payload: JsonObject) -> None:
        marks = payload.get("marks")
        if not isinstance(marks, list):
            return
        for mark in marks:
            if not isinstance(mark, dict):
                continue
            ref = self._canonical_ref(str(mark.get("ref") or ""))
            if ref:
                mark["ref"] = ref
                mark["label"] = ref
            mark.pop("source_ref", None)

    def _box_from_payload(self, payload: object) -> WebBoundingBox | None:
        if not isinstance(payload, Mapping):
            return None
        try:
            return WebBoundingBox.model_validate(payload)
        except Exception:
            return None

    def _int_value(self, value: object, *, default: int = 0) -> int:
        if isinstance(value, bool):
            return default
        if isinstance(value, int):
            return value
        if isinstance(value, float):
            return int(value)
        if isinstance(value, str):
            try:
                return int(value)
            except ValueError:
                return default
        return default

    def _origin_from_url(self, url: str) -> str:
        parsed = urlparse(url)
        if parsed.scheme and parsed.netloc:
            return f"{parsed.scheme}://{parsed.netloc}"
        return ""

    def _image_omitted_reason(self, screenshot: WebScreenshotObservation) -> str:
        capture_error = str(screenshot.raw.get("capture_error") or "").strip()
        if capture_error:
            return capture_error
        if screenshot.path:
            return "image was persisted internally but not attached to the model-visible JSON"
        return "no prompt-readable screenshot image was available"

    def _strip_path_fields(self, value: object) -> JsonObject:
        stripped = self._strip_value(value)
        return stripped if isinstance(stripped, dict) else {}

    def _strip_value(self, value: object) -> object:
        if isinstance(value, Mapping):
            output: JsonObject = {}
            for key, item in value.items():
                key_text = str(key)
                if key_text.endswith("_path") or key_text in {
                    "path",
                    "source_ref",
                    "source_screenshot_path",
                    "overlay_screenshot_path",
                }:
                    continue
                if key_text in {"data_url", "image_url", "screenshot_url"} and isinstance(item, str):
                    continue
                output[key_text] = self._strip_value(item)
            return output
        if isinstance(value, list):
            return [self._strip_value(item) for item in value]
        return value

    def _bbox_text(self, bbox: WebBoundingBox | None) -> str:
        if bbox is None:
            return ""
        return f"{bbox.x:g},{bbox.y:g},{bbox.width:g},{bbox.height:g}"
