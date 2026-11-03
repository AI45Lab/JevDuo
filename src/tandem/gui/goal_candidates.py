"""Current-observation action space for a whole GUI subgoal; no planner click whitelist."""

import json
import re
import xml.etree.ElementTree as ET
from datetime import date
from pathlib import Path
from urllib.parse import unquote, urlsplit

from .candidates import ActionCandidate
from .focus import KEYS, input_and_menu_context
from .targets import _attribute, _local, _pair, _state


def matches_field(hint, label):
    """Match an intended field to its observed label, not an arbitrary focused control."""
    def words(value):
        return set(re.findall(r"\w+", value.casefold())) - {"the", "a", "field", "required"}

    observed = words(label)
    intended = words(hint)
    return (bool(observed) and (observed <= intended or len(intended) >= 2 and intended <= observed)
            and not label.startswith(("Unnamed ", "Focused ")))


def launcher_input(value):
    """A launcher search is not a website's identically named Search field."""
    return (value.get("scope") == "desktop_launcher"
            or "desktop search" in value.get("target_hint", value["label"]).casefold())


def _input_target(value, targets):
    """Bind to one best observed field; spatial hints must not override its own label."""
    scores = []
    for target in targets:
        choice = value.get("kind") == "choice" and target.role in {
            "button", "push_button", "radio_button", "option", "list_item"}
        editable = (target.role in {"textbox", "entry", "searchbox", "spin_button"}
                    or launcher_input(value) and target.role == "text" and target.focused
                    or "type_text" in target.operations or target.input_type == "select-one")
        if not (editable or choice):
            continue
        if launcher_input(value) and target.application != "gnome-shell":
            continue
        if (target.role == "spin_button" and target.input_type != "date"
                and re.fullmatch(r"\d{4}-\d{2}-\d{2}", value["text"])):
            continue  # A complete date is not a Day/Month/Year segment or an ordinary numeric spinner.
        named = bool(target.name) and not target.name.startswith(("Unnamed ", "Focused "))
        labels = [target.name] if named else [target.description]
        if not named and len(target.nearby_text) <= 80 and "\n" not in target.nearby_text:
            labels.append(target.nearby_text)
        score = 0
        if choice:
            wanted = set(re.findall(r"\w+", value["text"].casefold()))
            option_name = re.sub(r"\([^)]*\)", "", target.name.casefold())
            observed = set(re.findall(r"\w+", option_name))
            hint = set(re.findall(r"\w+", value.get("target_hint", value["label"]).casefold()))
            if wanted and wanted <= observed <= wanted | hint:
                score = 6  # Match a specific option, never a container holding the entire form.
        for label in labels:
            if not label or not editable:
                continue
            hint = value.get("target_hint", value["label"])
            if not matches_field(hint, label):
                continue  # Descriptive context words cannot override the explicit intended field.
            ignored = {"brief", "required", "enter", "input", "field", "the", "a"}
            if (set(re.findall(r"\w+", hint.casefold())) - ignored
                    == set(re.findall(r"\w+", label.casefold())) - ignored):
                score = max(score, 8)
            elif matches_field(value["label"], label):
                score = max(score, 4)
            elif matches_field(hint, label):
                score = max(score, 3)
        if score:
            scores.append((score, target))
    best = max((score for score, _ in scores), default=0)
    matching = [target for score, target in scores if score == best]
    return matching[0] if len(matching) == 1 else None


def input_satisfied(value, target):
    """Chrome may omit a URL scheme in the displayed address, unlike ordinary text fields."""
    text = value["text"]
    if value.get("kind") == "choice" and target.role in {
            "button", "push_button", "radio_button", "option", "list_item"}:
        return target.checked is True or target.selected is True
    if target.input_type == "select-one":
        return target.value.strip().casefold() == text.strip().casefold()
    if ("address" in target.name.casefold() and "://" not in target.value
            and re.match(r"^(?:https?|file)://", text)):
        text = text.split("://", 1)[1]
    return target.value == text or value["input_mode"] == "insert" and text in target.value


