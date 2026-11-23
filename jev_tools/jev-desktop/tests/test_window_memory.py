import copy
import json
import unittest

from jev_desktop import DesktopError, JevLoop, RunOptions, Scope
from jev_desktop.window_memory import attach, closed_window
from support import Desktop, Model, node


class WindowMemoryTests(unittest.TestCase):
    def test_past_values_keep_native_closure_and_cancel_separate_from_completion(self):
        screen = {
            "app": "Editor",
            "window_id": "dialog",
            "window": "Adjust",
            "surface": "window",
            "complete": True,
            "context": [
                {
                    "role": "incrementor",
                    "name": "Height",
                    "value": "512",
                    "ref_id": "@old:e1",
                }
            ],
        }
        action = {
            "step": 3,
            "operation": "CLICK",
            "target": "Cancel",
            "ok": True,
            "delivery": "delivered_unverified",
            "argv": ["click", "@old:e2"],
        }
        record = closed_window(Scope(app="Editor", window_id="dialog"), screen, action)
        self.assertEqual(record["visible_context"][0]["value"], "512")
        self.assertEqual(record["closing_action"]["target"], "Cancel")
        self.assertEqual(record["closing_action"]["delivery"], "delivered_unverified")
        self.assertEqual(record["confirmed_absent"], "WINDOW_NOT_FOUND")
        self.assertNotIn("@old", json.dumps(record))
        self.assertNotIn("success", record)
        request = {"state": {}, "questions": {"target": {"type": "choice"}}}
        attach(request, [record])
        self.assertIn(
            "closure alone does not prove",
            request["state"]["closed_window_observation_rules"],
        )
        record["visible_context"][0]["value"] = "changed"
        self.assertEqual(
            request["state"]["closed_window_observations"][0]["visible_context"][0][
                "value"
            ],
            "512",
        )

    def test_hidden_values_secure_fields_and_partial_context_are_explicit(self):
        screen = {
            "window_id": "dialog",
            "complete": False,
            "context": [
                {"role": "textfield", "value": "secret", "states": ["secure"]},
                {"role": "incrementor", "value": "512"},
            ]
            + [{"role": "statictext", "name": "detail"}] * 205,
        }
        scope = Scope(window_id="dialog")
        record = closed_window(scope, screen)
        self.assertNotIn("secret", json.dumps(record))
        self.assertEqual(record["visible_context"][1]["value"], "512")
        self.assertFalse(record["complete"])
        self.assertTrue(record["context_truncated"])
        self.assertEqual(len(record["visible_context"]), 200)
        self.assertNotIn("512", json.dumps(closed_window(scope, screen, values=False)))
        self.assertIsNone(closed_window(Scope(window_id="different"), screen))
        self.assertIsNone(closed_window(Scope(app="Other", window_id="dialog"), screen))

    def test_confirmed_dialog_closure_retains_observation_for_next_decision(self):
        class Windows(Desktop):
            closed = False

            def application_windows(self, scope):
                return (
                    [
                        {
                            "id": "dialog",
                            "app_name": "Editor",
                            "pid": 1,
                            "process_instance": "one",
                            "title": "Adjust",
                        }
                    ]
                    if scope.window_id == "main" and not self.closed
                    else []
                )

            def observe(self, scope):
                if scope.window_id == "dialog" and self.closed:
                    raise DesktopError("closed", "WINDOW_NOT_FOUND")
                self.nodes = [
                    node(name="Apply" if scope.window_id == "dialog" else "Open")
                ]
                seen = super().observe(scope)
                seen.screen["context"] = (
                    [{"role": "incrementor", "name": "Height", "value": "512"}]
                    if scope.window_id == "dialog"
                    else []
                )
                return seen

            def execute(self, scope, operation, selected, text):
                if scope.window_id == "dialog":
                    self.closed = True
                return super().execute(scope, operation, selected, text)

        for remember, values in [(True, True), (True, False), (False, True)]:
            with self.subTest(remember=remember, values=values):
                desktop = Windows()
                model = Model("APP_WINDOW_0", "SWITCH_WINDOW", "1", "CLICK", "DONE")
                events = list(
                    JevLoop(desktop, model, sleep=lambda _: None).run(
                        "Adjust image",
                        RunOptions(
                            scope=Scope(app="Editor", window_id="main"),
                            follow_application_windows=True,
                            remember_closed_windows=remember,
                            values=values,
                        ),
                    )
                )
                self.assertEqual(events[-1]["stop"], "done")
                self.assertEqual(len(desktop.actions), 1)
                state = model.requests[-1]["state"]
                if not remember:
                    self.assertNotIn("closed_window_observations", state)
                    continue
                record = state["closed_window_observations"][0]
                self.assertEqual(record["window_id"], "dialog")
                self.assertEqual(record["closing_action"]["target"], 'button "Apply"')
                self.assertEqual("value" in record["visible_context"][0], values)

    def test_attaching_no_past_windows_keeps_request_unchanged(self):
        request = {"state": {}, "questions": {}}
        original = copy.deepcopy(request)
        attach(request, [])
        self.assertEqual(request, original)
