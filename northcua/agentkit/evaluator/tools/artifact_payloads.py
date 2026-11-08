"""Payload bounding helpers for evaluator artifact tools."""

from __future__ import annotations

import json
from pathlib import Path

from PIL import Image

from northcua.agentkernel.json_types import JsonObject

from ..protocol import EvaluationEvidenceRef

MAX_IMAGE_BYTES = 10_000_000
MAX_IMAGE_PIXELS = 40_000_000
MAX_TOOL_JSON_CHARS = 50_000
MAX_TOOL_STRING_CHARS = 2_000
MAX_TOOL_LIST_ITEMS = 25
MAX_A11Y_TREE_DEPTH = 3
MAX_A11Y_CHILDREN = 8


class ArtifactToolPayloadLimiter:
    """Compact artifact tool output before returning it to the evaluator."""

    @classmethod
    def bounded_tool_payload(cls, payload: object) -> JsonObject:
        """Return a JSON-object payload that stays within prompt budgets.

        Args:
            payload: Raw artifact tool output.

        Returns:
            Bounded JSON object safe to send back to the evaluator model.
        """

        bounded = cls.bounded_json_value(payload)
        if isinstance(bounded, dict):
            serialized = json.dumps(bounded, ensure_ascii=False)
            if len(serialized) <= MAX_TOOL_JSON_CHARS:
                return {str(key): value for key, value in bounded.items()}
            return {
                "status": str(bounded.get("status") or "success"),
                "truncated": True,
                "summary": "Evaluator artifact tool output exceeded the prompt budget and was truncated.",
                "preview": serialized[:MAX_TOOL_STRING_CHARS],
                "original_chars": len(serialized),
            }
        return {"status": "success", "value": bounded}

    @classmethod
    def bounded_json_value(cls, value: object) -> object:
        """Recursively truncate large strings and long lists.

        Args:
            value: JSON-like value to bound.

        Returns:
            Bounded JSON-like value.
        """

        if isinstance(value, str):
            if len(value) <= MAX_TOOL_STRING_CHARS:
                return value
            return f"{value[:MAX_TOOL_STRING_CHARS]}... [truncated chars={len(value)}]"
        if isinstance(value, dict):
            return {str(key): cls.bounded_json_value(item) for key, item in value.items()}
        if isinstance(value, list):
            bounded_items = [cls.bounded_json_value(item) for item in value[:MAX_TOOL_LIST_ITEMS]]
            if len(value) > MAX_TOOL_LIST_ITEMS:
                bounded_items.append({"truncated": True, "omitted_items": len(value) - MAX_TOOL_LIST_ITEMS})
            return bounded_items
        return value

    @staticmethod
    def compact_evidence_ref(ref: EvaluationEvidenceRef) -> JsonObject:
        """Return a small model-facing summary of one evidence ref.

        Args:
            ref: Evidence reference to compact.

        Returns:
            JSON object with stable evidence fields and compact metadata.
        """

        metadata = ref.metadata
        compact_metadata: JsonObject = {}
        for key in ("variant", "tool_name", "selected_image_kind", "source", "event"):
            value = metadata.get(key)
            if isinstance(value, (str, int, float, bool)):
                compact_metadata[key] = value
        return {
            "id": ref.id,
            "kind": ref.kind.value,
            "label": ref.label,
            "summary": ref.summary,
            "session_step": ref.session_step,
            "round_step": ref.round_step,
            "pinned": ref.pinned,
            "selection_reason": ref.selection_reason,
            "metadata": compact_metadata,
        }

    @classmethod
    def compact_a11y_snapshot(cls, snapshot: object) -> JsonObject:
        """Return a bounded preview of a persisted accessibility snapshot.

        Args:
            snapshot: Raw accessibility snapshot object.

        Returns:
            Compact JSON preview.
        """

        if not isinstance(snapshot, dict):
            return {"value": str(snapshot)}
        compact: JsonObject = {
            str(key): value
            for key, value in snapshot.items()
            if key != "tree" and isinstance(value, (str, int, float, bool, type(None)))
        }
        tree = snapshot.get("tree")
        if isinstance(tree, dict):
            compact["tree_preview"] = cls.compact_a11y_node(tree)
        return compact

    @classmethod
    def compact_a11y_node(cls, node: dict[str, object], *, depth: int = 0) -> JsonObject:
        """Return a bounded preview of an accessibility tree node.

        Args:
            node: Accessibility tree node.
            depth: Current recursion depth.

        Returns:
            Compact JSON preview of the node.
        """

        compact: JsonObject = {}
        for key in ("role", "name", "value", "text", "states", "actions"):
            value = node.get(key)
            if value in (None, "", [], {}):
                continue
            compact[key] = cls.bounded_json_value(value)
        if depth >= MAX_A11Y_TREE_DEPTH:
            children = node.get("children")
            child_count = len(children) if isinstance(children, list) else 0
            if child_count:
                compact["omitted_children"] = child_count
            return compact
        children = node.get("children")
        if isinstance(children, list) and children:
            child_nodes = [child for child in children if isinstance(child, dict)]
            compact["children"] = [
                cls.compact_a11y_node(child, depth=depth + 1) for child in child_nodes[:MAX_A11Y_CHILDREN]
            ]
            if len(child_nodes) > MAX_A11Y_CHILDREN:
                compact["omitted_children"] = len(child_nodes) - MAX_A11Y_CHILDREN
        return compact

    @staticmethod
    def image_size_error(path: Path) -> str:
        """Return a validation error for oversize images, or an empty string.

        Args:
            path: Image path to stat.

        Returns:
            Error string when invalid, otherwise an empty string.
        """

        try:
            size = path.stat().st_size
        except OSError:
            return "Image file could not be statted."
        if size > MAX_IMAGE_BYTES:
            return f"Image file is too large for evaluator tool inspection: {size} bytes."
        return ""

    @staticmethod
    def image_pixel_error(image: Image.Image) -> str:
        """Return a validation error for huge images, or an empty string.

        Args:
            image: Open PIL image to inspect.

        Returns:
            Error string when invalid, otherwise an empty string.
        """

        width, height = image.size
        if width <= 0 or height <= 0:
            return "Image dimensions are invalid."
        if width * height > MAX_IMAGE_PIXELS:
            return f"Image dimensions are too large for evaluator tool inspection: {width}x{height}."
        return ""


__all__ = ["ArtifactToolPayloadLimiter"]