def input_frontier(space, targets, inputs):
    """Expose actionable supplied inputs and missing required values on the current page."""
    fields = list(targets)
    pending = [c for c in space.candidates() if c.operation == "type_text"]
    inputs = [v for v in inputs if not launcher_input(v) or _input_target(v, fields) is not None]
    bindings = [(v, _input_target(v, fields)) for v in inputs]
    missing = [t.name for t in fields if (t.role in {"entry", "textbox", "text", "spin_button"}
               or t.input_type == "select-one") and not t.value
               and re.search(r"\brequired\b|\*", t.name + " " + t.description, re.I)
               and not any(bound is t for _, bound in bindings)]
    unbound = [v["label"] for v, target in bindings if target is None]
    conflicts = [t.name for t in fields
                 if len({v["text"] for v, bound in bindings if bound is t}) > 1]
    for target in fields:
        if target.input_type == "date" and any(
                bound is target and not re.fullmatch(r"\d{4}-\d{2}-\d{2}", v["text"])
                for v, bound in bindings):
            conflicts.append(target.name + " requires ISO YYYY-MM-DD")
    navigation = next((t for t in fields if t.focused and "address" in t.name.casefold()
                       and any(bound is t and input_satisfied(v, t)
                               and re.match(r"^(?:https?|file)://", v["text"])
                               for v, bound in bindings)), None)
    if pending:
        # Keep recovery/navigation choices, but do not advance past a currently unfilled supplied field.
        space.controls.pop("done", None)
        space.target_heads["click"] = [c for c in space.target_heads["click"] if not re.fullmatch(
            r"(?:next(?: step)?|continue|submit(?: claim| form)?)", next(
                (t.name for t in targets if t.id == c.target_id), ""), re.I)]
    typed_ids = {c.target_id for c in pending if c.target_id}
    if navigation:
        typed_ids.add(navigation.id)
        space.controls.pop("done", None)
    # Type actions already focus their own observed field; a separate click adds no prerequisite.
    space.target_heads["click"] = [c for c in space.target_heads["click"] if c.target_id not in typed_ids]
    satisfied = [v["label"] for v, target in bindings
                 if target is not None and input_satisfied(v, target)]
    return {"next_intent": ("Press ENTER to navigate to the supplied URL in the focused address bar."
                            if navigation else pending[0].description if pending else ""),
            "missing_required_fields": missing, "unbound_inputs": unbound,
            "conflicting_fields": conflicts, "satisfied_inputs": satisfied}


