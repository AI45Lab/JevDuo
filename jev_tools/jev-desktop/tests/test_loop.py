import json
import unittest
from contextlib import closing

from jev_desktop import JevLoop, Limits, RunOptions, Scope
from support import Desktop, Model, node


class LoopTests(unittest.TestCase):
    def run_loop(self, desktop, model, options=None, limits=None):
        pauses = []
        events = list(
            JevLoop(desktop, model, sleep=pauses.append, limits=limits).run("complete goal", options)
        )
        self.assertTrue(desktop.cleaned)
        return events, pauses

    def test_target_then_operation_zero_confidence_and_done(self):
        desktop, model = Desktop(), Model("1", "CLICK", "DONE", confidence=0)
        events, pauses = self.run_loop(desktop, model)
        self.assertEqual(events[-1]["stop"], "done")
        self.assertEqual(
            [list(r["questions"]) for r in model.requests], [["target"], ["operation"], ["target"]]
        )
        second = model.requests[1]
        self.assertEqual(second["state"]["selected_target"]["choice"], "1")
        self.assertEqual(list(second["questions"]["operation"]["criteria"]), ["CLICK"])
        self.assertEqual(len(desktop.actions), 1)
        self.assertEqual(pauses, [1])
        self.assertEqual(len(desktop.observations), 3)
        self.assertNotIn("PRIVATE-VALUE", json.dumps(events))

    def test_terminal_states_do_not_execute(self):
        for terminal in ("DONE", "BLOCKED"):
            with self.subTest(terminal=terminal):
                desktop = Desktop()
                events, pauses = self.run_loop(desktop, Model(terminal))
                self.assertEqual(events[-1]["stop"], terminal.lower())
                self.assertEqual(desktop.actions, [])
                self.assertEqual(pauses, [])

    def test_invalid_choice_or_operation_never_executes(self):
        for choices in (
            ("999",),
            ("1", "TYPE_TEXT"),
            ({"answers": {}},),
            (
                {
                    "answers": {
                        "target": {"choice": "1", "confidence": float("nan"), "probabilities": {"1": 1}}
                    }
                },
            ),
        ):
            with self.subTest(choices=choices):
                desktop = Desktop()
                with self.assertRaises(RuntimeError):
                    self.run_loop(desktop, Model(*choices))
                self.assertTrue(desktop.cleaned)
                self.assertEqual(desktop.actions, [])

    def test_risk_check_names_chosen_action_and_fails_closed(self):
        for risk in (0.5, 0.99, None, float("nan"), -1, "0.1", True):
            with self.subTest(risk=risk):
                desktop = Desktop()
                model = Model("1", "CLICK", {"answers": {"destructive": {"noul": risk}}})
                events, _ = self.run_loop(desktop, model, RunOptions(confirm_destructive=True))
                self.assertTrue(events[-1]["stop"].startswith("confirm:"))
                self.assertEqual(model.requests[-1]["state"]["step"]["operation"], "CLICK")
                self.assertEqual(desktop.actions, [])
        desktop = Desktop()
        model = Model("1", "CLICK", {"answers": {"destructive": {"noul": 0.1}}}, "DONE")
        self.assertEqual(
            self.run_loop(desktop, model, RunOptions(confirm_destructive=True))[0][-1]["stop"], "done"
        )
        self.assertEqual(len(desktop.actions), 1)

    def test_budget_counts_risk_and_bootstrap_without_exceeding(self):
        for budget in (1, 2):
            desktop, model = Desktop(), Model("1", "CLICK")
            events, _ = self.run_loop(
                desktop, model, RunOptions(confirm_destructive=True), Limits(calls=budget)
            )
            self.assertIn("model call budget", events[-1]["stop"])
            self.assertEqual(len(model.requests), budget)
            self.assertEqual(desktop.actions, [])
        desktop = Desktop()
        desktop.observe_error = "WINDOW_NOT_FOUND"
        desktop.catalog = [{"id": str(i), "name": str(i)} for i in range(300)]
        model = Model("NEXT_PAGE")
        events, _ = self.run_loop(desktop, model, limits=Limits(calls=1))
        self.assertIn("model call budget", events[-1]["stop"])
        self.assertEqual(len(model.requests), 1)
        self.assertEqual(desktop.launched, [])

    def test_action_and_stall_budgets_and_wait_exemption(self):
        desktop = Desktop()
        events, _ = self.run_loop(desktop, Model("1", "CLICK"), limits=Limits(steps=1))
        self.assertIn("action budget", events[-1]["stop"])
        desktop = Desktop()
        events, _ = self.run_loop(desktop, Model("1", "CLICK", "1", "CLICK", "1", "CLICK"))
        self.assertIn("3 turns that changed nothing", events[-1]["stop"])
        events, _ = self.run_loop(Desktop(), Model("WAIT", "WAIT", "WAIT", "DONE"))
        self.assertEqual(events[-1]["stop"], "done")

    def test_failed_action_does_not_observe_or_replay(self):
        desktop = Desktop()
        desktop.result = {"ok": False, "delivery": "delivery_uncertain", "error": {"code": "TIMEOUT"}}
        events, pauses = self.run_loop(desktop, Model("1", "CLICK"))
        self.assertEqual(events[-1]["stop"], "action_failed")
        self.assertEqual(events[0]["turn"]["delivery"], "delivery_uncertain")
        self.assertEqual(len(desktop.actions), 1)
        self.assertEqual(len(desktop.observations), 1)
        self.assertEqual(pauses, [1])

    def test_pinned_window_closure_and_post_observation_error(self):
        for code, stop in (
            ("WINDOW_NOT_FOUND", "window_closed"),
            ("SURFACE_OUTSIDE_WINDOW", "surface_outside_window"),
        ):
            desktop = Desktop()
            desktop.on_execute = lambda code=code, desktop=desktop: setattr(desktop, "observe_error", code)
            events, _ = self.run_loop(desktop, Model("1", "CLICK"), RunOptions(scope=Scope(window_id="w1")))
            self.assertEqual(events[-1]["stop"], stop)
            self.assertEqual(len(desktop.actions), 1)
        desktop = Desktop()
        desktop.on_execute = lambda: setattr(desktop, "observe_error", "TIMEOUT")
        with self.assertRaises(RuntimeError):
            self.run_loop(desktop, Model("1", "CLICK"))
        self.assertEqual(len(desktop.actions), 1)
        self.assertTrue(desktop.cleaned)

    def test_drill_and_widen_change_scope_without_post_read(self):
        desktop = Desktop([node(children_count=5)])
        events, _ = self.run_loop(desktop, Model("1", "DRILL", "WIDEN", "DONE"))
        self.assertEqual([s.root for s in desktop.observations], [None, "@s1:e1", None])
        self.assertEqual(events[-1]["stop"], "done")

    def test_ordered_text_callback_redaction_and_no_values(self):
        secrets = ["first-private-試験", "second-private-value"]
        for callback in (False, True):
            with self.subTest(callback=callback):
                desktop = Desktop(
                    [node(role="textfield", available_actions=["SetValue"], value="PRIVATE-VALUE")]
                )
                desktop.result = {"ok": True, "delivery": "delivered_verified", "details": {"echo": secrets}}
                values = iter(secrets)
                source = (lambda field, history, values=values: next(values)) if callback else secrets
                events, _ = self.run_loop(
                    desktop,
                    Model("1", "TYPE_TEXT", "1", "TYPE_TEXT", "DONE"),
                    RunOptions(text=source, values=False),
                )
                self.assertEqual([a[-1] for a in desktop.actions], secrets)
                self.assertNotIn(secrets[0], json.dumps(events[-1]))
                self.assertNotIn(secrets[1], json.dumps(events[-1]))
                self.assertNotIn("PRIVATE-VALUE", json.dumps(events))
        desktop = Desktop([node(role="textfield", available_actions=["SetValue"], value="PRIVATE-VALUE")])
        model = Model("DONE")
        self.run_loop(desktop, model, RunOptions(values=False))
        self.assertNotIn("PRIVATE-VALUE", json.dumps(model.requests))

    def test_text_exhaustion_and_callback_refusal(self):
        desktop = Desktop([node(role="textfield", available_actions=["SetValue"])])
        model = Model("1", "TYPE_TEXT", "DONE")
        self.run_loop(desktop, model, RunOptions(text="only"))
        self.assertEqual(
            list(model.requests[-1]["questions"]["target"]["criteria"]), ["WAIT", "DONE", "BLOCKED"]
        )
        desktop = Desktop([node(role="textfield", available_actions=["SetValue"])])
        events, _ = self.run_loop(desktop, Model("1", "TYPE_TEXT"), RunOptions(text=lambda f, h: None))
        self.assertIn("no value was supplied", events[-1]["stop"])
        self.assertEqual(desktop.actions, [])

    def test_closing_generator_and_model_exception_clean_resources(self):
        desktop = Desktop()
        with closing(JevLoop(desktop, Model("1", "CLICK"), sleep=lambda _: None).run("goal")) as events:
            next(events)
            self.assertFalse(desktop.cleaned)
        self.assertTrue(desktop.cleaned)
        desktop = Desktop()
        with self.assertRaises(RuntimeError):
            list(JevLoop(desktop, Model(), sleep=lambda _: None).run("goal"))
        self.assertTrue(desktop.cleaned)

    def test_launch_bootstrap_pins_window_and_never_replays(self):
        desktop = Desktop()
        desktop.observe_error = "WINDOW_NOT_FOUND"
        events, pauses = self.run_loop(desktop, Model("app_0", "DONE"))
        self.assertEqual(events[0]["turn"]["operation"], "LAUNCH")
        self.assertEqual(desktop.observations[-1].window_id, "launched-window")
        self.assertEqual(pauses, [1])
        self.assertEqual(len(desktop.launched), 1)
        desktop = Desktop()
        desktop.observe_error = "WINDOW_NOT_FOUND"
        desktop.result = {"ok": False, "error": {"code": "TIMEOUT"}}
        events, _ = self.run_loop(desktop, Model("app_0"))
        self.assertEqual(events[-1]["stop"], "action_failed")
        self.assertEqual(len(desktop.launched), 1)

    def test_existing_app_or_root_does_not_trigger_bootstrap(self):
        for scope in (Scope(app="Fixture"), Scope(root="@s1:e1"), Scope(window_id="w1")):
            desktop = Desktop()
            desktop.observe_error = "WINDOW_NOT_FOUND"
            if scope.window_id:
                self.run_loop(desktop, Model(), RunOptions(scope=scope))
            else:
                with self.assertRaises(RuntimeError):
                    self.run_loop(desktop, Model(), RunOptions(scope=scope))
            self.assertEqual(desktop.launched, [])
