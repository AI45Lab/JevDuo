import unittest

from jev_desktop import RunOptions
from jev_desktop.actions import extend_space, operation_description
from jev_desktop.policy import action_space, build_request
from support import node


class ContextMenuGuidanceTests(unittest.TestCase):
    def test_window_target_explains_context_menu_only_when_the_key_is_offered(self):
        screen = {"window_id": "w1", "window": "List", "headed_text": True}
        for keys, offered in [(('shift+f10',), True), (('return',), False)]:
            space = action_space([node()], headed=True, drillable=False)
            extend_space(space, screen, RunOptions(keys=keys))
            request = build_request("Use the selected item", screen, space, [])
            purpose = request["questions"]["target"]["criteria"]["WINDOW"]["purpose"]
            self.assertEqual("PRESS_KEY:shift+f10" in purpose, offered)
            if offered:
                self.assertIn("Verify selection", purpose)

    def test_context_menu_description_keeps_focus_distinct_from_selection(self):
        description = operation_description("PRESS_KEY:shift+f10")
        self.assertIn("FOCUS alone does not select", description)

