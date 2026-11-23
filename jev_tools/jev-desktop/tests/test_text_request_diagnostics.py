import unittest

from jev_desktop.text_request import build_text_request
from support import node


class TextRequestDiagnosticsTests(unittest.TestCase):
    def test_physical_fallback_explains_literal_input_limits_without_changing_semantic_fields(self):
        target = node(role="textfield", name="Editor", available_actions=["SetFocus"], states=["editable"])
        request = build_text_request("Update a setting", {"headed_text": True}, target, [])
        self.assertEqual(request.field["input_constraints"]["route"], "physical_keyboard")
        self.assertIn("single-line JSON", request.field["input_constraints"]["guidance"])
        self.assertIn("explicitly requested formatting", request.field["input_constraints"]["guidance"])
        for actions, screen in ((["SetValue"], {"headed_text": True}), (["TypeText"], {"headed_text": True}), (["SetFocus"], {})):
            with self.subTest(actions=actions, screen=screen):
                target["available_actions"] = actions
                self.assertNotIn("input_constraints", build_text_request("Update", screen, target, []).field)

    def test_text_writer_keeps_control_meanings_and_counts_without_unusable_ref_ids(self):
        screen = {
            "app": "Editor",
            "context": [{"role": "textfield", "name": "Path", "value": "/home/user/"}],
            "unaddressable_targets": [
                {
                    "role": "button",
                    "name": "Install",
                    "ref_id": "@s1:e2",
                    "ref_failure": "ambiguous_identity",
                },
                {
                    "role": "button",
                    "name": "Install",
                    "ref_id": "@s1:e3",
                    "ref_failure": "ambiguous_identity",
                },
            ],
        }
        request = build_text_request("goal", screen, node(), [])
        self.assertEqual(request.screen["visible_context"], screen["context"])
        self.assertEqual(
            request.screen["ref_diagnostics"],
            [
                {
                    "control": {
                        "role": "button",
                        "name": "Install",
                        "ref_failure": "ambiguous_identity",
                    },
                    "count": 2,
                }
            ],
        )
        self.assertNotIn("@s1:e2", str(request.screen))
        self.assertNotIn("unaddressable_targets", request.screen)
        request.screen["visible_context"][0]["value"] = "changed"
        self.assertEqual(screen["context"][0]["value"], "/home/user/")
        self.assertEqual(screen["unaddressable_targets"][0]["ref_id"], "@s1:e2")

    def test_values_false_applies_to_diagnostic_control_values(self):
        screen = {"unaddressable_targets": [{"role": "textfield", "value": "PRIVATE", "ref_id": "@s1:e1"}]}
        request = build_text_request("goal", screen, node(), [], values=False)
        self.assertNotIn("PRIVATE", str(request))
