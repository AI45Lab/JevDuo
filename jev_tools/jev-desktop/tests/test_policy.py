import unittest

from jev_desktop import Limits
from jev_desktop.policy import (
    action_space,
    build_operation_request,
    build_request,
    launch_request,
    validate_choice,
)
from jev_desktop.screen import collect, fingerprint, offerable
from support import answer, node


class PolicyTests(unittest.TestCase):
    def test_physical_text_requires_explicit_permission_and_readable_editable_target(self):
        field = node(role="textfield", value="old", available_actions=["Click", "SetFocus"])
        self.assertNotIn("TYPE_TEXT", action_space([field])["by_target"]["1"]["operations"])
        self.assertIn("TYPE_TEXT", action_space([field], headed=True)["by_target"]["1"]["operations"])
        empty = {k: v for k, v in field.items() if k != "value"}
        self.assertIn("TYPE_TEXT", action_space([empty], headed=True)["by_target"]["1"]["operations"])
        for change in ({"states": ["readonly"]}, {"states": ["secure"]}, {"value": None}, {"role": "button"}):
            with self.subTest(change=change):
                space = action_space([{**field, **change}], headed=True)
                self.assertNotIn("TYPE_TEXT", space["by_target"]["1"]["operations"])
        space = action_space([field], headed=True, typable=False)
        self.assertNotIn("TYPE_TEXT", space["by_target"]["1"]["operations"])

    def test_physical_combobox_input_requires_positive_editability_evidence(self):
        combo = node(role="combobox", value="Red", available_actions=["Click", "SetFocus"])
        for states, expected in [([], False), (["editable"], True), (["editable", "readonly"], False), (["editable", "secure"], False)]:
            with self.subTest(states=states):
                offered = action_space([{**combo, "states": states}], headed=True)["by_target"]["1"]["operations"]
                self.assertEqual("TYPE_TEXT" in offered, expected)
                self.assertEqual("INSERT_TEXT" in offered, expected)
        semantic = {**combo, "available_actions": ["SetValue"]}
        self.assertIn("TYPE_TEXT", action_space([semantic])["by_target"]["1"]["operations"])

    def test_unaddressable_ref_diagnostics_explain_missing_controls_without_offering_them(self):
        nodes = [node(name="Open extension")]
        screen = {"unaddressable_targets": [
            {"role": "button", "name": "Install", "ref_id": "@s1:e2", "ref_failure": "ambiguous_identity"},
            {"role": "button", "name": "Install", "ref_id": "@s1:e3", "ref_failure": "ambiguous_identity"},
        ]}
        request = build_request("Install extension", screen, action_space(nodes), [])
        self.assertEqual(request["state"]["ref_diagnostics"], [{"control": {
            "role": "button", "name": "Install", "ref_failure": "ambiguous_identity",
        }, "count": 2}])
        self.assertNotIn("@s1:e2", str(request))
        self.assertNotIn("@s1:e3", str(request))
        self.assertEqual(screen["unaddressable_targets"][0]["ref_id"], "@s1:e2")
        self.assertIn("supported window keys", request["questions"]["target"]["instructions"]["rules"])

    def test_capabilities_and_text_availability(self):
        nodes = [
            node(),
            node(role="checkbox", available_actions=["Toggle"]),
            node(role="textfield", available_actions=["SetValue", "Click"]),
        ]
        space = action_space(nodes, typable=False)
        self.assertEqual(space["by_target"]["1"]["operations"], ["CLICK"])
        self.assertEqual(space["by_target"]["2"]["operations"], ["CHECK", "UNCHECK"])
        self.assertEqual(space["by_target"]["3"]["operations"], ["CLICK"])

    def test_dense_observation_reserves_control_slots(self):
        nodes = [node(ref_id=f"@s1:e{i}") for i in range(300)]
        space = action_space(nodes)
        request = build_request("goal", {"root": "@s1:e0"}, space, [])
        self.assertEqual(len(request["questions"]["target"]["criteria"]), 255)
        self.assertTrue(space["truncated"])
        self.assertIn("prefer a region", request["questions"]["target"]["instructions"]["rules"])
        self.assertFalse(action_space(nodes[:251])["truncated"])
        self.assertEqual(
            len(
                launch_request("goal", [{"id": str(i)} for i in range(600)])["questions"]["application"][
                    "criteria"
                ]
            ),
            253,
        )

    def test_no_values_in_every_model_stage(self):
        n = node(role="textfield", value="PRIVATE", available_actions=["SetValue"])
        screen = {"context": [n]}
        space = action_space([n], values=False)
        request = build_operation_request("goal", screen, space, [], target=answer("1"), values=False)
        self.assertNotIn("PRIVATE", str(request))

    def test_choices_validate_numeric_range_and_option_membership(self):
        for bad in (
            None,
            {},
            answer("missing"),
            answer("1", confidence=True),
            answer("1", confidence=float("nan")),
            {"choice": "1", "confidence": 1, "probabilities": {"1": 0.2}},
            {"choice": "1", "confidence": 1, "probabilities": {"missing": 1}},
            {"choice": "1", "confidence": 1, "probabilities": {"1": -0.1, "DONE": 1.1}},
        ):
            with self.subTest(bad=bad):
                with self.assertRaises(ValueError):
                    validate_choice(bad, ["1", "DONE"])
        self.assertEqual(validate_choice(answer("1", confidence=0), ["1"])["confidence"], 0)

    def test_tree_context_and_inert_nodes(self):
        tree = {
            "role": "window",
            "children": [
                node(role="treeitem", children=[node(role="cell")]),
                node(states=["disabled"]),
                node(states=["hidden"]),
                node(available_actions=[]),
            ],
        }
        nodes = offerable(collect(tree))
        self.assertEqual(len(nodes), 1)
        self.assertEqual(nodes[0]["path"], ["window"])

    def test_state_comparison_ignores_ref_rotation_but_sees_effects(self):
        self.assertEqual(fingerprint([node()]), fingerprint([node(ref_id="@s2:e2")]))
        for change in (
            {"name": "Other"},
            {"value": "new"},
            {"states": ["checked"]},
            {"bounds": {"x": 5}},
            {"available_actions": ["Click", "Expand"]},
        ):
            self.assertNotEqual(fingerprint([node()]), fingerprint([node(**change)]))

    def test_invalid_budgets(self):
        for value in (0, -1, True, 1.2):
            with self.assertRaises(ValueError):
                Limits(calls=value)
