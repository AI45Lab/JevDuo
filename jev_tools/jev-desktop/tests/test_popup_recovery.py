import copy
import unittest

from jev_desktop import JevLoop, Observation, RunOptions
from jev_desktop.popup_recovery import needs_popup_observation, observed_popup
from support import Desktop, Model, node


class PopupRecoveryTests(unittest.TestCase):
    def setUp(self):
        bounds = {"x": 20, "y": 20, "width": 100, "height": 40}
        owner = ['window "Editor"', 'webarea "Document"']
        self.opener = node(name="Commands", states=["haspopup"], path=[*owner, "toolbar"], bounds=bounds)
        self.menu = node(role="menu", name="", path=[*owner, "group"], states=["focused"], bounds=bounds)
        self.item = node(role="menuitem", name="Create item", path=[*owner, "group", "menu"], bounds=bounds)
        screen = {"app": "Fixture", "window_id": "w1", "surface": "window", "complete": True}
        self.before = Observation([self.opener], screen)
        self.after = Observation([self.opener, self.menu, self.item], copy.deepcopy(screen))
        self.failure = {
            "ok": False,
            "delivery": "delivered_unverified",
            "error": {
                "code": "ACTION_FAILED",
                "disposition": {"delivery": "delivered_unverified", "retry": "unsafe"},
                "details": {
                    "kind": "popup_not_observed",
                    "steps": [{"mechanism": "semantic_api", "outcome": "succeeded"}],
                },
            },
        }

    def test_only_an_acknowledged_opener_failure_can_be_reconciled(self):
        self.assertTrue(needs_popup_observation(self.failure, "CLICK", self.opener))
        for operation in ("TYPE_TEXT", "INSERT_TEXT", "CLEAR", "PRESS_KEY:return"):
            self.assertFalse(needs_popup_observation(self.failure, operation, self.opener))
        uncertain = copy.deepcopy(self.failure)
        uncertain["delivery"] = "delivery_uncertain"
        self.assertFalse(needs_popup_observation(uncertain, "CLICK", self.opener))
        no_ack = copy.deepcopy(self.failure)
        no_ack["error"]["details"]["steps"] = []
        self.assertFalse(needs_popup_observation(no_ack, "CLICK", self.opener))

    def test_new_focused_owned_menu_requires_readable_actionable_items(self):
        evidence = observed_popup(self.before, self.after, self.opener)
        self.assertEqual(evidence["window_id"], "w1")
        self.assertEqual(evidence["items"][0]["what"], 'menuitem "Create item"')
        for field, value in (
            ("window_id", "another-window"),
            ("app", "Other"),
            ("complete", False),
            ("surface", "alert"),
            ("root", "subtree"),
            ("unclassified_surfaces", ["menu"]),
        ):
            with self.subTest(field=field):
                after = copy.deepcopy(self.after)
                after.screen[field] = value
                self.assertIsNone(observed_popup(self.before, after, self.opener))
        for index, field, value in (
            (1, "states", []),
            (1, "states", ["focused", "hidden"]),
            (1, "path", ['window "Editor"', 'webarea "Other"']),
            (1, "bounds", {"width": 0, "height": 50}),
            (2, "ref_actionable", False),
            (2, "states", ["offscreen"]),
            (2, "available_actions", []),
            (2, "path", ["unrelated menu"]),
        ):
            with self.subTest(index=index, field=field):
                after = copy.deepcopy(self.after)
                after.nodes[index][field] = value
                self.assertIsNone(observed_popup(self.before, after, self.opener))
        self.assertIsNone(observed_popup(self.after, self.after, self.opener))
        ambiguous = copy.deepcopy(self.after)
        ambiguous.nodes.append({**self.menu, "name": "Another menu"})
        self.assertIsNone(observed_popup(self.before, ambiguous, self.opener))

    def test_loop_preserves_failure_and_continues_without_replaying_opener(self):
        desktop = Desktop([self.opener])
        outcomes = iter([self.failure, {"ok": True, "delivery": "delivered_verified"}])

        def execute():
            desktop.result = next(outcomes)
            desktop.nodes = [self.opener, self.menu, self.item]

        desktop.on_execute = execute
        model = Model("1", "CLICK", "3", "CLICK", "DONE")
        events = list(
            JevLoop(desktop, model, sleep=lambda _: None).run(
                "Create an item", RunOptions(reconcile_observed_popups=True)
            )
        )
        self.assertEqual(events[-1]["stop"], "done")
        self.assertEqual([a[2]["name"] for a in desktop.actions], ["Commands", "Create item"])
        turns = [e["turn"] for e in events if "turn" in e]
        self.assertFalse(turns[0]["ok"])
        self.assertEqual(turns[0]["delivery"], "delivered_unverified")
        self.assertTrue(any(e.get("recovery") == "observed_popup_after_unverified_opener" for e in events))
        self.assertNotIn("1", model.requests[2]["questions"]["target"]["criteria"])
        self.assertTrue(desktop.cleaned)

    def test_no_positive_evidence_or_no_opt_in_still_stops(self):
        for reconcile, popup in ((False, True), (True, False)):
            desktop = Desktop([self.opener])
            desktop.result = self.failure
            if popup:
                desktop.on_execute = lambda desktop=desktop: setattr(desktop, "nodes", self.after.nodes)
            events = list(
                JevLoop(desktop, Model("1", "CLICK"), sleep=lambda _: None).run(
                    "Goal", RunOptions(replan_safe_failures=True, reconcile_observed_popups=reconcile)
                )
            )
            self.assertEqual(events[-1]["stop"], "action_failed")
            self.assertEqual(len(desktop.actions), 1)