def scroll_point(tree, viewport):
    """Anchor wheel events inside the observed foreground window, not the desktop center."""
    windows = []
    for element in ET.fromstring(tree or "<empty/>").iter():
        if _local(element.tag) not in {"frame", "dialog"} or _state(element, "active") is not True:
            continue
        position = _pair(_attribute(element, "screencoord"))
        size = _pair(_attribute(element, "size"))
        if position and size and size[0] > 0 and size[1] > 0:
            windows.append((*position, *size))
    x, y, width, height = windows[-1] if windows else (0, 0, *viewport)
    return (max(0, min(viewport[0] - 1, x + width * 3 // 5)),
            max(0, min(viewport[1] - 1, y + height // 2)))


def _live_dialog(node, root):
    """Unknown visibility on a retained portal node is not proof of a blocking dialog."""
    if (_state(node, "modal") is not True or _state(node, "visible") is False
            or _state(node, "showing") is False):
        return False
    positioned = any(_pair(_attribute(e, "screencoord")) and
                     (size := _pair(_attribute(e, "size"))) and min(size) > 1 for e in node.iter())
    return bool(positioned and (_state(node, "showing") is True
                or _state(node, "active") is True
                or node.get("name") == root.get("foreground_title")))


def _keys(candidate):
    return candidate.metadata.get("key_sequence") or (
        [list(KEYS[candidate.id])] if candidate.id in KEYS else None)


def portal_needs_grounding(tree):
    """Retained coordinate-free portal nodes require screenshot confirmation, not background clicks."""
    root = ET.fromstring(tree or "<empty/>")
    return next((node.get("name", "") for app in root.iter() if "desktop-portal" in app.get("name", "")
                 for node in app.iter() if _local(node.tag) == "dialog" and len(node) > 0
                 and _state(node, "modal") is True and _state(node, "visible") is not False
                 and _state(node, "showing") is not False and not _live_dialog(node, root)), "")


def foreground_tree(raw_tree):
    """GNOME's visible app menu disambiguates stale ACTIVE flags on background windows."""
    root = ET.fromstring(raw_tree)
    apps = list(root)
    shell = next((a for a in apps if a.get("name") == "gnome-shell"), None)
    if shell is None:
        return raw_tree
    overview = [e for e in shell.iter() if e.get("name") == "Overview"]
    search_visible = any(
        _state(e, "editable") is True and _state(e, "showing") is True
        and _pair(_attribute(e, "screencoord")) and _pair(_attribute(e, "size"))
        for panel in overview for e in panel.iter())
    if search_visible:
        # X11 still names the old window while GNOME's full-screen overview owns input.
        for app in apps:
            if app is not shell:
                root.remove(app)
        shell.set("active", "true")
        root.set("foreground_title", "GNOME Overview")
        root.set("foreground_class", '["gnome-shell"]')
        return ET.tostring(root, encoding="unicode")
    # A portal dialog belongs to a separate process; stale X11 focus must not discard it.
    portals = [(app, node) for app in apps if "desktop-portal" in app.get("name", "")
               for node in app.iter() if _local(node.tag) == "dialog"
               and _live_dialog(node, root)]
    if portals:
        app, dialog = portals[-1]
        app.set("active", "true")
        root.set("foreground_title", dialog.get("name", ""))
        root.set("foreground_class", json.dumps([app.get("name", "")]))
    if root.get("foreground_class"):
        def normalize(value):
            return re.sub(r"[^a-z0-9]", "", value.casefold())

        def active(element, value):
            key = next((k for k in element.attrib if _local(k) == "active"), "active")
            element.set(key, "true" if value else "false")

        classes = [normalize(v) for v in json.loads(root.get("foreground_class")) if v]
        title = root.get("foreground_title", "")
        for app in apps:
            matching = (any(normalize(app.get("name", "")).endswith(c) for c in classes)
                        or bool(title) and any(e.get("name") == title for e in app.iter()
                                              if _local(e.tag) in {"frame", "dialog"}))
            uncertain_portal = "desktop-portal" in app.get("name", "") and portal_needs_grounding(
                ET.tostring(app, encoding="unicode"))
            if app is not shell and not matching and not uncertain_portal:
                root.remove(app)
            elif app is not shell:
                windows = [e for e in app.iter() if _local(e.tag) in {"frame", "dialog"}]
                named = [e for e in windows if title and e.get("name") == title]
                if named:
                    active(app, False)
                    for window in windows:
                        active(window, window in named)
        return ET.tostring(root, encoding="unicode")
    names = []
    for element in shell.iter():
        attrs = {k.rsplit("}", 1)[-1]: v for k, v in element.attrib.items()}
        position = re.findall(r"-?\d+", attrs.get("screencoord", ""))
        if (element.tag.rsplit("}", 1)[-1] == "menu" and attrs.get("name")
                and attrs.get("showing") == "true" and len(position) == 2
                and 0 <= int(position[1]) < 32):
            names.append(attrs["name"].casefold())
    matches = [app for app in apps if app is not shell and any(
        name in e.get("name", "").casefold() for e in app.iter()
        if e.tag.rsplit("}", 1)[-1] in {"frame", "dialog"} for name in names)]
    if len(matches) != 1:
        return raw_tree
    for app in apps:
        if app is not shell and app is not matches[0]:
            root.remove(app)
    return ET.tostring(root, encoding="unicode")


def goal_candidates(space, targets, tree, inputs, viewport=(1920, 1080)):
    _, menus = input_and_menu_context(tree or "<empty/>")
    fields = list(targets)
    bindings = [_input_target(value, fields) for value in inputs]
    typed = []
    for index, value in enumerate(inputs):
        hint = value.get("target_hint", value["label"])
        target = bindings[index]
        if target is None:
            continue  # Ambiguous/unlabelled fields need an observed click or visual grounding first.
        if len({v["text"] for v, bound in zip(inputs, bindings, strict=True) if bound is target}) > 1:
            continue
        if input_satisfied(value, target):
            continue
        if value.get("kind") == "choice" and target.role in {
                "button", "push_button", "radio_button", "option", "list_item"}:
            space.target_heads["click"].insert(0, ActionCandidate(
                id=f"GOAL_CHOICE:{index}", operation="click", source="goal_input", target_id=target.id,
                description=f"Select the supplied option {value['text']!r} on {target.name!r}.",
                metadata={"semantic_key": target.semantic_key}))
            continue
        if target.input_type == "date":
            try:
                if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value["text"]):
                    continue
                date.fromisoformat(value["text"])
            except ValueError:
                continue  # Ask the planner for an unambiguous date rather than guessing its order.
            if len(target.date_order) != 3 or set(target.date_order) != {"year", "month", "day"}:
                continue
            parts = [t for t in fields if t.role == "spin_button"
                     and t.name.casefold().split(" ", 1)[0] in target.date_order
                     and abs(t.y - target.y) <= 8 and target.x - 8 <= t.x < target.x + target.width]
            if len(parts) != 3 or {t.name.casefold().split(" ", 1)[0] for t in parts} != {
                    "year", "month", "day"}:
                continue
            expected = dict(zip(("year", "month", "day"), value["text"].split("-"), strict=True))
            target = next((t for part in ("year", "month", "day") for t in parts
                           if t.name.casefold().split(" ", 1)[0] == part and (not t.value.isdigit()
                               or int(t.value) != int(expected[part]))), None)
            if target is None:
                continue
            value = {**value, "text": expected[target.name.casefold().split(" ", 1)[0]]}
            hint = target.name
        typed.append(ActionCandidate(
            id=f"GOAL_TEXT:{index}", operation="type_text", source="goal_input", target_id=target.id,
            description=(f"Select {value['text']!r} in the observed native choice {target.name!r} "
                         "using keyboard prefix and Enter." if target.input_type == "select-one" else
                         f"{value['input_mode']} the observed field {target.name!r} with the "
                         f"supplied {value['label']}: {value['text']!r}"),
            metadata={"text": value["text"], "input_mode": value["input_mode"],
                      "choice_input": target.input_type == "select-one",
                      "target_hint": hint, "field_label": target.name,
                      "semantic_key": target.semantic_key},
        ))
    space.target_heads["type_text"] = typed
    unnamed_entries = [t for t in targets if t.name.startswith(("Unnamed ", "Focused "))
                       and t.role in {"text", "textbox", "entry", "searchbox"}][:2]
    present = {c.target_id for c in space.target_heads["click"]}
    space.target_heads["click"] = [ActionCandidate(
        id="FOCUS:" + t.id, operation="click", target_id=t.id, source="a11y",
        description=f"Focus the observed {t.name!r} in {' / '.join(t.parent_context)} before typing.",
        metadata={"semantic_key": t.semantic_key}) for t in unnamed_entries if t.id not in present
    ] + space.target_heads["click"]
    native_files = [t for t in targets if t.role in {"canvas", "label"}
                    and "nautilus" in (t.application + " ".join(t.parent_context)).lower()][:4]
    space.target_heads["double_click"].extend(ActionCandidate(
        id="OPEN:" + t.id, operation="double_click", target_id=t.id, source=t.source,
        description=f"Open the observed file-manager item {t.name!r}.",
        metadata={"semantic_key": t.semantic_key}) for t in native_files)
    sliders = [t for t in targets if t.role == "slider"][:2]
    space.target_heads["set_slider"] = [ActionCandidate(
        id=f"SLIDER:{t.id}:{fraction}", operation="set_slider", target_id=t.id,
        description=f"Set {t.name!r} to {fraction:.0%} of its range"
                    + (f" (value {t.range_min + fraction * (t.range_max - t.range_min):.2f})"
                       if t.range_min is not None and t.range_max is not None else "")
                    + f"; current observed value: {t.value or 'unknown'}.",
        metadata={"fraction": fraction, "semantic_key": t.semantic_key})
        for t in sliders for fraction in (0.1, 0.25, 0.5, 0.75, 0.9)
        if (not t.value or t.range_min is None or t.range_max is None
            or abs(float(t.value) - (t.range_min + fraction * (t.range_max - t.range_min)))
            > max(1e-6, (t.range_max - t.range_min) * 0.02))]
    # A slider's default center click is ambiguous and was repeatedly seeking to the same midpoint.
    slider_ids = {t.id for t in sliders}
    space.target_heads["click"] = [c for c in space.target_heads["click"] if c.target_id not in slider_ids]
    space.target_heads["right_click"] = [ActionCandidate(
        id="RIGHT:" + c.target_id, operation="right_click", source=c.source, target_id=c.target_id,
        description="Open the context menu for " + c.description,
        metadata=dict(c.metadata)) for c in space.target_heads["click"][:8]]
    sx, sy = scroll_point(tree, viewport)
    for candidate in space.target_heads["scroll"]:
        candidate.metadata.update(scroll_x=sx, scroll_y=sy)
    for menu in menus[:16]:
        space.target_heads["keypress"].append(ActionCandidate(
            id="MENU_KEY:" + menu["path"], operation="keypress", source="a11y_menu",
            description="Open the observed menu path " + menu["path"],
            metadata={"key_sequence": menu["key_sequence"]}))
    space.target_heads["keypress"].extend(ActionCandidate(
        id="GOAL_KEY:" + "+".join(keys), operation="keypress", source="global",
        description=description, metadata={"key_sequence": [keys]}) for keys, description in [
            (["ctrl", "s"], "Save the current document."),
            (["ctrl", "o"], "Open the application's file chooser for an existing document."),
            (["alt", "tab"], "Switch away from the foreground window to another open application."),
            (["shift", "f10"], "Open the focused object's context menu."),
            (["ctrl", "l"], "Focus the browser or native file-dialog location field."),
            (["ctrl", "f"], "Open the current application's Find field."),
            (["shift", "tab"], "Move focus to the previous field."),
            (["down"], "Move down in the current selection or menu."),
            (["up"], "Move up in the current selection or menu."),
            (["right"], "Move right in the current selection, calendar or menu."),
            (["left"], "Move left in the current selection, calendar or menu."),
            (["ctrl", "t"], "Open a separate browser tab, preserving the current tab."),
            (["ctrl", "home"], "Move to the beginning of the current document or page."),
            (["ctrl", "end"], "Move to the end of the current document or page."),
            (["f6"], "Move keyboard focus to the next pane."),
            (["space"], "Select or activate the currently focused control."),
        ])
    title = ET.fromstring(tree or "<empty/>").get("foreground_title", "")
    if re.match(r"New Tab(?:\s*[-–]|$)", title, re.I) and any(
            "address" in t.name.casefold() and _input_target(v, fields) is t
            for t in fields for v in inputs):
        space.target_heads["click"] = [c for c in space.target_heads["click"] if not any(
            t.id == c.target_id and t.scope == "browser_chrome" and t.name.casefold() == "new tab"
            for t in targets)]
    space.controls = {k: v for k, v in space.controls.items()
                      if k in {"wait", "done", "visual_fallback", "return_to_planner"}}
    space.controls["done"] = ActionCandidate(
        id="STAGE_DONE", operation="done", source="stage_control",
        description="The current subtask's completion criteria are satisfied. Request ONE independent "
                    "stage verification; this does not finish the original task.")
    space.controls["return_to_planner"].description = (
        "The current subtask needs unavailable information, new reasoning or an unsupported operation. "
        "Return control with existing progress preserved; do not repeat an ineffective action.")
    return space


def modal_frontier(space, targets, tree):
    """A real blocking dialog owns interaction; background movement is not useful recovery."""
    root = ET.fromstring(tree or "<empty/>")
    dialogs = [n for n in root.iter() if _local(n.tag) in {"dialog", "alert"} and _live_dialog(n, root)]
    visual = next((c.metadata for c in space.candidates() if c.metadata.get("blocking_overlay")), {})
    if not dialogs and not visual:
        return {}
    dialog = dialogs[-1] if dialogs else None
    name = dialog.get("name", "Dialog") if dialog is not None else visual["overlay_name"]
    position = _pair(_attribute(dialog, "screencoord")) if dialog is not None else None
    size = _pair(_attribute(dialog, "size")) if dialog is not None else None
    bounds = [*position, *size] if position and size else visual.get("overlay_bounds")
    by_id = {t.id: t for t in targets}

    def inside(candidate):
        target = by_id.get(candidate.target_id)
        focus = candidate.metadata.get("focus_point")
        if focus and bounds:
            x, y, width, height = bounds
            if not (x <= focus[0] < x + width and y <= focus[1] < y + height):
                return False
        if candidate.operation == "keypress":
            return _keys(candidate) in [
                [[k]] for k in ("esc", "escape", "enter", "tab", "up", "down", "left", "right", "f6", "space")
            ] + [[["ctrl", "l"]], [["shift", "tab"]]]
        if bounds:
            point = candidate.metadata.get("visual_point")
            if target:
                point = [target.x + target.width // 2, target.y + target.height // 2]
            if candidate.operation == "scroll":
                point = [candidate.metadata["scroll_x"], candidate.metadata["scroll_y"]]
            x, y, width, height = bounds
            return bool(point and x <= point[0] < x + width and y <= point[1] < y + height)
        return bool(candidate.metadata.get("blocking_overlay") or target and name in target.parent_context)

    for operation in space.target_heads:
        space.target_heads[operation] = [c for c in space.target_heads[operation] if inside(c)]
    space.controls = {k: c for k, c in space.controls.items()
                      if k in {"return_to_planner", "visual_fallback"}}
    return {"blocking_overlay": name, "overlay_bounds": bounds,
            "overlay_kind": visual.get("overlay_kind", "unknown"),
            "next_intent": f"The active dialog {name!r} owns input; work inside its visible controls."}


def document_mismatch(document, tree, targets):
    """Reject observed contradictions, not a website's human title or a generic app title."""
    title = ET.fromstring(tree or "<empty/>").get("foreground_title", "")
    filename = Path(document).name.casefold()
    if Path(document).stem.casefold() in title.casefold():
        return False
    addresses = [t.value for t in targets if "address" in t.name.casefold() and "://" in t.value]
    if Path(document).suffix.casefold() in {".html", ".htm"} and addresses:
        return all(Path(unquote(urlsplit(v).path)).name.casefold() != filename for v in addresses)
    return bool(re.search(r"\buntitled\b|\S+\.(?:pptx?|docx?|xlsx?|od[ptsg]|pdf)\b", title, re.I))


def application_mismatch(application, tree, known_only=False):
    """Only positive app-family contradictions gate editing; ambiguous titles remain unknown."""
    root = ET.fromstring(tree or "<empty/>")
    actual = (root.get("foreground_title", "") + " " + root.get("foreground_class", "")).casefold()
    families = ({"wps", "wpp", "et"}, {"libreoffice", "soffice"}, {"chrome", "chromium"},
                {"shotcut"}, {"totem", "videos"}, {"nautilus", "files"})
    wanted = set(re.findall(r"\w+", application.casefold()))
    seen = set(re.findall(r"\w+", actual))
    desired = next((f for f in families if f & wanted), None)
    current = next((f for f in families if f & seen), None)
    if desired and current:
        return desired != current
    tokens = [w for w in wanted if len(w) >= 4 and w not in {
        "classic", "presentation", "office", "desktop", "google"}]
    if any(w in actual for w in tokens):
        return False
    observed = root.get("foreground_class", "").casefold()
    return bool(tokens and (current or observed and not known_only and not any(
        name in observed for name in ("gnome-shell", "desktop-portal"))))


def document_frontier(space, targets, document=None, application=None):
    """Permit opening the right document, never typing task content into a contradicted one."""
    by_id = {t.id: t for t in targets}
    filename = Path(document).name.casefold() if document else ""

    def navigation(candidate):
        target = by_id.get(candidate.target_id)
        if candidate.operation == "type_text":
            text = candidate.metadata.get("text", "")
            return bool(target and (application and target.application == "gnome-shell"
                        and text.casefold() == application.casefold()
                        or filename and ("address" in target.name.casefold() or target.scope == "dialog")
                        and Path(unquote(urlsplit(text).path)).name.casefold() == filename))
        if candidate.operation == "keypress":
            return _keys(candidate) in [
                [["ctrl", "l"]], [["ctrl", "o"]], [["esc"]], [["escape"]], [["alt", "tab"]]
            ] or candidate.id == "KEYPRESS_ENTER" and any(
                t.focused and ("address" in t.name.casefold() or t.scope == "dialog"
                               or t.application == "gnome-shell") for t in targets)
        if candidate.source == "visual_grounder" and candidate.operation in {"click", "double_click"}:
            label = candidate.metadata.get("target_label", "").casefold()
            return bool(filename and filename in label or application and matches_field(application, label))
        return bool(target and candidate.operation in {"click", "double_click"} and (
            target.scope in {"browser_chrome", "desktop_launcher"}
            or target.application == "gnome-shell"
            or filename and filename in target.name.casefold()
            or application and application.casefold() in target.name.casefold()))

    for operation in space.target_heads:
        space.target_heads[operation] = [c for c in space.target_heads[operation] if navigation(c)]
    space.controls = {k: c for k, c in space.controls.items()
                      if k in {"return_to_planner", "visual_fallback"}}
    return {"document_recovery": document, "application_recovery": application,
            "next_intent": f"Navigate to {application or ''} {document or ''} first. "
                           "Do not edit the wrong application/document."}
