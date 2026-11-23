import unittest

from jev_desktop import DesktopError, JevLoop, RunOptions, Scope
from jev_desktop.modal_recovery import mark_modal_refusal, release_modal_refusals
from support import Desktop, Model, node


class ModalRecoveryTest(unittest.TestCase):
    def test_predispatch_child_close_returns_to_parent_and_releases_modal_refusal(self):
        class Dialog(Desktop):
            closed = False
            child_reads = 0
            def owned_dialog(self, scope):
                return Scope(app="Editor", window_id="dialog") if not self.closed else None
            def observe(self, scope):
                if scope.window_id == "dialog":
                    self.child_reads += 1
                    if self.child_reads > 1:
                        self.closed = True
                        raise DesktopError("closed", "WINDOW_NOT_FOUND")
                self.nodes = [node(name="Confirm" if scope.window_id == "dialog" else "Apply")]
                return super().observe(scope)
            def execute(self, scope, operation, selected, text):
                self.result = {"ok": True, "delivery": "delivered_verified"}
                if not self.actions:
                    self.result = {"ok": False, "delivery": "not_delivered", "error": {
                        "code": "ACTION_FAILED", "details": {"cause": "blocked_by_modal_dialog"},
                        "disposition": {"delivery": "not_delivered", "retry": "safe"}}}
                return super().execute(scope, operation, selected, text)
        desktop = Dialog()
        events = list(JevLoop(desktop, Model("1", "CLICK", "1", "CLICK", "1", "CLICK", "DONE"), sleep=lambda _: None).run(
            "Apply", RunOptions(scope=Scope(app="Editor", window_id="main"), replan_no_effect=True,
                                replan_safe_failures=True, follow_owned_dialogs=True)))
        self.assertEqual(events[-1]['stop'], 'done')
        self.assertEqual([a[0].window_id for a in desktop.actions], ["main", "main"])
        self.assertTrue(any(e.get('recovery') == 'owned_dialog_closed' for e in events))

    def test_a_modal_refusal_expires_only_after_returning_from_the_closed_dialog(self):
        class Windows(Desktop):
            first = True
            closed = False

            def application_windows(self, scope):
                return (
                    [
                        {
                            "id": "dialog",
                            "app_name": "Editor",
                            "pid": 1,
                            "process_instance": "a",
                            "title": "Confirm",
                        }
                    ]
                    if scope.window_id == "main" and not self.closed
                    else []
                )

            def observe(self, scope):
                if scope.window_id == "dialog" and self.closed:
                    raise DesktopError("closed", "WINDOW_NOT_FOUND")
                self.nodes = [node(name="Confirm" if scope.window_id == "dialog" else "Action")]
                return super().observe(scope)

            def execute(self, scope, operation, selected, text):
                self.result = {"ok": True, "delivery": "delivered_verified"}
                if scope.window_id == "main" and self.first:
                    self.first = False
                    self.result = {
                        "ok": False,
                        "delivery": "not_delivered",
                        "error": {
                            "code": "ACTION_FAILED",
                            "details": {"cause": "blocked_by_modal_dialog"},
                            "disposition": {"delivery": "not_delivered", "retry": "safe"},
                        },
                    }
                if scope.window_id == "dialog":
                    self.closed = True
                return super().execute(scope, operation, selected, text)

        desktop = Windows()
        model = Model("1", "CLICK", "APP_WINDOW_0", "SWITCH_WINDOW", "1", "CLICK", "1", "CLICK", "DONE")
        events = list(
            JevLoop(desktop, model, sleep=lambda _: None).run(
                "Open control",
                RunOptions(
                    scope=Scope(app="Editor", window_id="main"),
                    replan_safe_failures=True,
                    follow_application_windows=True,
                ),
            )
        )
        self.assertEqual(events[-1]["stop"], "done")
        self.assertEqual([action[0].window_id for action in desktop.actions], ["main", "dialog", "main"])

    def test_unknown_delivery_is_never_marked_temporary_or_released(self):
        block = {"operation": "CLICK", "target": "Action", "window_id": "main"}
        outcome = {
            "error": {
                "details": {"cause": "blocked_by_modal_dialog"},
                "disposition": {"delivery": "delivery_uncertain", "retry": "unsafe"},
            }
        }
        marked = mark_modal_refusal(block, outcome)
        self.assertEqual(marked, block)
        options = RunOptions(blocked_actions=(marked,))
        self.assertEqual(release_modal_refusals(options, Scope(window_id="main")).blocked_actions, (block,))
