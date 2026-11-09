"""Capability-aware local choices with uniquely bound text and submission gates."""

from __future__ import annotations

import re

from .bindings import LocalBindings, normalized
from .operations import date_keys
from .targets import ActionCandidate

_COMMIT = re.compile(r"^(send|submit|publish)(?:\b|$)", re.I)


def is_commit(label, commit_targets, target_id=""):
    # Save As / Upload can OPEN a chooser; only the planner can mark those as final commit controls.
    return (
        bool(target_id and target_id in commit_targets)
        or bool(_COMMIT.search(label.strip()))
        or (normalized(label) in {normalized(name) for name in commit_targets})
    )


def local_candidates(
    goal,
    targets,
    *,
    inputs=(),
    visual_targets=(),
    allowed_keys=(),
    reactive=False,
    bindings=None,
    commit_targets=(),
    screen_size=None,
    operation_kind="",
    command_keys=(),
    file_paths=(),
    drag_path=(),
    control_hint="",
    control_id="",
):
    """Input text is planner-owned; only observed capabilities become physical actions."""

    def action(name, operation, description, commands=(), target_id=None, **metadata):
        return ActionCandidate(
            name,
            operation,
            description,
            target_id=target_id,
            source="local_executor",
            metadata={"commands": list(commands), **metadata},
        )

    if bindings is None:
        bindings = LocalBindings(inputs)
        bindings.observe(targets, "")
    pending = bool(bindings.pending)
    bound = {t.id for t in bindings.targets.values()}
    specific_control = operation_kind == "controls" and bool(control_hint or control_id or visual_targets)
    if reactive:
        candidates = [
            action(
                "observe",
                "wait",
                "Do not press a key yet; inspect the next fresh frame.",
                [("wait", {"ms": 50})],
            )
        ]
    else:
        words = set(re.findall(r"\w+", goal.lower()))
        ordered = sorted(
            targets,
            key=lambda t: len(
                words & set(re.findall(r"\w+", " ".join((t.name, t.description, *t.parent_context)).lower()))
            ),
            reverse=True,
        )
        candidates = []
        for target in ordered:
            if specific_control:
                labels = {normalized(control_hint)} if control_hint else {
                    normalized(v["label"]) for v in visual_targets}
                if control_id and target.id != control_id:
                    continue
                if not control_id and normalized(target.name) not in labels:
                    continue
            if (
                None in (target.x, target.y, target.width, target.height)
                or min(target.width, target.height) <= 0
            ):
                continue
            cx, cy = target.x + target.width // 2, target.y + target.height // 2
            if screen_size and not (0 <= cx < screen_size[0] and 0 <= cy < screen_size[1]):
                continue
            if (
                target.source == "dom"
                and screen_size
                and len(target.name) > 512
                and (target.width * target.height > screen_size[0] * screen_size[1] / 2)
            ):
                continue  # A page-sized text ancestor is not an independent control.
            if operation_kind in {"fill", "keys", "files", "drag"}:
                continue
            if operation_kind == "select" and normalized(target.name) not in {
                normalized(item["text"]) for item in bindings.inputs
            }:
                continue
            point = {"pixel_point": [cx, cy]}
            label = f'{target.role} "{target.name}" in {" > ".join(target.parent_context[-2:])}'
            commit = is_commit(target.name, commit_targets, target.id)
            if target.id in bound or pending and commit:
                continue
            for operation in ("click", "double_click", "toggle", "expand", "collapse"):
                if operation not in target.operations:
                    continue
                verb = operation
                if operation == "toggle":
                    if target.checked is None:
                        continue
                    verb = "Uncheck" if target.checked else "Check"
                command = "double_click" if operation == "double_click" else "click"
                candidates.append(
                    action(
                        operation + ":" + target.id,
                        operation,
                        f"{verb.capitalize()} {label}.",
                        [(command, point)],
                        semantic_key=target.semantic_key,
                        target_id=target.id,
                        commit=commit,
                        one_shot=specific_control,
                        **({"input_index": 0} if operation_kind == "select" else {}),
                    )
                )
            if len(candidates) >= 40:
                break
        for index, target in bindings.targets.items():
            row, item = bindings.rows[index], bindings.inputs[index]
            if row["status"] in {"verified", "readback_mismatch", "written", "unobservable_after_write"}:
                continue
            point = {"pixel_point": [target.x + target.width // 2, target.y + target.height // 2]}
            if screen_size and not (0 <= point["pixel_point"][0] < screen_size[0]
                                    and 0 <= point["pixel_point"][1] < screen_size[1]):
                continue
            commands = [
                ("click", point),
                ("keypress", {"key": "ctrl+a"}),
                ("type", {"text": item["text"], "target_kind": item.get("target_kind", "editable")}),
            ]
            if item.get("kind") == "select":
                commands = [("click", point), ("keypress", {"keys": list(item["text"]) + ["enter"]})]
            elif target.input_type == "date":
                sequence = date_keys(target, item["text"])
                if sequence is None:
                    continue
                commands = [
                    ("click", {"pixel_point": [target.x + 8, target.y + target.height // 2]}),
                    ("keypress", {"keys": ["home", *sequence]}),
                ]
            candidates.insert(
                0,
                action(
                    f"type:{target.id}:{index}",
                    "type_text",
                    f"{'Select' if item.get('kind') == 'select' else 'Replace'} "
                    f'{target.role} "{target.name}" in {" > ".join(target.parent_context[-2:])} '
                    f"with exactly {item['text']!r}. Current value: {target.value!r}.",
                    commands,
                    semantic_key=target.semantic_key,
                    expected_value=item["text"],
                    field=normalized(item["field"]),
                    input_index=index,
                    target_id=target.id,
                ),
            )
        candidates.extend(
            [
                action(
                    "scroll_down",
                    "scroll",
                    "Scroll the foreground content down to reveal more controls.",
                    [("scroll", {"delta_y": -4})],
                ),
                action(
                    "scroll_up",
                    "scroll",
                    "Scroll the foreground content up to reveal earlier controls.",
                    [("scroll", {"delta_y": 4})],
                ),
                action(
                    "observe",
                    "wait",
                    "Wait briefly for the current interface to finish updating.",
                    [("wait", {"ms": 300})],
                ),
            ]
        )
    for index, target in enumerate(visual_targets):
        if operation_kind in {"keys", "files", "drag"}:
            continue
        if pending and is_commit(target["label"], commit_targets):
            continue
        operation = target.get("operation", "click")
        if operation_kind == "fill" and bindings.inputs and not bindings.targets:
            item = bindings.inputs[0]
            if normalized(target["label"]) != normalized(item.get("target_hint") or item["field"]):
                continue
            candidates.insert(
                0,
                action(
                    f"visual_type:{index}",
                    "type_text",
                    f"Replace the screenshot-grounded field {target['label']!r} "
                    f"with exactly {item['text']!r}.",
                    [
                        ("click", {"x": target["x"], "y": target["y"]}),
                        ("keypress", {"key": "ctrl+a"}),
                        ("type", {"text": item["text"], "target_kind": item.get("target_kind", "editable")}),
                    ],
                    semantic_key="visual_input:" + normalized(target["label"]),
                    input_index=0,
                ),
            )
            continue
        command = "click" if operation == "right_click" else operation
        candidates.insert(
            index,
            action(
                f"visual:{index}",
                operation,
                f"{operation} the screenshot-grounded {target['label']!r}.",
                [
                    (
                        command,
                        {
                            "x": target["x"],
                            "y": target["y"],
                            **({"button": "right"} if operation == "right_click" else {}),
                        },
                    )
                ],
                semantic_key="visual:" + normalized(target["label"]),
                commit=is_commit(target["label"], commit_targets),
                one_shot=specific_control,
            ),
        )
    if command_keys:
        candidates.insert(
            0,
            action(
                "specified_keys",
                "keypress",
                f"Execute the specified keys: {list(command_keys)!r}.",
                [("keypress", {"keys": list(command_keys)})],
                one_shot=True,
                semantic_key="keys:" + repr(command_keys),
            ),
        )
    if file_paths:
        names = " ".join('"' + path + '"' for path in file_paths) if len(file_paths) > 1 else file_paths[0]
        candidates.insert(
            0,
            action(
                "choose_files",
                "type_text",
                "In the foreground file chooser, select exactly these paths using its location entry: "
                f"{list(file_paths)!r}.",
                [
                    ("keypress", {"key": "ctrl+l"}),
                    ("type", {"text": names, "target_kind": "editable"}),
                    ("keypress", {"key": "enter"}),
                ],
                one_shot=True,
                semantic_key="files:" + repr(file_paths),
            ),
        )
    if drag_path:
        candidates.insert(
            0,
            action(
                "specified_drag",
                "drag",
                "Drag along the supplied screenshot-grounded path.",
                [("drag", {"path": list(drag_path)})],
                one_shot=True,
                semantic_key="drag:" + repr(drag_path),
            ),
        )
    candidates.extend(
        action(
            "key:" + key,
            "keypress",
            f"Press {key} using the normal keyboard control.",
            [("keypress", {"key": key})],
        )
        for key in (() if specific_control or operation_kind in {
            "fill", "select", "keys", "files", "drag"} else allowed_keys)
        if not pending or key.casefold() not in {"enter", "ctrl+enter", "ctrl+s"}
    )
    if not pending and not specific_control and operation_kind not in {"keys", "files", "drag"}:
        candidates.append(
            action(
                "done", "done", "The visible local goal is complete; return evidence for independent review."
            )
        )
    candidates.append(
        action(
            "blocked",
            "return_to_planner",
            "The needed target, verified field value or business information is missing; return the blocker.",
        )
    )
    return candidates
