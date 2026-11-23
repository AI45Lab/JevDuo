import copy
import json
import unittest

from jev_desktop import JevLoop, Limits, RunOptions, Scope, TextModelError
from support import Desktop, Model, node


class Writer:
    def __init__(self, value="生成的标题", on_generate=None):
        self.value = value
        self.requests = []
        self.on_generate = on_generate

    def generate(self, request):
        self.requests.append(copy.deepcopy(request))
        if self.on_generate:
            self.on_generate(request)
        return self.value


def editor():
    return Desktop(
        [node(role="textfield", name="Title", value="old", available_actions=["SetValue", "Click"])]
    )


class TextLoopTests(unittest.TestCase):
    def test_changed_field_discards_text_before_fresh_target_selection(self):
        desktop = editor()
        values = iter(("old receiver text", "new receiver text"))

        class RegeneratingWriter:
            def __init__(self):
                self.requests = []

            def generate(self, request):
                self.requests.append(request)
                if len(self.requests) == 1:
                    desktop.nodes[0].update(name="Ready editor", ref_id="@s2:e2", value="ready")
                return next(values)

        writer = RegeneratingWriter()
        model = Model("1", "TYPE_TEXT", "1", "TYPE_TEXT", "DONE")
        events = self.run_loop(desktop, model, RunOptions(
            text_model=writer, text_replans=1, replan_changed_text_targets=True,
        ))
        self.assertEqual(events[-1]["stop"], "done")
        self.assertEqual(len(model.requests), 5)
        self.assertEqual(writer.requests[1].field["what"], 'textfield "Ready editor"')
        self.assertEqual([(action[2]["ref_id"], action[-1]) for action in desktop.actions], [("@s2:e2", "new receiver text")])

    def test_changed_receiver_replanning_refuses_unproven_scope(self):
        for change in ({"window_id": "other"}, {"app": "Other"}, {"root": "other"}, {"complete": False}, {"unclassified_surfaces": [{"id": "unknown"}]}):
            with self.subTest(change=change):
                class ChangedDesktop(Desktop):
                    changed = False
                    screen_change = change

                    def observe(self, scope):
                        seen = super().observe(scope)
                        if self.changed:
                            seen.screen.update(self.screen_change)
                        return seen

                desktop = ChangedDesktop(editor().nodes)
                writer = Writer(on_generate=lambda _, desktop=desktop: setattr(desktop, "changed", True))
                events = self.run_loop(desktop, Model("1", "TYPE_TEXT"), RunOptions(
                    text_model=writer, text_replans=2, replan_changed_text_targets=True,
                ))
                self.assertEqual(events[-1]["stop"], "screen_changed_during_text_generation")
                self.assertEqual(desktop.actions, [])

    def test_changed_field_replans_remain_bounded_without_writing(self):
        desktop = editor()
        writer = Writer(on_generate=lambda _: desktop.nodes[0].update(name=desktop.nodes[0]["name"] + "x"))
        events = self.run_loop(desktop, Model("1", "TYPE_TEXT", "1", "TYPE_TEXT"), RunOptions(
            text_model=writer, text_replans=1, replan_changed_text_targets=True,
        ))
        self.assertEqual(events[-1]["stop"], "screen_changed_during_text_generation")
        self.assertEqual(len(writer.requests), 2)
        self.assertEqual(desktop.actions, [])

    def test_context_change_discards_generated_text_and_requires_new_decisions(self):
        desktop = editor()
        desktop.nodes.append(node(role="tab", name="First metadata", ref_id="@s1:e2", available_actions=[]))
        values = iter(("discarded text", "fresh text"))

        class RegeneratingWriter:
            def __init__(self):
                self.requests = []

            def generate(self, request):
                self.requests.append(request)
                if len(self.requests) == 1:
                    desktop.nodes[1]["name"] = "Updated metadata"
                return next(values)

        writer = RegeneratingWriter()
        model = Model("1", "TYPE_TEXT", "1", "TYPE_TEXT", "DONE")
        events = self.run_loop(
            desktop,
            model,
            RunOptions(
                text_model=writer,
                text_replans=2,
                reference_context={"guest_local_time": "2026-11-13T01:00:00+08:00"},
            ),
        )
        self.assertEqual(events[-1]["stop"], "done")
        self.assertEqual(len(writer.requests), 2)
        self.assertEqual([action[-1] for action in desktop.actions], ["fresh text"])
        self.assertTrue(any(event.get("recovery") == "discard_text_and_replan" for event in events))
        self.assertEqual(len(model.requests), 5)
        self.assertIn("guest_local_time", writer.requests[0].reference_context)

    def test_replanning_stays_bounded_when_context_keeps_changing(self):
        desktop = editor()
        desktop.nodes.append(node(role="tab", name="Initial", ref_id="@s1:e2", available_actions=[]))
        writer = Writer(on_generate=lambda _: desktop.nodes[1].update(name=desktop.nodes[1]["name"] + "x"))
        events = self.run_loop(
            desktop, Model("1", "TYPE_TEXT", "1", "TYPE_TEXT"), RunOptions(text_model=writer, text_replans=1)
        )
        self.assertEqual(events[-1]["stop"], "screen_changed_during_text_generation")
        self.assertEqual(desktop.actions, [])
        self.assertEqual(len(writer.requests), 2)

    def run_loop(self, desktop, model, options, *, limits=None):
        result = list(
            JevLoop(desktop, model, limits=limits, sleep=lambda _: None).run("写一个关于月球的标题", options)
        )
        self.assertTrue(desktop.cleaned)
        return result

    def test_generate_only_after_target_and_operation_then_verify_screen(self):
        desktop, model = editor(), Model("1", "TYPE_TEXT", "DONE")
        writer = Writer()
        events = self.run_loop(desktop, model, RunOptions(text_model=writer))
        self.assertEqual(events[-1]["stop"], "done")
        self.assertEqual(len(writer.requests), 1)
        self.assertEqual(writer.requests[0].goal, "写一个关于月球的标题")
        self.assertEqual(writer.requests[0].field["what"], 'textfield "Title"')
        self.assertEqual(writer.requests[0].field["holds"], "old")
        self.assertEqual(writer.requests[0].screen["app"], "Fixture")
        self.assertEqual(desktop.actions[0][-1], writer.value)
        self.assertEqual(desktop.actions[0][0].window_id, "w1")
        self.assertEqual(events[0]["turn"]["text_source"], "text_model")
        self.assertNotIn(writer.value, json.dumps(events, ensure_ascii=False))
        self.assertEqual(len(desktop.observations), 4)
        self.assertEqual(model.requests[0]["state"]["text_input"]["source"], "text_model")
        self.assertIn("Generate", model.requests[1]["questions"]["operation"]["criteria"]["TYPE_TEXT"])

    def test_click_and_terminals_do_not_generate(self):
        for choices in (("DONE",), ("BLOCKED",), ("1", "CLICK", "DONE")):
            with self.subTest(choices=choices):
                desktop, writer = editor(), Writer()
                self.run_loop(desktop, Model(*choices), RunOptions(text_model=writer))
                self.assertEqual(writer.requests, [])

    def test_declined_generated_text_reselects_an_alternate_operation_without_writing(self):
        desktop = Desktop([
            node(role="textfield", name="Empty document", available_actions=["SetValue"]),
            node(ref_id="@s1:e2", name="Save As"),
        ])
        model = Model("1", "TYPE_TEXT", "2", "CLICK", "DONE")
        writer = Writer(None)
        events = self.run_loop(desktop, model, RunOptions(
            text_model=writer, replan_safe_failures=True, text_replans=1,
        ))
        self.assertEqual(events[-1]["stop"], "done")
        self.assertEqual([action[1] for action in desktop.actions], ["CLICK"])
        self.assertEqual(len(writer.requests), 1)
        retry = model.requests[2]
        self.assertNotIn("1", retry["questions"]["target"]["criteria"])
        self.assertIn("No input was dispatched", retry["state"]["text_generation_feedback"][0]["result"])

    def test_declined_text_replanning_is_bounded_across_different_fields(self):
        desktop = Desktop([node(ref_id=f"@s1:e{i}", role="textfield", name=f"Field {i}", available_actions=["SetValue"]) for i in range(3)])
        writer = Writer(None)
        events = self.run_loop(desktop, Model("1", "TYPE_TEXT", "2", "TYPE_TEXT", "3", "TYPE_TEXT"), RunOptions(
            text_model=writer, replan_safe_failures=True, text_replans=2,
        ))
        self.assertIn("no value was supplied", events[-1]["stop"])
        self.assertEqual(len(writer.requests), 3)
        self.assertEqual(desktop.actions, [])

    def test_scope_refusal_withholds_equivalent_text_modes_before_another_generation(self):
        desktop = Desktop([
            node(role="textfield", name="Unrelated settings file", value="old", available_actions=["SetValue", "TypeText", "Click"]),
            node(ref_id="@s1:e2", name="Open requested preferences"),
        ])
        writer = Writer("unrequested replacement")
        model = Model("1", "TYPE_TEXT", "broader_or_unproven", "2", "CLICK", "within_request", "DONE")
        events = self.run_loop(desktop, model, RunOptions(text_model=writer, review_goal_scope=True))
        self.assertEqual(events[-1]["stop"], "done")
        self.assertEqual(len(writer.requests), 1)
        target_retry = model.requests[3]
        text_actions = target_retry["state"]["action_sets"][target_retry["questions"]["target"]["criteria"]["1"]["action_set"]]["operations"]
        self.assertFalse({"TYPE_TEXT", "INSERT_TEXT", "CLEAR"}.intersection(text_actions))
        self.assertEqual([action[1] for action in desktop.actions], ["CLICK"])

    def test_empty_malformed_or_declined_text_never_executes(self):
        for value in (None, "", " \n ", 123, {"text": "unexpected"}):
            with self.subTest(value=value):
                desktop, writer = editor(), Writer(value)
                events = self.run_loop(
                    desktop, Model("1", "TYPE_TEXT", "DONE"), RunOptions(text_model=writer)
                )
                self.assertIn("no value was supplied", events[-1]["stop"])
                self.assertEqual(desktop.actions, [])
                self.assertEqual(len(desktop.observations), 1)

    def test_model_error_is_sanitized_and_does_not_dispatch(self):
        def fail(_request):
            raise RuntimeError("provider echoed PRIVATE-GENERATION")

        desktop, writer = editor(), Writer(on_generate=fail)
        with self.assertRaisesRegex(RuntimeError, "text_model failed") as caught:
            self.run_loop(desktop, Model("1", "TYPE_TEXT"), RunOptions(text_model=writer))
        self.assertNotIn("PRIVATE-GENERATION", str(caught.exception))
        self.assertTrue(desktop.cleaned)
        self.assertEqual(desktop.actions, [])

    def test_no_values_applies_to_both_models(self):
        desktop, model, writer = editor(), Model("1", "TYPE_TEXT", "DONE"), Writer()
        self.run_loop(desktop, model, RunOptions(text_model=writer, values=False))
        self.assertNotIn("holds", writer.requests[0].field)
        self.assertNotIn("PRIVATE-VALUE", str(writer.requests))
        self.assertNotIn("PRIVATE-VALUE", str(model.requests))
        self.assertTrue(all("value" not in entry for entry in writer.requests[0].screen["visible_context"]))

    def test_supplied_values_first_then_model_and_callback_refusal_is_final(self):
        desktop, writer = editor(), Writer()
        events = self.run_loop(
            desktop,
            Model("1", "TYPE_TEXT", "1", "TYPE_TEXT", "DONE"),
            RunOptions(text="explicit", text_model=writer),
        )
        self.assertEqual([item[-1] for item in desktop.actions], ["explicit", writer.value])
        self.assertEqual(
            [e["turn"]["text_source"] for e in events if "turn" in e], ["supplied", "text_model"]
        )
        self.assertEqual(len(writer.requests), 1)
        desktop, writer = editor(), Writer()
        events = self.run_loop(
            desktop, Model("1", "TYPE_TEXT"), RunOptions(text=lambda f, h: None, text_model=writer)
        )
        self.assertIn("no value was supplied", events[-1]["stop"])
        self.assertEqual(writer.requests, [])

    def test_generation_counts_toward_budget_before_call(self):
        for budget in (2, 3):
            with self.subTest(budget=budget):
                desktop, writer = editor(), Writer()
                events = self.run_loop(
                    desktop,
                    Model("1", "TYPE_TEXT"),
                    RunOptions(text_model=writer),
                    limits=Limits(calls=budget),
                )
                self.assertIn("model call budget", events[-1]["stop"])
                self.assertEqual(len(writer.requests), budget - 2)
                self.assertEqual(len(desktop.actions), budget - 2)

    def test_destructive_gate_precedes_generation(self):
        desktop, writer = editor(), Writer()
        model = Model("1", "TYPE_TEXT", {"answers": {"destructive": {"noul": 0.9}}})
        events = self.run_loop(desktop, model, RunOptions(text_model=writer, confirm_destructive=True))
        self.assertTrue(events[-1]["stop"].startswith("confirm:"))
        self.assertEqual(writer.requests, [])
        self.assertEqual(desktop.actions, [])

    def test_changed_field_or_state_stops_after_generation_without_writing(self):
        for changed in (
            {"value": "new human input"},
            {"name": "Another field"},
            {"states": ["disabled"]},
            {"bounds": {"x": 2, "y": 4}},
            {"path": ["another form"]},
        ):
            with self.subTest(changed=changed):
                desktop = editor()
                writer = Writer(
                    on_generate=lambda _r, changed=changed, desktop=desktop: desktop.nodes[0].update(changed)
                )
                events = self.run_loop(
                    desktop, Model("1", "TYPE_TEXT", "DONE"), RunOptions(text_model=writer)
                )
                self.assertEqual(events[-1]["stop"], "screen_changed_during_text_generation")
                self.assertEqual(desktop.actions, [])
                self.assertNotIn(writer.value, str(events))

    def test_rotating_unaddressable_refs_are_ignored_but_changed_identity_evidence_stops(self):
        for change_failure in (False, True):
            with self.subTest(change_failure=change_failure):
                desktop = editor()
                original = desktop.observe
                reason = ["ambiguous_identity"]

                def observe(scope, original=original, desktop=desktop, reason=reason):
                    seen = original(scope)
                    seen.screen["unaddressable_targets"] = [
                        {
                            "role": "button",
                            "name": "Close",
                            "ref_id": f"@snapshot{len(desktop.observations)}:e4",
                            "ref_failure": reason[0],
                        }
                    ]
                    return seen

                desktop.observe = observe
                writer = Writer(
                    on_generate=(
                        (lambda _r, reason=reason: reason.__setitem__(0, "unread_identity"))
                        if change_failure
                        else None
                    )
                )
                events = self.run_loop(
                    desktop, Model("1", "TYPE_TEXT", "DONE"), RunOptions(text_model=writer)
                )
                self.assertEqual(len(writer.requests), 1)
                self.assertEqual(len(desktop.actions), 0 if change_failure else 1)
                self.assertEqual(
                    events[-1]["stop"], "screen_changed_during_text_generation" if change_failure else "done"
                )

    def test_window_closure_and_observation_error_do_not_write_or_regenerate(self):
        for code in ("WINDOW_NOT_FOUND", "TIMEOUT"):
            with self.subTest(code=code):
                desktop = editor()
                writer = Writer(
                    on_generate=lambda _r, code=code, desktop=desktop: setattr(desktop, "observe_error", code)
                )
                if code == "WINDOW_NOT_FOUND":
                    events = self.run_loop(desktop, Model("1", "TYPE_TEXT"), RunOptions(text_model=writer))
                    self.assertEqual(events[-1]["stop"], "window_closed")
                else:
                    with self.assertRaisesRegex(RuntimeError, "read failed"):
                        self.run_loop(desktop, Model("1", "TYPE_TEXT"), RunOptions(text_model=writer))
                self.assertTrue(desktop.cleaned)
                self.assertEqual(desktop.actions, [])
                self.assertEqual(len(writer.requests), 1)

    def test_ref_rotation_is_allowed_and_request_mutation_is_detached(self):
        class RotatingDesktop(Desktop):
            def observe(self, scope):
                result = super().observe(scope)
                result.nodes[0]["ref_id"] = f"@s{len(self.observations)}:e1"
                return result

        desktop = RotatingDesktop(editor().nodes)

        def mutate(request):
            request.field.clear()
            request.screen.clear()
            request.history.append({"invalid": True})

        events = self.run_loop(
            desktop,
            Model("1", "TYPE_TEXT", "DONE"),
            RunOptions(text_model=Writer(on_generate=mutate), scope=Scope(window_id="w1")),
        )
        self.assertEqual(events[-1]["stop"], "done")
        self.assertEqual(len(desktop.actions), 1)
        self.assertNotIn("invalid", str(events))

    def test_generated_text_preserves_whitespace_and_redacts_delivery_evidence(self):
        value = '  标题 "月球"\n第二行  '
        desktop, writer = editor(), Writer(value)
        desktop.result.update(
            argv=["set-value", "@s1:e1", value], details={"echo": value}, post_state={"value": value}
        )
        events = self.run_loop(desktop, Model("1", "TYPE_TEXT", "DONE"), RunOptions(text_model=writer))
        self.assertEqual(desktop.actions[0][-1], value)
        self.assertNotIn(value, str(events))
        self.assertEqual(events[0]["turn"]["argv"][-1], "[redacted]")

    def test_changed_context_without_element_change_stops(self):
        class ContextDesktop(Desktop):
            marker = "old context"

            def observe(self, scope):
                result = super().observe(scope)
                result.screen["context"] = [{"role": "statictext", "name": self.marker}]
                return result

        desktop = ContextDesktop(editor().nodes)
        writer = Writer(on_generate=lambda _: setattr(desktop, "marker", "new context"))
        events = self.run_loop(desktop, Model("1", "TYPE_TEXT"), RunOptions(text_model=writer))
        self.assertEqual(events[-1]["stop"], "screen_changed_during_text_generation")
        self.assertEqual(desktop.actions, [])

    def test_adapter_text_error_is_preserved_without_regeneration(self):
        def fail(_request):
            raise TextModelError("text_model: HTTP 401")

        desktop, writer = editor(), Writer(on_generate=fail)
        with self.assertRaisesRegex(TextModelError, "HTTP 401"):
            self.run_loop(desktop, Model("1", "TYPE_TEXT"), RunOptions(text_model=writer))
        self.assertEqual(len(writer.requests), 1)
        self.assertEqual(desktop.actions, [])

    def test_failed_generated_write_is_not_replayed_or_regenerated(self):
        desktop, writer = editor(), Writer()
        desktop.result = {"ok": False, "delivery": "delivery_uncertain", "error": {"code": "TIMEOUT"}}
        events = self.run_loop(desktop, Model("1", "TYPE_TEXT"), RunOptions(
            text_model=writer, text_replans=2, replan_changed_text_targets=True,
        ))
        self.assertEqual(events[-1]["stop"], "action_failed")
        self.assertEqual(events[0]["turn"]["delivery"], "delivery_uncertain")
        self.assertEqual(len(writer.requests), 1)
        self.assertEqual(len(desktop.actions), 1)
