"""Existing screenshot-grounded target fallback, reused without changing the JEV service."""

import base64
import json
import re

from PIL import Image

from .candidates import ActionCandidate
from .goal_candidates import matches_field


def ground_visual(client, instruction, plan, screenshot):
    with Image.open(screenshot) as image:
        width, height = image.size
    proposals = plan["action_proposals"]
    value = client.json_request([
        {"role": "system", "content": (
            "Ground the immediate GUI intent in the CURRENT screenshot. Propose up to four distinct "
            "visible target alternatives, never a screen grid or a sequence. Do not execute anything. "
            "Return JSON {blocking_overlay:null|{name,bounds,kind},"
            "candidates:[{operation,target,bounds,text,input_mode,target_hint}]}. "
            "If a modal or file chooser blocks the task, identify it and propose ONLY its controls. "
            "kind=task_dialog when it is the requested interaction (save, open, choose); "
            "kind=obstruction only for an unrelated prompt preventing the local objective; "
            "otherwise kind=unknown. Do not close a task dialog as a generic recovery. "
            "Do not label an ordinary document/window as a blocking overlay. "
            "Operations: click, double_click, right_click, type_text, scroll_up, scroll_down. "
            "bounds=[x,y,width,height] in "
            "ORIGINAL screenshot pixels. Ground only foreground unobscured targets. For type_text copy exact "
            "text/input_mode AND target_hint from ONE supplied input, and ground the visible field named "
            "by that hint. target must be its observed label/identity, not the text to enter. Never put "
            "unrelated text in the currently focused field; otherwise click to focus first. Use [] if no "
            "For date/calendar fields, propose opening the calendar, NOT whole-string type_text: "
            "the screenshot alone does not establish the native date input's segment order. "
            "required target is visible. Do not invent targets or completion evidence."
        )},
        {"role": "user", "content": [
            {"type": "text", "text": json.dumps({"task": instruction, "local_plan": plan,
                                                   "screenshot_size": [width, height]}, ensure_ascii=False)},
            {"type": "image_url", "image_url": {"url": "data:image/png;base64,"
             + base64.b64encode(screenshot.read_bytes()).decode("ascii")}},
        ]},
    ], lambda v: isinstance(v.get("candidates"), list)
       and all(isinstance(i, dict) for i in v["candidates"]), role="ground_visual")
    allowed_text = {(p.get("text"), p.get("input_mode"), p.get("target_hint", p.get("label", "")))
                    for p in proposals if p.get("operation") == "type_text"}
    candidates = []
    for item in value["candidates"][:4]:
        operation, box = item.get("operation"), item.get("bounds")
        if operation not in {"click", "double_click", "right_click", "type_text", "scroll_up", "scroll_down"}:
            continue
        if not isinstance(box, list) or len(box) != 4 or any(type(v) is not int for v in box):
            continue
        x, y, w, h = box
        if not (0 <= x < x + w <= width and 0 <= y < y + h <= height):
            continue
        point = [x + w // 2, y + h // 2]
        metadata = {"visual_point": point, "visual_bounds": box, "target_label": item.get("target", "")}
        overlay = value.get("blocking_overlay")
        if isinstance(overlay, dict) and isinstance(overlay.get("name"), str):
            bounds = overlay.get("bounds")
            if (isinstance(bounds, list) and len(bounds) == 4 and all(type(v) is int for v in bounds)
                    and 0 <= bounds[0] < bounds[0] + bounds[2] <= width
                    and 0 <= bounds[1] < bounds[1] + bounds[3] <= height):
                metadata.update(blocking_overlay=True, overlay_name=overlay["name"], overlay_bounds=bounds)
                metadata["overlay_kind"] = overlay.get("kind", "unknown")
        if operation == "type_text":
            if (re.fullmatch(r"\d{4}-\d{2}-\d{2}", str(item.get("text", "")))
                    and re.search(r"date|time|calendar|日期|时间", str(item.get("target_hint", "")), re.I)):
                continue  # Native date typing requires observed CDP input_type/date_order.
            if ((item.get("text"), item.get("input_mode"), item.get("target_hint")) not in allowed_text
                    or not matches_field(item.get("target_hint", ""), item.get("target", ""))):
                continue
            metadata.update(text=item["text"], input_mode=item["input_mode"],
                            target_hint=item["target_hint"], field_label=item["target"])
        name = f"VISUAL:{operation}:{x}:{y}:{w}:{h}"
        if operation.startswith("scroll_"):
            name = f"{operation.upper()}:{name}"
            operation = "scroll"
            metadata.update(scroll_x=point[0], scroll_y=point[1])
        candidates.append(ActionCandidate(
            id=name, operation=operation, source="visual_grounder", metadata=metadata,
            description=f"{item['operation']} screenshot-visible {item.get('target', 'control')!r} "
                        f"at box {box}." + (f" Text: {metadata['text']!r}." if "text" in metadata else "")))
    return candidates
