import unittest

from jev_desktop import JevLoop, RunOptions
from jev_desktop.pointer_recovery import POINTER_OPERATIONS, shared_receiver_refusal
from support import Desktop, Model, node


def refusal(nested=False):
    proof = {"kind": "hit_test_unproven", "reason": "sibling_overlap", "physical_delivery_started": False}
    return {
        "ok": False, "delivery": "not_delivered",
        "error": {"code": "TIMEOUT" if nested else "ACTION_FAILED",
                  "details": {"kind": "actionability_timeout", "last_report": {"phase": "preflight", "report": proof}} if nested else proof,
                  "disposition": {"delivery": "not_delivered", "retry": "safe"}},
    }


class PointerRecoveryTests(unittest.TestCase):
    def test_only_proven_shared_preflight_refusals_authorize_family_suppression(self):
        for nested in (False, True):
            self.assertTrue(shared_receiver_refusal(refusal(nested), "POINTER_CLICK"))
        for operation in ("CLICK", "FOCUS", "DRAG_TO:1", "HOVER"):
            self.assertFalse(shared_receiver_refusal(refusal(), operation))
        for change in ({"physical_delivery_started": True}, {"reason": "ancestry_unknown"}, {"kind": "other"}):
            candidate = refusal()
            candidate["error"]["details"].update(change)
            self.assertFalse(shared_receiver_refusal(candidate, "POINTER_CLICK"))
        for delivery in ("delivered_unverified", "delivery_uncertain", "unknown"):
            candidate = refusal()
            candidate["delivery"] = delivery
            self.assertFalse(shared_receiver_refusal(candidate, "RIGHT_CLICK"))
        candidate = refusal(True)
        candidate["error"]["details"]["last_report"]["phase"] = "after_action"
        self.assertFalse(shared_receiver_refusal(candidate, "POINTER_CLICK"))

    def test_unknown_child_geometry_uses_the_same_receiver_guard(self):
        for nested in (False, True):
            candidate = refusal(nested)
            details = candidate["error"]["details"]
            proof = details["last_report"]["report"] if nested else details
            proof["reason"] = "child_bounds_unknown"
            for operation in ("POINTER_CLICK", "RIGHT_CLICK", "DOUBLE_CLICK", "TRIPLE_CLICK"):
                self.assertTrue(shared_receiver_refusal(candidate, operation))
            proof["physical_delivery_started"] = True
            self.assertFalse(shared_receiver_refusal(candidate, "POINTER_CLICK"))

    def test_unknown_child_bounds_withhold_pointer_aliases_but_keep_semantic_routes(self):
        class Headed(Desktop):
            def observe(self, scope):
                seen = super().observe(scope)
                seen.screen["headed_text"] = True
                return seen

            def execute(self, scope, operation, target, text):
                super().execute(scope, operation, target, text)
                if operation != "POINTER_CLICK":
                    return {"ok": True, "delivery": "delivered_verified"}
                result = refusal(True)
                result["error"]["details"]["last_report"]["report"]["reason"] = "child_bounds_unknown"
                return result

        desktop = Headed([node(bounds={"x": 1, "y": 1, "width": 50, "height": 20}, available_actions=["Click", "SetFocus"])])
        model = Model("1", "POINTER_CLICK", "1", "CLICK", "DONE")
        events = list(JevLoop(desktop, model, sleep=lambda _: None).run(
            "Use another observed route", RunOptions(replan_no_effect=True, replan_safe_failures=True)
        ))
        self.assertEqual(events[-1]["stop"], "done")
        self.assertEqual([action[1] for action in desktop.actions], ["POINTER_CLICK", "CLICK"])
        request = model.requests[2]
        item = request["questions"]["target"]["criteria"]["1"]
        operations = request["state"]["action_sets"][item["action_set"]]["operations"]
        self.assertFalse(set(operations).intersection(POINTER_OPERATIONS))
        self.assertIn("CLICK", operations)
        self.assertIn("FOCUS", operations)

    def test_unchanged_target_uses_window_keyboard_route_instead_of_trying_every_pointer_verb(self):
        class Headed(Desktop):
            def observe(self, scope):
                seen = super().observe(scope)
                seen.screen["headed_text"] = True
                return seen
            def execute(self, scope, operation, target, text):
                super().execute(scope, operation, target, text)
                return refusal() if operation == "POINTER_CLICK" else {"ok": True, "delivery": "delivered_unverified"}
        desktop = Headed([node(bounds={"x": 1, "y": 1, "width": 50, "height": 20}, available_actions=["Click", "SetFocus"])])
        model = Model("1", "POINTER_CLICK", "WINDOW", "PRESS_KEY:shift+f10", "DONE")
        events = list(JevLoop(desktop, model, sleep=lambda _: None).run("Open a menu", RunOptions(replan_no_effect=True, replan_safe_failures=True)))
        self.assertEqual(events[-1]["stop"], "done")
        self.assertEqual([a[1] for a in desktop.actions], ["POINTER_CLICK", "PRESS_KEY:shift+f10"])
        request = model.requests[2]
        item = request["questions"]["target"]["criteria"]["1"]
        operations = request["state"]["action_sets"][item["action_set"]]["operations"]
        self.assertFalse(set(operations).intersection(POINTER_OPERATIONS))
        self.assertIn("CLICK", operations)
        self.assertIn("FOCUS", operations)
        self.assertIn("no input was sent", str(request["state"]["cycle_feedback"]))
        self.assertTrue(any(e.get("recovery") == "reobserve_after_unproven_pointer" for e in events))

    def test_changed_ui_restores_the_original_pointer_operation(self):
        class Moving(Desktop):
            def observe(self, scope):
                seen = super().observe(scope)
                seen.screen["headed_text"] = True
                return seen
            def execute(self, scope, operation, target, text):
                super().execute(scope, operation, target, text)
                if len(self.actions) == 1:
                    self.nodes[0]["bounds"]["x"] = 100
                    return refusal(True)
                return {"ok": True, "delivery": "delivered_verified"}
        desktop = Moving([node(bounds={"x": 1, "y": 1, "width": 50, "height": 20})])
        events = list(JevLoop(desktop, Model("1", "POINTER_CLICK", "1", "POINTER_CLICK", "DONE"), sleep=lambda _: None).run("Click after layout changes", RunOptions(replan_no_effect=True, replan_safe_failures=True)))
        self.assertEqual(events[-1]["stop"], "done")
        self.assertEqual([a[1] for a in desktop.actions], ["POINTER_CLICK", "POINTER_CLICK"])

    def test_unknown_surface_does_not_generalize_a_receiver_refusal(self):
        class UnknownSurface(Desktop):
            def observe(self, scope):
                seen = super().observe(scope)
                seen.screen.update(headed_text=True, unclassified_surfaces=[{"id": "unread-popup"}])
                return seen
            def execute(self, scope, operation, target, text):
                super().execute(scope, operation, target, text)
                return refusal() if operation == "POINTER_CLICK" else {"ok": True, "delivery": "delivered_verified"}
        desktop = UnknownSurface([node(bounds={"x": 1, "y": 1, "width": 50, "height": 20})])
        model = Model("1", "POINTER_CLICK", "1", "HOVER", "BLOCKED")
        events = list(JevLoop(desktop, model, sleep=lambda _: None).run("Inspect a popup", RunOptions(replan_no_effect=True, replan_safe_failures=True)))
        self.assertEqual([a[1] for a in desktop.actions], ["POINTER_CLICK", "HOVER"])
        self.assertFalse(any(e.get("recovery") == "reobserve_after_unproven_pointer" for e in events))
