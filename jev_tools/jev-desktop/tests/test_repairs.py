import copy
import unittest

from jev_desktop import AgentDesktop, DesktopConfig, JevLoop, Observation, RunOptions, Scope
from jev_desktop.actions import extend_space
from jev_desktop.policy import action_space
from jev_desktop.screen import overlay_role, target_identity
from jev_desktop.selection import observed_options
from jev_desktop.selection_value import selected_value
from jev_desktop.text_request import unchanged_text_target
from support import Desktop, Model, Transport, failure, node, success


class RepairTests(unittest.TestCase):
    def test_selection_uses_semantic_route_without_window_focus(self):
        transport = Transport()
        desktop = AgentDesktop(DesktopConfig(headed=True), transport)
        with desktop.run_context("Choose", False):
            desktop.execute(Scope(app="Fixture", window_id="w1"), "SELECT", node(role="combobox"), "Large")
        self.assertEqual(transport.calls[0][1], ["select", "@s1:e1", "Large"])
        self.assertEqual(len(transport.calls), 1)

    def test_pointer_selection_fallback_needs_explicit_safe_non_delivery(self):
        for delivery in ("not_delivered", "delivered_unverified", "delivery_uncertain"):
            for retry in ("safe", "unsafe"):
                refusal = failure(delivery=delivery, retry=retry)
                refusal["error"]["details"] = {"kind": "pointer_required"}
                replies = iter((refusal, success()))
                transport = Transport(lambda _, replies=replies: next(replies))
                desktop = AgentDesktop(DesktopConfig(headed=True), transport)
                with desktop.run_context("Choose", False):
                    desktop.execute(Scope(app="Fixture"), "SELECT", node(role="combobox"), "Large")
                expected = 2 if delivery == "not_delivered" and retry == "safe" else 1
                self.assertEqual(len(transport.calls), expected)

    def test_embedded_combo_value_uses_explicit_selection_even_in_closed_menu(self):
        selected = node(role="menuitem", name="Large", states=["selected"])
        menu = node(role="menu", states=["hidden"], children=[selected])
        combo = node(role="combobox", value="\ufffc", children=[menu])
        self.assertEqual(selected_value(combo), "Large")
        selected["states"] = ["focused"]
        self.assertIsNone(selected_value(combo))
        selected["states"] = ["selected"]
        menu["children"].append(node(role="menuitem", name="Small", states=["selected"]))
        self.assertIsNone(selected_value(combo))
        menu["children"] = [selected]
        menu["children_count"] = 5
        self.assertIsNone(selected_value(combo))

    def test_owned_nested_combo_menu_choices_and_other_container_boundary(self):
        option = node(role="menuitem", name="Light")
        combo = node(role="combobox", children=[node(role="menu", children=[option])])
        self.assertEqual([n["label"] for n in observed_options(combo)], ["Light"])
        combo["children"].append(node(role="combobox", children=[node(role="menuitem", name="Other")]))
        self.assertEqual([n["label"] for n in observed_options(combo)], ["Light"])
        combo["children"][0]["states"] = ["hidden"]
        self.assertEqual(observed_options(combo), [])

    def test_bad_geometry_retains_semantic_actions_and_excludes_pointer_drag(self):
        for width, height in ((0, 10), (10, 0), (-1, 10), (float("nan"), 10), (float("inf"), 10)):
            field = node(bounds={"x": 0, "y": 0, "width": width, "height": height})
            space = action_space(
                [field, node(ref_id="@s1:e2", bounds={"x": 1, "y": 1, "width": 10, "height": 10})],
                headed=True,
            )
            extend_space(space, {"headed_text": True, "window_id": "w1"}, RunOptions())
            self.assertEqual(space["by_target"]["1"]["operations"], ["CLICK"])

    def test_failed_drag_pair_stays_filtered_after_target_indices_rotate(self):
        source = node(
            name="Source",
            available_actions=["Click", "Drag"],
            bounds={"x": 0, "y": 0, "width": 10, "height": 10},
        )
        destination = node(
            name="Destination", ref_id="@s1:e2", bounds={"x": 30, "y": 0, "width": 10, "height": 10}
        )
        blocked = {
            "operation": "DRAG_TO:2",
            "source_identity": target_identity(source),
            "destination_identity": target_identity(destination),
            "window_id": "w1",
        }
        for nodes in ([source, destination], [destination, source]):
            space = action_space(copy.deepcopy(nodes), headed=True)
            extend_space(
                space,
                {"headed_text": True, "window_id": "w1"},
                RunOptions(blocked_actions=(blocked,), replan_safe_failures=True),
            )
            entry = next(e for e in space["by_target"].values() if e["node"].get("name") == "Source")
            self.assertNotIn("DRAG", entry["operations"])

    def test_new_alert_takes_priority_over_an_existing_navigation_menu(self):
        tree = {"role": "window", "children": [{"role": "menu"}, {"role": "alert"}]}
        self.assertEqual(overlay_role(tree), "alert")
        tree["children"][1]["states"] = ["hidden"]
        self.assertEqual(overlay_role(tree), "menu")

    def test_document_alerts_stay_in_the_window_and_do_not_mask_native_menus(self):
        web = {"role": "webarea", "children": [{"role": "alert", "name": "Status"}]}
        self.assertIsNone(overlay_role({"role": "window", "children": [web]}))
        self.assertEqual(overlay_role({"role": "window", "children": [web, {"role": "menu"}]}), "menu")

    def test_text_replan_requires_unique_unchanged_field_and_same_window(self):
        target = node(role="textfield", name="Query", value="", states=["focused"])
        before = Observation(
            [target],
            {"app": "Fixture", "window_id": "w1", "window": "Test", "surface": "window", "complete": True},
        )
        after = copy.deepcopy(before)
        after.screen["context"] = [{"role": "tab", "name": "Changed metadata"}]
        self.assertTrue(unchanged_text_target(before, after, target))
        for key, value in (("value", "user edit"), ("states", []), ("path", ["Other page"])):
            changed = copy.deepcopy(after)
            changed.nodes[0][key] = value
            self.assertFalse(unchanged_text_target(before, changed, target))
        after.nodes.append(copy.deepcopy(target))
        self.assertFalse(unchanged_text_target(before, after, target))

    def test_infeasible_is_explicit_and_emits_no_native_write(self):
        desktop = Desktop()
        events = list(
            JevLoop(desktop, Model("INFEASIBLE")).run("Impossible goal", RunOptions(allow_infeasible=True))
        )
        self.assertEqual(events[-1]["stop"], "infeasible")
        self.assertEqual(desktop.actions, [])
