import copy
import unittest

from jev_desktop import JevLoop, RunOptions, Scope
from jev_desktop.selection_recovery import input_changed, needs_selection_observation
from support import Desktop, Model, node


def selection_error():
    return {
        "ok": False,
        "delivery": "delivered_unverified",
        "error": {
            "code": "ACTION_FAILED",
            "details": {"cause": "write_ignored"},
            "disposition": {"delivery": "delivered_unverified", "retry": "unsafe"},
        },
    }


class PickerDesktop(Desktop):
    def __init__(self):
        super().__init__(
            [
                node(
                    role="listbox",
                    name="Picker",
                    available_actions=["Select"],
                    children=[
                        node(role="option", name="Desktop", ref_id="@s1:e9", available_actions=["Select"]),
                    ],
                ),
                node(
                    role="combobox",
                    name="Path",
                    ref_id="@s1:e2",
                    value="/home/user/",
                    states=["editable"],
                    available_actions=["SetValue"],
                ),
                node(name="Save", ref_id="@s1:e3"),
            ]
        )
        self.saved = False

    def execute(self, scope, operation, target, text):
        result = super().execute(scope, operation, target, text)
        if operation == "SELECT":
            self.nodes[1]["value"] = "/home/user/Desktop/Untitled-1"
            return selection_error()
        if operation == "TYPE_TEXT":
            self.nodes[1]["value"] = text
        if operation == "CLICK":
            self.saved = self.nodes[1]["value"] == "/home/user/Desktop/test.py"
        return result


class SelectionRecoveryTests(unittest.TestCase):
    def test_failed_selection_can_continue_from_observed_input_change_without_replaying_it(self):
        desktop = PickerDesktop()
        model = Model("1", "SELECT", "1", "2", "TYPE_TEXT", "3", "CLICK", "DONE")
        events = list(
            JevLoop(desktop, model, sleep=lambda _: None).run(
                "Save a file",
                RunOptions(
                    text="/home/user/Desktop/test.py",
                    reconcile_observed_popups=True,
                ),
            )
        )
        self.assertTrue(desktop.saved)
        self.assertEqual(events[-1]["stop"], "done")
        self.assertEqual([action[1] for action in desktop.actions], ["SELECT", "TYPE_TEXT", "CLICK"])
        self.assertFalse(next(event["turn"] for event in events if "turn" in event)["ok"])
        self.assertTrue(
            any(event.get("recovery") == "observed_input_change_after_selection" for event in events)
        )

    def test_identity_scope_and_input_evidence_are_required(self):
        desktop = PickerDesktop()
        before = desktop.observe(Scope(app="Editor", window_id="w1"))
        after = copy.deepcopy(before)
        after.nodes[1]["value"] = "/home/user/Desktop/"
        self.assertIsNotNone(input_changed(before, after))
        for change in (
            {"window_id": "other"},
            {"app": "Other"},
            {"complete": False},
            {"root": "other"},
            {"unclassified_surfaces": [{"id": "unknown"}]},
        ):
            with self.subTest(change=change):
                candidate = copy.deepcopy(after)
                candidate.screen.update(change)
                self.assertIsNone(input_changed(before, candidate))
        for changes in (
            {"ref_actionable": False},
            {"states": ["readonly"]},
            {"states": ["secure"]},
            {"states": [], "available_actions": []},
        ):
            with self.subTest(field=changes):
                candidate = copy.deepcopy(after)
                candidate.nodes[1].update(changes)
                self.assertIsNone(input_changed(before, candidate))
        candidate = copy.deepcopy(before)
        candidate.nodes[1]["states"].append("focused")
        self.assertIsNone(input_changed(before, candidate))
        candidate = copy.deepcopy(after)
        candidate.nodes.append(copy.deepcopy(candidate.nodes[1]))
        self.assertIsNone(input_changed(before, candidate))

    def test_uncertain_delivery_and_unrelated_errors_do_not_authorize_reconciliation(self):
        self.assertTrue(needs_selection_observation(selection_error(), "SELECT"))
        for delivery in ("delivery_uncertain", "unknown", "not_delivered"):
            outcome = selection_error()
            outcome["delivery"] = delivery
            outcome["error"]["disposition"]["delivery"] = delivery
            self.assertFalse(needs_selection_observation(outcome, "SELECT"))
        self.assertFalse(needs_selection_observation(selection_error(), "TYPE_TEXT"))
        outcome = selection_error()
        outcome["error"]["details"]["cause"] = "different"
        self.assertFalse(needs_selection_observation(outcome, "SELECT"))
