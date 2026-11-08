"""Shared model-facing artifact projector for AgentKit evaluators."""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping

from northcua.agentkernel.json_types import JsonObject

from ..protocol import EvaluationEvidenceManifest, EvaluationEvidenceRef, RoundEvaluationBundle
from .models import (
    EvaluatorDetailLevel,
    EvaluatorEvidenceView,
    EvaluatorGeometryPolicy,
    EvaluatorToolResultView,
    EvaluatorTraceView,
)

_PATH_KEY_RE = re.compile(r"(^|_)(path|paths|uri|url_path)($|_)")
_GEOMETRY_KEY_RE = re.compile(r"^(bbox|bbox_|bounds$|rect$|geometry$|x$|y$|width$|height$)")
_IMAGE_DATA_PREFIX = "data:image/"
_REDACTED_IMAGE_MARKER_PREFIX = "[redacted image data url;"
_MAX_TEXT_CHARS = 1_200
_MAX_LIST_ITEMS = 25
_MAX_TOOL_RESULT_ITEMS = 40
_MAX_TOOL_CALL_ITEMS = 80
_STANDARD_MAX_STEP_REFS = 120
_TIGHT_MAX_STEP_REFS = 40
_KEY_TERMINAL_STATUSES = {"finish", "fail", "error", "succeeded", "failed", "timed_out", "canceled"}


def _coerce_detail(value: object) -> EvaluatorDetailLevel:
    if isinstance(value, EvaluatorDetailLevel):
        return value
    try:
        return EvaluatorDetailLevel(str(value or EvaluatorDetailLevel.STANDARD.value))
    except ValueError:
        return EvaluatorDetailLevel.STANDARD


