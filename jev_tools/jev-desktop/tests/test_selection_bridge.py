import copy
import unittest
from contextlib import contextmanager

from jev_desktop import AgentDesktop, DesktopConfig, JevLoop, RunOptions, Scope
from jev_desktop.actions import extend_space
from jev_desktop.policy import action_space
from jev_desktop.selection import observed_options, selection_bindings
from support import Desktop, Model, Transport, node


def fixture():
    child = node(ref_id="@s1:e2", role="group", name="Caption", available_actions=["SetFocus"])
    owner = node(
        ref_id="@s1:e1", role="document", name="Canvas", available_actions=["Select"], children=[child]
    )
    return owner, child


class SelectionBridgeTests(unittest.TestCase):
    def test_qualified_child_selection_uses_its_observed_owner_and_label(self):
        owner, child = fixture()
        original = copy.deepcopy([owner, child])
        space = action_space([owner, child], headed=True)
        target = space["by_target"]["2"]
        self.assertIn("SELECT_ITEM", target["operations"])
        self.assertEqual([owner, child], original)
        transport = Transport()
        desktop = AgentDesktop(DesktopConfig(headed=True), transport)
        with desktop.run_context("Choose Caption", False):
            result = desktop.execute(
                Scope(window_id="pinned"), "SELECT_ITEM", target["node"], "IGNORED CALLER TEXT"
            )
        self.assertTrue(result["ok"])
        self.assertEqual(transport.calls[-1][1], ["select", "@s1:e1", "Caption"])

    def test_rejects_unproven_owners_and_unavailable_or_duplicate_choices(self):
        for mutation in (
            "owner-unproven",
            "owner-unavailable",
            "owner-not-selectable",
            "duplicate",
            "child-unavailable",
            "child-unproven",
        ):
            with self.subTest(mutation=mutation):
                owner, child = fixture()
                if mutation == "owner-unproven":
                    owner["ref_actionable"] = False
                if mutation == "owner-unavailable":
                    owner["states"] = ["hidden"]
                if mutation == "owner-not-selectable":
                    owner["available_actions"] = []
                if mutation == "duplicate":
                    owner["children"].append({**child, "ref_id": "@s1:e3", "name": "CAPTION"})
                if mutation == "child-unavailable":
                    child["states"] = ["offscreen"]
                if mutation == "child-unproven":
                    child["ref_actionable"] = False
                self.assertEqual(selection_bindings([owner, child]), {})

    def test_competing_owners_are_not_silently_chosen(self):
        owner, child = fixture()
        other = {**owner, "ref_id": "@s1:e3"}
        self.assertEqual(selection_bindings([owner, other, child]), {})

    def test_owner_selection_stays_available_when_child_has_no_ref(self):
        owner, child = fixture()
        child.pop("ref_id")
        self.assertEqual(selection_bindings([owner]), {})
        space = action_space([owner])
        self.assertIn("SELECT", space["by_target"]["1"]["operations"])
        self.assertEqual(space["by_target"]["1"]["selection_options"][0]["label"], "Caption")

    def test_caller_restrictions_still_withhold_the_bound_action(self):
        owner, child = fixture()
        space = extend_space(
            action_space([owner, child], headed=True),
            {"window_id": "pinned", "headed_text": True},
            RunOptions(allowed_operations=("FOCUS",)),
        )
        self.assertTrue(
            all("SELECT_ITEM" not in target["operations"] for target in space["by_target"].values())
        )

    def test_standard_combo_options_can_use_the_same_selection_contract(self):
        owner, child = fixture()
        owner["role"] = "combobox"
        child["role"] = "option"
        self.assertEqual(
            selection_bindings([owner, child])["@s1:e2"], {"ref_id": "@s1:e1", "label": "Caption"}
        )

    def test_value_labels_are_not_exposed_when_values_are_disabled(self):
        owner, child = fixture()
        child["name"] = ""
        child["value"] = "PRIVATE"
        self.assertEqual(selection_bindings([owner, child], values=False), {})

    def test_uncertain_native_selection_is_not_replayed(self):
        owner, child = fixture()
        target = action_space([owner, child])["by_target"]["2"]["node"]

        def handler(argv):
            return {
                "ok": False,
                "error": {
                    "code": "TIMEOUT",
                    "disposition": {"delivery": "delivery_uncertain", "retry": "unsafe"},
                },
            }

        transport = Transport(handler)
        desktop = AgentDesktop(DesktopConfig(headed=True), transport)
        with desktop.run_context("Choose Caption", False):
            result = desktop.execute(Scope(window_id="pinned"), "SELECT_ITEM", target, None)
        self.assertFalse(result["ok"])
        self.assertEqual(len(transport.calls), 1)

    def test_model_to_native_flow_uses_observed_value_label_without_supplied_text(self):
        owner, child = fixture()
        child.pop("name")
        child["value"] = "Observed Value"
        transport = Transport()
        native = AgentDesktop(DesktopConfig(headed=True), transport)

        class BoundDesktop(Desktop):
            @contextmanager
            def run_context(self, goal, cursor):
                with native.run_context(goal, cursor):
                    yield

            def execute(self, scope, operation, target, text):
                return native.execute(scope, operation, target, text)

        desktop = BoundDesktop([owner, child])
        events = list(
            JevLoop(desktop, Model("2", "SELECT_ITEM", "DONE"), sleep=lambda _: None).run(
                "Choose the observed option"
            )
        )
        self.assertEqual(events[-1]["stop"], "done")
        selections = [argv for _, argv, _ in transport.calls if argv[0] == "select"]
        self.assertEqual(selections, [["select", "@s1:e1", "Observed Value"]])

    def test_document_exposes_image_and_table_objects_without_descending_into_cells(self):
        for role in ("image", "table"):
            with self.subTest(role=role):
                owner, child = fixture()
                child["role"] = role
                child["children"] = [node(ref_id="@s1:e3", role="option", name="Nested cell")]
                self.assertEqual([item["label"] for item in observed_options(owner)], ["Caption"])
                self.assertEqual(selection_bindings([owner, child])["@s1:e2"]["ref_id"], "@s1:e1")
