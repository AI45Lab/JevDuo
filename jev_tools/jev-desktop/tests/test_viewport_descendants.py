import unittest

from jev_desktop import AgentDesktop, Scope
from jev_desktop.screen import overlay_role
from support import Transport, node, success


class ViewportDescendantTests(unittest.TestCase):
    def test_anonymous_focused_control_is_context_without_gaining_a_usable_ref(self):
        from jev_desktop.policy import action_space

        focused = node(role="combobox", name="", states=["focused", "haspopup"], ref_actionable=False)

        def handler(argv):
            if argv[0] == "list-surfaces":
                return success({"surfaces": []})
            return success({"tree": {"role": "window", "children": [focused]}, "complete": True,
                "window": {"id": "w1"}})

        seen = AgentDesktop(transport=Transport(handler)).observe(Scope(window_id="w1"))
        self.assertEqual(seen.screen["context"][0]["role"], "combobox")
        self.assertIn("focused", seen.screen["context"][0]["states"])
        space = action_space(seen.nodes, headed=True, drillable=False)
        self.assertFalse(space["by_target"])

    def test_clipped_parent_preserves_visible_child_context(self):
        child = node(name="Visible category")
        tree = {"role": "window", "children": [
            {"role": "group", "states": ["offscreen"], "children": [child]},
        ]}

        def handler(argv):
            if argv[0] == "list-surfaces":
                return success({"surfaces": []})
            return success({"tree": tree, "complete": True, "window": {"id": "w1"}})

        seen = AgentDesktop(transport=Transport(handler)).observe(Scope(window_id="w1"))
        self.assertEqual([n["name"] for n in seen.nodes], ["Visible category"])
        self.assertEqual([n["name"] for n in seen.screen["context"]], ["Visible category"])

    def test_child_overlay_does_not_inherit_its_parents_pointer_clipping(self):
        for state, expected in [("offscreen", "alert"), ("hidden", None)]:
            tree = {"role": "group", "states": [state], "children": [{"role": "alert"}]}
            self.assertEqual(overlay_role(tree), expected)

