"""Server-side artifact inspection tools for AgentKit evaluators."""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

from nexau.archs.tool.formatters import ToolFormatterContext
from nexau.archs.tool.tool import Tool
from PIL import Image, ImageChops, ImageStat

from northcua.agentkernel.json_types import JsonObject
from northcua.agentkernel.tools import contract_to_nexau_tool
from northcua.core.enums import EvaluationEvidenceKind

from ..artifacts import ArtifactPathResolver
from ..projection import EvaluatorDetailLevel, EvaluatorEvidenceProjector
from ..protocol import EvaluationEvidenceManifest, EvaluationEvidenceRef, RoundEvaluationBundle
from ..tool_contracts import ARTIFACT_TOOL_REFS, EvaluatorArtifactToolRef
from .artifact_payloads import MAX_TOOL_LIST_ITEMS, ArtifactToolPayloadLimiter

_OCR_TIMEOUT_SECONDS = 30
_IMAGE_RESAMPLE_LANCZOS = Image.Resampling.LANCZOS
_SNAPSHOT_TEXT_RE = re.compile(r'^(?P<role>[a-zA-Z0-9_-]+)\s+"(?P<text>.*?)"')
ROUND_BUNDLE_SECTION_ALIASES: dict[str, str] = {
    "evidence": "selected_evidence_manifest",
    "selected_evidence": "selected_evidence_manifest",
    "steps": "step_refs",
    "summary": "step_summaries",
    "summaries": "step_summaries",
    "tool_trace": "tool_results",
    "tools": "tool_results",
    "trajectory": "trace",
    "history": "trace",
    "user_request": "round_request",
    "request": "round_request",
    "prompt": "round_request",
    "messages": "trace",
    "transcript": "trace",
}


def _format_tool_output(context: ToolFormatterContext) -> str:
    output = context.tool_output
    if isinstance(output, str):
        return output
    return json.dumps(output, ensure_ascii=False, indent=2)


def _coerce_int(value: object, *, default: int) -> int:
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.isdigit():
        return int(value)
    return default


