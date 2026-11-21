import unittest

from jev_desktop import JevLoop, RunOptions
from support import Desktop, Model, node


class ClickCycleTests(unittest.TestCase):
    def test_unknown_surfaces_do_not_turn_unchanged_classified_content_into_no_effect_evidence(self):
        for first_unknown in (True, False):
            with self.subTest(first_unknown=first_unknown):
                class UnclassifiedDesktop(Desktop):
                    def observe(self, scope, unknown=first_unknown):
                        seen = super().observe(scope)
                        if unknown or len(self.observations) > 1:
                            seen.screen["unclassified_surfaces"] = [{"id": "unread-popup"}]
                        return seen
                desktop = UnclassifiedDesktop()
                desktop.result = {"ok": True, "delivery": "delivered_unverified"}
                model = Model("1", "CLICK", "1", "CLICK", "DONE")
                events = list(JevLoop(desktop, model, sleep=lambda _: None).run("Inspect effect", RunOptions(replan_no_effect=True)))
                self.assertEqual(events[-1]["stop"], "done")
                self.assertEqual(len(desktop.actions), 2 if first_unknown else 1)
                if not first_unknown:
                    self.assertTrue(any(e.get("recovery") == "replan_changed_action_context" for e in events))
                self.assertFalse(any(e.get("diagnostic") == "unverified_click_without_observed_change" for e in events))

    def test_confirmed_window_focus_without_a_content_change_is_not_repeated(self):
        class FocusedDesktop(Desktop):
            def observe(self, scope):
                seen = super().observe(scope)
                seen.screen["headed_text"] = True
                return seen

            def execute(self, scope, operation, target, text):
                result = super().execute(scope, operation, target, text)
                if operation == "FOCUS_WINDOW":
                    return {"ok": True, "focus_evidence": {"window_id": scope.window_id, "is_focused": True}}
                return result

        desktop = FocusedDesktop()
        model = Model("WINDOW", "FOCUS_WINDOW", "WINDOW", "PRESS_KEY:ctrl+shift+p", "DONE")
        events = list(JevLoop(desktop, model, sleep=lambda _: None).run("Navigate", RunOptions(replan_no_effect=True)))
        self.assertEqual(events[-1]["stop"], "done")
        self.assertEqual([a[1] for a in desktop.actions], ["FOCUS_WINDOW", "PRESS_KEY:ctrl+shift+p"])
        self.assertNotIn("FOCUS_WINDOW", model.requests[3]["questions"]["operation"]["criteria"])
        self.assertIn("focus_evidence", events[0]["turn"])

    def desktop(self, *, change_context=False):
        desktop = Desktop([node(name="Open options"), node(name="Alternate route", ref_id="@s1:e2")])
        desktop.result = {"ok": True, "delivery": "delivered_unverified"}

        def toggle():
            if desktop.actions[-1][2]["name"] == "Open options":
                desktop.nodes[0]["states"] = [] if desktop.nodes[0].get("states") else ["expanded"]
                if change_context and len(desktop.actions) == 2:
                    desktop.nodes[1]["name"] = "Changed page content"

        desktop.on_execute = toggle
        return desktop

    def test_return_to_same_ui_withholds_repeated_click_before_another_dispatch(self):
        desktop = self.desktop()
        model = Model("1", "CLICK", "1", "CLICK", "2", "CLICK", "DONE")
        events = list(JevLoop(desktop, model, sleep=lambda _: None).run("Choose an option", RunOptions(replan_no_effect=True)))
        self.assertEqual(events[-1]["stop"], "done")
        self.assertEqual([a[2]["name"] for a in desktop.actions], ["Open options", "Open options", "Alternate route"])
        self.assertNotIn("1", model.requests[4]["questions"]["target"]["criteria"])
        self.assertIn("cycle_feedback", model.requests[4]["state"])

    def test_changed_context_and_default_mode_keep_the_click_available(self):
        for change_context, enabled in ((True, True), (False, False)):
            with self.subTest(change_context=change_context, enabled=enabled):
                desktop = self.desktop(change_context=change_context)
                model = Model("1", "CLICK", "1", "CLICK", "1", "CLICK", "DONE")
                events = list(JevLoop(desktop, model, sleep=lambda _: None).run("Choose an option", RunOptions(replan_no_effect=enabled)))
                self.assertEqual(events[-1]["stop"], "done")
                self.assertIn("1", model.requests[4]["questions"]["target"]["criteria"])
                self.assertEqual(len(desktop.actions), 3)
