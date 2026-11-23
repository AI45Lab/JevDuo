import copy
import unittest

from jev_desktop import AgentDesktop, DesktopConfig, Observation, Scope
from support import Transport, failure, node, success


class ClipboardDesktop(AgentDesktop):
    def __init__(self, value="original", *, refusal=None, complete=True, app="Editor"):
        self.value = value
        self.clipboard = "unrelated clipboard"
        self.selected = False
        self.focused = False
        self.complete = complete
        self.app = app
        self.refusal = refusal or failure("PLATFORM_NOT_SUPPORTED", delivery="not_delivered", retry="safe")
        self.fail_key = None
        self.partial_selection = False
        self.readback_value = None
        self.field = node(role="textfield", name="Content", value=value, available_actions=["SetFocus", "SetValue"])
        self.native = Transport(self.handle)
        super().__init__(DesktopConfig(headed=True, clipboard_text=True), self.native)

    def observe(self, scope):
        field = {**self.field, "value": self.value, "states": ["focused"] if self.focused else []}
        field["path"] = ["window " + self.value, "editor"]
        return Observation([field], {
            "app": self.app, "window_id": "w1", "surface": "window", "complete": self.complete,
            "context": [copy.deepcopy(field)],
        })

    def handle(self, argv):
        a = argv[1:] if argv[0] == "--headed" else argv
        if a[0] == "set-value":
            return self.refusal
        if a[0] == "focus":
            self.focused = True
        elif a[0] == "clipboard-clear":
            self.clipboard = ""
        elif a[0] == "clipboard-set":
            assert a[1] == "--"
            self.clipboard = a[2]
        elif a[0] == "clipboard-get":
            return success({"type": "text", "text": self.clipboard})
        elif a[0] == "press":
            assert a[2:] == ["--window-id", "w1", "--app", self.app]
            if a[1] == "ctrl+a":
                self.selected = True
            elif a[1] == "ctrl+c":
                self.clipboard = self.value[:1] if self.partial_selection else self.value
            elif a[1] == "ctrl+v":
                self.value = self.readback_value if self.readback_value is not None else self.clipboard
            if a[1] == self.fail_key:
                return failure("TIMEOUT", delivery="delivery_uncertain", retry="unsafe")
        return success()

    def write(self, text="first\nsecond\tend", field=None):
        with self.run_context("Replace only the selected field", False):
            return self.execute(Scope(self.app, window_id="w1"), "TYPE_TEXT", field or self.field, text)


class ClipboardTextTests(unittest.TestCase):
    def test_complete_selection_and_exact_readback_on_generic_fields(self):
        for app in ("Editor", "Mail"):
            for old in ("", "previous\ncontent"):
                with self.subTest(app=app, old=old):
                    desktop = ClipboardDesktop(old, app=app)
                    result = desktop.write()
                    self.assertTrue(result["ok"])
                    self.assertEqual(result["delivery"], "delivered_verified")
                    self.assertEqual(result["details"]["application_commit"], "not_verified")
                    self.assertEqual(result["post_state"]["value"], "first\nsecond\tend")
                    self.assertEqual(desktop.selected, bool(old))

    def test_unknown_native_write_never_falls_back(self):
        for delivery, retry in (("delivery_uncertain", "unsafe"), ("delivered_unverified", "unsafe"), ("not_delivered", "unsafe")):
            desktop = ClipboardDesktop(refusal=failure("ACTION_NOT_SUPPORTED", delivery=delivery, retry=retry))
            result = desktop.write()
            self.assertFalse(result["ok"])
            self.assertEqual(len(desktop.native.calls), 1)

    def test_successful_native_write_does_not_touch_clipboard(self):
        desktop = ClipboardDesktop(refusal=success())
        self.assertTrue(desktop.write()["ok"])
        self.assertEqual(len(desktop.native.calls), 1)

    def test_partial_or_stale_copy_prevents_paste(self):
        desktop = ClipboardDesktop()
        desktop.partial_selection = True
        result = desktop.write()
        self.assertFalse(result["ok"])
        self.assertEqual(result["delivery"], "not_delivered")
        self.assertEqual(result["error"]["disposition"]["retry"], "unsafe")
        self.assertFalse(any("ctrl+v" in a for _, a, _ in desktop.native.calls))
        self.assertEqual(desktop.value, "original")

    def test_incomplete_readback_never_prepares_or_pastes(self):
        desktop = ClipboardDesktop(complete=False)
        self.assertFalse(desktop.write()["ok"])
        self.assertEqual(len(desktop.native.calls), 1)

    def test_secure_readonly_unreadable_and_unaddressable_fields_are_excluded(self):
        for extra in ({"states": ["secure"]}, {"states": ["readonly"]}, {"value": None}, {"ref_actionable": False}):
            desktop = ClipboardDesktop()
            self.assertFalse(desktop.write(field={**desktop.field, **extra})["ok"])
            self.assertEqual(len(desktop.native.calls), 1)

    def test_lost_paste_reply_is_uncertain_and_is_never_replayed(self):
        desktop = ClipboardDesktop()
        desktop.fail_key = "ctrl+v"
        result = desktop.write()
        self.assertFalse(result["ok"])
        self.assertEqual(result["delivery"], "delivery_uncertain")
        self.assertEqual(sum("ctrl+v" in a for _, a, _ in desktop.native.calls), 1)

    def test_readback_mismatch_is_not_a_verified_write(self):
        desktop = ClipboardDesktop()
        desktop.readback_value = "wrong result"
        result = desktop.write()
        self.assertFalse(result["ok"])
        self.assertEqual(result["delivery"], "delivery_uncertain")

    def test_text_that_looks_like_cli_flags_remains_one_literal_argument(self):
        desktop = ClipboardDesktop()
        text = "--image=/tmp/not-an-image\nsecond line"
        self.assertTrue(desktop.write(text)["ok"])
        setters = [a for _, a, _ in desktop.native.calls if a[0] == "clipboard-set"]
        self.assertEqual(setters, [["clipboard-set", "--", text]])

    def test_insertion_does_not_gain_a_replacement_fallback(self):
        desktop = ClipboardDesktop()
        with desktop.run_context("Insert", False):
            desktop.execute(Scope("Editor", window_id="w1"), "INSERT_TEXT", desktop.field, "append")
        self.assertEqual(len(desktop.native.calls), 1)

    def test_changed_value_or_window_aborts_before_paste(self):
        for change in ("value", "window"):
            desktop = ClipboardDesktop()
            read = desktop.observe

            def changed(scope, change=change, read=read):
                seen = read(scope)
                if change == "value":
                    seen.nodes[0]["value"] = "changed outside this action"
                else:
                    seen.screen["window_id"] = "other"
                return seen

            desktop.observe = changed
            self.assertFalse(desktop.write()["ok"])
            self.assertEqual(len(desktop.native.calls), 1)


if __name__ == "__main__":
    unittest.main()
