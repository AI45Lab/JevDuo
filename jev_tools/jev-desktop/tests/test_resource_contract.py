import unittest

from jev_desktop import AgentDesktop, DesktopConfig, JevLoop, RunOptions, Scope
from jev_desktop.actions import extend_space
from jev_desktop.policy import action_space, build_operation_request
from support import Desktop, Model, Transport, failure, node, success


class ResourceContractTests(unittest.TestCase):
    def test_caller_bound_path_is_a_choice_and_one_literal_launch_argument(self):
        path = '/home/user/a "quoted" $(literal).txt'
        options = RunOptions(resources=(path,))
        space = extend_space(action_space([]), {"app": "Editor", "window_id": "w1", "headed_text": True}, options)
        selected = {"type": "choice", "choice": "WINDOW", "confidence": 1, "probabilities": {"WINDOW": 1}}
        request = build_operation_request("Open this file", {}, space, [], target=selected)
        self.assertIn(path, request["state"]["selected_target"]["element"]["resources"].values())
        transport = Transport(lambda _: success())
        desktop = AgentDesktop(DesktopConfig(headed=True), transport)
        with desktop.run_context("Open resource", False):
            result = desktop.execute(Scope("Editor", window_id="w1"), "OPEN_RESOURCE:0", space["by_target"]["WINDOW"]["node"], None)
        self.assertEqual(transport.calls[0][1], ["launch", "Editor", "--arg=" + path, "--timeout", "5000"])
        self.assertLess(5, transport.calls[0][2])
        self.assertEqual(result["details"]["verification_scope"], "launch_request")

    def test_no_binding_no_headed_permission_and_caller_restrictions_withhold_resource(self):
        for headed, options in ((False, RunOptions(resources=("/tmp/file",))),
                                (True, RunOptions()),
                                (True, RunOptions(resources=("/tmp/file",), allowed_operations=("FOCUS_WINDOW",)))):
            space = extend_space(action_space([]), {"app": "Editor", "window_id": "w1", "headed_text": headed}, options)
            operations = space["by_target"].get("WINDOW", {}).get("operations", [])
            self.assertNotIn("OPEN_RESOURCE:0", operations)

    def test_missing_or_invalid_path_is_rejected_before_dispatch(self):
        for path in ("--execute", "relative.txt", "/tmp/a\nb", "https://example.org", None):
            with self.subTest(path=path), self.assertRaises(ValueError):
                RunOptions(resources=(path,))
        with self.assertRaises(ValueError):
            RunOptions(resources=("/tmp/file", "/tmp/file"))
        transport = Transport()
        desktop = AgentDesktop(DesktopConfig(headed=True), transport)
        with desktop.run_context("Open", False), self.assertRaises(ValueError):
            desktop.execute(Scope("Editor", window_id="w1"), "OPEN_RESOURCE:9", {"role": "window", "window_id": "w1", "resources": {}}, None)
        self.assertFalse(transport.calls)

    def test_uncertain_launch_is_preserved_and_never_replayed(self):
        transport = Transport(lambda _: failure())
        desktop = AgentDesktop(DesktopConfig(headed=True), transport)
        with desktop.run_context("Open", False):
            result = desktop.execute(Scope("Editor", window_id="w1"), "OPEN_RESOURCE:0",
                                     {"role": "window", "window_id": "w1", "resources": {"0": "/tmp/file"}}, None)
        self.assertFalse(result["ok"])
        self.assertEqual(len(transport.calls), 1)

    def test_acknowledged_handoff_can_reobserve_without_relaunch_or_false_verification(self):
        class Headed(Desktop):
            def observe(self, scope):
                seen = super().observe(scope)
                seen.screen["headed_text"] = True
                return seen

        desktop = Headed()
        desktop.result = {"ok": False, "delivery": "delivered_unverified", "error": {
            "code": "ACTION_FAILED", "disposition": {"delivery": "delivered_unverified", "retry": "unsafe"}}}
        desktop.on_execute = lambda: desktop.nodes.append(node(ref_id="@s2:e2", name="file.txt"))
        events = list(JevLoop(desktop, Model("WINDOW", "OPEN_RESOURCE:0", "DONE"), sleep=lambda _: None).run(
            "Open file", RunOptions(resources=("/tmp/file.txt",), reconcile_observed_popups=True)))
        self.assertEqual(len(desktop.actions), 1)
        recovery = next(e for e in events if e.get("recovery") == "observe_after_resource_handoff")
        self.assertIn("unverified", recovery["verification"])
        self.assertEqual(recovery["blocked_action"]["operation"], "OPEN_RESOURCE:0")
        self.assertTrue(any(e.get("turn", {}).get("ok") is False for e in events))

    def test_maximize_is_pinned_and_never_uses_resource_arguments(self):
        transport = Transport()
        desktop = AgentDesktop(DesktopConfig(headed=True), transport)
        with desktop.run_context("Expose controls", False):
            desktop.execute(Scope("Editor", window_id="w1"), "MAXIMIZE_WINDOW", None, None)
        self.assertEqual(transport.calls[0][1], ["--headed", "maximize", "--window-id", "w1"])


if __name__ == "__main__":
    unittest.main()
