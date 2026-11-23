import unittest

from jev_desktop import AgentDesktop, DesktopConfig, JevLoop, RunOptions, Scope
from jev_desktop.option_activation import activation_node, activation_options
from support import Desktop, Model, Transport, failure, node


def picker():
    return node(
        role="listbox",
        name="Open",
        path=['window "Editor"'],
        available_actions=["Select"],
        children=[
            node(role="option", name="Desktop", ref_id="@s1:e2", available_actions=["Click"]),
            node(role="option", name="Downloads", ref_id="@s1:e3", available_actions=["Click"]),
        ],
    )


class OptionActivationTests(unittest.TestCase):
    def test_no_effect_activation_withholds_that_option_but_keeps_another(self):
        desktop = Desktop([picker()])
        desktop.result = {"ok": True, "delivery": "delivered_unverified"}
        model = Model("1", "ACTIVATE_OPTION", "1", "1", "ACTIVATE_OPTION", "1", "DONE")
        events = list(JevLoop(desktop, model, sleep=lambda _: None).run("Open a folder", RunOptions(replan_no_effect=True)))
        self.assertEqual(events[-1]["stop"], "done")
        self.assertEqual([a[2]["name"] for a in desktop.actions], ["Desktop", "Downloads"])

    def test_activation_cycle_withholds_the_same_option_when_the_complete_ui_returns(self):
        desktop = Desktop([picker()])
        desktop.result = {"ok": True, "delivery": "delivered_unverified"}
        def toggle():
            desktop.nodes[0]["states"] = [] if desktop.nodes[0].get("states") else ["expanded"]
        desktop.on_execute = toggle
        model = Model("1", "ACTIVATE_OPTION", "1", "1", "ACTIVATE_OPTION", "1", "1", "ACTIVATE_OPTION", "1", "DONE")
        events = list(JevLoop(desktop, model, sleep=lambda _: None).run("Open a folder", RunOptions(replan_no_effect=True)))
        self.assertEqual(events[-1]["stop"], "done")
        self.assertEqual([a[2]["name"] for a in desktop.actions], ["Desktop", "Desktop", "Downloads"])

    def test_loop_activates_the_chosen_option_ref_instead_of_selecting_the_container(self):
        desktop = Desktop([picker()])
        model = Model("1", "ACTIVATE_OPTION", "1", "DONE")
        events = list(JevLoop(desktop, model, sleep=lambda _: None).run("Open Desktop"))
        self.assertEqual(events[-1]["stop"], "done")
        self.assertEqual(len(desktop.actions), 1)
        _, operation, target, _ = desktop.actions[0]
        self.assertEqual(operation, "ACTIVATE_OPTION")
        self.assertEqual(target["ref_id"], "@s1:e2")
        self.assertEqual(target["path"], ['window "Editor"', 'listbox "Open"'])
        self.assertEqual(model.requests[2]["questions"]["option"]["criteria"]["1"]["label"], "Desktop")
        self.assertIn("native click target", model.requests[2]["questions"]["option"]["instructions"])

    def test_native_dispatch_clicks_the_option_without_container_select_or_text(self):
        transport = Transport()
        desktop = AgentDesktop(DesktopConfig(headed=True), transport)
        target = activation_node(picker(), activation_options(picker())[0])
        with desktop.run_context("Open Desktop", False):
            result = desktop.execute(
                Scope(app="Editor", window_id="w1"), "ACTIVATE_OPTION", target, "Desktop"
            )
        self.assertTrue(result["ok"])
        self.assertEqual(transport.calls[0][1], ["click", "@s1:e2"])

    def test_unproven_missing_hidden_or_duplicate_option_refs_are_not_offered(self):
        for change in (
            {"ref_actionable": False},
            {"ref_id": None},
            {"states": ["hidden"]},
            {"states": ["offscreen"]},
            {"available_actions": ["Select"]},
        ):
            with self.subTest(change=change):
                container = picker()
                container["children"][0].update(change)
                self.assertEqual([n["label"] for n in activation_options(container)], ["Downloads"])
        container = picker()
        container["children"][1]["ref_id"] = "@s1:e2"
        self.assertEqual(activation_options(container), [])
        with self.assertRaises(ValueError):
            activation_node(picker(), {"ref": "@unknown:e4"})

    def test_uncertain_activation_stops_without_replaying(self):
        desktop = Desktop([picker()])
        desktop.result = {**failure(), "delivery": "delivery_uncertain"}
        model = Model("1", "ACTIVATE_OPTION", "1")
        events = list(
            JevLoop(desktop, model, sleep=lambda _: None).run(
                "Open Desktop", RunOptions(replan_safe_failures=True)
            )
        )
        self.assertEqual(events[-1]["stop"], "action_failed")
        self.assertEqual(len(desktop.actions), 1)
