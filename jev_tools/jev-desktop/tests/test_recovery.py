import unittest

from jev_desktop import JevLoop, Limits, RunOptions
from support import Desktop, Model, node


class HeadedDesktop(Desktop):
    def observe(self, scope):
        seen = super().observe(scope)
        seen.screen["headed_text"] = True
        return seen


class RecoveryTests(unittest.TestCase):
    def failed(self, delivery="not_delivered", retry="safe"):
        return {
            "ok": False,
            "delivery": delivery,
            "error": {
                "code": "TIMEOUT",
                "message": "Receiver hit test unproven",
                "disposition": {"delivery": delivery, "retry": retry},
            },
        }

    def test_safe_failure_reobserves_and_offers_a_different_operation(self):
        desktop = HeadedDesktop(
            [node(available_actions=["SetFocus"], bounds={"x": 1, "y": 1, "width": 20, "height": 20})]
        )
        outcomes = iter([self.failed(), {"ok": True, "delivery": "delivered_verified"}])
        desktop.on_execute = lambda: setattr(desktop, "result", next(outcomes))
        model = Model("1", "POINTER_CLICK", "1", "FOCUS", "DONE")
        events = list(
            JevLoop(desktop, model, sleep=lambda _: None).run(
                "Activate the observed control", RunOptions(replan_safe_failures=True)
            )
        )
        self.assertEqual(events[-1]["stop"], "done")
        self.assertEqual([a[1] for a in desktop.actions], ["POINTER_CLICK", "FOCUS"])
        operations = model.requests[3]["questions"]["operation"]["criteria"]
        self.assertNotIn("POINTER_CLICK", operations)
        self.assertIn("FOCUS", operations)
        self.assertEqual(model.requests[2]["state"]["recent_actions"][0]["error"]["code"], "TIMEOUT")
        self.assertTrue(any(e.get("recovery") == "reobserve_after_not_delivered" for e in events))
        self.assertTrue(desktop.cleaned)

    def test_uncertain_or_unsafe_delivery_never_replans(self):
        for delivery, retry in [
            ("delivery_uncertain", "unsafe"),
            ("delivered_unverified", "unsafe"),
            ("not_delivered", "unsafe"),
        ]:
            with self.subTest(delivery=delivery, retry=retry):
                desktop = Desktop()
                desktop.result = self.failed(delivery, retry)
                model = Model("1", "CLICK")
                events = list(
                    JevLoop(desktop, model, sleep=lambda _: None).run(
                        "Goal", RunOptions(replan_safe_failures=True)
                    )
                )
                self.assertEqual(events[-1]["stop"], "action_failed")
                self.assertEqual(len(desktop.actions), 1)
                self.assertEqual(len(model.requests), 2)

    def test_failed_text_does_not_consume_another_supplied_value(self):
        desktop = Desktop([node(role="textfield", available_actions=["SetValue"])])
        desktop.result = self.failed()
        model = Model("1", "TYPE_TEXT")
        events = list(
            JevLoop(desktop, model, sleep=lambda _: None).run(
                "Fill", RunOptions(text=["first", "second"], replan_safe_failures=True)
            )
        )
        self.assertEqual(events[-1]["stop"], "action_failed")
        self.assertEqual(len(desktop.actions), 1)
        self.assertEqual(desktop.actions[0][-1], "first")

    def test_safe_replanning_still_obeys_action_budget(self):
        desktop = Desktop()
        desktop.result = self.failed()
        model = Model("1", "CLICK")
        events = list(
            JevLoop(desktop, model, limits=Limits(steps=1), sleep=lambda _: None).run(
                "Goal", RunOptions(replan_safe_failures=True)
            )
        )
        self.assertIn("action budget", events[-1]["stop"])
        self.assertEqual(len(desktop.actions), 1)
