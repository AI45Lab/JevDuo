import copy
import unittest

from jev_desktop import JevLoop, RunOptions, Scope
from jev_desktop.selection_recovery import (
    changed_selection_context,
    input_changed,
    needs_destroyed_target_observation,
    needs_value_observation,
)
from support import Desktop, Model, node


def unconfirmed():
    return {
        "ok": False,
        "delivery": "delivered_unverified",
        "error": {
            "code": "ACTION_FAILED",
            "details": {"kind": "post_action_verification", "cause": "commit_unconfirmed"},
            "disposition": {"delivery": "delivered_unverified", "retry": "unsafe"},
        },
    }


class ValueObservationTests(unittest.TestCase):
    def test_destroyed_checked_menu_item_can_continue_from_new_ui_without_replay(self):
        class ClosingMenu(Desktop):
            def execute(self, scope, operation, target, text):
                super().execute(scope, operation, target, text)
                self.nodes = [node(name="Settings", ref_id="@s2:e1")]
                return {
                    "ok": False, "delivery": "delivered_unverified",
                    "error": {
                        "code": "STALE_REF", "details": {
                            "kind": "post_action_verification",
                            "after_action": {"disposition": {"delivery": "delivered_unverified"}},
                        },
                        "disposition": {"delivery": "delivered_unverified", "retry": "unsafe"},
                    },
                }
        desktop = ClosingMenu([node(role="checkbox", name="Auto Save", available_actions=["Toggle"])])
        events = list(JevLoop(desktop, Model("1", "CHECK", "DONE"), sleep=lambda _: None).run("Enable saving", RunOptions(reconcile_observed_popups=True)))
        self.assertEqual(events[-1]["stop"], "done")
        self.assertEqual(len(desktop.actions), 1)
        failed = next(e["turn"] for e in events if "turn" in e)
        self.assertFalse(failed["ok"])
        self.assertTrue(needs_destroyed_target_observation(failed, "CHECK"))
        candidate = copy.deepcopy(failed)
        candidate["error"]["details"]["after_action"]["disposition"]["delivery"] = "delivery_uncertain"
        self.assertFalse(needs_destroyed_target_observation(candidate, "CHECK"))

    def test_acknowledged_value_failure_can_reobserve_without_replaying_the_write(self):
        desktop = Desktop([node(role="incrementor", name="Column", value="80", available_actions=["SetValue"])])
        desktop.result = unconfirmed()
        desktop.on_execute = lambda: desktop.nodes[0].update(value="50")
        model = Model("1", "TYPE_TEXT", "DONE")
        events = list(JevLoop(desktop, model, sleep=lambda _: None).run("Set column 50", RunOptions(text="50", reconcile_observed_popups=True)))
        self.assertEqual(events[-1]["stop"], "done")
        self.assertEqual(len(desktop.actions), 1)
        self.assertFalse(next(e["turn"] for e in events if "turn" in e)["ok"])
        evidence = next(e["evidence"] for e in events if e.get("recovery") == "observed_input_change_after_value")
        self.assertEqual(evidence["input_changes"][0]["previous_value"], "80")
        self.assertEqual(evidence["input_changes"][0]["current_value"], "[redacted]")
        self.assertEqual(desktop.nodes[0]["value"], "50")

    def test_unchanged_or_uncertain_value_failure_stops_after_one_dispatch(self):
        for delivery, changed in (("delivered_unverified", False), ("delivery_uncertain", True), ("not_delivered", True)):
            desktop = Desktop([node(role="incrementor", name="Column", value="80", available_actions=["SetValue"])])
            desktop.result = unconfirmed()
            desktop.result["delivery"] = delivery
            if changed:
                desktop.on_execute = lambda target=desktop: target.nodes[0].update(value="50")
            events = list(JevLoop(desktop, Model("1", "TYPE_TEXT"), sleep=lambda _: None).run("Set 50", RunOptions(text="50", reconcile_observed_popups=True)))
            self.assertEqual(events[-1]["stop"], "action_failed")
            self.assertEqual(len(desktop.actions), 1)

    def test_numeric_input_change_requires_same_complete_scope_and_readable_unique_field(self):
        before = Desktop([node(role="incrementor", name="Column", value="80", available_actions=["SetValue"])]).observe(Scope())
        after = copy.deepcopy(before)
        after.nodes[0]["value"] = "50"
        self.assertIsNotNone(input_changed(before, after))
        for change in ({"states": ["secure"]}, {"states": ["readonly"]}, {"available_actions": []}, {"ref_actionable": False}):
            candidate = copy.deepcopy(after)
            candidate.nodes[0].update(change)
            self.assertIsNone(input_changed(before, candidate))
        candidate = copy.deepcopy(after)
        candidate.nodes.append(copy.deepcopy(candidate.nodes[0]))
        self.assertIsNone(input_changed(before, candidate))

    def test_selection_context_change_is_observation_only_and_scope_bound(self):
        before = Desktop([node(role="combobox", description="Old", available_actions=["Select"])]).observe(Scope())
        after = copy.deepcopy(before)
        self.assertIsNone(changed_selection_context(before, after))
        after.nodes[0]["description"] = "New"
        self.assertIsNotNone(changed_selection_context(before, after))
        for change in ({"window_id": "other"}, {"app": "other"}, {"complete": False}, {"unclassified_surfaces": [{}]}):
            candidate = copy.deepcopy(after)
            candidate.screen.update(change)
            self.assertIsNone(changed_selection_context(before, candidate))

    def test_value_observation_rejects_wrong_error_and_disposition(self):
        self.assertTrue(needs_value_observation(unconfirmed(), "TYPE_TEXT"))
        for change in ({"kind": "other"}, {"cause": "other"}):
            outcome = unconfirmed()
            outcome["error"]["details"].update(change)
            self.assertFalse(needs_value_observation(outcome, "TYPE_TEXT"))
        outcome = unconfirmed()
        outcome["error"]["disposition"]["delivery"] = "delivery_uncertain"
        self.assertFalse(needs_value_observation(outcome, "TYPE_TEXT"))
        self.assertFalse(needs_value_observation(unconfirmed(), "CLICK"))
