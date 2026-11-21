import unittest

from jev_desktop import JevLoop, Limits
from jev_desktop.policy import action_space, build_request
from jev_desktop.target_pages import target_page
from support import Desktop, Model, node


class TargetPageTests(unittest.TestCase):
    def test_every_observed_candidate_is_offered_once_with_stable_refs(self):
        nodes = [node(ref_id=f"@s1:e{i}", name=f"Candidate {i}") for i in range(301)]
        space = action_space(nodes, max_elements=len(nodes))
        space["by_target"]["WINDOW"] = {"node": {"role": "window"}, "operations": ["PRESS_KEY:escape"]}
        offered = []
        for index in range(4):
            page = target_page(space, index)
            request = build_request("goal", {"context": nodes}, page, [])
            choices = request["questions"]["target"]["criteria"]
            self.assertLessEqual(len(choices), 104)
            self.assertEqual(request["state"]["visible_context"], nodes)
            self.assertIn("WINDOW", choices)
            self.assertEqual("PREVIOUS_PAGE" in choices, index > 0)
            self.assertEqual("NEXT_PAGE" in choices, index < 3)
            offered.extend(item["node"]["ref_id"] for key, item in page["by_target"].items() if key != "WINDOW")
        self.assertEqual(offered, [n["ref_id"] for n in nodes])
        with self.assertRaises(ValueError):
            target_page(space, 4)

    def test_focused_and_editable_controls_appear_before_pointer_only_content(self):
        nodes = [node(ref_id=f"@s1:e{i}") for i in range(110)]
        nodes.extend([
            node(ref_id="@s1:e111", role="combobox", available_actions=["SetFocus"], value="/home/user/"),
            node(ref_id="@s1:e112", role="button", states=["focused"]),
        ])
        space = action_space(nodes, headed=True, max_elements=len(nodes))
        first = target_page(space, 0)
        self.assertEqual(list(first["by_target"])[:2], ["112", "111"])
        self.assertEqual(first["by_target"]["111"]["node"]["ref_id"], "@s1:e111")
        second = target_page(space, 1)
        self.assertEqual(len(first["by_target"]) + len(second["by_target"]), len(nodes))

    def test_semantic_widget_capabilities_are_not_buried_by_focusable_decoration(self):
        nodes = [node(ref_id=f"@s1:e{i}", name=f"Focusable {i}", available_actions=["Click", "SetFocus"]) for i in range(110)]
        nodes.append(node(ref_id="@s1:e111", role="checkbox", name="Desired preference", available_actions=["Toggle"]))
        space = action_space(nodes, max_elements=len(nodes))
        first = target_page(space, 0)
        self.assertEqual(next(iter(first["by_target"])), "111")
        self.assertEqual(first["by_target"]["111"]["node"]["ref_id"], "@s1:e111")
        self.assertEqual(first["by_target"]["111"]["operations"], ["CHECK", "UNCHECK"])

    def test_loop_can_act_beyond_old_cutoff_without_gui_paging_writes(self):
        desktop = Desktop([node(ref_id=f"@s1:e{i}", name=f"Candidate {i}") for i in range(301)])
        model = Model("NEXT_PAGE", "NEXT_PAGE", "NEXT_PAGE", "PREVIOUS_PAGE", "NEXT_PAGE", "301", "CLICK", "DONE")
        events = list(JevLoop(desktop, model, limits=Limits(steps=2, calls=10)).run("Click Candidate 300"))
        self.assertEqual(events[-1]["stop"], "done")
        self.assertEqual(len(desktop.actions), 1)
        self.assertEqual(desktop.actions[0][2]["ref_id"], "@s1:e300")
        self.assertEqual(len(model.requests), 8)
        revisited = model.requests[5]["state"]["candidate_page"]
        self.assertEqual(revisited["visited_indices"], [0, 1, 2, 3])
        self.assertEqual(revisited["recent_indices"], [0, 1, 2, 3, 2, 3])
        self.assertIn("already inspected", model.requests[3]["questions"]["target"]["criteria"]["PREVIOUS_PAGE"])
        self.assertEqual(model.requests[7]["state"]["candidate_page"]["visited_indices"], [0])

    def test_page_browsing_consumes_model_call_budget(self):
        desktop = Desktop([node(ref_id=f"@s1:e{i}") for i in range(200)])
        events = list(JevLoop(desktop, Model("NEXT_PAGE", "PREVIOUS_PAGE"), limits=Limits(steps=2, calls=2)).run("goal"))
        self.assertIn("model call budget", events[-1]["stop"])
        self.assertEqual(desktop.actions, [])
