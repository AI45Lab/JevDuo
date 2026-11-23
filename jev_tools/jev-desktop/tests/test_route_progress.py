import unittest

from jev_desktop import JevLoop, Limits, RunOptions, Scope
from jev_desktop.policy import action_space
from jev_desktop.screen import collect
from support import Desktop, Model, node


class HeadedDesktop(Desktop):
    def observe(self, scope):
        observed = super().observe(scope)
        observed.screen["headed_text"] = True
        observed.screen["context"] = [
            {key: target[key] for key in ("role", "name", "states", "value") if key in target}
            for root in observed.nodes
            for target in collect(root)
        ]
        return observed


def slides():
    return node(
        role="document",
        name="Slides View",
        available_actions=["Select"],
        children=[
            node(role="group", name="Slide 2", ref_id="@s1:e2", available_actions=[], states=["selected"]),
            node(role="group", name="Slide 3", ref_id="@s1:e3", available_actions=[]),
        ],
    )


class RouteProgressTest(unittest.TestCase):
    def run_loop(self, desktop, model, *, options=None, limits=None, goal="Reach Slide 3"):
        loop = JevLoop(desktop, model, limits=limits, sleep=lambda _: None)
        return list(loop.run(goal, options or RunOptions(replan_no_effect=True)))

    def test_click_only_checkbox_off_command_requires_checked_actionable_receiver(self):
        checkbox = node(role="checkbox", available_actions=["Click", "SetFocus"], states=["checked"])
        choice = action_space([checkbox])["by_target"]["1"]
        self.assertIn("UNCHECK", choice["operations"])
        self.assertEqual(choice["node"]["available_actions"], ["Click", "SetFocus"])
        for changes in (
            {"states": []},
            {"states": ["checked", "disabled"]},
            {"states": ["checked", "offscreen"]},
            {"states": ["checked", "indeterminate"]},
            {"role": "radiobutton"},
            {"ref_actionable": False},
        ):
            with self.subTest(changes=changes):
                offers = action_space([{**checkbox, **changes}])["by_target"]
                self.assertNotIn("UNCHECK", offers.get("1", {}).get("operations", []))

    def test_cancel_ends_the_route_and_exposes_window_keys_without_dispatching_select(self):
        desktop = HeadedDesktop([slides()])
        model = Model("1", "SELECT", "CANCEL", "WINDOW", "PRESS_KEY:down", "DONE")
        events = self.run_loop(desktop, model)

        self.assertEqual([entry[1] for entry in desktop.actions], ["PRESS_KEY:down"])
        self.assertEqual(events[0]["recovery"], "replan_cancelled_selection")
        self.assertEqual(events[-1]["stop"], "done")
        self.assertNotIn("1", model.requests[3]["questions"]["target"]["criteria"])
        self.assertEqual(model.requests[3]["state"]["elements"], [])
        self.assertEqual(model.requests[3]["state"]["route_feedback"][0]["operation"], "SELECT")
        keys = model.requests[4]["questions"]["operation"]["criteria"]
        self.assertIn("PRESS_KEY:shift+escape", keys)
        self.assertIn("editing focus", keys["PRESS_KEY:f2"])

    def test_cancel_does_not_replan_an_incomplete_observation(self):
        class Incomplete(HeadedDesktop):
            def observe(self, scope):
                result = super().observe(scope)
                result.screen["complete"] = False
                return result

        desktop = Incomplete([slides()])
        events = self.run_loop(desktop, Model("1", "SELECT", "CANCEL"))
        self.assertEqual(events[-1]["stop"], "selection_cancelled")
        self.assertEqual(desktop.actions, [])

        terminal = Model("DONE", "DONE")
        events = self.run_loop(desktop, terminal, options=RunOptions(verify_terminal=True))
        self.assertEqual(events[-1]["stop"], "completion_unverified")
        self.assertTrue(all("completion" not in request["questions"] for request in terminal.requests))

    def test_verified_unchanged_selection_withholds_exact_option_and_alias(self):
        document = slides()
        alias = document["children"][0]
        desktop = HeadedDesktop([document, alias])

        def refresh_refs():
            ref = f"@s{len(desktop.actions) + 1}"
            document["ref_id"] = f"{ref}:e1"
            alias["ref_id"] = f"{ref}:e2"

        desktop.on_execute = refresh_refs
        model = Model("1", "SELECT", "1", "1", "SELECT", "1", "DONE")
        events = self.run_loop(desktop, model)

        self.assertEqual([entry[-1] for entry in desktop.actions], ["Slide 2", "Slide 3"])
        self.assertNotIn("2", model.requests[3]["questions"]["target"]["criteria"])
        self.assertEqual(model.requests[5]["questions"]["option"]["criteria"]["1"]["label"], "Slide 3")
        self.assertTrue(
            any(event.get("diagnostic") == "verified_selection_without_observed_change" for event in events)
        )

    def test_selection_becomes_available_again_after_observed_change(self):
        document = slides()
        desktop = HeadedDesktop([document])

        def advance():
            if desktop.actions[-1][-1] == "Slide 3":
                document["children"][0]["states"] = []
                document["children"][1]["states"] = ["selected"]

        desktop.on_execute = advance
        model = Model("1", "SELECT", "1", "1", "SELECT", "1", "1", "SELECT", "1", "DONE")
        self.run_loop(desktop, model)
        self.assertEqual([entry[-1] for entry in desktop.actions], ["Slide 2", "Slide 3", "Slide 2"])

    def test_unverified_selection_does_not_withhold_an_option(self):
        desktop = HeadedDesktop([slides()])
        desktop.result = {"ok": True, "delivery": "delivered_unverified"}
        model = Model("1", "SELECT", "1", "1", "SELECT", "2", "DONE")
        self.run_loop(desktop, model)
        self.assertEqual([entry[-1] for entry in desktop.actions], ["Slide 2", "Slide 3"])
        self.assertEqual(model.requests[5]["questions"]["option"]["criteria"]["1"]["label"], "Slide 2")

    def test_premature_done_replans_and_review_receives_observed_edit_milestone(self):
        textfield = node(
            role="textfield",
            name="Note",
            states=["editable", "focused"],
            value="",
            available_actions=["SetValue"],
        )
        desktop = HeadedDesktop([textfield])
        desktop.on_execute = lambda: textfield.update(value=desktop.actions[-1][-1])
        model = Model("DONE", "needs_work", "1", "TYPE_TEXT", "DONE", "satisfied")
        events = self.run_loop(
            desktop,
            model,
            goal="Set the note to example",
            options=RunOptions(verify_terminal=True, text="example"),
        )

        self.assertEqual([entry[1] for entry in desktop.actions], ["TYPE_TEXT"])
        self.assertEqual(model.requests[2]["state"]["terminal_feedback"][0]["assessment"], "needs_work")
        self.assertEqual(
            model.requests[-1]["state"]["observed_milestones"][0]["observed_after"][0]["value"], "example"
        )
        self.assertEqual(events[-1]["stop"], "done")
        self.assertEqual(events[-1]["verification"]["assessment"], "satisfied")

    def test_done_without_evidence_is_bounded_and_cannot_write(self):
        desktop = HeadedDesktop([slides()])
        model = Model("DONE", "unverified", "DONE")
        events = self.run_loop(desktop, model, options=RunOptions(verify_terminal=True))

        self.assertEqual(events[-1]["stop"], "completion_unverified")
        self.assertEqual(desktop.actions, [])
        self.assertEqual(sum("completion" in request["questions"] for request in model.requests), 1)

    def test_terminal_review_cannot_exceed_the_original_budget(self):
        desktop = HeadedDesktop([slides()])
        model = Model("DONE")
        events = self.run_loop(
            desktop,
            model,
            options=RunOptions(verify_terminal=True),
            limits=Limits(calls=1),
        )
        self.assertIn("model call budget", events[-1]["stop"])
        self.assertEqual(desktop.actions, [])

    def test_blocked_and_infeasible_share_one_reconsideration_per_observation(self):
        desktop = HeadedDesktop([slides()])
        model = Model("BLOCKED", "INFEASIBLE")
        events = self.run_loop(
            desktop,
            model,
            options=RunOptions(verify_terminal=True, allow_infeasible=True, scope=Scope(window_id="w1")),
        )
        self.assertEqual(events[0]["recovery"], "reconsider_terminal_obstruction")
        self.assertEqual(model.requests[1]["state"]["terminal_feedback"][0]["proposal"], "BLOCKED")
        self.assertEqual(events[-1]["stop"], "infeasible")
        self.assertEqual(desktop.actions, [])
