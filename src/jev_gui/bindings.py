"""Input identity and readback state shared by local choices and recovery reports."""

from __future__ import annotations

import re

from .operations import matching_value
from .targets import EDITABLE_ROLES


def normalized(text):
    return " ".join(str(text or "").casefold().split())


def target_identity(target):
    """Field values and generated snapshot IDs are not control identity."""
    return (
        getattr(target, "application", ""),
        target.role,
        tuple(target.parent_context),
        target.x,
        target.y,
        target.width,
        target.height,
    )


def resolve_input(item, targets, previous=None):
    """Bind one field uniquely; never distribute text across several possible inputs."""
    if item.get("kind") == "select":
        editable = [
            t for t in targets if t.role in {"combo_box", "entry", "list_box"} and "click" in t.operations
        ]
    else:
        editable = [t for t in targets if t.role in EDITABLE_ROLES and "type_text" in t.operations]
    scope = normalized(item.get("form", ""))
    if scope:
        editable = [t for t in editable if scope in normalized(" ".join(t.parent_context))]
    if previous is not None:
        matches = [t for t in editable if target_identity(t) == previous]
        if len(matches) == 1:
            return matches
    if item.get("target_id"):
        return [t for t in editable if t.id == item["target_id"]]
    hint = normalized(item.get("target_hint") or item["field"])
    exact = [t for t in editable if hint in {normalized(t.name), normalized(t.description)}]
    if exact:
        return exact
    nearby = [t for t in editable if hint == normalized(getattr(t, "nearby_text", ""))]
    if nearby:
        return nearby
    return [
        t
        for t in editable
        if re.search(
            r"(?<!\w)" + re.escape(hint) + r"(?!\w)",
            normalized(" ".join((t.name, t.description, getattr(t, "nearby_text", "")))),
        )
    ]


class LocalBindings:
    """Only our acknowledged write followed by matching readback verifies an input."""

    def __init__(self, inputs, history=None):
        self.inputs = inputs
        self.rows = [
            {"field": item["field"], "expected": item["text"], "status": "pending"} for item in inputs
        ]
        self.identities = {}
        self.targets = {}
        self.surface = None
        self.history = history if history is not None else {}

    def observe(self, targets, surface):
        self.targets = {}
        same_surface = self.surface == surface
        self.surface = surface
        for index, (item, row) in enumerate(zip(self.inputs, self.rows, strict=True)):
            matches = resolve_input(item, targets, self.identities.get(index) if same_surface else None)
            row["matches"] = [t.id for t in matches]
            if len(matches) != 1:
                if row["status"] == "written":
                    row["status"] = "unobservable_after_write"
                elif row["status"] not in {"verified", "unobservable_after_write"}:
                    row["status"] = "missing" if not matches else "ambiguous"
                continue
            target = matches[0]
            self.targets[index] = target
            self.identities[index] = target_identity(target)
            row["target_id"] = target.id
            row["observed"] = target.value
            history_key = (surface, target_identity(target), item["text"])
            if row["status"] in {"written", "verified", "readback_mismatch", "unobservable_after_write"}:
                if target.role == "password_text" or (item.get("kind") == "select" and not target.value):
                    row["status"] = "unobservable_after_write"
                else:
                    row["status"] = (
                        "verified"
                        if matching_value(item["text"], target.value, target.date_order)
                        else "readback_mismatch"
                    )
            elif history_key in self.history and target.value == item["text"]:
                row["status"] = "verified"
            else:
                row["status"] = "pending"
            if row["status"] == "verified":
                self.history[history_key] = True
        for index, target in list(self.targets.items()):
            if sum(other.id == target.id for other in self.targets.values()) > 1:
                self.rows[index]["status"] = "ambiguous"
        self.targets = {i: t for i, t in self.targets.items() if self.rows[i]["status"] != "ambiguous"}

    def wrote(self, index):
        self.rows[index]["status"] = "written"

    @property
    def pending(self):
        return [row for row in self.rows if row["status"] not in {"verified", "unobservable_after_write"}]

    def report(self):
        return [
            dict(row, authority="acknowledged_write_then_accessibility_readback; not task success")
            for row in self.rows
        ]
