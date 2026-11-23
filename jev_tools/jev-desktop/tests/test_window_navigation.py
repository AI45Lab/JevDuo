import unittest

from jev_desktop import JevLoop, Limits, RunOptions, Scope
from jev_desktop.window_navigation import application_windows
from support import Desktop, Model


class WindowNavigationTest(unittest.TestCase):
    def test_collapsed_tree_cell_replans_an_unverified_click_as_expansion(self):
        from support import node

        desktop = Desktop(
            [node(role="cell", name="Category", available_actions=["Click", "Expand", "Collapse"])]
        )
        desktop.result = {"ok": True, "delivery": "delivered_unverified"}
        model = Model("1", "CLICK", "1", "EXPAND", "DONE")
        events = list(
            JevLoop(desktop, model, sleep=lambda _: None).run(
                "Open category", RunOptions(replan_no_effect=True)
            )
        )
        self.assertEqual([a[1] for a in desktop.actions], ["CLICK", "EXPAND"])
        self.assertNotIn("CLICK", model.requests[3]["questions"]["operation"]["criteria"])
        self.assertEqual(events[-1]["stop"], "done")

    def test_expansion_replanning_requires_the_same_unique_collapsed_control(self):
        from jev_desktop.screen import expansion_still_collapsed
        from support import node

        cell = node(role="cell", name="Category", available_actions=["Click", "Expand"])
        self.assertTrue(expansion_still_collapsed(cell, [cell]))
        for after in (
            [],
            [cell, cell],
            [{**cell, "states": ["expanded"]}],
            [{**cell, "ref_actionable": False}],
            [{**cell, "available_actions": ["Click"]}],
            [{**cell, "name": "Other"}],
        ):
            self.assertFalse(expansion_still_collapsed(cell, after))
        self.assertFalse(expansion_still_collapsed({**cell, "role": "menuitem"}, [cell]))

    def test_unchanged_accessibility_does_not_prove_an_image_command_had_no_effect(self):
        from jev_desktop.screen import selection_still_unselected
        from support import node

        item = node(role="menuitem", name="Image operation", available_actions=["Click"])
        self.assertFalse(selection_still_unselected(item, [item]))
        cell = node(role="cell", name="Choice", available_actions=["Select"])
        self.assertTrue(selection_still_unselected(cell, [cell]))
        self.assertFalse(selection_still_unselected(cell, [{**cell, "states": ["selected"]}]))

    def test_owned_dialog_inside_selected_application_window_returns_to_that_window(self):
        from jev_desktop import DesktopError

        class Windows(Desktop):
            closed = False

            def application_windows(self, scope):
                return (
                    [
                        {
                            "id": "settings",
                            "app_name": "Editor",
                            "pid": 1,
                            "process_instance": "a",
                            "title": "Settings",
                        }
                    ]
                    if scope.window_id == "main"
                    else []
                )

            def owned_dialog(self, scope):
                return (
                    Scope(app="Portal", window_id="modal")
                    if scope.window_id == "settings" and not self.closed
                    else None
                )

            def observe(self, scope):
                if scope.window_id == "modal" and self.closed:
                    raise DesktopError("closed", "WINDOW_NOT_FOUND")
                return super().observe(scope)

            def execute(self, scope, operation, node, text):
                if scope.window_id == "modal":
                    self.closed = True
                return super().execute(scope, operation, node, text)

        desktop = Windows()
        model = Model("APP_WINDOW_0", "SWITCH_WINDOW", "WAIT", "1", "CLICK", "DONE")
        events = list(
            JevLoop(desktop, model, sleep=lambda _: None).run(
                "Choose value",
                RunOptions(
                    scope=Scope(app="Editor", window_id="main"),
                    follow_application_windows=True,
                    follow_owned_dialogs=True,
                ),
            )
        )
        resumed = next(e for e in events if e.get("recovery") == "owned_dialog_closed")
        self.assertEqual(resumed["scope"]["window_id"], "settings")
        self.assertEqual(events[-1]["stop"], "done")
        self.assertEqual(desktop.observations[-1].window_id, "settings")

    def test_table_cell_selection_uses_observed_name_and_native_capability(self):
        from jev_desktop.policy import action_space
        from support import node

        cell = node(
            role="cell", name="Choice", path=['window "Editor"', "table"], available_actions=["Select"]
        )
        offered = action_space([cell])["by_target"]["1"]["operations"]
        self.assertIn("SELECT_ITEM", offered)
        self.assertNotIn(
            "SELECT_ITEM",
            action_space([{**cell, "available_actions": ["Click"]}])["by_target"]["1"]["operations"],
        )

    def test_unverified_no_effect_click_offers_an_alternative_without_replay(self):
        class Headed(Desktop):
            def observe(self, scope):
                seen = super().observe(scope)
                seen.screen["headed_text"] = True
                return seen

        from support import node

        desktop = Headed(
            [
                node(
                    role="cell",
                    available_actions=["Click", "Select"],
                    path=["table"],
                    bounds={"x": 10, "y": 10, "width": 20, "height": 20},
                )
            ]
        )
        desktop.result = {"ok": True, "delivery": "delivered_unverified"}
        model = Model("1", "CLICK", "1", "POINTER_CLICK", "DONE")
        events = list(
            JevLoop(desktop, model, sleep=lambda _: None).run("Activate", RunOptions(replan_no_effect=True))
        )
        self.assertEqual([a[1] for a in desktop.actions], ["CLICK", "POINTER_CLICK"])
        self.assertNotIn("CLICK", model.requests[3]["questions"]["operation"]["criteria"])
        self.assertTrue(
            any(e.get("diagnostic") == "unverified_click_without_observed_change" for e in events)
        )

    def test_inventory_rejects_other_process_instances_and_unreadable_windows(self):
        owner = {"id": "main", "app_name": "Editor", "pid": 1, "process_instance": "a"}
        child = {**owner, "id": "settings", "visible": True, "accessible": True}
        rows = [
            owner,
            child,
            {**child, "id": "reused", "process_instance": "b"},
            {**child, "id": "hidden", "visible": False},
        ]
        result = application_windows(
            lambda *args: {"ok": True, "data": rows}, Scope(app="Editor", window_id="main")
        )
        self.assertEqual(result, [child])

    def test_model_selects_another_window_without_replaying_opener(self):
        class Windows(Desktop):
            def application_windows(self, scope):
                return [
                    {
                        "id": "settings",
                        "app_name": "Editor",
                        "pid": 1,
                        "process_instance": "a",
                        "title": "Settings",
                    }
                ]

        desktop = Windows()
        model = Model("APP_WINDOW_0", "SWITCH_WINDOW", "DONE")
        events = list(
            JevLoop(desktop, model, limits=Limits(steps=3, calls=5)).run(
                "Adjust setting",
                RunOptions(scope=Scope(app="Editor", window_id="main"), follow_application_windows=True),
            )
        )
        self.assertEqual(events[0]["turn"]["operation"], "SWITCH_WINDOW")
        self.assertEqual(desktop.observations[-1].window_id, "settings")
        self.assertEqual(desktop.actions, [])
        self.assertEqual(events[-1]["stop"], "done")

    def test_window_changed_during_decision_does_not_switch(self):
        class Windows(Desktop):
            reads = 0

            def application_windows(self, scope):
                self.reads += 1
                return (
                    [{"id": "settings", "app_name": "Editor", "pid": 1, "process_instance": "a"}]
                    if self.reads == 1
                    else []
                )

        desktop = Windows()
        events = list(
            JevLoop(desktop, Model("APP_WINDOW_0", "SWITCH_WINDOW", "DONE")).run(
                "Adjust setting",
                RunOptions(scope=Scope(app="Editor", window_id="main"), follow_application_windows=True),
            )
        )
        self.assertEqual(events[0]["recovery"], "application_window_changed")
        self.assertTrue(all(s.window_id == "main" for s in desktop.observations))
        self.assertEqual(desktop.actions, [])


class ScopedInventoryTests(unittest.TestCase):
    def test_navigation_reads_only_the_current_application_before_identity_filtering(self):
        calls = []
        def cli(*argv):
            calls.append(argv)
            return {"ok": True, "data": [
                {"id": "main", "app_name": "Editor", "pid": 1, "process_instance": "one"},
                {"id": "dialog", "app_name": "Editor", "pid": 1, "process_instance": "one", "visible": True, "accessible": True},
                {"id": "other", "app_name": "Editor", "pid": 2, "process_instance": "two", "visible": True, "accessible": True},
            ]}
        result = application_windows(cli, Scope(app="Editor", window_id="main"))
        self.assertEqual(calls, [("list-windows", "--app", "Editor")])
        self.assertEqual([item["id"] for item in result], ["dialog"])
