import copy
import unittest

from jev_desktop import JevLoop, RunOptions
from jev_desktop.pointer_recovery import POINTER_OPERATIONS, shared_receiver_refusal
from support import Desktop, Model, node


def unknown_hit():
    return {
        "ok": False,
        "delivery": "not_delivered",
        "error": {
            "code": "ACTION_FAILED",
            "details": {
                "kind": "pointer_hit_unproven",
                "hit_test": "Unknown",
                "delivered_clicks": 0,
                "physical_delivery_started": False,
            },
            "disposition": {"delivery": "not_delivered", "retry": "safe"},
        },
    }


class PointerHitRecoveryTests(unittest.TestCase):
    def test_only_explicit_no_input_unknown_hit_is_shared(self):
        for operation in POINTER_OPERATIONS[:-1]:
            self.assertTrue(shared_receiver_refusal(unknown_hit(), operation))
        for change in (
            {"physical_delivery_started": True},
            {"physical_delivery_started": None},
            {"delivered_clicks": 1},
            {"delivered_clicks": None},
            {"delivered_clicks": False},
            {"hit_test": "False"},
            {"hit_test": None},
        ):
            candidate = unknown_hit()
            candidate["error"]["details"].update(change)
            self.assertFalse(shared_receiver_refusal(candidate, "POINTER_CLICK"))
        for delivery in ("delivery_uncertain", "delivered_unverified", "delivered_verified"):
            candidate = unknown_hit()
            candidate["delivery"] = delivery
            self.assertFalse(shared_receiver_refusal(candidate, "POINTER_CLICK"))
            candidate = unknown_hit()
            candidate["error"]["disposition"]["delivery"] = delivery
            self.assertFalse(shared_receiver_refusal(candidate, "POINTER_CLICK"))

    def test_unknown_hit_withholds_pointer_family_and_asks_for_semantic_route(self):
        class Headed(Desktop):
            def observe(self, scope):
                seen = super().observe(scope)
                seen.screen["headed_text"] = True
                return seen

            def execute(self, scope, operation, target, text):
                super().execute(scope, operation, target, text)
                return copy.deepcopy(unknown_hit()) if operation == "POINTER_CLICK" else self.result

        desktop = Headed([
            node(bounds={"x": 1, "y": 1, "width": 50, "height": 20}, available_actions=["Click", "SetFocus"])
        ])
        model = Model("1", "POINTER_CLICK", "1", "CLICK", "DONE")
        events = list(JevLoop(desktop, model, sleep=lambda _: None).run(
            "activate the control", RunOptions(replan_no_effect=True, replan_safe_failures=True)
        ))
        self.assertEqual(events[-1]["stop"], "done")
        self.assertEqual([action[1] for action in desktop.actions], ["POINTER_CLICK", "CLICK"])
        request = model.requests[2]
        entry = request["questions"]["target"]["criteria"]["1"]
        operations = request["state"]["action_sets"][entry["action_set"]]["operations"]
        self.assertFalse(set(operations).intersection(POINTER_OPERATIONS))
        self.assertIn("CLICK", operations)
        self.assertIn("FOCUS", operations)
        self.assertTrue(any(event.get("recovery") == "reobserve_after_unproven_pointer" for event in events))
