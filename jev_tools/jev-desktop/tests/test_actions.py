import json
import unittest

from jev_desktop import AgentDesktop, DesktopConfig, JevLoop, RunOptions, Scope
from jev_desktop.actions import extend_space
from jev_desktop.policy import action_space
from support import Desktop, Model, Transport, node, success


class HeadedDesktop(Desktop):
    def observe(self, scope):
        seen = super().observe(scope)
        seen.screen["headed_text"] = True
        return seen


class ActionTests(unittest.TestCase):
    def test_context_menu_key_preserves_pinned_window_and_caller_restrictions(self):
        desktop = HeadedDesktop()
        model = Model("WINDOW", "PRESS_KEY:shift+f10", "DONE")
        events = list(JevLoop(desktop, model, sleep=lambda _: None).run(
            "Open the focused control context menu", RunOptions()
        ))
        self.assertEqual(events[-1]["stop"], "done")
        self.assertEqual(desktop.actions[0][1], "PRESS_KEY:shift+f10")
        self.assertEqual(desktop.actions[0][0].window_id, "w1")
        for headed, keys in ((False, None), (True, ("return",))):
            options = RunOptions() if keys is None else RunOptions(keys=keys)
            space = extend_space(action_space([]), {"headed_text": headed, "window_id": "w1"}, options)
            offered = space["by_target"].get("WINDOW", {}).get("operations", [])
            self.assertNotIn("PRESS_KEY:shift+f10", offered)

    def test_default_menu_accelerator_reaches_only_the_pinned_window(self):
        desktop = HeadedDesktop()
        model = Model("WINDOW", "PRESS_KEY:alt+t", "DONE")
        events = list(
            JevLoop(desktop, model, sleep=lambda _: None).run("Open application tools", RunOptions())
        )
        self.assertEqual(events[-1]["stop"], "done")
        self.assertEqual(desktop.actions[0][1], "PRESS_KEY:alt+t")
        self.assertEqual(desktop.actions[0][0].window_id, "w1")

    def test_caller_key_restrictions_still_hide_default_accelerators(self):
        desktop = HeadedDesktop()
        model = Model("WINDOW", "PRESS_KEY:return", "DONE")
        list(JevLoop(desktop, model, sleep=lambda _: None).run("Confirm", RunOptions(keys=("return",))))
        offered = model.requests[1]["questions"]["operation"]["criteria"]
        self.assertNotIn("PRESS_KEY:alt+t", offered)
        self.assertEqual(desktop.actions[0][1], "PRESS_KEY:return")

    def test_window_return_can_submit_after_typing(self):
        desktop = HeadedDesktop([node(role="textfield", available_actions=["SetValue"], value="query")])
        desktop.on_execute = lambda: desktop.nodes[0].update(value="loaded results")
        model = Model("WINDOW", "PRESS_KEY:return", "DONE")
        events = list(JevLoop(desktop, model, sleep=lambda _: None).run("Submit search", RunOptions()))
        self.assertEqual(events[-1]["stop"], "done")
        self.assertEqual(desktop.actions[0][1], "PRESS_KEY:return")
        self.assertEqual(desktop.actions[0][0].window_id, "w1")

    def test_insert_constraint_removes_replacement_and_keeps_exact_caller_text(self):
        desktop = HeadedDesktop([node(role="textfield", available_actions=["SetFocus"], value="existing")])
        model = Model("1", "INSERT_TEXT", "DONE")
        options = RunOptions(text=" appended", input_mode="insert", allowed_operations=("INSERT_TEXT",))
        list(JevLoop(desktop, model, sleep=lambda _: None).run("Append", options))
        offered = model.requests[1]["questions"]["operation"]["criteria"]
        self.assertEqual(list(offered), ["INSERT_TEXT"])
        self.assertEqual(desktop.actions[0][-1], " appended")

    def test_native_commands_include_physical_permission_and_exact_window(self):
        transport = Transport()
        desktop = AgentDesktop(DesktopConfig(headed=True), transport)
        field = node(drag_targets={"2": "@s1:e2"})
        with desktop.run_context("goal", False):
            for operation in (
                "POINTER_CLICK",
                "INSERT_TEXT",
                "FOCUS",
                "SCROLL_UP",
                "SCROLL_LEFT",
                "SCROLL_RIGHT",
                "RIGHT_CLICK",
                "DOUBLE_CLICK",
                "HOVER",
                "DRAG_TO:2",
            ):
                desktop.execute(Scope("Editor", window_id="w1"), operation, field, "value")
            desktop.execute(Scope("Editor", window_id="w1"), "PRESS_KEY:return", field, None)
        self.assertTrue(all(argv[0] == "--headed" for _, argv, _ in transport.calls))
        self.assertEqual(
            transport.calls[-1][1], ["--headed", "press", "return", "--window-id", "w1", "--app", "Editor"]
        )
        self.assertEqual(transport.calls[1][1], ["--headed", "type", "@s1:e1", "value"])
        self.assertEqual(transport.calls[-2][1], ["--headed", "drag", "--from", "@s1:e1", "--to", "@s1:e2"])

    def test_non_headed_or_unpinned_keys_never_reach_transport(self):
        for headed, scope in ((False, Scope(window_id="w1")), (True, Scope())):
            transport = Transport()
            desktop = AgentDesktop(DesktopConfig(headed=headed), transport)
            with desktop.run_context("goal", False), self.assertRaises(RuntimeError):
                desktop.execute(scope, "PRESS_KEY:return", None, None)
            self.assertEqual(transport.calls, [])

    def test_drag_bindings_have_no_cycles_and_unproven_refs_are_not_offered(self):
        nodes = [
            node(bounds={"x": 0, "y": 0, "width": 20, "height": 20}),
            node(ref_id="@s1:e2", bounds={"x": 50, "y": 0, "width": 20, "height": 20}),
            node(ref_id="@s1:e3", ref_actionable=False),
        ]
        space = action_space(nodes, headed=True)
        extend_space(space, {"headed_text": True, "window_id": "w1"}, RunOptions())
        self.assertIn("DRAG", space["by_target"]["1"]["operations"])
        self.assertNotIn("3", space["by_target"])
        json.dumps(space)

    def test_uncertain_write_cannot_be_replayed_by_a_new_loop(self):
        desktop = HeadedDesktop(
            [node(role="textfield", name="Name", value="", available_actions=["SetFocus"])]
        )
        options = RunOptions(
            text="private",
            blocked_actions=(
                {
                    "operation": "TYPE_TEXT",
                    "target": 'textfield "Name"',
                    "window_id": "w1",
                },
            ),
        )
        events = list(JevLoop(desktop, Model("1", "INSERT_TEXT"), sleep=lambda _: None).run("Retry", options))
        self.assertEqual(events[-1]["stop"], "unsafe_write_replay_blocked")
        self.assertEqual(desktop.actions, [])

    def test_unscoped_unresolved_delivery_blocks_dispatch_in_a_new_window(self):
        desktop = HeadedDesktop()
        options = RunOptions(blocked_actions=({"operation": "*", "target": "*", "window_id": None},))
        events = list(JevLoop(desktop, Model("1", "CLICK"), sleep=lambda _: None).run("Retry", options))
        self.assertEqual(events[-1]["stop"], "unsafe_write_replay_blocked")
        self.assertEqual(desktop.actions, [])

    def test_drag_chooses_a_bound_destination_in_a_third_decision(self):
        desktop = HeadedDesktop(
            [
                node(bounds={"x": 0, "y": 0, "width": 20, "height": 20}),
                node(ref_id="@s1:e2", name="Drop here", bounds={"x": 50, "y": 0, "width": 20, "height": 20}),
            ]
        )
        model = Model("1", "DRAG", "2", "DONE")
        list(
            JevLoop(desktop, model, sleep=lambda _: None).run(
                "Drag", RunOptions(allowed_operations=("DRAG",))
            )
        )
        self.assertEqual(desktop.actions[0][1], "DRAG_TO:2")
        self.assertEqual(desktop.actions[0][2]["drag_targets"]["2"], "@s1:e2")
        self.assertEqual(list(model.requests[2]["questions"]), ["destination"])

    def test_dense_headed_screen_stays_within_choice_limit(self):
        desktop = HeadedDesktop([node(ref_id=f"@s1:e{i}") for i in range(400)])
        model = Model("BLOCKED")
        list(JevLoop(desktop, model, sleep=lambda _: None).run("Inspect", RunOptions()))
        self.assertLessEqual(len(model.requests[0]["questions"]["target"]["criteria"]), 255)
        self.assertEqual(model.requests[0]["state"]["candidate_page"]["total_candidates"], 400)
        self.assertIn("NEXT_PAGE", model.requests[0]["questions"]["target"]["criteria"])

    def test_interrupt_while_settling_preserves_the_dispatched_action(self):
        def interrupted(_):
            raise RuntimeError("stopped")

        desktop = HeadedDesktop()
        events = []
        with self.assertRaisesRegex(RuntimeError, "stopped"):
            for event in JevLoop(desktop, Model("1", "CLICK"), sleep=interrupted).run("Apply"):
                events.append(event)
        self.assertEqual(events[0]["turn"]["operation"], "CLICK")
        self.assertTrue(events[0]["turn"]["ok"])
        self.assertTrue(desktop.cleaned)

    def test_repeated_blocker_rechecks_actual_context_before_stopping(self):
        previous = {
            "app": "Fixture",
            "window": "Test",
            "context": [{"role": "textfield", "value": "PRIVATE-VALUE"}],
        }
        for changed in (False, True):
            blocker = {**previous, "context": []} if changed else previous
            desktop = HeadedDesktop()
            model = Model("DONE")
            events = list(
                JevLoop(desktop, model, sleep=lambda _: None).run(
                    "Retry", RunOptions(previous_blocker=blocker)
                )
            )
            self.assertTrue(desktop.observations)
            self.assertEqual(events[-1]["stop"], "done" if changed else "repeated_native_blocker")
            self.assertEqual(len(model.requests), int(changed))

    def test_incomplete_read_uses_a_new_snapshot_without_sending_an_action(self):
        reads = []

        def handler(argv):
            if argv[0] == "list-surfaces":
                return success({"surfaces": []})
            reads.append(argv)
            return success(
                {
                    "app": "Editor",
                    "window": {"id": "w1"},
                    "complete": len(reads) > 1,
                    "identity_unproven": "cut short" if len(reads) == 1 else None,
                    "tree": {"role": "window", "children": [node(ref_id=f"@s{len(reads)}:e1")]},
                }
            )

        desktop = AgentDesktop(DesktopConfig(snapshot_timeout_ms=3000), Transport(handler))
        seen = desktop.observe(Scope(window_id="w1"))
        self.assertEqual(seen.nodes[0]["ref_id"], "@s2:e1")
        self.assertEqual(reads[0][reads[0].index("--timeout-ms") + 1], "3000")
        remaining = int(reads[1][reads[1].index("--timeout-ms") + 1])
        self.assertGreater(remaining, 0)
        self.assertLess(remaining, 3000)
