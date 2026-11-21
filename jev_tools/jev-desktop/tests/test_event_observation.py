import copy
import json
import unittest

from jev_desktop import DesktopError, JevLoop, Limits, RunOptions
from support import Desktop, Model, node


class ValueDesktop(Desktop):
    def __init__(self):
        super().__init__([node(role="textfield", name="Height", available_actions=["SetValue"])])
        self.context = [{"role": "textfield", "name": "Height", "value": "19.68"}]
        self.on_execute = lambda: self.context[0].update(value="20.00")

    def observe(self, scope):
        observation = super().observe(scope)
        observation.screen["context"] = copy.deepcopy(self.context)
        return observation


class EventObservationTests(unittest.TestCase):
    def run_value(self, **options):
        desktop = ValueDesktop()
        events = list(
            JevLoop(desktop, Model("1", "TYPE_TEXT"), limits=Limits(steps=1), sleep=lambda _: None).run(
                "change the field", RunOptions(text="20.00", **options)
            )
        )
        return desktop, next(event for event in events if "turn" in event)

    def test_value_write_distinguishes_before_and_after_and_redacts_supplied_text(self):
        desktop, event = self.run_value(include_context=True)
        self.assertEqual(event["screen_phase"], "before_action")
        self.assertEqual(event["after_screen_phase"], "after_action")
        self.assertEqual(event["screen"]["visible_context"][0]["value"], "19.68")
        self.assertEqual(event["after_screen"]["visible_context"][0]["value"], "[redacted]")
        self.assertNotIn("20.00", json.dumps(event))
        self.assertEqual(len(desktop.observations), 2)
        self.assertEqual(len(desktop.actions), 1)
        self.assertTrue(desktop.cleaned)

    def test_context_is_opt_in_for_both_observations(self):
        _, event = self.run_value()
        for key in ("screen", "after_screen"):
            self.assertNotIn("context", event[key])
            self.assertNotIn("visible_context", event[key])
        self.assertNotIn("19.68", json.dumps(event))

    def test_no_values_hides_values_in_both_observations(self):
        _, event = self.run_value(include_context=True, values=False)
        for key in ("screen", "after_screen"):
            self.assertEqual(event[key]["visible_context"], [{"role": "textfield", "name": "Height"}])
        self.assertNotIn("19.68", json.dumps(event))

    def test_context_limit_applies_to_both_observations(self):
        desktop = Desktop()
        original = desktop.observe

        def observe(scope):
            observation = original(scope)
            observation.screen["context"] *= 201
            return observation

        desktop.observe = observe
        events = list(
            JevLoop(desktop, Model("1", "CLICK"), limits=Limits(steps=1), sleep=lambda _: None).run(
                "click", RunOptions(include_context=True)
            )
        )
        event = next(event for event in events if "turn" in event)
        for key in ("screen", "after_screen"):
            self.assertEqual(len(event[key]["visible_context"]), 200)
            self.assertTrue(event[key]["context_truncated"])

    def test_consumer_cannot_mutate_history_or_observed_context(self):
        desktop, model = ValueDesktop(), Model("1", "TYPE_TEXT", "DONE")
        events = JevLoop(desktop, model, sleep=lambda _: None).run(
            "change the field", RunOptions(text="20.00", include_context=True)
        )
        event = next(events)
        self.assertIn("turn", event)
        event["after_screen"]["visible_context"][0]["name"] = "injected name"
        event["turn"]["operation"] = "injected operation"
        list(events)
        self.assertEqual(desktop.context[0]["name"], "Height")
        self.assertNotIn("injected", json.dumps(model.requests))
        self.assertTrue(desktop.cleaned)

    def test_failed_delivery_has_no_claimed_after_observation_or_replay(self):
        desktop = Desktop()
        desktop.result = {"ok": False, "delivery": "delivery_uncertain", "error": {"code": "TIMEOUT"}}
        events = list(JevLoop(desktop, Model("1", "CLICK"), sleep=lambda _: None).run("click"))
        event = next(event for event in events if "turn" in event)
        self.assertEqual(event["screen_phase"], "before_action")
        self.assertNotIn("after_screen", event)
        self.assertEqual(events[-1]["stop"], "action_failed")
        self.assertEqual(len(desktop.observations), 1)
        self.assertEqual(len(desktop.actions), 1)
        self.assertTrue(desktop.cleaned)

    def test_post_action_read_failure_keeps_receipt_without_claiming_after_observation(self):
        desktop = Desktop()
        desktop.on_execute = lambda: setattr(desktop, "observe_error", "TIMEOUT")
        events = JevLoop(desktop, Model("1", "CLICK"), sleep=lambda _: None).run("click")
        event = next(events)
        self.assertIn("turn", event)
        self.assertEqual(event["screen_phase"], "before_action")
        self.assertNotIn("after_screen", event)
        with self.assertRaises(DesktopError):
            next(events)
        self.assertEqual(len(desktop.observations), 2)
        self.assertEqual(len(desktop.actions), 1)
        self.assertTrue(desktop.cleaned)
