"""Prompt text budgeting for historical web ref snapshots."""

from __future__ import annotations

import copy
import json
import re
from collections.abc import Mapping

from nexau.core.messages import Message, TextBlock, ToolResultBlock

from northcua.core.json_types import JsonObject

_SNAPSHOT_OMISSION_NOTICE = (
    "[historical observation only: its refs are expired and invalid. Use only refs from the latest retained "
    "tool-result observation, or call web_snapshot for fresh refs.]"
)
_HISTORICAL_VISUAL_REF_NOTICE = (
    "- Historical observation visual only: any visible numbers in this retained observation are expired and invalid "
    "for ref tools. Use only refs from the latest retained tool-result observation, or call web_snapshot."
)
_INITIAL_OBSERVATION_OMISSION_NOTICE = (
    "[initial page observation omitted from this replay turn because it is outside the web ref-snapshot window; "
    "use the latest retained tool-result observation for current page state and refs.]"
)
REFS_INVALIDATED_NOTICE = (
    "[current actionable refs invalidated: fresh page observation recovery failed. "
    "Call web_snapshot before using any ref.]"
)


class WebRefSnapshotWindowBudgeter:
    """Keep only the newest historical full ref snapshots in prompt replay."""

    def __init__(self, *, max_history_snapshots: int) -> None:
        self.max_history_snapshots = max(0, int(max_history_snapshots))

    def apply(self, messages: list[Message]) -> list[Message]:
        """Return copied messages with older historical snapshots compacted."""

        copied = copy.deepcopy(messages)
        remaining = min(self.max_history_snapshots, 1)
        for message in reversed(copied):
            changed = False
            content = list(message.content)
            for block_index in range(len(content) - 1, -1, -1):
                block = content[block_index]
                if not isinstance(block, ToolResultBlock):
                    continue
                if self._tool_result_invalidates_refs(block):
                    remaining = 0
                new_block, retained = self._budget_tool_result(block, remaining)
                if retained:
                    remaining -= 1
                if new_block is not block:
                    content[block_index] = new_block
                    changed = True
            for block_index, block in enumerate(content):
                if not isinstance(block, TextBlock):
                    continue
                new_text, retained, text_changed = self._budget_user_observation_text(block.text, remaining)
                if retained:
                    remaining -= 1
                if text_changed:
                    content[block_index] = TextBlock(text=new_text)
                    changed = True
                    break
            if changed:
                message.content = content
        return copied

    def latest_retained_tool_result_ids(self, messages: list[Message]) -> set[str]:
        """Return tool-result ids that still expose the latest actionable refs.

        Callers use this after ``apply`` to keep auxiliary visual evidence, such
        as Web SoM images, aligned with the same observation that owns the
        latest model-visible ref table.
        """

        retained: set[str] = set()
        for message in reversed(messages):
            for block in reversed(list(message.content)):
                if not isinstance(block, ToolResultBlock):
                    continue
                if self._tool_result_has_retained_refs(block):
                    return {str(block.tool_use_id)}
        return retained

    def _tool_result_invalidates_refs(self, block: ToolResultBlock) -> bool:
        content = block.content
        if isinstance(content, str):
            return REFS_INVALIDATED_NOTICE in content
        if isinstance(content, list):
            return any(isinstance(item, TextBlock) and REFS_INVALIDATED_NOTICE in item.text for item in content)
        return False

    def _tool_result_has_retained_refs(self, block: ToolResultBlock) -> bool:
        content = block.content
        if isinstance(content, str):
            return self._text_has_retained_refs(content)
        if isinstance(content, list):
            return any(isinstance(item, TextBlock) and self._text_has_retained_refs(item.text) for item in content)
        return False

    def _text_has_retained_refs(self, text: str) -> bool:
        if _SNAPSHOT_OMISSION_NOTICE in text or "Expired refs:" in text:
            return False
        if _HISTORICAL_VISUAL_REF_NOTICE in text:
            return False
        payload = self._json_payload(text)
        if isinstance(payload, dict):
            observation = payload.get("observation")
            if not isinstance(observation, dict):
                return False
            snapshot = observation.get("snapshot")
            has_snapshot_refs = isinstance(snapshot, dict) and bool(snapshot.get("refs") or snapshot.get("text"))
            return has_snapshot_refs or self._has_screenshot_annotation(observation)
        return self._looks_like_observation_text(text)

    def _budget_tool_result(self, block: ToolResultBlock, remaining: int) -> tuple[ToolResultBlock, bool]:
        if isinstance(block.content, list):
            content = list(block.content)
            changed = False
            retained = False
            found_snapshot = False
            local_remaining = remaining
            for index in range(len(content) - 1, -1, -1):
                item = content[index]
                if not isinstance(item, TextBlock):
                    continue
                new_text, has_snapshot, text_changed = self._budget_text(item.text, remaining=local_remaining)
                if not has_snapshot:
                    continue
                found_snapshot = True
                if local_remaining > 0 and not text_changed:
                    retained = True
                    local_remaining = 0
                    continue
                if text_changed:
                    content[index] = TextBlock(text=new_text)
                    changed = True
            if not found_snapshot:
                return block, False
            if not changed:
                return block, retained
            return block.model_copy(update={"content": content}), retained
        if isinstance(block.content, str):
            new_text, has_snapshot, changed = self._budget_text(block.content, remaining=remaining)
            if not has_snapshot:
                return block, False
            if not changed:
                return block, True
            return block.model_copy(update={"content": new_text}), False
        return block, False

    def _budget_user_observation_text(self, text: str, remaining: int) -> tuple[str, bool, bool]:
        if not self._looks_like_user_observation(text):
            return text, False, False
        if remaining > 0:
            return text, True, False
        return self._compact_initial_user_observation_text(text), False, True

    def _looks_like_user_observation(self, text: str) -> bool:
        return "Task:\n" in text and self._looks_like_observation_text(text)

    def _looks_like_observation_text(self, text: str) -> bool:
        legacy_observation = "Current page:" in text and (
            "Interactive refs (" in text
            or "Visible marked refs (" in text
            or "Snapshot refs (" in text
            or "Current actionable refs (" in text
            or "Current refs (" in text
        )
        return legacy_observation or self._looks_like_new_observation_text(text)

    def _looks_like_new_observation_text(self, text: str) -> bool:
        return (
            "Initial page observation:" in text
            or "Latest page observation:" in text
            or "Post-action page observation:" in text
            or "Page observation:" in text
        ) and (
            "Interactive refs (" in text
            or "Visible marked refs (" in text
            or "Snapshot refs (" in text
            or "Current actionable refs (" in text
            or "Current refs (" in text
            or "Actionable refs (" in text
            or "Observation refs (" in text
        )

    def _compact_user_observation_text(self, text: str) -> str:
        if self._looks_like_user_observation(text):
            return self._compact_initial_user_observation_text(text)
        return self._compact_ref_observation_text(text)

    def _compact_ref_observation_text(self, text: str) -> str:
        compacted = text
        # Legacy web traces used separate Snapshot and Set-of-Mark sections.
        # Keep those compaction patterns so old prompt history is still bounded.
        compacted = re.sub(
            r"(Snapshot:\n)(.*?)(\n\n(?:Set-of-Mark label map|Set-of-Mark refs|Interactive refs \(|Visible marked refs \(|Snapshot refs \(|Current actionable refs \(|Current refs \(|Actionable refs \(|Observation refs \())",
            r"\1[omitted by web ref-snapshot window]\3",
            compacted,
            count=1,
            flags=re.S,
        )
        compacted = re.sub(
            r"((?:Set-of-Mark label map|Set-of-Mark refs).*?)(\n\n(?:Interactive refs \(|Visible marked refs \(|Snapshot refs \(|Current actionable refs \(|Current refs \(|Actionable refs \(|Observation refs \())",
            r"Set-of-Mark refs: [omitted by web ref-snapshot window]\2",
            compacted,
            count=1,
            flags=re.S,
        )
        compacted = re.sub(
            r"((?:Interactive refs|Visible marked refs|Snapshot refs|Current actionable refs|Current refs|Actionable refs|Observation refs) \([^)]+\):\n).*",
            "Expired refs:\n- [omitted; use only refs from the latest retained tool-result observation]",
            compacted,
            count=1,
            flags=re.S,
        )
        compacted = self._expire_som_instruction(compacted)
        if _SNAPSHOT_OMISSION_NOTICE not in compacted:
            compacted = f"{compacted}\n\n{_SNAPSHOT_OMISSION_NOTICE}"
        return compacted

    def _expire_som_instruction(self, text: str) -> str:
        """Replace actionable historical SoM wording with an expired-ref notice."""

        return re.sub(
            r"^- SoM marks are numeric refs: use the visible number exactly with ref tools\.$",
            _HISTORICAL_VISUAL_REF_NOTICE,
            text,
            flags=re.M,
        )

    def _compact_initial_user_observation_text(self, text: str) -> str:
        marker_index = self._initial_observation_marker_index(text)
        if marker_index < 0:
            return self._compact_ref_observation_text(text)
        task_text = text[:marker_index].rstrip()
        lines = [task_text, "", _INITIAL_OBSERVATION_OMISSION_NOTICE]
        if _SNAPSHOT_OMISSION_NOTICE not in text:
            lines.extend(["", _SNAPSHOT_OMISSION_NOTICE])
        return "\n".join(lines)

    def _initial_observation_marker_index(self, text: str) -> int:
        candidates = [
            text.find("\n\nInitial page observation:"),
            text.find("\n\nCurrent page:"),
        ]
        present = [candidate for candidate in candidates if candidate >= 0]
        return min(present) if present else -1

    def _budget_text(self, text: str, *, remaining: int) -> tuple[str, bool, bool]:
        payload = self._json_payload(text)
        if not isinstance(payload, dict):
            if not self._looks_like_observation_text(text):
                return text, False, False
            if remaining > 0:
                return text, True, False
            return self._compact_user_observation_text(text), True, True
        observation = payload.get("observation")
        if not isinstance(observation, dict):
            return text, False, False
        snapshot = observation.get("snapshot")
        has_full_snapshot = isinstance(snapshot, dict) and bool(snapshot.get("text") or snapshot.get("refs"))
        has_som_annotation = self._has_screenshot_annotation(observation)
        if not has_full_snapshot and not has_som_annotation:
            return text, False, False
        if remaining > 0:
            return text, True, False
        compacted = dict(payload)
        compacted["observation"] = self._compact_observation(observation)
        compacted["snapshot_omitted_notice"] = _SNAPSHOT_OMISSION_NOTICE
        return json.dumps(compacted, ensure_ascii=False), True, True

    def _json_payload(self, text: str) -> JsonObject | None:
        stripped = text.strip()
        if not stripped.startswith("{"):
            return None
        try:
            payload = json.loads(stripped)
        except json.JSONDecodeError:
            return None
        return payload if isinstance(payload, dict) else None

    def _compact_observation(self, observation: Mapping[str, object]) -> JsonObject:
        compact = {str(key): value for key, value in observation.items()}
        snapshot = compact.get("snapshot")
        if isinstance(snapshot, dict):
            compact["snapshot"] = {
                "text": "[omitted by web ref-snapshot window]",
                "refs": [],
                "ref_count": self._int_value(snapshot.get("ref_count")),
            }
        screenshot = compact.get("screenshot")
        if isinstance(screenshot, dict):
            compact["screenshot"] = self._compact_screenshot_annotation(screenshot)
        return compact

    def _has_screenshot_annotation(self, observation: Mapping[str, object]) -> bool:
        screenshot = observation.get("screenshot")
        if not isinstance(screenshot, dict):
            return False
        annotation = screenshot.get("annotation")
        if not isinstance(annotation, dict):
            return False
        marks = annotation.get("marks")
        return bool(marks)

    def _compact_screenshot_annotation(self, screenshot: Mapping[str, object]) -> JsonObject:
        """Remove actionable historical SoM marks from compacted JSON observations."""

        compact = {str(key): value for key, value in screenshot.items()}
        annotation = compact.get("annotation")
        if isinstance(annotation, dict):
            compact["annotation"] = {
                "kind": annotation.get("kind") or "set_of_mark",
                "marked_count": self._int_value(annotation.get("marked_count")),
                "visible_interactive_count": self._int_value(annotation.get("visible_interactive_count")),
                "omitted_count": self._int_value(annotation.get("omitted_count")),
                "truncated": bool(annotation.get("truncated", False)),
                "marks": [],
                "notice": _SNAPSHOT_OMISSION_NOTICE,
            }
        return compact

    def _int_value(self, value: object) -> int:
        if isinstance(value, bool) or value is None:
            return 0
        if isinstance(value, int):
            return value
        if isinstance(value, float):
            return int(value)
        if isinstance(value, str):
            try:
                return int(value)
            except ValueError:
                return 0
        return 0


def snapshot_omission_notice() -> str:
    """Return the prompt-visible historical snapshot omission notice."""

    return _SNAPSHOT_OMISSION_NOTICE


def initial_observation_omission_notice() -> str:
    """Return the prompt-visible initial observation omission notice."""

    return _INITIAL_OBSERVATION_OMISSION_NOTICE
