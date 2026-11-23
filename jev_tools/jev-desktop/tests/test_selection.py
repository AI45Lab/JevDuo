import json
import unittest

from jev_desktop import JevLoop, Limits, RunOptions
from jev_desktop.actions import extend_space
from jev_desktop.policy import action_space, build_operation_request, build_request
from support import Desktop, Model, node


class HeadedDesktop(Desktop):
    def observe(self, scope):
        seen = super().observe(scope)
        seen.screen["headed_text"] = True
        return seen

    def execute(self, scope, operation, target, text):
        result = super().execute(scope, operation, target, text)
        if result["ok"] and operation in ("TYPE_TEXT", "SELECT"):
            next(n for n in self.nodes if n["ref_id"] == target["ref_id"])["value"] = text
        return result


def option(label, index=2, **extra):
    return node(ref_id=f"@s1:e{index}", role="option", name=label, available_actions=["Select"], **extra)


def container(children, **extra):
    return node(role="listbox", name="Language", available_actions=["Select"], children=children, **extra)


class SelectionTests(unittest.TestCase):
    def test_container_selection_binds_unique_labels_without_using_globally_ambiguous_child_refs(self):
        from jev_desktop.option_activation import activation_options

        parent = container([
            option("French", ref_actionable=False, ref_failure="ambiguous_identity"),
            option("Unknown", 3, ref_actionable=False, ref_failure="identity_evidence_incomplete"),
        ])
        self.assertEqual(activation_options(parent), [])
        desktop = Desktop([parent])
        model = Model("1", "SELECT", "1", "DONE")
        events = list(JevLoop(desktop, model, sleep=lambda _: None).run("Choose French"))
        self.assertEqual(events[-1]["stop"], "done")
        self.assertEqual([(a[1], a[2]["ref_id"], a[-1]) for a in desktop.actions], [("SELECT", parent["ref_id"], "French")])
        self.assertIsNone(model.requests[2]["questions"]["option"]["criteria"]["1"]["ref"])
        parent["children"].append(option(" french ", 4, ref_actionable=False, ref_failure="ambiguous_identity"))
        self.assertNotIn("SELECT", action_space([parent])["by_target"].get("1", {}).get("operations", []))

    def test_selectable_document_exposes_its_observed_direct_group_choices(self):
        document = node(
            role="document",
            name="Page browser",
            available_actions=["Select", "SetFocus"],
            children=[
                {"role": "group", "name": "Page 1", "states": ["selected"]},
                {"role": "group", "name": "Product Comparison", "available_actions": ["SetFocus"]},
            ],
        )
        desktop = Desktop([document])
        model = Model("1", "SELECT", "2", "DONE")
        events = list(JevLoop(desktop, model, sleep=lambda _: None).run("Choose Product Comparison"))
        self.assertEqual(events[-1]["stop"], "done")
        self.assertEqual(desktop.actions[0][1], "SELECT")
        self.assertEqual(desktop.actions[0][-1], "Product Comparison")

    def test_document_choices_require_selection_and_do_not_descend_into_page_content(self):
        from jev_desktop.selection import observed_options

        document = node(
            role="document",
            available_actions=["Select"],
            children=[
                {"role": "group", "name": "Page", "children": [{"role": "option", "name": "Body text"}]},
                {"role": "group", "name": "Hidden page", "states": ["hidden"]},
            ],
        )
        self.assertEqual([n["label"] for n in observed_options(document)], ["Page"])
        self.assertEqual(observed_options({**document, "available_actions": ["SetFocus"]}), [])
        duplicated = {
            **document,
            "children": [{"role": "group", "name": "Page"}, {"role": "group", "name": "PAGE"}],
        }
        self.assertEqual(observed_options(duplicated), [])

    def test_shared_ancestor_paths_preserve_every_choice_without_repeating_the_text(self):
        nodes = [
            node(
                ref_id=f"@s1:e{i}",
                name=f"Item {i}",
                path=["window " + ("long title " * 40), "webarea Article", "group " + str(i // 10)],
            )
            for i in range(50)
        ]
        space = action_space(nodes)
        original = build_request("Inspect", {}, space, [])
        compact = build_request("Inspect", {}, space, [], compact=True)
        for index, item in compact["questions"]["target"]["criteria"].items():
            if index not in space["by_target"]:
                continue
            parts = []
            region = item["where"].get("region")
            while region:
                record = compact["state"]["regions"][region]
                parts.append(record["element"])
                region = record["parent"]
            self.assertEqual(
                " > ".join(reversed(parts)), original["questions"]["target"]["criteria"][index]["where"]
            )
            self.assertEqual(
                {
                    **{k: v for k, v in item.items() if k not in ("where", "action_set")},
                    **compact["state"]["action_sets"][item["action_set"]],
                },
                {k: v for k, v in original["questions"]["target"]["criteria"][index].items() if k != "where"},
            )
        self.assertEqual(compact["state"]["elements"], original["state"]["elements"])
        self.assertLess(len(json.dumps(compact)), len(json.dumps(original)) * 0.6)

    def test_compact_window_and_operation_keep_their_exact_capabilities(self):
        nodes = [
            node(available_actions=["Click"], path=[]),
            node(ref_id="@s1:e2", available_actions=["SetFocus"], value="text", path=["Editor"]),
        ]
        space = action_space(nodes, headed=True)
        screen = {"headed_text": True, "window_id": "w1", "window": "Editor", "root": "@s1:e0"}
        extend_space(space, screen, RunOptions())
        compact = build_request("Inspect", screen, space, [], compact=True)
        original = build_request("Inspect", screen, space, [])
        for index in space["by_target"]:
            item = compact["questions"]["target"]["criteria"][index]
            capabilities = compact["state"]["action_sets"][item["action_set"]]
            self.assertEqual(capabilities["operations"], space["by_target"][index]["operations"])
            self.assertEqual(
                capabilities["available_actions"],
                original["questions"]["target"]["criteria"][index]["available_actions"],
            )
        target = {"choice": "WINDOW", "confidence": 1.0, "probabilities": {"WINDOW": 1.0}}
        request = build_operation_request("Inspect", screen, space, [], target=target, compact=True)
        selected = request["state"]["selected_target"]["element"]
        self.assertEqual(
            selected["element"], original["questions"]["target"]["criteria"]["WINDOW"]["element"]
        )
        self.assertEqual(
            list(request["questions"]["operation"]["criteria"]),
            request["state"]["action_sets"][selected["action_set"]]["operations"],
        )

    def test_focus_is_offered_only_when_foreground_permission_is_enabled(self):
        target = node(available_actions=["SetFocus"])
        self.assertEqual(action_space([target], headed=False)["by_target"], {})
        self.assertIn("FOCUS", action_space([target], headed=True)["by_target"]["1"]["operations"])

    def test_leaf_selection_capability_never_requests_a_second_text_value(self):
        for role in ("option", "row", "tab", "treeitem", "menuitem"):
            n = option("https://example.com/film")
            n["role"] = role
            space = action_space([n], headed=True)
            self.assertNotIn("SELECT", space["by_target"].get("1", {}).get("operations", []))

    def test_container_selects_an_observed_label_without_text_supply(self):
        desktop = Desktop([container([option("English"), option("French", 3)])])
        model = Model("1", "SELECT", "2", "DONE")
        events = list(JevLoop(desktop, model, sleep=lambda _: None).run("Choose French"))
        self.assertEqual(events[-1]["stop"], "done")
        self.assertEqual(desktop.actions[0][1], "SELECT")
        self.assertEqual(desktop.actions[0][-1], "French")
        self.assertEqual(list(model.requests[2]["questions"]), ["option"])

    def test_select_between_two_url_writes_preserves_the_second_url(self):
        first = "https://example.com/film"
        second = "https://example.com/director"
        field = node(role="textfield", name="Address", available_actions=["SetValue"])
        choices = container([option("English", 3), option("French", 4)], ref_id="@s1:e2")
        desktop = HeadedDesktop([field, choices])
        model = Model(
            "1", "TYPE_TEXT", "WINDOW", "PRESS_KEY:return", "2", "SELECT", "2", "1", "TYPE_TEXT", "DONE"
        )
        events = list(
            JevLoop(desktop, model, sleep=lambda _: None).run(
                "Visit both pages and choose French", RunOptions(text=[first, second])
            )
        )
        self.assertEqual(events[-1]["stop"], "done")
        self.assertEqual(
            [(a[1], a[-1]) for a in desktop.actions],
            [("TYPE_TEXT", first), ("PRESS_KEY:return", None), ("SELECT", "French"), ("TYPE_TEXT", second)],
        )
        self.assertEqual(model.requests[4]["state"]["text_input"]["next_value"], second)
        self.assertEqual(model.requests[7]["state"]["text_input"]["remaining_values"], 1)

    def test_decision_sees_the_next_input_without_consuming_it(self):
        model = Model("DONE")
        desktop = Desktop()
        list(JevLoop(desktop, model).run("Inspect", RunOptions(text=["first", "second"])))
        self.assertEqual(model.requests[0]["state"]["text_input"]["next_value"], "first")
        self.assertEqual(model.requests[0]["state"]["text_input"]["remaining_values"], 2)
        self.assertEqual(desktop.actions, [])

    def test_no_values_omits_supplied_preview_and_value_only_choices(self):
        model = Model("DONE")
        list(JevLoop(Desktop(), model).run("Inspect", RunOptions(text=["PRIVATE-NEXT"], values=False)))
        self.assertNotIn("PRIVATE-NEXT", json.dumps(model.requests))
        space = action_space([container([option("", value="PRIVATE-OPTION")])], values=False)
        self.assertEqual(space["by_target"], {})

    def test_callback_is_not_called_to_build_a_preview_or_select_an_option(self):
        calls = []

        def callback(*args):
            calls.append(args)
            return "unused"

        model = Model("1", "SELECT", "1", "DONE")
        desktop = Desktop([container([option("English")])])
        list(JevLoop(desktop, model, sleep=lambda _: None).run("Choose English", RunOptions(text=callback)))
        self.assertEqual(calls, [])
        self.assertEqual(desktop.actions[0][-1], "English")
        self.assertEqual(model.requests[0]["state"]["text_input"]["source"], "callback")

    def test_select_never_calls_the_text_writer(self):
        class Writer:
            def generate(self, request):
                raise AssertionError("selection must not generate text")

        desktop = Desktop([container([option("French")])])
        events = list(
            JevLoop(desktop, Model("1", "SELECT", "1", "DONE"), sleep=lambda _: None).run(
                "Choose French", RunOptions(text_model=Writer())
            )
        )
        self.assertEqual(events[-1]["stop"], "done")
        self.assertEqual(desktop.actions[0][-1], "French")

    def test_duplicate_hidden_disabled_and_unproven_choices_are_not_offered(self):
        children = [
            option("Same", 2),
            option(" same ", 3),
            option("Hidden", 4, states=["hidden"]),
            option("Disabled", 5, states=["disabled"]),
            option("Unproven", 6, ref_actionable=False),
            option("French", 7),
        ]
        desktop = Desktop([container(children)])
        model = Model("1", "SELECT", "1", "DONE")
        list(JevLoop(desktop, model, sleep=lambda _: None).run("Choose French"))
        self.assertEqual(desktop.actions[0][-1], "French")
        self.assertEqual(list(model.requests[2]["questions"]["option"]["criteria"]), ["1", "CANCEL"])

    def test_nested_independent_container_choices_do_not_bind_to_the_parent(self):
        nested = container([option("Unrelated")], ref_id="@s1:e4")
        desktop = Desktop([container([nested, option("French")])])
        model = Model("1", "SELECT", "1", "DONE")
        list(JevLoop(desktop, model, sleep=lambda _: None).run("Choose French"))
        self.assertEqual(desktop.actions[0][-1], "French")

    def test_cancelled_and_invalid_option_choices_do_not_dispatch(self):
        for choice in ("CANCEL", "invented"):
            desktop = Desktop([container([option("French")])])
            model = Model("1", "SELECT", choice)
            if choice == "CANCEL":
                events = list(JevLoop(desktop, model, sleep=lambda _: None).run("Choose"))
                self.assertEqual(events[-1]["stop"], "selection_cancelled")
            else:
                with self.assertRaises(RuntimeError):
                    list(JevLoop(desktop, model, sleep=lambda _: None).run("Choose"))
            self.assertEqual(desktop.actions, [])

    def test_selection_pages_keep_the_choice_limit_and_bind_the_observed_value(self):
        desktop = Desktop([container([option(f"Language {i}", i + 2) for i in range(600)])])
        model = Model("1", "SELECT", "NEXT_PAGE", "301", "DONE")
        events = list(JevLoop(desktop, model, sleep=lambda _: None).run("Choose Language 300"))
        self.assertEqual(events[-1]["stop"], "done")
        self.assertEqual(desktop.actions[0][-1], "Language 300")
        for request in model.requests:
            for question in request["questions"].values():
                self.assertLessEqual(len(question["criteria"]), 255)

    def test_choice_phase_respects_the_original_model_budget(self):
        desktop = Desktop([container([option("French")])])
        model = Model("1", "SELECT")
        events = list(
            JevLoop(desktop, model, limits=Limits(calls=2), sleep=lambda _: None).run("Choose French")
        )
        self.assertIn("2 model call budget", events[-1]["stop"])
        self.assertEqual(desktop.actions, [])

    def test_native_selection_refusal_is_not_replayed(self):
        desktop = Desktop([container([option("French")])])
        desktop.result = {"ok": False, "delivery": "not_delivered", "error": {"code": "ACTION_NOT_SUPPORTED"}}
        events = list(JevLoop(desktop, Model("1", "SELECT", "1"), sleep=lambda _: None).run("Choose French"))
        self.assertEqual(events[-1]["stop"], "action_failed")
        self.assertEqual(len(desktop.actions), 1)