class ArtifactInspectionToolFactory:
    """Build server-side read-only evaluator tools bound to one bundle."""

    def __init__(
        self,
        *,
        artifact_root: Path,
        bundle: RoundEvaluationBundle,
        evidence_manifest: EvaluationEvidenceManifest,
        step_trace_by_session_step: dict[int, JsonObject],
    ) -> None:
        self.artifact_root = artifact_root
        self.bundle = bundle
        self.evidence_manifest = evidence_manifest
        self.step_trace_by_session_step = step_trace_by_session_step
        self.evidence_by_id = {
            ref.id: ref
            for ref in (
                *evidence_manifest.refs,
                *bundle.screenshot_refs,
                *bundle.dom_refs,
                *bundle.a11y_refs,
                *bundle.runtime_event_refs,
            )
        }
        self.path_resolver = ArtifactPathResolver(artifact_root)
        self.payload_limiter = ArtifactToolPayloadLimiter()
        self.projector = EvaluatorEvidenceProjector(bundle=bundle, evidence_manifest=evidence_manifest)

    def build_tools(self) -> list[Tool]:
        """Create read-only artifact inspection tools.

        Returns:
            NexAU tools bound to this round's artifact bundle.
        """

        return [self._build_tool(ref) for ref in ARTIFACT_TOOL_REFS]

    def _build_tool(self, spec: EvaluatorArtifactToolRef) -> Tool:
        return contract_to_nexau_tool(
            spec.contract,
            implementation=self._bounded_implementation(getattr(self, spec.implementation_name)),
            formatter=_format_tool_output,
            disable_parallel=True,
        )

    def _bounded_implementation(self, implementation):
        def _run(**kwargs: object) -> JsonObject:
            result = implementation(**kwargs)
            if self._is_debug_detail(str(kwargs.get("detail_level") or "")):
                return result if isinstance(result, dict) else {"status": "success", "value": result}
            return self.payload_limiter.bounded_tool_payload(result)

        return _run

    def _read_round_bundle(self, section: str = "", detail_level: str = "standard") -> JsonObject:
        data: JsonObject = self._bundle_data_without_assistant_texts()
        if self._is_debug_detail(detail_level):
            return self._read_round_bundle_raw(section=section, data=data)
        section = section.strip()
        if not section:
            return self.projector.compact_bundle(detail_level=detail_level)
        if section in {"all", "bundle", "full"}:
            return {
                "status": "success",
                "section": "all",
                "value": self.projector.compact_bundle(detail_level=detail_level),
                "raw_available_with_detail_level": EvaluatorDetailLevel.DEBUG.value,
            }
        section = ROUND_BUNDLE_SECTION_ALIASES.get(section, section)
        if section in {"selected_evidence_manifest", "evidence_manifest"}:
            return {
                "status": "success",
                "section": section,
                "value": self.projector.compact_manifest_refs(),
            }
        if section == "trace":
            return {
                "status": "success",
                "section": section,
                "value": self.projector.compact_trace(detail_level=detail_level),
            }
        if section == "assistant_texts":
            return {
                "status": "failure",
                "error": "assistant_texts is not exposed separately; use step_refs assistant_summary entries.",
            }
        if section == "final":
            return {
                "status": "success",
                "section": section,
                "value": {
                    "final_output_candidates": self._final_output_candidates(),
                    "last_step": (data.get("step_refs") or [])[-1:] if isinstance(data.get("step_refs"), list) else [],
                },
            }
        if section not in data:
            return {"status": "failure", "error": f"Unknown section: {section}"}
        return {"status": "success", "section": section, "value": self.projector.model_facing_value(data[section])}

    def _read_round_bundle_raw(self, *, section: str, data: JsonObject) -> JsonObject:
        section = section.strip()
        if not section:
            return data
        if section in {"all", "bundle", "full"}:
            return {"status": "success", "section": "all", "value": data}
        section = ROUND_BUNDLE_SECTION_ALIASES.get(section, section)
        if section in {"selected_evidence_manifest", "evidence_manifest"}:
            return {
                "status": "success",
                "section": section,
                "value": [ref.model_dump(mode="json") for ref in self.evidence_manifest.refs],
            }
        if section == "trace":
            return {
                "status": "success",
                "section": section,
                "value": {
                    "step_refs": data.get("step_refs", []),
                    "step_summaries": data.get("step_summaries", []),
                    "tool_calls": data.get("tool_calls", []),
                    "tool_results": data.get("tool_results", []),
                },
            }
        if section == "assistant_texts":
            return {
                "status": "failure",
                "error": "assistant_texts is not exposed separately; use step_refs assistant_summary entries.",
            }
        if section == "final":
            return {
                "status": "success",
                "section": section,
                "value": {
                    "final_output_candidates": self._final_output_candidates(),
                    "last_step": (data.get("step_refs") or [])[-1:] if isinstance(data.get("step_refs"), list) else [],
                },
            }
        if section not in data:
            return {"status": "failure", "error": f"Unknown section: {section}"}
        return {"status": "success", "section": section, "value": data[section]}

    def _read_step_trace(self, session_step: int, detail_level: str = "standard") -> JsonObject:
        trace = self.step_trace_by_session_step.get(session_step)
        if trace is None:
            return {"status": "failure", "error": f"Unknown session_step: {session_step}"}
        if self._is_debug_detail(detail_level):
            return {"status": "success", "trace": trace, "detail_level": EvaluatorDetailLevel.DEBUG.value}
        return {
            "status": "success",
            "detail_level": EvaluatorDetailLevel.STANDARD.value,
            "trace": self.projector.model_facing_object(trace),
            "raw_available_with_detail_level": EvaluatorDetailLevel.DEBUG.value,
        }

    def _query_runtime_events(self, query: str = "") -> JsonObject:
        query_text = query.strip().lower()
        matches = []
        for ref in self.bundle.runtime_event_refs:
            haystack = f"{ref.label} {ref.summary}".lower()
            if query_text and query_text not in haystack:
                continue
            matches.append(self.payload_limiter.compact_evidence_ref(ref))
        return {"status": "success", "matches": matches}

    def _query_tool_results(
        self, tool_name: str = "", session_step: int = 0, detail_level: str = "standard"
    ) -> JsonObject:
        tool_name_normalized = tool_name.strip().lower()
        matches = []
        for result in self.bundle.tool_results:
            name = str(result.get("name") or "")
            action = str(result.get("action") or "")
            step = _coerce_int(result.get("session_step"), default=0)
            if tool_name_normalized and not self._matches_tool_result_name(
                requested=tool_name_normalized,
                name=name,
                action=action,
            ):
                continue
            if session_step and step and step != session_step:
                continue
            matches.append(
                result if self._is_debug_detail(detail_level) else self.projector.compact_tool_result(result)
            )
        return {"status": "success", "matches": matches}

    def _query_a11y_snapshot(
        self, evidence_ref: str = "", session_step: int = 0, detail_level: str = "standard"
    ) -> JsonObject:
        ref: EvaluationEvidenceRef | None = None
        if evidence_ref:
            ref = self.evidence_by_id.get(evidence_ref)
        elif session_step:
            ref = next((item for item in self.bundle.a11y_refs if item.session_step == session_step), None)
        if ref is None or not ref.path:
            return {"status": "failure", "error": "No matching accessibility snapshot found."}
        path = self._resolve_path(ref.path)
        if path is None or not path.exists():
            return {"status": "failure", "error": f"Accessibility artifact missing: {path}"}
        snapshot = json.loads(path.read_text(encoding="utf-8"))
        if self._is_debug_detail(detail_level):
            return {"status": "success", "snapshot": snapshot, "detail_level": EvaluatorDetailLevel.DEBUG.value}
        return {
            "status": "success",
            "snapshot": self.projector.model_facing_object(self.payload_limiter.compact_a11y_snapshot(snapshot)),
            "raw_available_with_detail_level": EvaluatorDetailLevel.DEBUG.value,
        }

    def _query_dom_snapshot(
        self, evidence_ref: str = "", session_step: int = 0, detail_level: str = "standard"
    ) -> JsonObject:
        ref = self._resolve_evidence_ref(
            evidence_ref=evidence_ref,
            session_step=session_step,
            kind=EvaluationEvidenceKind.DOM,
        )
        if ref is None or not ref.path:
            return {"status": "failure", "error": "No matching DOM snapshot found."}
        json_path = self._resolve_path(ref.path)
        if json_path is None or not json_path.exists():
            return {"status": "failure", "error": f"DOM artifact missing: {json_path}"}
        try:
            structured = json.loads(json_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            structured = {"raw_json_text": json_path.read_text(encoding="utf-8")}
        text_path_value = str(ref.metadata.get("text_path") or "")
        text_path = self._resolve_path(text_path_value) if text_path_value else None
        text_preview = text_path.read_text(encoding="utf-8") if text_path is not None and text_path.exists() else ""
        visible_text_candidates = self._visible_text_candidates(structured=structured, text_preview=text_preview)
        if self._is_debug_detail(detail_level):
            return {
                "status": "success",
                "evidence_ref": ref.id,
                "session_step": ref.session_step,
                "round_step": ref.round_step,
                "dom": self.payload_limiter.bounded_json_value(structured),
                "text_preview": text_preview,
            }
        return {
            "status": "success",
            "evidence_ref": ref.id,
            "session_step": ref.session_step,
            "round_step": ref.round_step,
            "visible_text_candidates": visible_text_candidates,
            "dom_summary": self.projector.model_facing_object(structured),
            "text_preview": self.projector.compact_text(text_preview),
            "raw_available_with_detail_level": EvaluatorDetailLevel.DEBUG.value,
        }

    def _query_web_page_state(self, session_step: int, detail_level: str = "standard") -> JsonObject:
        trace = self.step_trace_by_session_step.get(session_step)
        if trace is None:
            return {"status": "failure", "error": f"Unknown session_step: {session_step}"}
        observation_artifact = trace.get("observation_artifact")
        terminal_verification = trace.get("terminal_verification")
        result = {
            "status": "success",
            "session_step": session_step,
            "round_step": _coerce_int(trace.get("step_index"), default=0),
            "url": str(trace.get("url") or ""),
            "page": {
                "url": self._nested_string(observation_artifact, "page_url"),
                "title": self._nested_string(observation_artifact, "page_title"),
                "settled": self._nested_bool(observation_artifact, "settled"),
                "settle_reason": self._nested_string(observation_artifact, "settle_reason"),
                "ref_count": self._nested_int(observation_artifact, "ref_count"),
            },
            "artifacts": {
                "screenshot_path": str(trace.get("screenshot_path") or ""),
                "dom_snapshot_path": str(trace.get("dom_snapshot_path") or ""),
                "dom_snapshot_json_path": str(trace.get("dom_snapshot_json_path") or ""),
            },
            "final_output_candidates": self._final_output_candidates(),
            "final_output_verification": self._final_output_verification(session_step=session_step),
            "terminal_verification": terminal_verification if isinstance(terminal_verification, dict) else {},
        }
        if self._is_debug_detail(detail_level):
            return result
        result.pop("artifacts", None)
        result["terminal_verification"] = self.projector.model_facing_object(result.get("terminal_verification"))
        result["raw_available_with_detail_level"] = EvaluatorDetailLevel.DEBUG.value
        return result

    def _query_som_annotation(
        self,
        evidence_ref: str = "",
        session_step: int = 0,
        include_geometry: bool = False,
        ref_filter: list[str] | None = None,
        detail_level: str = "standard",
    ) -> JsonObject:
        ref = self._resolve_som_evidence_ref(evidence_ref=evidence_ref, session_step=session_step)
        if ref is not None and not session_step:
            session_step = ref.session_step
        som_metadata = self._som_metadata_for_ref(ref)
        if not som_metadata and session_step:
            ref, som_metadata = self._som_metadata_for_step(session_step=session_step, fallback_ref=ref)
        if not som_metadata:
            ref, som_metadata = self._fallback_som_annotation_from_single_sidecar()
        if not isinstance(som_metadata, dict) or not som_metadata:
            return {"status": "failure", "error": "No matching SoM annotation found."}
        if self._is_debug_detail(detail_level):
            marks = som_metadata.get("marks")
            return {
                "status": "success",
                "evidence_ref": ref.id if ref is not None else "",
                "session_step": ref.session_step if ref is not None else session_step,
                "kind": str(som_metadata.get("kind") or "set_of_mark"),
                "truncated": bool(som_metadata.get("truncated", False)),
                "omitted_count": _coerce_int(som_metadata.get("omitted_count"), default=0),
                "max_marks": _coerce_int(som_metadata.get("max_marks"), default=0),
                "marks": marks if isinstance(marks, list) else [],
            }
        marks = som_metadata.get("marks")
        del marks
        return self.projector.compact_som_annotation(
            evidence_ref=ref.id if ref is not None else "",
            session_step=ref.session_step if ref is not None else session_step,
            som_metadata=som_metadata,
            include_geometry=include_geometry,
            ref_filter=ref_filter,
        )

    def _query_web_terminal_verification(self, session_step: int = 0, detail_level: str = "standard") -> JsonObject:
        if session_step:
            trace = self.step_trace_by_session_step.get(session_step)
            traces = [trace] if trace is not None else []
        else:
            round_steps = {
                step.session_step
                for step in self.bundle.step_refs
                if isinstance(step.session_step, int) and step.session_step > 0
            }
            if round_steps:
                traces = [
                    self.step_trace_by_session_step[step]
                    for step in sorted(round_steps)
                    if step in self.step_trace_by_session_step
                ]
            else:
                traces = list(self.step_trace_by_session_step.values())
        for trace in reversed(traces):
            if not isinstance(trace, dict):
                continue
            record = trace.get("terminal_verification")
            if isinstance(record, dict) and record:
                return {
                    "status": "success",
                    "terminal_verification": (
                        record if self._is_debug_detail(detail_level) else self.projector.model_facing_object(record)
                    ),
                    "raw_available_with_detail_level": EvaluatorDetailLevel.DEBUG.value,
                }
            metadata = trace.get("observation_metadata")
            if isinstance(metadata, dict) and metadata.get("web_terminal_verification_status"):
                return {
                    "status": "success",
                    "terminal_verification": (
                        metadata
                        if self._is_debug_detail(detail_level)
                        else self.projector.model_facing_object(metadata)
                    ),
                    "raw_available_with_detail_level": EvaluatorDetailLevel.DEBUG.value,
                }
        return {"status": "failure", "error": "No Web terminal verification record found."}

    def _compare_images(self, left_ref: str, right_ref: str) -> JsonObject:
        left_path = self._image_path_for_ref(left_ref)
        right_path = self._image_path_for_ref(right_ref)
        if left_path is None or right_path is None:
            return {"status": "failure", "error": "Both evidence refs must resolve to images."}
        if not left_path.exists() or not right_path.exists():
            return {"status": "failure", "error": "One or both image files do not exist."}
        oversize_error = self.payload_limiter.image_size_error(left_path) or self.payload_limiter.image_size_error(
            right_path
        )
        if oversize_error:
            return {"status": "failure", "error": oversize_error}
        with Image.open(left_path) as left_image, Image.open(right_path) as right_image:
            pixel_error = self.payload_limiter.image_pixel_error(left_image) or self.payload_limiter.image_pixel_error(
                right_image
            )
            if pixel_error:
                return {"status": "failure", "error": pixel_error}
            left_rgb = left_image.convert("RGB")
            right_rgb = right_image.convert("RGB")
            same_size = left_rgb.size == right_rgb.size
            if not same_size:
                right_rgb = right_rgb.resize(left_rgb.size, _IMAGE_RESAMPLE_LANCZOS)
            diff = ImageChops.difference(left_rgb, right_rgb)
            mean_delta = sum(ImageStat.Stat(diff).mean) / 3.0
            right_size = list(right_rgb.size)
            left_size = list(left_rgb.size)
        return {
            "status": "success",
            "same_size": same_size,
            "left_size": left_size,
            "right_size": right_size,
            "mean_pixel_delta": round(mean_delta, 4),
        }

    def _extract_image_text_summary(self, evidence_ref: str) -> JsonObject:
        image_path = self._image_path_for_ref(evidence_ref)
        if image_path is None or not image_path.exists():
            return {"status": "failure", "error": "Evidence ref does not resolve to a local image."}
        oversize_error = self.payload_limiter.image_size_error(image_path)
        if oversize_error:
            return {"status": "failure", "error": oversize_error}
        tesseract = shutil.which("tesseract")
        if tesseract is None:
            return {
                "status": "failure",
                "error": "Missing required dependency: tesseract is not installed or not on PATH.",
                "missing_dependency": "tesseract",
            }
        with tempfile.TemporaryDirectory(prefix="northcua-eval-ocr-") as temp_dir:
            output_base = Path(temp_dir) / "ocr"
            try:
                completed = subprocess.run(
                    [tesseract, str(image_path), str(output_base)],
                    capture_output=True,
                    text=True,
                    check=False,
                    timeout=_OCR_TIMEOUT_SECONDS,
                )
            except subprocess.TimeoutExpired:
                return {
                    "status": "failure",
                    "error": f"tesseract OCR timed out after {_OCR_TIMEOUT_SECONDS} seconds.",
                    "timeout_seconds": _OCR_TIMEOUT_SECONDS,
                }
            if completed.returncode != 0:
                return {"status": "failure", "error": completed.stderr.strip() or "tesseract OCR failed"}
            text_path = output_base.with_suffix(".txt")
            extracted = text_path.read_text(encoding="utf-8").strip() if text_path.exists() else ""
        return {"status": "success" if extracted else "skipped", "summary": extracted or "OCR produced no text."}

    def _query_evidence_manifest(self, kind: str = "") -> JsonObject:
        selected = []
        kind_normalized = kind.strip().lower()
        for ref in self.evidence_manifest.refs:
            if kind_normalized and ref.kind.value != kind_normalized:
                continue
            selected.append(self.payload_limiter.compact_evidence_ref(ref))
        return {"status": "success", "refs": selected}

    def _image_path_for_ref(self, evidence_ref: str) -> Path | None:
        ref = self._find_evidence_ref(evidence_ref)
        if ref is None or not ref.path:
            return None
        return self._resolve_path(ref.path)

    def _resolve_evidence_ref(
        self,
        *,
        evidence_ref: str,
        session_step: int,
        kind: EvaluationEvidenceKind,
    ) -> EvaluationEvidenceRef | None:
        if evidence_ref:
            ref = self.evidence_by_id.get(evidence_ref)
            if ref is None or ref.kind != kind:
                return None
            return ref
        candidates = {
            EvaluationEvidenceKind.DOM: self.bundle.dom_refs,
            EvaluationEvidenceKind.A11Y: self.bundle.a11y_refs,
            EvaluationEvidenceKind.IMAGE: self.bundle.screenshot_refs,
        }.get(kind, [])
        if session_step:
            return self._preferred_step_evidence_ref([item for item in candidates if item.session_step == session_step])
        return candidates[-1] if candidates else None

    @staticmethod
    def _preferred_step_evidence_ref(candidates: list[EvaluationEvidenceRef]) -> EvaluationEvidenceRef | None:
        """Return the current evidence for a step when historical refs also exist."""

        if not candidates:
            return None

        def _rank(ref: EvaluationEvidenceRef) -> tuple[int, int]:
            variant = str(ref.metadata.get("variant") or "")
            if variant == "terminal_verification":
                return (0, 0)
            if variant in {"current", "raw"}:
                return (1, 0)
            if ref.pinned:
                return (2, 0)
            if variant == "pre_terminal_current":
                return (4, 0)
            return (3, 0)

        return min(enumerate(candidates), key=lambda item: (_rank(item[1]), item[0]))[1]

    def _resolve_som_evidence_ref(self, *, evidence_ref: str, session_step: int) -> EvaluationEvidenceRef | None:
        if evidence_ref:
            ref = self._find_evidence_ref(evidence_ref)
            if ref is None or ref.kind != EvaluationEvidenceKind.IMAGE:
                return None
            return ref
        candidates = [
            ref for ref in self.bundle.screenshot_refs if not session_step or ref.session_step == session_step
        ]
        return next(
            (
                ref
                for ref in sorted(candidates, key=self._som_ref_priority, reverse=True)
                if self._som_metadata_for_ref(ref)
            ),
            None,
        )

    def _som_metadata_for_step(
        self,
        *,
        session_step: int,
        fallback_ref: EvaluationEvidenceRef | None,
    ) -> tuple[EvaluationEvidenceRef | None, JsonObject]:
        candidates = [ref for ref in self.bundle.screenshot_refs if ref.session_step == session_step]
        for ref in sorted(candidates, key=self._som_ref_priority, reverse=True):
            metadata = self._som_metadata_for_ref(ref)
            if metadata:
                return ref, metadata
        trace = self.step_trace_by_session_step.get(session_step, {})
        if isinstance(trace, dict):
            metadata = self._som_metadata_from_step_trace(trace)
            if metadata:
                return fallback_ref, metadata
        return fallback_ref, {}

    def _som_metadata_from_step_trace(self, trace: JsonObject) -> JsonObject:
        for artifact in trace.get("tool_artifacts", []) if isinstance(trace.get("tool_artifacts"), list) else []:
            if not isinstance(artifact, dict):
                continue
            candidate = artifact.get("som_metadata")
            if isinstance(candidate, dict) and candidate:
                return candidate
            selected_path = str(artifact.get("selected_image_path") or artifact.get("som_image_path") or "")
            candidate = self._som_metadata_for_path(selected_path)
            if candidate:
                return candidate
        observation_artifact = trace.get("observation_artifact")
        if isinstance(observation_artifact, dict):
            for key in ("annotated_screenshot_path", "screenshot_path"):
                candidate = self._som_metadata_for_path(str(observation_artifact.get(key) or ""))
                if candidate:
                    return candidate
        return {}

    def _som_metadata_for_ref(self, ref: EvaluationEvidenceRef | None) -> JsonObject:
        if ref is None:
            return {}
        candidate = ref.metadata.get("som_metadata")
        if isinstance(candidate, dict) and candidate:
            return candidate
        return self._som_metadata_for_path(ref.path)

    def _som_metadata_for_path(self, path: str) -> JsonObject:
        if not path:
            return {}
        resolved = self._resolve_path(path)
        if resolved is None:
            return {}
        return self._load_som_annotation(resolved.with_suffix(".annotation.json"))

    def _fallback_som_annotation_from_single_sidecar(self) -> tuple[EvaluationEvidenceRef | None, JsonObject]:
        sidecars = sorted((self.artifact_root / "screenshots").glob("*.annotation.json"))
        if len(sidecars) != 1:
            return None, {}
        return None, self._load_som_annotation(sidecars[0])

    def _load_som_annotation(self, path: Path) -> JsonObject:
        if not path.exists():
            return {}
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return {}
        return payload if isinstance(payload, dict) else {}

    def _find_evidence_ref(self, evidence_ref: str) -> EvaluationEvidenceRef | None:
        if ref := self.evidence_by_id.get(evidence_ref):
            return ref
        for ref in (
            *self.bundle.screenshot_refs,
            *self.bundle.dom_refs,
            *self.bundle.a11y_refs,
            *self.bundle.runtime_event_refs,
        ):
            if ref.id == evidence_ref:
                return ref
        return None

    @staticmethod
    def _som_ref_priority(ref: EvaluationEvidenceRef) -> tuple[int, int, int]:
        variant = str(ref.metadata.get("variant") or ref.metadata.get("selected_image_kind") or "").lower()
        path = ref.path.lower()
        has_metadata = int(
            isinstance(ref.metadata.get("som_metadata"), dict) and bool(ref.metadata.get("som_metadata"))
        )
        is_som_named = int(path.endswith("_som.png") or "_som." in path)
        is_som_variant = int(variant in {"som", "set_of_mark"})
        return (has_metadata, is_som_variant, is_som_named)

    @staticmethod
    def _matches_tool_result_name(*, requested: str, name: str, action: str) -> bool:
        name_normalized = name.lower()
        action_normalized = action.lower()
        aliases = {name_normalized, action_normalized}
        return requested in aliases or name_normalized.endswith(f"_{requested}")

    def _final_output_candidates(self) -> list[JsonObject]:
        for index in range(len(self.bundle.assistant_texts), 0, -1):
            text = self.bundle.assistant_texts[index - 1]
            if text:
                return [{"source": "assistant_final_text", "index": index, "text": text}]
        return []

    def _final_output_verification(self, *, session_step: int) -> JsonObject:
        dom_ref = self._resolve_evidence_ref(
            evidence_ref="",
            session_step=session_step,
            kind=EvaluationEvidenceKind.DOM,
        )
        if dom_ref is None or not dom_ref.path:
            return {"status": "missing_dom", "matches": []}
        dom_result = self._query_dom_snapshot(evidence_ref=dom_ref.id, session_step=session_step)
        if dom_result.get("status") != "success":
            return {"status": "missing_dom", "matches": []}
        visible_candidates = [item for item in dom_result.get("visible_text_candidates", []) if isinstance(item, dict)]
        visible_texts = [str(item.get("text") or "") for item in visible_candidates]
        matches: list[JsonObject] = []
        for output in self._final_output_candidates():
            output_text = str(output.get("text") or "").strip()
            if not output_text:
                continue
            for visible in visible_candidates:
                visible_text = str(visible.get("text") or "").strip()
                if not visible_text:
                    continue
                exact_match = output_text == visible_text
                substring_match = output_text in visible_text or visible_text in output_text
                if exact_match or substring_match:
                    matches.append(
                        {
                            "match_type": "exact" if exact_match else "substring",
                            "output": output,
                            "visible_text": visible,
                            "dom_evidence_ref": dom_ref.id,
                        }
                    )
                    break
        status = "matched" if any(match.get("match_type") == "exact" for match in matches) else "not_matched"
        if matches and status == "not_matched":
            status = "partial_match"
        return {
            "status": status,
            "matches": matches,
            "visible_text_count": len(visible_texts),
            "dom_evidence_ref": dom_ref.id,
        }

    def _bundle_data_without_assistant_texts(self) -> JsonObject:
        data: JsonObject = self.bundle.model_dump(mode="json")
        data.pop("assistant_texts", None)
        return data

    @staticmethod
    def _visible_text_candidates(*, structured: object, text_preview: str) -> list[JsonObject]:
        candidates: list[JsonObject] = []
        seen: set[str] = set()

        def _append(*, text: str, role: str = "", ref: str = "") -> None:
            normalized = " ".join(text.split())
            if not normalized or normalized in seen:
                return
            seen.add(normalized)
            candidates.append({"text": normalized, "role": role, "ref": ref})

        if isinstance(structured, dict):
            elements = structured.get("elements")
            if isinstance(elements, list):
                for element in elements:
                    if not isinstance(element, dict):
                        continue
                    _append(
                        text=str(element.get("name") or element.get("text") or ""),
                        role=str(element.get("role") or ""),
                        ref=str(element.get("ref_id") or element.get("ref") or ""),
                    )
                    if len(candidates) >= MAX_TOOL_LIST_ITEMS:
                        return candidates
        for line in text_preview.splitlines():
            match = _SNAPSHOT_TEXT_RE.match(line.strip())
            if match is None:
                continue
            _append(text=match.group("text"), role=match.group("role"))
            if len(candidates) >= MAX_TOOL_LIST_ITEMS:
                break
        return candidates

    def _image_ref_for_path(self, path: str) -> EvaluationEvidenceRef | None:
        if not path:
            return None
        normalized = str(self._resolve_path(path) or path)
        for ref in self.bundle.screenshot_refs:
            ref_path = str(self._resolve_path(ref.path) or ref.path)
            if ref_path == normalized:
                return ref
        return None

    @staticmethod
    def _nested_string(value: object, key: str) -> str:
        return str(value.get(key) or "") if isinstance(value, dict) else ""

    @staticmethod
    def _nested_bool(value: object, key: str) -> bool:
        return bool(value.get(key)) if isinstance(value, dict) else False

    @staticmethod
    def _nested_int(value: object, key: str) -> int:
        return _coerce_int(value.get(key), default=0) if isinstance(value, dict) else 0

    def _resolve_path(self, value: str) -> Path | None:
        return self.path_resolver.resolve(value)

    @staticmethod
    def _is_debug_detail(detail_level: object) -> bool:
        return str(detail_level or "").strip().lower() == EvaluatorDetailLevel.DEBUG.value


__all__ = ["ARTIFACT_TOOL_REFS", "ArtifactInspectionToolFactory", "EvaluatorArtifactToolRef"]
