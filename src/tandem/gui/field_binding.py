"""Observed field identity and supplied-value binding; no model or execution calls."""

import re


def matches_field(hint, label, owner=""):
    """Match an intended field to its observed label, not an arbitrary focused control."""
    def words(value):
        return set(re.findall(r"\w+", value.casefold())) - {"the", "a", "field", "required"}

    observed = words(label)
    intended = words(hint)
    intended -= words(owner) - observed
    weak_labels = {"name", "search", "value", "title", "date", "number"}
    if observed <= weak_labels and len(observed) == 1 and intended != observed:
        return False  # A qualifier distinguishes this field from a different form's generic label.
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
        editable = (target.role in {"textbox", "entry", "searchbox", "spin_button", "password_text"}
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
            owner = " ".join((target.application, *target.parent_context))
            if launcher_input(value):
                owner += " desktop"
            if not matches_field(hint, label, owner):
                continue  # Descriptive context words cannot override the explicit intended field.
            ignored = {"brief", "required", "enter", "input", "field", "the", "a"}
            if (set(re.findall(r"\w+", hint.casefold())) - ignored
                    == set(re.findall(r"\w+", label.casefold())) - ignored):
                score = max(score, 8)
            elif matches_field(value["label"], label):
                score = max(score, 4)
            elif matches_field(hint, label, owner):
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
