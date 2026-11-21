import copy
import unittest

from jev_desktop import JevLoop, RunOptions, Scope
from jev_desktop.actions import extend_space
from jev_desktop.native_intent import filter_intents, intent, matches
from jev_desktop.policy import action_space
from support import Desktop, Model, node


def fixture():
    child = node(role="menuitem", name="Blue", ref_id="@s1:e2", path=['combobox "Theme"'], available_actions=["Click", "Select"])
    other = node(role="menuitem", name="Green", ref_id="@s1:e3", available_actions=["Click", "Select"])
    owner = node(role="combobox", name="Theme", available_actions=["Select"], children=[child, other])
    return owner, child


class NativeIntentTests(unittest.TestCase):
    def test_selection_aliases_are_blocked_in_both_directions_but_other_labels_remain(self):
        scope = Scope(app="Editor", window_id="w1")
        owner, child = fixture()
        initial = action_space([owner, child])
        bound = initial["by_target"]["2"]["node"]
        for operation, receiver in (("SELECT", owner), ("SELECT_ITEM", bound)):
            previous = intent(scope, operation, receiver, "Blue", [owner, child])
            space = action_space([owner, child])
            filter_intents(space, scope, [owner, child], [previous])
            self.assertEqual([o["label"] for o in space["by_target"]["1"]["selection_options"]], ["Green"])
            self.assertNotIn("SELECT_ITEM", space["by_target"]["2"]["operations"])
            other_scope = Scope(app="Editor", window_id="w2")
            self.assertFalse(matches(previous, intent(other_scope, "SELECT", owner, "Blue", [owner, child])))
            renamed = {**owner, "name": "Other theme"}
            self.assertFalse(matches(previous, intent(scope, "SELECT", renamed, "Blue", [renamed])))

    def test_actual_loop_dispatches_selection_once_and_withholds_both_native_aliases(self):
        class ChangedDesktop(Desktop):
            def execute(self, scope, operation, target, text):
                super().execute(scope, operation, target, text)
                self.nodes[0]["description"] = "Changed"
                return {"ok": False, "delivery": "delivered_unverified", "error": {
                    "code": "ACTION_FAILED", "details": {"cause": "write_ignored"},
                    "disposition": {"delivery": "delivered_unverified", "retry": "unsafe"},
                }}
        for choices in (("1", "SELECT", "1", "DONE"), ("2", "SELECT_ITEM", "DONE")):
            desktop = ChangedDesktop(list(fixture()))
            model = Model(*choices)
            events = list(JevLoop(desktop, model, sleep=lambda _: None).run("Choose blue", RunOptions(reconcile_observed_popups=True)))
            self.assertEqual(events[-1]["stop"], "done")
            self.assertEqual(len(desktop.actions), 1)
            request = model.requests[-1]
            criterion = request["questions"]["target"]["criteria"]["2"]
            operations = request["state"]["action_sets"][criterion["action_set"]]["operations"]
            self.assertNotIn("SELECT_ITEM", operations)

    def test_activation_and_click_aliases_bind_actual_option_not_parent(self):
        owner, child = fixture()
        space = action_space([owner, child], headed=True)
        extend_space(space, {"headed_text": True, "window_id": "w1"}, RunOptions())
        scope = Scope(app="Editor", window_id="w1")
        previous = intent(scope, "ACTIVATE_OPTION", child, "Blue", [owner, child])
        filter_intents(space, scope, [owner, child], [previous])
        self.assertEqual([o["label"] for o in space["by_target"]["1"]["activation_options"]], ["Green"])
        self.assertNotIn("CLICK", space["by_target"]["2"]["operations"])
        other = intent(scope, "CLICK", owner, None, [owner, child])
        self.assertFalse(matches(previous, other))
        newer = copy.deepcopy(child)
        newer.update(ref_id="@s2:e9", bounds={"x": 20, "y": 20, "width": 20, "height": 20})
        self.assertTrue(matches(previous, intent(scope, "CLICK", newer, None, [newer])))
        self.assertFalse(matches(previous, intent(scope, "POINTER_CLICK", newer, None, [newer])))
