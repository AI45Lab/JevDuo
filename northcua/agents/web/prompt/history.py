"""Web-specific prompt history projection.

The generic AgentKit history replayer preserves rich tool JSON so desktop and
generic agents can replay exact prior observations. Web follows the same shape:
post-action observations live in tool results, and a separate window budgeter
trims older ref snapshots when the prompt budget is exceeded.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping

from nexau.core.messages import ImageBlock, TextBlock, ToolResultContentBlock, ToolUseBlock

from northcua.agentkit.prompt_history import ToolResultContentProjector, ToolUseBlockFactory
from northcua.core.models import AssistantToolCallRecord, ToolExecutionRecord

from .ref_window import REFS_INVALIDATED_NOTICE

_SOURCE_REF_RE = re.compile(r"@e\d+\b")


class WebHistoryToolUseBlockFactory(ToolUseBlockFactory):
    """Build assistant tool-use history with source-only refs redacted."""

    def build(self, tool_call: AssistantToolCallRecord) -> ToolUseBlock:
        raw_arguments = tool_call.arguments
        parsed_arguments = self._json_object_from_string(raw_arguments)
        return ToolUseBlock(
            id=tool_call.id,
            name=tool_call.name,
            input=self._strip_ref_arguments(parsed_arguments),
            raw_input=None,
        )

    def _strip_ref_arguments(self, value: object) -> dict[str, object]:
        sanitized = _sanitize_history_value(value)
        return sanitized if isinstance(sanitized, dict) else {}


class WebHistoryToolResultProjector(ToolResultContentProjector):
    """Project web tool outputs for desktop-like prompt replay."""

    def build_content(self, execution: ToolExecutionRecord) -> str | list[ToolResultContentBlock]:
        value = execution.llm_tool_output if execution.llm_tool_output is not None else execution.tool_output
        if value is None:
            return ""

        image_url = self.image_url(value)
        rendered_text = self.render_web_tool_output(value)
        if image_url:
            blocks: list[ToolResultContentBlock] = []
            if rendered_text:
                blocks.append(TextBlock(text=rendered_text))
            blocks.append(ImageBlock(url=image_url))
            return blocks
        if rendered_text:
            return rendered_text
        return self.text(self._compact_web_tool_output(value))

    def render_web_tool_output(self, value: object) -> str:
        """Return the model-visible replay text for one Web tool result.

        Runtime traces keep the full structured JSON for debugging, but the
        next model step should see the same compact observation surface as the
        leading user observation: action summary, current page metadata, refs,
        and an optional screenshot image block.
        """

        rendered_value = self._compact_web_tool_output(value)
        if not isinstance(rendered_value, Mapping):
            return self.text(rendered_value)
        parts: list[str] = []
        summary = self._summary_text(rendered_value)
        if summary:
            parts.append(summary)
        error_text = self._tool_error_text(rendered_value)
        if error_text:
            parts.append(error_text)
        observation = self._observation_payload(rendered_value)
        if isinstance(observation, Mapping):
            observation_text = WebToolResultObservationTextRenderer().render(observation)
            if observation_text:
                parts.append(observation_text)
        observation_error = rendered_value.get("observation_error")
        if isinstance(observation_error, Mapping):
            error_text = self._observation_error_text(observation_error)
            if error_text:
                parts.append(error_text)
            if observation_error.get("refs_invalidated") is True:
                parts.append(REFS_INVALIDATED_NOTICE)
        return "\n\n".join(part for part in parts if part)

    def _compact_web_tool_output(self, value: object) -> object:
        return _sanitize_history_value(value)

    def _summary_text(self, value: Mapping[str, object]) -> str:
        for key in self.summary_text_keys:
            candidate = value.get(key)
            if isinstance(candidate, str) and candidate.strip():
                result = self._prompt_significant_web_result(value)
                if result is None:
                    return candidate
                return f"{candidate}\nresult: {self.text(result)}"
        status = value.get("status")
        action = value.get("action")
        if isinstance(status, str) and isinstance(action, str):
            return f"{action} [{status}]"
        if isinstance(status, str):
            return f"Tool result [{status}]"
        if isinstance(action, str):
            return action
        return ""

    def _prompt_significant_web_result(self, value: Mapping[str, object]) -> object | None:
        result = value.get("result")
        if result is None or result == "" or result == [] or result == {}:
            return None
        if self._contains_observation_payload(value) or self._looks_like_snapshot_result(result):
            return None
        encoded = self.text(result)
        if len(encoded) > 1200:
            return {"summary": "[large result omitted from prompt replay]"}
        return result

    def _tool_error_text(self, value: Mapping[str, object]) -> str:
        if value.get("status") != "error":
            return ""
        lines: list[str] = []
        code = str(value.get("code") or "").strip()
        reason_code = str(value.get("reason_code") or "").strip()
        summary = str(value.get("concise_log_summary") or "").strip()
        hint = str(value.get("hint") or "").strip()
        if code:
            lines.append(f"- code: {code}")
        if reason_code and reason_code != code:
            lines.append(f"- reason_code: {reason_code}")
        if summary:
            lines.append(f"- summary: {summary}")
        if hint:
            lines.append(f"- hint: {hint}")
        return "Tool feedback:\n" + "\n".join(lines) if lines else ""

    def _observation_payload(self, value: Mapping[str, object]) -> Mapping[str, object] | None:
        observation = value.get("observation")
        if isinstance(observation, Mapping):
            return observation
        result = value.get("result")
        if isinstance(result, Mapping):
            nested = result.get("observation")
            if isinstance(nested, Mapping):
                return nested
        return None

    def _contains_observation_payload(self, value: Mapping[str, object]) -> bool:
        return self._observation_payload(value) is not None

    def _looks_like_snapshot_result(self, value: object) -> bool:
        if not isinstance(value, Mapping):
            return False
        if isinstance(value.get("snapshot"), str) and ("refs" in value or "ref_count" in value):
            return True
        annotation = value.get("annotation")
        return isinstance(annotation, Mapping) and bool(annotation)

    def _observation_error_text(self, value: Mapping[str, object]) -> str:
        code = str(value.get("code") or "post_action_observation_failed")
        hint = str(value.get("hint") or "").strip()
        error = str(value.get("error") or "").strip()
        lines = [f"Post-action observation error: {code}"]
        if error:
            lines.append(f"- error: {error}")
        if hint:
            lines.append(f"- hint: {hint}")
        return "\n".join(lines)


class WebToolResultObservationTextRenderer:
    """Render model-visible Web observation payloads as compact prompt text."""

    def render(self, observation: Mapping[str, object]) -> str:
        page = observation.get("page")
        snapshot = observation.get("snapshot")
        screenshot = observation.get("screenshot")
        page_payload = page if isinstance(page, Mapping) else {}
        snapshot_payload = snapshot if isinstance(snapshot, Mapping) else {}
        screenshot_payload = screenshot if isinstance(screenshot, Mapping) else {}

        title = str(page_payload.get("title") or "(untitled)")
        url = str(page_payload.get("url") or "(blank)")
        observation_id = str(observation.get("observation_id") or "").strip()
        settled = bool(observation.get("settled", True))
        settle_reason = str(observation.get("settle_reason") or "").strip()
        screenshot_kind = str(screenshot_payload.get("kind") or "none")
        image_attached = bool(screenshot_payload.get("image_attached", False))
        image_omitted_reason = str(screenshot_payload.get("image_omitted_reason") or "").strip()
        annotation = screenshot_payload.get("annotation")
        annotation_payload = annotation if isinstance(annotation, Mapping) else {}
        refs = snapshot_payload.get("refs")
        ref_items = refs if isinstance(refs, list) else []
        ref_count = self._int_value(snapshot_payload.get("ref_count"), default=len(ref_items))

        lines = [
            "Post-action page observation:",
            f"- title: {title}",
            f"- url: {url}",
        ]
        if observation_id:
            lines.append(f"- observation_id: {observation_id}")
        settled_line = f"- settled: {str(settled).lower()}"
        if settle_reason:
            settled_line = f"{settled_line} ({settle_reason})"
        lines.append(settled_line)
        screenshot_line = f"- screenshot: {screenshot_kind}; image_attached={str(image_attached).lower()}"
        if image_omitted_reason:
            screenshot_line = f"{screenshot_line}; image_omitted_reason={image_omitted_reason}"
        lines.append(screenshot_line)
        if screenshot_kind == "set_of_mark":
            lines.append("- SoM marks are numeric refs: use the visible number exactly with ref tools.")
        lines.append("")
        refs_title = "Actionable refs" if screenshot_kind == "set_of_mark" else "Observation refs"
        lines.append(f"{refs_title} ({ref_count}):")
        if ref_items:
            for item in ref_items:
                if isinstance(item, Mapping):
                    lines.append(self._ref_line(item))
        else:
            text = str(snapshot_payload.get("text") or "").strip()
            if text:
                lines.extend(text.splitlines())
            else:
                lines.append("- (none)")
        omitted_count = self._int_value(annotation_payload.get("omitted_count"))
        if omitted_count:
            suffix = " (truncated)" if bool(annotation_payload.get("truncated", False)) else ""
            lines.append(f"- omitted candidates: {omitted_count}{suffix}")
        return "\n".join(lines).strip()

    def _ref_line(self, ref: Mapping[str, object]) -> str:
        ref_id = str(ref.get("ref") or "").strip()
        role = str(ref.get("role") or "element").strip() or "element"
        label = str(ref.get("label") or ref.get("name") or ref.get("text") or "").strip()
        state = str(ref.get("state") or "").strip()
        suffix = f" {state}" if state else ""
        if label:
            return f'- {ref_id} {role} "{label}"{suffix}'
        return f"- {ref_id} {role}{suffix}"

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


def _sanitize_history_value(value: object) -> object:
    if isinstance(value, Mapping):
        out: dict[str, object] = {}
        for key, item in value.items():
            key_text = str(key)
            if key_text in ToolResultContentProjector.image_payload_keys:
                out[key_text] = item
                continue
            out[key_text] = _sanitize_history_value(item)
        return out
    if isinstance(value, list):
        return [_sanitize_history_value(item) for item in value]
    if isinstance(value, str):
        return _sanitize_history_text(value)
    return value


def _sanitize_history_text(text: str) -> str:
    return _SOURCE_REF_RE.sub("[historical source ref omitted]", text)
