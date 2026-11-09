"""Shared semantic element table for the slow planner and fast selector."""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from collections import Counter
from dataclasses import replace
from statistics import median

from .targets import UITarget, _is_overlay, _local, _state

_LABEL_ROLES = {"label", "static", "text"}
_MEMORY_USAGE = re.compile(r"\s+-\s+Memory usage\s+-\s+[\d.]+\s+[KMG]B\b", re.I)


def _norm(value: str) -> str:
    return " ".join(_MEMORY_USAGE.sub("", value or "").lower().split())


def _key(target: UITarget) -> str:
    return "|".join((target.role, _norm(target.name), *(_norm(p) for p in target.parent_context),
                     _norm(target.nearby_text) if "dom" in target.source else ""))


def _bounds(element: ET.Element) -> tuple[int, int, int, int] | None:
    attrs = {key.rsplit("}", 1)[-1]: value for key, value in element.attrib.items()}
    values = [int(v) for v in re.findall(r"-?\d+", attrs.get("screencoord", "") + attrs.get("size", ""))]
    return tuple(values) if len(values) == 4 else None


def _a11y_context(root: ET.Element | None) -> tuple[list[dict], bool]:
    if root is None:
        return [], True
    labels = []
    chrome_active = False

    def active(element: ET.Element) -> bool:
        return any(
            key.rsplit("}", 1)[-1] == "active" and value == "true"
            for key, value in element.attrib.items()
        )

    apps = [root] if root.tag.rsplit("}", 1)[-1] == "application" else list(root)
    for app in apps:
        app_active = active(app) or any(active(child) for child in app)
        chrome_active = chrome_active or (
            app_active and "chrome" in app.attrib.get("name", "").lower()
        )
    owners = {element: app.get("name", "") for app in apps for element in app.iter()}
    for element in root.iter():
        role = element.tag.rsplit("}", 1)[-1].replace("-", "_")
        if role not in _LABEL_ROLES:
            continue
        box = _bounds(element)
        text = " ".join((element.attrib.get("name") or element.text or "").split())
        if box and text and len(text) <= 120:
            x, y, width, height = box
            labels.append({"text": text, "box": box, "cx": x + width / 2, "cy": y + height / 2,
                           "application": owners.get(element, "")})
    return labels, chrome_active


def _nearest_label(target: UITarget, labels: list[dict]) -> str:
    if None in (target.x, target.y, target.width, target.height):
        return ""
    cx, cy = target.x + target.width / 2, target.y + target.height / 2
    best, best_score = "", float("inf")
    for item in labels:
        if item["application"] != target.application:
            continue
        x, y, width, height = item["box"]
        left = (0 <= target.x - x - width <= 160
                and abs(item["cy"] - cy) <= max(14, min(40, target.height)))
        above = (0 <= target.y - y - height <= 48 and x + width >= target.x
                 and x <= target.x + target.width)
        if not left and not above:
            continue
        score = (
            target.x - x - width + abs(item["cy"] - cy)
            if left
            else 1000 + target.y - y - height + abs(item["cx"] - cx) / 4
        )
        if score < best_score:
            best, best_score = item["text"], score
    return best


def _content_y(targets: list[UITarget], snapshot: dict | None) -> float:
    if snapshot and snapshot.get("viewport", {}).get("screen_y") is not None:
        return float(snapshot["viewport"]["screen_y"])
    address = next((item for item in targets if item.name == "Address and search bar"), None)
    if address and address.y is not None and address.height is not None:
        return address.y + address.height + 8
    return 110.0


def _scope(target: UITarget, content_y: float) -> str:
    if target.role in {"menu", "menu_item"}:
        return "menu"
    parents = " ".join(target.parent_context).lower()
    if any(word in parents for word in ("dialog", "file chooser", "restore pages")):
        return "dialog"
    application = target.application.lower()
    if "chrome" not in application and "chromium" not in application:
        return "native"
    if target.y is not None and target.height is not None and target.y + target.height / 2 >= content_y:
        return "web_content"
    return "browser_chrome"


