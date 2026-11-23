import unittest

from jev_desktop import JevLoop, RunOptions
from jev_desktop.goal_scope import preparation_operation
from support import Desktop, Model, node


class MenuPreparationTests(unittest.TestCase):
    def test_rejected_drag_destination_is_removed_without_blocking_another_destination(self):
        class Headed(Desktop):
            def observe(self, scope):
                seen = super().observe(scope)
                seen.screen["headed_text"] = True
                return seen

        desktop = Headed(
            [
                node(name="Source", bounds={"x": 0, "y": 0, "width": 20, "height": 20}),
                node(
                    ref_id="@s1:e2",
                    name="Outside request",
                    bounds={"x": 40, "y": 0, "width": 20, "height": 20},
                ),
                node(
                    ref_id="@s1:e3",
                    name="Requested destination",
                    bounds={"x": 80, "y": 0, "width": 20, "height": 20},
                ),
            ]
        )
        model = Model("1", "DRAG", "2", "broader_or_unproven", "1", "DRAG", "3", "within_request", "DONE")
        events = list(
            JevLoop(desktop, model, sleep=lambda _: None).run(
                "Move to requested destination", RunOptions(review_goal_scope=True)
            )
        )
        self.assertEqual([a[1] for a in desktop.actions], ["DRAG_TO:3"])
        self.assertNotIn("2", model.requests[6]["questions"]["destination"]["criteria"])
        self.assertEqual(model.requests[3]["state"]["proposed_destination"]["name"], "Outside request")
        self.assertEqual(model.requests[7]["state"]["proposed_destination"]["name"], "Requested destination")
        self.assertEqual(events[-1]["stop"], "done")

    def test_drag_rejection_tracks_observed_destination_when_choice_indices_change(self):
        from jev_desktop.actions import extend_space
        from jev_desktop.goal_scope import filter_rejections, state_key
        from jev_desktop.policy import action_space

        source = node(name="Source", bounds={"x": 0, "y": 0, "width": 20, "height": 20})
        destination = node(
            ref_id="@s1:e2", name="Destination", bounds={"x": 40, "y": 0, "width": 20, "height": 20}
        )
        seen = Desktop([source, destination]).observe(RunOptions().scope)
        screen = {**seen.screen, "headed_text": True}
        space = action_space([source, destination], headed=True)
        extend_space(space, screen, RunOptions())
        rejected = state_key(seen, "DRAG_TO:2", space["by_target"]["1"]["node"])
        shifted = action_space([source, node(ref_id="@s1:e4", name="Other"), destination], headed=True)
        extend_space(shifted, screen, RunOptions())
        filter_rejections(shifted, seen, [rejected])
        self.assertNotIn("DRAG", shifted["by_target"]["1"]["operations"])

    def test_opening_a_native_menu_needs_no_commit_review(self):
        desktop = Desktop([node(role="menu", name="Format")])
        model = Model("1", "CLICK", "DONE")
        events = list(
            JevLoop(desktop, model, sleep=lambda _: None).run(
                "Change document appearance", RunOptions(review_goal_scope=True)
            )
        )
        self.assertEqual(events[-1]["stop"], "done")
        self.assertEqual(len(desktop.actions), 1)
        self.assertFalse(any("goal_scope" in r["questions"] for r in model.requests))

    def test_menu_leaf_commands_still_receive_scope_review(self):
        desktop = Desktop([node(role="menuitem", name="Apply format")])
        model = Model("1", "CLICK", "broader_or_unproven", "DONE")
        events = list(
            JevLoop(desktop, model, sleep=lambda _: None).run(
                "Change document appearance", RunOptions(review_goal_scope=True)
            )
        )
        self.assertEqual(events[-1]["stop"], "done")
        self.assertEqual(desktop.actions, [])
        self.assertTrue(any("goal_scope" in r["questions"] for r in model.requests))

    def test_repeated_clicks_on_a_menu_are_not_single_opener_preparation(self):
        self.assertFalse(preparation_operation("DOUBLE_CLICK", node(role="menu")))
        self.assertFalse(preparation_operation("TRIPLE_CLICK", node(role="menu")))
        self.assertFalse(preparation_operation("CLICK", node(role="menubutton")))
        self.assertTrue(preparation_operation("CLICK", node(role="menubutton", states=["haspopup"])))
