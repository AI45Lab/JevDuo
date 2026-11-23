import unittest

from jev_desktop import AgentDesktop, DesktopConfig, DesktopError, Scope
from jev_desktop.desktop import delivery_evidence
from support import Transport, failure, node, success


class DesktopTests(unittest.TestCase):
    def test_window_focus_preserves_verified_focus_separately_from_delivery(self):
        for focused, expected in (({"id": "w1", "is_focused": True}, True), ({"id": "other", "is_focused": True}, False), ({"id": "w1", "is_focused": False}, False), ({"id": "w1", "is_focused": 1}, False), ({}, False)):
            with self.subTest(focused=focused):
                transport = Transport(lambda _, focused=focused: {"ok": True, "data": {"focused": focused}})
                desktop = AgentDesktop(DesktopConfig(headed=True), transport)
                with desktop.run_context("Focus", False):
                    outcome = desktop.execute(Scope("Fixture", window_id="w1"), "FOCUS_WINDOW", None, None)
                self.assertEqual("focus_evidence" in outcome, expected)
                self.assertIsNone(outcome.get("delivery"))
                self.assertEqual(len(transport.calls), 1)

    def test_text_replacement_is_one_native_action_and_never_borrows_clipboard(self):
        for headed in (False, True):
            with self.subTest(headed=headed):
                transport = Transport(lambda _: success({"post_state": {"value": "試験"}}))
                desktop = AgentDesktop(DesktopConfig(headed=headed), transport)
                with desktop.run_context("write", False):
                    result = desktop.execute(Scope("Fixture", window_id="w1"), "TYPE_TEXT", node(), "試験")
                argv = (["--headed"] if headed else []) + ["set-value", "@s1:e1", "試験"]
                self.assertEqual(transport.calls[0][1], argv)
                self.assertEqual(len(transport.calls), 1)
                self.assertEqual(result["argv"], argv)
                self.assertEqual(result["post_state"], {"value": "試験"})

    def test_refused_or_uncertain_replacement_never_sends_followup_keys(self):
        for headed in (False, True):
            for delivery, retry in (
                ("not_delivered", "safe"),
                ("delivered_unverified", "unsafe"),
                ("delivery_uncertain", "unsafe"),
            ):
                with self.subTest(headed=headed, delivery=delivery):
                    transport = Transport(
                        lambda _, delivery=delivery, retry=retry: failure(
                            "ACTION_NOT_SUPPORTED", delivery=delivery, retry=retry
                        )
                    )
                    desktop = AgentDesktop(DesktopConfig(headed=headed), transport)
                    with desktop.run_context("write", False):
                        result = desktop.execute(Scope("Fixture"), "TYPE_TEXT", node(), "試験")
                    self.assertFalse(result["ok"])
                    self.assertEqual(result["delivery"], delivery)
                    self.assertEqual(len(transport.calls), 1)

    def test_cursor_session_is_explicit_and_cleanup_survives_exception(self):
        transport = Transport(lambda args: success({"session_id": "session-new"}))
        desktop = AgentDesktop(DesktopConfig(session="session-old"), transport)
        with self.assertRaisesRegex(RuntimeError, "fixture"):
            with desktop.run_context("goal", True):
                self.assertEqual(desktop.session, "session-new")
                raise RuntimeError("fixture")
        self.assertEqual(desktop.session, "session-old")
        self.assertEqual(
            transport.calls[-1][1],
            ["--session", "session-new", "--agent-id", "jev-desktop", "cursor-overlay", "disable"],
        )

    def test_nested_run_is_refused_and_execute_requires_context(self):
        desktop = AgentDesktop(transport=Transport())
        with self.assertRaises(RuntimeError):
            desktop.execute(Scope(), "WAIT", None, None)
        with desktop.run_context("one", False):
            with self.assertRaises(RuntimeError):
                with desktop.run_context("two", False):
                    pass

    def test_all_operations_compile_and_preserve_evidence(self):
        transport = Transport()
        pauses = []
        desktop = AgentDesktop(transport=transport, sleep=pauses.append)
        with desktop.run_context("goal", False):
            for operation in ("CLICK", "CHECK", "UNCHECK", "EXPAND", "COLLAPSE", "SCROLL"):
                result = desktop.execute(Scope(), operation, node(), None)
                self.assertEqual(result["delivery"], "delivered_verified")
                self.assertEqual(result["argv"][0], operation.lower())
            self.assertEqual(desktop.execute(Scope(), "DRILL", node(), None)["root"], "@s1:e1")
            self.assertIsNone(desktop.execute(Scope(), "WIDEN", None, None)["root"])
            desktop.execute(Scope(), "WAIT", None, None)
        self.assertEqual(pauses, [0.25])
        self.assertEqual(transport.calls[-1][1], ["scroll", "@s1:e1", "--direction", "down"])
        result = delivery_evidence(
            {
                "ok": False,
                "error": {
                    "code": "TIMEOUT",
                    "details": {
                        "after_action": {"steps": [{"name": "clicked"}], "post_state": {"checked": True}}
                    },
                },
            }
        )
        self.assertEqual(result["steps"], [{"name": "clicked"}])
        self.assertEqual(result["post_state"], {"checked": True})