def _overlap(a: UITarget, b: UITarget) -> float:
    wide = min(a.x + a.width, b.x + b.width) - max(a.x, b.x)
    tall = min(a.y + a.height, b.y + b.height) - max(a.y, b.y)
    intersection = wide * tall if wide > 0 and tall > 0 else 0
    union = a.width * a.height + b.width * b.height - intersection
    return intersection / union if union > 0 else 0.0


def _roles_compatible(a: str, b: str) -> bool:
    groups = (
        {"button", "push_button", "toggle_button"},
        {"text", "textbox", "entry", "searchbox", "password_text"},
        {"tab", "page_tab"},
        {"checkbox", "check_box"},
        {"table_cell", "grid_cell"},
    )
    return a == b or any(a in group and b in group for group in groups)


def _aligned_snapshot(targets, snapshot):
    """Return the snapshot and whether three independent controls verified its screen mapping."""
    if not snapshot:
        return snapshot, False
    pairs = set()
    for item in snapshot.get("elements", []):
        matches = [t for t in targets if _norm(t.name) == _norm(item.get("name", ""))
                   and _roles_compatible(t.role, item.get("role", ""))
                   and t.source.startswith("a11y")
                   and "chrome" in t.application.lower()
                   and 0.8 <= t.width / max(1, item.get("width", 0)) <= 1.25
                   and 0.8 <= t.height / max(1, item.get("height", 0)) <= 1.25]
        if len(matches) == 1:
            target = matches[0]
            pairs.add(((target.x, target.y, target.width, target.height),
                       (item["x"], item["y"], item["width"], item["height"])))
    # DOM aliases are one physical anchor; ambiguous correspondences cannot calibrate a frame.
    native_counts = Counter(native for native, dom in pairs)
    dom_counts = Counter(dom for native, dom in pairs)
    offsets = [(native[0] - dom[0], native[1] - dom[1]) for native, dom in pairs
               if native_counts[native] == dom_counts[dom] == 1]
    if len(offsets) < 3 or any(max(v) - min(v) > 4 for v in zip(*offsets, strict=True)):
        return snapshot, False
    dx, dy = (median(v) for v in zip(*offsets, strict=True))
    aligned = {**snapshot, "elements": [{**i, "x": i["x"] + dx, "y": i["y"] + dy}
                                       for i in snapshot["elements"]]}
    if "screen_y" in snapshot.get("viewport", {}):
        aligned["viewport"] = {**snapshot["viewport"], "screen_y": snapshot["viewport"]["screen_y"] + dy}
    return aligned, True


def _dom_target(item: dict, snapshot_id: str) -> UITarget | None:
    try:
        x, y = round(float(item["x"])), round(float(item["y"]))
        width, height = round(float(item["width"])), round(float(item["height"]))
        name = str(item["name"]).strip()
    except (KeyError, TypeError, ValueError):
        return None
    if not name or width < 2 or height < 2:
        return None
    target = UITarget(
        id=f"dom:{snapshot_id}:{item.get('index', 0)}",
        snapshot_id=snapshot_id,
        role=str(item.get("role") or "unknown"),
        name=name,
        value=str(item.get("value") or ""),
        enabled=True,
        visible=True,
        showing=True,
        selected=item.get("selected"),
        checked=item.get("checked"),
        focused=item.get("focused"),
        x=x,
        y=y,
        width=width,
        height=height,
        semantic_key="",
        parent_context=tuple(item.get("parent_context") or ()),
        source="dom",
        application="Google Chrome",
        description=str(item.get("description") or ""),
        nearby_text=str(item.get("nearby_text") or ""),
        scope="web_content",
        label_source=str(item.get("label_source") or "dom"),
        operations=tuple(item.get("operations") or ("click",)),
        input_type=str(item.get("input_type") or ""),
        date_order=tuple(item.get("date_order") or ()),
    )
    return replace(target, semantic_key=_key(target))


