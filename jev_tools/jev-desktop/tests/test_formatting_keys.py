import unittest

from jev_desktop import AgentDesktop, DesktopConfig, JevLoop, RunOptions, Scope
from jev_desktop.actions import extend_space
from jev_desktop.policy import action_space
from support import Desktop, Model, Transport


class FormattingKeysTest(unittest.TestCase):
    def test_model_can_select_formatting_and_selection_keys_for_its_pinned_window(self):
        class Headed(Desktop):
            def observe(self, scope):
                seen = super().observe(scope)
                seen.screen["headed_text"] = True
                return seen

        for key in (
            "ctrl+b",
            "ctrl+i",
            "ctrl+u",
            "ctrl+e",
            "ctrl+j",
            "ctrl+r",
            "shift+home",
            "shift+end",
            "ctrl+shift+home",
            "ctrl+shift+end",
            "shift+up",
            "shift+down",
            "ctrl+g",
            "ctrl+shift+p",
            "ctrl+shift+e",
            "ctrl+shift+f",
            "ctrl+shift+x",
        ):
            with self.subTest(key=key):
                desktop = Headed()
                model = Model("WINDOW", "PRESS_KEY:" + key, "DONE")
                events = list(
                    JevLoop(desktop, model, sleep=lambda _: None).run(
                        "Format selected text", RunOptions(scope=Scope(app="Editor", window_id="pinned"))
                    )
                )
                self.assertEqual(events[-1]["stop"], "done")
                scope, operation, _, _ = desktop.actions[0]
                transport = Transport()
                native = AgentDesktop(DesktopConfig(headed=True), transport)
                with native.run_context("Format selected text", False):
                    native.execute(scope, operation, None, None)
                presses = [argv for _, argv, _ in transport.calls if "press" in argv]
                self.assertEqual(
                    presses, [["--headed", "press", key, "--window-id", "pinned", "--app", "Editor"]]
                )

    def test_headless_and_caller_key_restrictions_withhold_formatting(self):
        for headed, keys in ((False, None), (True, ("return",))):
            with self.subTest(headed=headed):
                options = RunOptions() if keys is None else RunOptions(keys=keys)
                space = extend_space(
                    action_space([]), {"headed_text": headed, "window_id": "pinned"}, options
                )
                offered = space["by_target"].get("WINDOW", {}).get("operations", [])
                self.assertNotIn("PRESS_KEY:ctrl+b", offered)
