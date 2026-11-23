import unittest

from jev_desktop import JevLoop, RunOptions
from support import Desktop, Model, node
from test_text_loop import Writer


def refusal(delivery="not_delivered", retry="safe", outer=None):
    return {
        "ok": False,
        "delivery": delivery if outer is None else outer,
        "error": {
            "code": "ACTION_FAILED",
            "message": "Keyboard focus could not be proven",
            "details": {"check": "target_focus", "physical_delivery_started": False},
            "disposition": {"delivery": delivery, "retry": retry},
        },
    }


class SafeTextRefusalTests(unittest.TestCase):
    def test_refused_input_preserves_error_then_selects_an_alternate_route_and_generates_fresh_text(self):
        class AlternateDesktop(Desktop):
            def execute(self, scope, operation, target, text):
                self.actions.append((scope, operation, target, text))
                if target["name"] == "Unavailable field":
                    return refusal()
                if operation == "CLICK":
                    self.nodes = [node(role="textfield", name="File name", available_actions=["SetValue"])]
                return {"ok": True, "delivery": "delivered_verified"}

        desktop = AlternateDesktop([
            node(role="textfield", name="Unavailable field", available_actions=["SetValue"]),
            node(ref_id="@s1:e2", name="Create file"),
        ])
        class FreshWriter(Writer):
            def generate(self, request):
                super().generate(request)
                return f"generation-{len(self.requests)}"

        writer = FreshWriter()
        model = Model("1", "TYPE_TEXT", "2", "CLICK", "1", "TYPE_TEXT", "DONE")
        events = list(JevLoop(desktop, model, sleep=lambda _: None).run(
            "Create a file", RunOptions(text_model=writer, replan_safe_failures=True, text_replans=1),
        ))
        self.assertEqual(events[-1]["stop"], "done")
        self.assertEqual([a[1] for a in desktop.actions], ["TYPE_TEXT", "CLICK", "TYPE_TEXT"])
        self.assertEqual(len(writer.requests), 2)
        self.assertEqual(writer.requests[1].field["what"], 'textfield "File name"')
        self.assertEqual(desktop.actions[-1][-1], "generation-2")
        self.assertNotIn("1", model.requests[2]["questions"]["target"]["criteria"])
        self.assertEqual(events[0]["turn"]["error"]["details"]["physical_delivery_started"], False)
        self.assertTrue(any(e.get("recovery") == "replan_after_not_delivered_text" for e in events))

    def test_uncertain_or_contradictory_delivery_and_supplied_text_still_stop(self):
        for outcome in (refusal("delivery_uncertain", "safe"), refusal(outer="delivery_uncertain"), refusal(retry="unsafe")):
            with self.subTest(outcome=outcome):
                desktop = Desktop([node(role="textfield", available_actions=["SetValue"])])
                desktop.result = outcome
                events = list(JevLoop(desktop, Model("1", "TYPE_TEXT"), sleep=lambda _: None).run(
                    "Write", RunOptions(text_model=Writer(), replan_safe_failures=True, text_replans=2),
                ))
                self.assertEqual(events[-1]["stop"], "action_failed")
                self.assertEqual(len(desktop.actions), 1)
        for options in (RunOptions(text=["supplied"], replan_safe_failures=True, text_replans=2), RunOptions(text_model=Writer(), replan_safe_failures=True, text_replans=0)):
            desktop = Desktop([node(role="textfield", available_actions=["SetValue"])])
            desktop.result = refusal()
            events = list(JevLoop(desktop, Model("1", "TYPE_TEXT"), sleep=lambda _: None).run("Write", options))
            self.assertEqual(events[-1]["stop"], "action_failed")
            self.assertEqual(len(desktop.actions), 1)

    def test_refusals_across_distinct_fields_exhaust_the_shared_replan_budget(self):
        desktop = Desktop([
            node(role="textfield", name="First", available_actions=["SetValue"]),
            node(ref_id="@s1:e2", role="textfield", name="Second", available_actions=["SetValue"]),
        ])
        desktop.result = refusal()
        events = list(JevLoop(desktop, Model("1", "TYPE_TEXT", "2", "TYPE_TEXT"), sleep=lambda _: None).run(
            "Write", RunOptions(text_model=Writer(), replan_safe_failures=True, text_replans=1),
        ))
        self.assertEqual(events[-1]["stop"], "action_failed")
        self.assertEqual(len(desktop.actions), 2)
        self.assertEqual(sum(e.get("recovery") == "replan_after_not_delivered_text" for e in events), 1)