def _merge(base: UITarget, dom: UITarget) -> UITarget:
    use_dom_name = (
        base.label_source in {"unresolved", "synthetic"}
        or not base.name
    )
    merged = replace(
        base,
        name=dom.name if use_dom_name else base.name,
        value=dom.value if "type_text" in dom.operations else dom.value or base.value,
        checked=dom.checked if dom.checked is not None else base.checked,
        selected=dom.selected if dom.selected is not None else base.selected,
        source="a11y+dom",
        description=dom.description or base.description,
        nearby_text=dom.nearby_text or base.nearby_text,
        scope="web_content",
        label_source=dom.label_source,
        operations=dom.operations,
        parent_context=dom.parent_context or base.parent_context,
        input_type=dom.input_type or base.input_type,
        date_order=dom.date_order or base.date_order,
    )
    return replace(merged, semantic_key=_key(merged))


def _date_fields(targets):
    """Compose adjacent, explicitly named date segments; never infer order from locale or a screenshot."""
    parts = [replace(t, name=t.name.split(" ", 1)[0],
                     nearby_text=t.nearby_text or t.name.partition(" ")[2])
             for t in targets if t.role == "spin_button"
             and t.name.casefold().split(" ", 1)[0] in {"day", "month", "year"}]
    for day in [t for t in parts if t.name.casefold() == "day"]:
        nearby = [t for t in parts if t.application == day.application
                  and t.parent_context == day.parent_context and abs(t.y - day.y) <= 4
                  and abs(t.x - day.x) <= 180]
        if len(nearby) != 3 or {t.name.casefold() for t in nearby} != {"day", "month", "year"}:
            continue
        name = next((t.nearby_text for t in nearby if re.search(r"[\w]{3}", t.nearby_text)), "")
        if not name:
            continue
        ordered = sorted(nearby, key=lambda t: t.x)
        values = {t.name.casefold(): t.value for t in nearby}
        value = (f"{int(values['year']):04d}-{int(values['month']):02d}-{int(values['day']):02d}"
                 if all(v.isdigit() and int(v) > 0 for v in values.values()) else "")
        left, top = min(t.x for t in nearby), min(t.y for t in nearby)
        field = replace(day, id="date:" + day.id, role="entry", name=name, value=value,
                        x=left, y=top, width=max(t.x + t.width for t in nearby) - left,
                        height=max(t.y + t.height for t in nearby) - top, description="Segmented date",
                        operations=("click", "type_text"), input_type="date",
                        date_order=tuple(t.name.casefold() for t in ordered))
        yield replace(field, semantic_key=_key(field))


def unrepresented_window(root: ET.Element | None) -> str:
    """A foreground window absent from A11y must not inherit the background page's controls."""
    if root is None:
        return ""
    title = root.get("foreground_title", "")
    windows = {n.get("name") for n in root.iter()
               if n.tag.rsplit("}", 1)[-1] in {"frame", "dialog"} and n.get("name")}
    return title if windows and title not in windows else ""


def _native_input_owner(root):
    if root is None:
        return None
    owners = []

    foreground = root.get("foreground_title", "")

    def visit(node, web=False, frame_name=""):
        if _local(node.tag) == "frame":
            frame_name = node.get("name", "")
        web = web or _local(node.tag) in {"document", "document_web", "web_document"}
        if (not web and (_is_overlay(node) or _local(node.tag) == "frame" and _state(node, "modal") is True)
                and (not foreground or frame_name == foreground or node.get("name") == foreground)
                and _state(node, "visible") is not False
                and _state(node, "showing") is not False and (
                    _state(node, "active") is True
                    or bool(root.get("foreground_title")) and node.get("name") == root.get("foreground_title")
                    or not root.get("foreground_title") and _state(node, "modal") is True
                    and _state(node, "showing") is True
                    or any(_state(child, "focused") is True for child in node.iter()))):
            owners.append(node)
        for child in node:
            visit(child, web, frame_name)

    visit(root)
    return owners[-1] if owners else None