class EvaluatorEvidenceProjector:
    """Project canonical evaluator artifacts into low-noise model-facing views."""

    def __init__(
        self,
        *,
        bundle: RoundEvaluationBundle,
        evidence_manifest: EvaluationEvidenceManifest,
    ) -> None:
        self.bundle = bundle
        self.evidence_manifest = evidence_manifest

    def compact_bundle(
        self,
        *,
        detail_level: object = EvaluatorDetailLevel.STANDARD,
        projection_level: str = "standard",
        final_full_steps: int = 20,
    ) -> JsonObject:
        """Return the default path-free bundle view for evaluator model tools."""

        if _coerce_detail(detail_level) == EvaluatorDetailLevel.DEBUG:
            return self.bundle.model_dump(mode="json")
        selected_steps = self._selected_step_indexes(
            projection_level=projection_level,
            final_full_steps=final_full_steps,
        )
        tool_call_limit = _MAX_TOOL_CALL_ITEMS if projection_level != "tight" else min(_MAX_TOOL_CALL_ITEMS, 30)
        tool_result_limit = _MAX_TOOL_RESULT_ITEMS if projection_level != "tight" else min(_MAX_TOOL_RESULT_ITEMS, 20)
        view = EvaluatorEvidenceView(
            session_id=self.bundle.session_id,
            round_num=self.bundle.round_num,
            round_request=self.bundle.round_request,
            session_request=self.bundle.session_request,
            step_refs=[self.compact_step_ref(self.bundle.step_refs[index]) for index in selected_steps],
            step_summaries=[
                self.compact_text(self.bundle.step_summaries[index])
                for index in selected_steps
                if index < len(self.bundle.step_summaries)
            ],
            tool_calls=[
                self.compact_tool_call(item) for item in self._tail_items(self.bundle.tool_calls, tool_call_limit)
            ],
            tool_results=[
                self.compact_tool_result(item) for item in self._tail_items(self.bundle.tool_results, tool_result_limit)
            ],
            selected_evidence_manifest=self.compact_manifest_refs(),
            evidence_counts={
                "selected": len(self.evidence_manifest.refs),
                "screenshots": len(self.bundle.screenshot_refs),
                "a11y": len(self.bundle.a11y_refs),
                "dom": len(self.bundle.dom_refs),
                "runtime_events": len(self.bundle.runtime_event_refs),
                "tool_results": len(self.bundle.tool_results),
            },
            projection={
                "detail_level": EvaluatorDetailLevel.STANDARD.value,
                "projection_level": projection_level,
                "raw_available_with_detail_level": EvaluatorDetailLevel.DEBUG.value,
                "path_free": True,
                "geometry_policy": EvaluatorGeometryPolicy.NONE.value,
                "step_refs_total": len(self.bundle.step_refs),
                "step_refs_included": len(selected_steps),
                "step_refs_omitted": max(0, len(self.bundle.step_refs) - len(selected_steps)),
                "final_full_steps": final_full_steps,
                "tool_calls_total": len(self.bundle.tool_calls),
                "tool_calls_included": min(len(self.bundle.tool_calls), tool_call_limit),
                "tool_results_total": len(self.bundle.tool_results),
                "tool_results_included": min(len(self.bundle.tool_results), tool_result_limit),
            },
        )
        return view.model_dump(mode="json")

    def compact_trace(
        self,
        *,
        detail_level: object = EvaluatorDetailLevel.STANDARD,
        projection_level: str = "standard",
        final_full_steps: int = 20,
    ) -> JsonObject:
        """Return a compact trace view."""

        if _coerce_detail(detail_level) == EvaluatorDetailLevel.DEBUG:
            return {
                "step_refs": [step.model_dump(mode="json") for step in self.bundle.step_refs],
                "step_summaries": self.bundle.step_summaries,
                "tool_calls": self.bundle.tool_calls,
                "tool_results": self.bundle.tool_results,
            }
        selected_steps = self._selected_step_indexes(
            projection_level=projection_level,
            final_full_steps=final_full_steps,
        )
        tool_call_limit = _MAX_TOOL_CALL_ITEMS if projection_level != "tight" else min(_MAX_TOOL_CALL_ITEMS, 30)
        tool_result_limit = _MAX_TOOL_RESULT_ITEMS if projection_level != "tight" else min(_MAX_TOOL_RESULT_ITEMS, 20)
        return EvaluatorTraceView(
            step_refs=[self.compact_step_ref(self.bundle.step_refs[index]) for index in selected_steps],
            step_summaries=[
                self.compact_text(self.bundle.step_summaries[index])
                for index in selected_steps
                if index < len(self.bundle.step_summaries)
            ],
            tool_calls=[
                self.compact_tool_call(item) for item in self._tail_items(self.bundle.tool_calls, tool_call_limit)
            ],
            tool_results=[
                self.compact_tool_result(item) for item in self._tail_items(self.bundle.tool_results, tool_result_limit)
            ],
            projection={
                "projection_level": projection_level,
                "step_refs_total": len(self.bundle.step_refs),
                "step_refs_included": len(selected_steps),
                "step_refs_omitted": max(0, len(self.bundle.step_refs) - len(selected_steps)),
            },
        ).model_dump(mode="json")

    def compact_manifest_refs(self) -> list[JsonObject]:
        """Return compact selected evidence refs."""

        return [self.compact_evidence_ref(ref) for ref in self.evidence_manifest.refs]

    def compact_evidence_ref(self, ref: EvaluationEvidenceRef) -> JsonObject:
        """Return one path-free evidence ref summary."""

        metadata = self.compact_metadata(ref.metadata)
        return {
            "id": ref.id,
            "kind": ref.kind.value,
            "label": self.compact_text(ref.label, limit=180),
            "summary": self.compact_text(ref.summary, limit=260),
            "session_step": ref.session_step,
            "round_step": ref.round_step,
            "pinned": ref.pinned,
            "selection_reason": ref.selection_reason,
            "metadata": metadata,
        }

    def compact_step_ref(self, value: object) -> JsonObject:
        """Return a compact path-free step ref."""

        data = value.model_dump(mode="json") if hasattr(value, "model_dump") else value
        if not isinstance(data, Mapping):
            return {"value": self.compact_text(data)}
        return {
            "session_step": data.get("session_step"),
            "round_step": data.get("round_step"),
            "status": data.get("status"),
            "result": self.compact_text(data.get("result"), limit=420),
            "assistant_summary": self.compact_text(data.get("assistant_text"), limit=420),
        }

    def compact_tool_call(self, value: object) -> JsonObject:
        """Return compact function/tool call metadata."""

        if not isinstance(value, Mapping):
            return {"value": self.compact_text(value)}
        name = value.get("name") or value.get("tool_name")
        arguments = value.get("arguments") or value.get("tool_input") or value.get("input")
        return {
            key: item
            for key, item in {
                "id": value.get("id") or value.get("call_id") or value.get("tool_call_id"),
                "name": name,
                "arguments": self.model_facing_value(arguments),
            }.items()
            if item not in (None, "", {}, [])
        }

    def compact_tool_result(self, value: object) -> JsonObject:
        """Return compact model-facing result metadata for one tool result."""

        if not isinstance(value, Mapping):
            return {"value": self.compact_text(value)}
        evidence_refs = {
            key: value.get(key)
            for key in (
                "image_ref",
                "raw_image_ref",
                "som_image_ref",
                "dom_ref",
                "a11y_ref",
                "runtime_event_ref",
            )
            if isinstance(value.get(key), str) and value.get(key)
        }
        observation = value.get("observation")
        if not isinstance(observation, Mapping):
            observation = {}
        page = observation.get("page") if isinstance(observation.get("page"), Mapping) else {}
        compact_observation = {
            key: item
            for key, item in {
                "url": page.get("url") or observation.get("url"),
                "title": page.get("title") or observation.get("title"),
                "settled": observation.get("settled"),
                "settle_reason": observation.get("settle_reason"),
                "observation_error": value.get("observation_error"),
            }.items()
            if item not in (None, "", {}, [])
        }
        result = value.get("result")
        view = EvaluatorToolResultView(
            name=str(value.get("name") or ""),
            action=str(value.get("action") or ""),
            session_step=self._coerce_int(value.get("session_step")),
            round_step=self._coerce_int(value.get("round_step")),
            status=str(value.get("status") or ""),
            text=self.compact_text(value.get("text")),
            params=self.model_facing_object(value.get("params")),
            result=self.model_facing_object(result),
            observation=compact_observation,
            recovery=self.model_facing_object(value.get("observation_recovery") or value.get("recovery")),
            evidence_refs=evidence_refs,
        )
        payload = view.model_dump(mode="json")
        return {key: item for key, item in payload.items() if item not in ("", {}, [], 0)}

    def compact_metadata(self, metadata: Mapping[str, object]) -> JsonObject:
        """Return metadata safe for prompt/tool output."""

        compact: JsonObject = {}
        for key in (
            "variant",
            "tool_name",
            "action",
            "selected_image_kind",
            "source",
            "event",
            "terminal_verification",
            "initial_state",
        ):
            value = metadata.get(key)
            if isinstance(value, (str, int, float, bool)):
                compact[key] = value
        som_metadata = metadata.get("som_metadata")
        if isinstance(som_metadata, Mapping):
            compact.update(self.compact_som_counts(som_metadata))
        return compact

    def _selected_step_indexes(self, *, projection_level: str, final_full_steps: int) -> list[int]:
        total = len(self.bundle.step_refs)
        if total <= 0:
            return []
        max_steps = _TIGHT_MAX_STEP_REFS if projection_level == "tight" else _STANDARD_MAX_STEP_REFS
        final_window = max(1, min(final_full_steps, max_steps, total))
        selected: set[int] = set(range(max(0, total - final_window), total))
        selected.update(range(min(3, total)))
        for index, step_ref in enumerate(self.bundle.step_refs):
            data = step_ref.model_dump(mode="json") if hasattr(step_ref, "model_dump") else step_ref
            if not isinstance(data, Mapping):
                continue
            status = str(data.get("status") or "").lower()
            result = str(data.get("result") or "").lower()
            if status in _KEY_TERMINAL_STATUSES or any(marker in result for marker in ("error", "timeout", "failed")):
                selected.add(index)
        if len(selected) > max_steps:
            head = sorted(index for index in selected if index < 3)
            tail = sorted(index for index in selected if index >= total - final_window)
            middle_budget = max(0, max_steps - len(set(head) | set(tail)))
            middle = [index for index in sorted(selected) if index not in set(head) and index not in set(tail)][
                :middle_budget
            ]
            selected = set(head) | set(middle) | set(tail)
        return sorted(selected)

    @staticmethod
    def _tail_items(items: list[object], limit: int) -> list[object]:
        if limit <= 0:
            return []
        return list(items[-limit:])

    def compact_som_annotation(
        self,
        *,
        evidence_ref: str,
        session_step: int,
        som_metadata: Mapping[str, object],
        include_geometry: bool = False,
        ref_filter: Iterable[str] | None = None,
    ) -> JsonObject:
        """Return low-noise SoM metadata, with geometry only on explicit request."""

        ref_filter_set = {str(item) for item in (ref_filter or []) if str(item)}
        geometry_policy = (
            EvaluatorGeometryPolicy.SELECTED_REFS
            if include_geometry and ref_filter_set
            else EvaluatorGeometryPolicy.NONE
        )
        raw_marks = som_metadata.get("marks")
        marks: list[JsonObject] = []
        if isinstance(raw_marks, list):
            for mark in raw_marks[:_MAX_LIST_ITEMS]:
                if not isinstance(mark, Mapping):
                    continue
                ref = str(mark.get("ref") or "")
                if ref_filter_set and ref not in ref_filter_set:
                    continue
                marks.append(self.compact_som_mark(mark, include_geometry=include_geometry and ref in ref_filter_set))
        counts = self.compact_som_counts(som_metadata)
        return {
            "status": "success",
            "evidence_ref": evidence_ref,
            "session_step": session_step,
            "kind": str(som_metadata.get("kind") or "set_of_mark"),
            **counts,
            "geometry_policy": geometry_policy.value,
            "marks": marks,
            "raw_available_with_detail_level": EvaluatorDetailLevel.DEBUG.value,
        }

    def compact_som_mark(self, mark: Mapping[str, object], *, include_geometry: bool) -> JsonObject:
        """Return a compact single-mark view."""

        compact: JsonObject = {}
        for key in ("ref", "label", "role", "name", "text", "visible", "enabled"):
            value = mark.get(key)
            if value not in (None, "", [], {}):
                compact[key] = self.compact_text(value, limit=120) if isinstance(value, str) else value
        if include_geometry:
            for key in ("bbox_css", "bbox_image"):
                value = mark.get(key)
                if isinstance(value, Mapping):
                    compact[key] = self.model_facing_object(value, geometry_policy=EvaluatorGeometryPolicy.FULL_DEBUG)
        return compact

    def compact_som_counts(self, som_metadata: Mapping[str, object]) -> JsonObject:
        """Return counts and quality flags for SoM metadata."""

        marks = som_metadata.get("marks")
        counts: JsonObject = {
            "marked_count": (
                len(marks) if isinstance(marks, list) else self._coerce_int(som_metadata.get("marked_count"))
            ),
            "visible_interactive_count": self._coerce_int(som_metadata.get("visible_interactive_count")),
            "omitted_count": self._coerce_int(som_metadata.get("omitted_count")),
            "truncated": bool(som_metadata.get("truncated", False)),
            "max_marks": self._coerce_int(som_metadata.get("max_marks")),
        }
        reason_counts = som_metadata.get("omitted_reason_counts")
        if isinstance(reason_counts, Mapping):
            counts["omitted_reason_counts"] = {
                str(key): value for key, value in reason_counts.items() if isinstance(value, (int, float, str))
            }
        return {key: value for key, value in counts.items() if value not in (None, "", {}, [])}

    def model_facing_object(
        self,
        value: object,
        *,
        geometry_policy: EvaluatorGeometryPolicy = EvaluatorGeometryPolicy.NONE,
    ) -> JsonObject:
        """Return a JSON object after model-facing redaction."""

        projected = self.model_facing_value(value, geometry_policy=geometry_policy)
        return projected if isinstance(projected, dict) else {}

    def model_facing_value(
        self,
        value: object,
        *,
        geometry_policy: EvaluatorGeometryPolicy = EvaluatorGeometryPolicy.NONE,
    ) -> object:
        """Recursively redact raw paths, image data, and geometry-heavy fields."""

        if isinstance(value, str):
            return self.compact_text(value)
        if isinstance(value, Mapping):
            out: JsonObject = {}
            for key, child in value.items():
                key_text = str(key)
                if self._is_path_key(key_text):
                    continue
                if self._is_geometry_key(key_text) and geometry_policy != EvaluatorGeometryPolicy.FULL_DEBUG:
                    continue
                if key_text == "marks" and geometry_policy == EvaluatorGeometryPolicy.NONE:
                    marks = child if isinstance(child, list) else []
                    out["mark_count"] = len(marks)
                    continue
                if key_text in {"som_metadata", "annotation"} and isinstance(child, Mapping):
                    out[key_text] = self.compact_som_counts(child)
                    continue
                projected = self.model_facing_value(child, geometry_policy=geometry_policy)
                if projected not in (None, "", [], {}):
                    out[key_text] = projected
            return out
        if isinstance(value, list):
            items = [self.model_facing_value(item, geometry_policy=geometry_policy) for item in value[:_MAX_LIST_ITEMS]]
            items = [item for item in items if item not in (None, "", [], {})]
            if len(value) > _MAX_LIST_ITEMS:
                items.append({"truncated": True, "omitted_items": len(value) - _MAX_LIST_ITEMS})
            return items
        return value

    @staticmethod
    def compact_text(value: object, *, limit: int = _MAX_TEXT_CHARS) -> str:
        """Return bounded single-line-ish text."""

        if value is None:
            return ""
        text = str(value)
        lowered = text.lower()
        if lowered.startswith(_IMAGE_DATA_PREFIX) or lowered.startswith(_REDACTED_IMAGE_MARKER_PREFIX):
            return f"[image data omitted; chars={len(text)}]"
        text = re.sub(r"\s+", " ", text).strip()
        if len(text) <= limit:
            return text
        return f"{text[: max(0, limit - 1)]}…"

    @staticmethod
    def _is_path_key(key: str) -> bool:
        lowered = key.lower()
        return (
            bool(_PATH_KEY_RE.search(lowered))
            or lowered.endswith("_image_url")
            or lowered.endswith("_data_url")
            or lowered
            in {
                "path",
                "file_path",
                "image_url",
                "overlay_image_url",
                "screenshot_path",
                "raw_image_path",
                "som_image_path",
                "selected_image_path",
                "dom_snapshot_path",
                "dom_snapshot_json_path",
                "clean_screenshot_path",
                "data_url",
                "image_data_url",
                "screenshot_url",
                "raw_image_url",
                "som_image_url",
            }
        )

    @staticmethod
    def _is_geometry_key(key: str) -> bool:
        return bool(_GEOMETRY_KEY_RE.search(key))

    @staticmethod
    def _coerce_int(value: object) -> int:
        if isinstance(value, int):
            return value
        if isinstance(value, str) and value.isdigit():
            return int(value)
        return 0


__all__ = ["EvaluatorEvidenceProjector"]