class ActionDeadlineTests(unittest.TestCase):
    def test_slow_native_ref_action_gets_one_long_deadline_without_replay(self):
        transport = Transport(lambda _: failure("TIMEOUT", delivery="delivery_uncertain", retry="unsafe"))
        desktop = AgentDesktop(DesktopConfig(action_timeout_ms=60000), transport)
        with desktop.run_context("change", False):
            result = desktop.execute(Scope(), "CLICK", node(), None)
        self.assertEqual(transport.calls[0][1], ["click", "@s1:e1", "--timeout-ms", "60000"])
        self.assertGreater(transport.calls[0][2], 60)
        self.assertEqual(result["delivery"], "delivery_uncertain")
        self.assertEqual(len(transport.calls), 1)

    def test_text_ref_gets_deadline_but_window_commands_keep_their_own_contract(self):
        transport = Transport()
        desktop = AgentDesktop(DesktopConfig(action_timeout_ms=15000, headed=True), transport)
        with desktop.run_context("change", False):
            desktop.execute(Scope(window_id="w1"), "TYPE_TEXT", node(), "hello")
            desktop.execute(Scope(window_id="w1"), "FOCUS_WINDOW", None, None)
            desktop.execute(Scope(window_id="w1"), "PRESS_KEY:Escape", None, None)
        self.assertEqual(transport.calls[0][1][-2:], ["--timeout-ms", "15000"])
        self.assertNotIn("--timeout-ms", transport.calls[1][1])
        self.assertNotIn("--timeout-ms", transport.calls[2][1])


class ObservationTests(unittest.TestCase):
    def backend(self, *, surface=None, window="w1", rooted=False, identity=False):
        def handler(args):
            if args[0] == "list-surfaces":
                return success({"surfaces": [{"type": "window", "unclassified": ["sheet"]}]})
            tree = {"role": "window", "children": [node(), node(ref_id="@s1:e2", states=["offscreen"])]}
            if surface and "--surface" not in args:
                tree["children"].append({"role": surface})
            data = {
                "app": "Fixture",
                "window": {"id": window, "title": "Test"},
                "tree": tree,
                "identity_unproven": identity,
                "complete": False,
            }
            if (rooted and "--root" in args) or "--surface" in args:
                data["window"] = {"id": "other"}
            return success(data)

        transport = Transport(handler)
        return AgentDesktop(transport=transport), transport

    def test_full_tree_and_unclassified_surfaces(self):
        desktop, transport = self.backend()
        seen = desktop.observe(Scope())
        self.assertEqual(len(seen.nodes), 1)
        self.assertFalse(seen.screen["complete"])
        self.assertEqual(seen.screen["unclassified_surfaces"], ["sheet"])
        self.assertNotIn("--max-depth", transport.calls[0][1])

    def test_wrong_window_root_and_surface_refused(self):
        for kwargs, scope, expected in (
            ({"window": "other"}, Scope(window_id="w1"), "SURFACE_OUTSIDE_WINDOW"),
            ({"rooted": True}, Scope(root="@s1:e1", window_id="w1"), "ROOT_OUTSIDE_WINDOW"),
            ({"surface": "sheet"}, Scope(window_id="w1"), "SURFACE_OUTSIDE_WINDOW"),
        ):
            with self.subTest(kwargs=kwargs):
                desktop, _ = self.backend(**kwargs)
                with self.assertRaises(DesktopError) as caught:
                    desktop.observe(scope)
                self.assertEqual(caught.exception.code, expected)

    def test_unproven_identity_is_not_a_model_input(self):
        desktop, _ = self.backend(identity=True)
        with self.assertRaises(DesktopError) as caught:
            desktop.observe(Scope())
        self.assertEqual(caught.exception.code, "INCOMPLETE_OBSERVATION")

    def test_transient_surface_absence_and_observation_fallback(self):
        snapshots = []

        def handler(args):
            if args[0] == "list-surfaces":
                return success({"surfaces": []})
            if "--surface" in args:
                return failure("ELEMENT_NOT_FOUND")
            snapshots.append(args)
            if len(snapshots) == 1:
                return failure("TIMEOUT")
            return success({"tree": {"role": "sheet", "children": [node()]}})

        transport = Transport(handler)
        seen = AgentDesktop(transport=transport).observe(Scope())
        self.assertEqual(seen.screen["surface"], "window")
        self.assertIn("--max-depth", snapshots[1])

    def test_missing_pinned_identity_and_root_timeout_do_not_fallback(self):
        for data in ({"window": {}}, {"window": {"id": "w1"}, "surface": {"window_id": "other"}}):
            transport = Transport(lambda args, data=data: success({**data, "tree": {"role": "window"}}))
            with self.assertRaises(DesktopError):
                AgentDesktop(transport=transport).observe(Scope(window_id="w1"))
        transport = Transport(lambda args: failure("TIMEOUT"))
        with self.assertRaises(DesktopError):
            AgentDesktop(transport=transport).observe(Scope(root="@s1:e1"))
        self.assertEqual(len(transport.calls), 1)