def enrich_targets(
    targets: list[UITarget],
    accessibility_tree: str | None,
    screen_size: tuple[int, int],
    browser_snapshot: dict | None = None,
) -> list[UITarget]:
    """Recover labels, classify scope, and merge DOM semantics without adding screen OCR."""
    try:
        root = ET.fromstring(accessibility_tree) if accessibility_tree else None
    except ET.ParseError:
        root = None
    if unrepresented_window(root):
        return []  # The existing visual fallback must locate this real input owner first.
    owner = _native_input_owner(root)
    if owner is not None:
        bounds = {_bounds(node) for node in owner.iter()}
        targets = [t for t in targets if (t.x, t.y, t.width, t.height) in bounds]
    owner_scope = ""
    if owner is not None:
        owner_scope = "menu" if _local(owner.tag) in {"menu", "popup_menu"} else "dialog"
    labels, chrome_active = _a11y_context(root)
    browser_snapshot, dom_coordinates_verified = _aligned_snapshot(targets, browser_snapshot)
    content_y = _content_y(targets, browser_snapshot if dom_coordinates_verified else None)
    enriched = []
    for target in targets:
        name, source = target.name, target.label_source
        if target.role == "page_tab":
            name = _MEMORY_USAGE.sub("", name)
        nearby = target.nearby_text or _nearest_label(target, labels)
        if not name and nearby and (target.role in {"entry", "text", "textbox", "searchbox",
                                                    "spin_button", "combo_box"}
                                     or "type_text" in target.operations):
            name, source = nearby, "a11y_nearby"
        if not name and target.focused:
            name, source = f"Focused {target.role.replace('_', ' ')}", "synthetic"
        # Keep unnamed native boxes for DOM labeling; discard any still unnamed after fusion.
        if not name and not browser_snapshot:
            continue
        item = replace(
            target,
            name=name,
            nearby_text=nearby,
            scope=owner_scope or _scope(target, content_y),
            label_source=source,
        )
        enriched.append(replace(item, semantic_key=_key(item)))
    enriched.extend(list(_date_fields(enriched)))
    chrome_targets = any(
        "chrome" in target.application.lower() or "chromium" in target.application.lower()
        for target in targets
    )
    if owner is None and browser_snapshot and (chrome_active or chrome_targets):
        snapshot_id = targets[0].snapshot_id if targets else "dom"
        for raw in browser_snapshot.get("elements", []):
            dom = _dom_target(raw, snapshot_id)
            if dom is None:
                continue
            compatible = [
                (index, _overlap(base, dom))
                for index, base in enumerate(enriched)
                if _roles_compatible(base.role, dom.role)
                and (_norm(base.name) == _norm(dom.name)
                     or not base.name
                     or base.label_source in {"unresolved", "synthetic"})
            ]
            matches = [index for index, overlap in compatible if overlap >= 0.5]
            if len(matches) == 1:
                enriched[matches[0]] = _merge(enriched[matches[0]], dom)
            elif dom_coordinates_verified:
                # Unmatched DOM nodes stay in the raw snapshot until their screen mapping is verified.
                enriched.append(dom)
    unique = {}
    for target in enriched:
        if not target.name:
            continue
        center_x = target.x + target.width / 2
        center_y = target.y + target.height / 2
        if not (0 <= center_x < screen_size[0] and 0 <= center_y < screen_size[1]):
            continue
        key = (
            target.role, _norm(target.name), round(target.x), round(target.y),
            round(target.width), round(target.height),
        )
        current = unique.get(key)
        if current is None or current.source == "a11y" and target.source != "a11y":
            unique[key] = target
    return list(unique.values())
