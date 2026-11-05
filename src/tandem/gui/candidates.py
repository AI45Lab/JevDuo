"""UITarget → ActionSpace 候选构建；从旧仓库 gui/action_candidate_builder.py 按契约移植。"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from typing import Any

from .targets import UITarget

EDITABLE_ROLES = {
    "textbox",
    "text",
    "entry",
    "searchbox",
    "spin_button",
    "password_text",
}
DOUBLE_CLICK_ROLES = {"icon", "list_item", "tree_item", "table_cell", "grid_cell"}
ROLE_GROUPS = {
    "editable": EDITABLE_ROLES,
    "textbox": EDITABLE_ROLES,
    "terminal": EDITABLE_ROLES,
}

OPERATION_DESCRIPTIONS = {
    "drag": "The next useful step is to drag an observed visual object to a screenshot-grounded destination.",
    "click": "The next useful step is to activate one observed semantic control.",
    "type_text": "The next useful step is to insert or replace text in an observed editable field or confirmed focused input required by the local goal.",
    "double_click": "The next useful step is to open one observed file or list item with a double click.",
    "set_slider": "The next useful step is to set a visible slider to the required value.",
    "keypress": "The next useful step is one supported keyboard navigation or submission command, not text replacement.",
    "scroll": "Scroll within the foreground view to reveal more controls or content.",
    "wait": "Wait briefly, then observe the current UI again.",
    "done": "Every requirement of the current local GUI goal is visibly satisfied.",
    "visual_fallback": "The required interaction depends on visual or spatial information not represented by the semantic targets.",
    "return_to_planner": "The current UI state contradicts the assumptions of the current stage and requires replanning.",
}


@dataclass(slots=True)
class ActionCandidate:
    id: str
    operation: str
    description: str
    target_id: str | None = None
    source: str = "a11y"
    metadata: dict = field(default_factory=dict)

    def compact(self) -> dict:
        return {key: value for key, value in asdict(self).items() if value not in (None, "", {})}


@dataclass(slots=True)
class ActionSpace:
    targets: list[UITarget]
    target_heads: dict[str, list[ActionCandidate]]
    controls: dict[str, ActionCandidate]

    def candidates(self) -> list[ActionCandidate]:
        return [candidate for candidates in self.target_heads.values() for candidate in candidates] + list(
            self.controls.values()
        )

    def operations(self) -> dict[str, str]:
        values = {
            operation: OPERATION_DESCRIPTIONS[operation]
            for operation, candidates in self.target_heads.items()
            if candidates
        }
        values.update({operation: candidate.description for operation, candidate in self.controls.items()})
        return values

    def compact(self) -> dict:
        return {
            "operations": self.operations(),
            "target_heads": {
                operation: [candidate.compact() for candidate in candidates]
                for operation, candidates in self.target_heads.items()
            },
            "controls": {key: value.compact() for key, value in self.controls.items()},
        }


class ActionCandidateBuilder:
    def build(
        self,
        targets: list[UITarget],
        max_candidates: int = 96,
        action_inputs: list[dict[str, Any]] | None = None,
        context: str = "",
    ) -> ActionSpace:
        context_words = set(re.findall(r"[a-z0-9]+", context.lower()))
        navigation_requested = bool(re.search(
            r"\b(sidebar|side bar|navigation (menu|panel|bar))\b|侧栏|导航栏", context, re.I))
        hint_words = {
            word
            for item in action_inputs or []
            for word in re.findall(
                r"[a-z0-9]+",
                str((item.get("target_hint") or {}).get("name_contains") or "").lower(),
            )
        }

        def relevance(target: UITarget) -> tuple[int, float, int]:
            words = set(re.findall(r"[a-z0-9]+", target.name.lower()))
            contextual_words = set(
                re.findall(r"[a-z0-9]+", " ".join(
                    (target.name, *target.parent_context, target.nearby_text)).lower())
            )
            return (
                int(navigation_requested and any(
                    "navigation" in parent.lower() for parent in target.parent_context)),
                len(words & hint_words) * 4
                + len(words & context_words) / max(1, len(words)) * 8
                + min(2, len(contextual_words & context_words)),
                -targets.index(target),
            )

        def passive_navigation_text(target: UITarget) -> bool:
            return (navigation_requested and target.scope == "web_content"
                    and target.role in {"text", "label", "static", "table_cell", "grid_cell"}
                    and "click" not in target.operations)

        close_browser_requested = any(
            phrase in context.lower()
            for phrase in (
                "close the browser",
                "close chrome",
                "close browser window",
                "close the tab",
                "close current tab",
                "close tab",
                "close window",
            )
        )
        click_targets = sorted(
            (
                target
                for target in targets
                if not passive_navigation_text(target)
                and (close_browser_requested or not (
                    target.name.lower() == "close"
                    and any("google chrome" in item.lower() for item in target.parent_context)
                    and (
                        len(target.parent_context) <= 2 or "memory usage" in target.parent_context[-1].lower()
                    )
                ))
            ),
            key=relevance,
            reverse=True,
        )[:max_candidates]
        editable_targets = [target for target in targets if target.role in EDITABLE_ROLES]
        double_targets = sorted(
            [target for target in targets if target.role in DOUBLE_CLICK_ROLES
             and not passive_navigation_text(target)],
            key=relevance, reverse=True
        )[:4]

        def candidates(operation: str, values: list[UITarget], description: str) -> list[ActionCandidate]:
            return [
                ActionCandidate(
                    id=f"{operation}:{target.id}",
                    operation=operation,
                    target_id=target.id,
                    description=description.format(role=target.role.replace("_", " "), name=target.name)
                                + (f" Context: {target.nearby_text}" if target.nearby_text else ""),
                    source=target.source,
                    metadata={"semantic_key": target.semantic_key},
                )
                for target in values[:max_candidates]
            ]

        type_candidates = (
            candidates(
                "type_text",
                editable_targets,
                'Replace text in the {role} "{name}".',
            )
            if action_inputs is None
            else []
        )
        for target in editable_targets:
            for action_input in action_inputs or []:
                if action_input.get("kind") != "text":
                    continue
                hint = action_input.get("target_hint") or {}
                role = str(hint.get("role") or "").replace("-", "_").lower()
                name = str(hint.get("name_contains") or "").lower()
                accepted_roles = ROLE_GROUPS.get(role, {role})
                if role and target.role not in accepted_roles:
                    continue
                if name and name not in target.name.lower():
                    continue
                input_id = str(action_input.get("input_id") or len(type_candidates))
                value = str(action_input.get("value") or "")
                if not value:
                    continue
                type_candidates.append(
                    ActionCandidate(
                        id=f"type_text:{target.id}:{input_id}",
                        operation="type_text",
                        target_id=target.id,
                        description=(
                            f'Replace text in the {target.role.replace("_", " ")} '
                            f'"{target.name}" with {value!r}.'
                        ),
                        source=target.source,
                        metadata={
                            "semantic_key": target.semantic_key,
                            "input_id": input_id,
                            "text": value,
                        },
                    )
                )
        type_candidates = type_candidates[:6]
        typed_target_ids = {candidate.target_id for candidate in type_candidates}
        click_targets = [target for target in click_targets if target.id not in typed_target_ids]

        keypresses = [
            ("KEYPRESS_ENTER", "Press ENTER to submit or activate the focused control."),
            ("KEYPRESS_ESCAPE", "Press ESCAPE to dismiss the current popup or mode."),
            ("KEYPRESS_TAB", "Press TAB to move focus to the next control."),
        ]
        lowered = context.lower()
        if "terminal" in lowered:
            keypresses.append(("KEYPRESS_CTRL_ALT_T", "Press CTRL+ALT+T to open a terminal window."))
        if any(word in lowered for word in ("browser", "chrome", "address", "url")):
            keypresses.append(("KEYPRESS_CTRL_L", "Press CTRL+L to focus the browser address bar."))
        if any(word in lowered for word in ("find", "search")):
            keypresses.append(("KEYPRESS_CTRL_F", "Press CTRL+F to open in-page search."))
        if any(word in lowered for word in ("close", "dismiss", "window", "dialog")):
            keypresses.append(("KEYPRESS_ALT_F4", "Press ALT+F4 to close the active window or dialog."))
        if any(word in lowered for word in ("slide", "presentation")):
            keypresses.extend(
                [
                    ("KEYPRESS_PAGE_DOWN", "Press PAGE DOWN to advance in a presentation or document."),
                    ("KEYPRESS_PAGE_UP", "Press PAGE UP to go back in a presentation or document."),
                ]
            )

        target_heads = {
            "click": candidates(
            "click",
            click_targets,
            'Click the visible {role} "{name}".',
            ),
            "type_text": type_candidates,
            "double_click": candidates(
            "double_click",
            double_targets,
            'Double-click the visible {role} "{name}".',
            ),
            "keypress": [
                ActionCandidate(id=choice_id, operation="keypress", description=description, source="global")
                for choice_id, description in keypresses
            ],
            "scroll": [
                ActionCandidate(
                    id="SCROLL_UP", operation="scroll", description="Scroll up.", source="global"
                ),
                ActionCandidate(
                    id="SCROLL_DOWN", operation="scroll", description="Scroll down.", source="global"
                ),
            ],
        }
        controls = {
            operation: ActionCandidate(
                id=operation.upper(),
                operation=operation,
                description=description,
                source="global",
            )
            for operation, description in OPERATION_DESCRIPTIONS.items()
            if operation not in target_heads
        }
        return ActionSpace(targets=targets, target_heads=target_heads, controls=controls)
