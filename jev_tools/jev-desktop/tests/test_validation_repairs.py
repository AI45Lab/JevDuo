"""Contracts observed in the completed eight-task Chrome validation."""

import copy
import unittest

from jev_desktop import AgentDesktop, DesktopConfig, DesktopError, JevLoop, RunOptions, Scope
from jev_desktop.contracts import Observation
from jev_desktop.goal_scope import filter_rejections, state_key
from jev_desktop.policy import action_space
from jev_desktop.popup_recovery import observed_popup
from support import Desktop, Model, Transport, node


class ValidationRepairTests(unittest.TestCase):
    def test_leaf_selection_is_an_exact_observed_value_without_text_generation(self):
        item = node(
            role="menuitem",
            name="Light",
            available_actions=["Select", "SetFocus"],
            path=['window "Settings"', 'webarea "Settings"', 'combobox "Mode"', "menu"],
        )
        desktop = Desktop([item])
        events = list(JevLoop(desktop, Model("1", "SELECT_ITEM", "DONE"), sleep=lambda _: None).run("Light"))
        assert desktop.actions[0][1:] == ("SELECT_ITEM", item, "Light")
        assert events[0]["turn"]["text_source"] == "observed_option"
        for role, name in (("button", "Light"), ("menuitem", "")):
            space = action_space([node(role=role, name=name, available_actions=["Select"])])
            assert not space["by_target"]

    def test_navigation_and_consuming_autocomplete_do_not_offer_scalar_combo_selection(self):
        for role, label, path in (
            ("menuitem", "On start-up", ['window "Settings"', 'webarea "Settings"', "menu"]),
            ("menuitem", "Add new folder", ['window "Bookmarks"', 'webarea "Bookmarks"', "menu"]),
            ("option", "JFK New York-Kennedy, NY", ['window "Flights"', 'webarea "Flights"', "listbox"]),
        ):
            observed = node(role=role, name=label, path=path, available_actions=["Select", "SetFocus"])
            entry = action_space([observed], headed=True)["by_target"]["1"]
            assert "SELECT_ITEM" not in entry["operations"]
            assert "FOCUS" in entry["operations"]

    def test_popup_opening_uses_native_semantics_and_commits_still_need_scope_review(self):
        for role in ("menubutton", "combobox"):
            desktop = Desktop([node(role=role, name="Finish update", states=["haspopup"])])
            model = Model("1", "CLICK", "DONE")
            list(
                JevLoop(desktop, model, sleep=lambda _: None).run(
                    "Open options", RunOptions(review_goal_scope=True)
                )
            )
            assert len(desktop.actions) == 1
            assert all("goal_scope" not in r["questions"] for r in model.requests)
        desktop = Desktop([node(role="button", name="Delete data", states=["haspopup"])])
        model = Model("1", "CLICK", "broader_or_unproven", "BLOCKED")
        list(
            JevLoop(desktop, model, sleep=lambda _: None).run(
                "Delete selected entries only", RunOptions(review_goal_scope=True)
            )
        )
        assert not desktop.actions
        assert "goal_scope" in model.requests[2]["questions"]

    def test_new_focused_inline_input_reconciles_only_same_pinned_document(self):
        opener = node(name="Origin FRA", path=['window "Flights"', 'webarea "Flights"'])
        before = Observation([opener], {"app": "Fixture", "window_id": "w1", "complete": True})
        field = node(
            role="textfield",
            name="Origin",
            states=["focused"],
            path=opener["path"],
            bounds={"x": 1, "y": 2, "width": 100, "height": 30},
        )
        after = Observation([field], {**before.screen, "surface": "window"})
        assert observed_popup(before, after, opener)["focused_new_control"]
        for change in ({"window_id": "w2"}, {"complete": False}, {"unclassified_surfaces": ["unknown"]}):
            changed = copy.deepcopy(after)
            changed.screen.update(change)
            assert observed_popup(before, changed, opener) is None
        before.nodes.append(field)
        assert observed_popup(before, after, opener) is None

    def test_new_unfocused_editable_input_is_positive_evidence_but_ambiguity_is_not(self):
        owner = ['window "Editor"', 'webarea "Document"']
        opener = node(name="Open search", path=owner)
        before = Observation([opener], {"app": "Fixture", "window_id": "w1", "complete": True})
        field = node(
            role="textfield",
            name="Query",
            states=[],
            path=owner,
            available_actions=["SetValue"],
            bounds={"x": 1, "y": 2, "width": 100, "height": 30},
        )
        after = Observation([field], {**before.screen, "surface": "window"})
        assert observed_popup(before, after, opener)["new_editable_control"]
        headed = Observation(
            [{**field, "available_actions": ["SetFocus"]}], {**after.screen, "headed_text": True}
        )
        assert observed_popup(before, headed, opener)["new_editable_control"]
        for state in ("readonly", "secure", "disabled", "offscreen"):
            invalid = Observation([{**headed.nodes[0], "states": [state]}], headed.screen)
            assert observed_popup(before, invalid, opener) is None

        for change in (
            {"states": ["disabled"]},
            {"states": ["readonly"]},
            {"available_actions": ["SetFocus"]},
            {"ref_actionable": False},
            {"path": ['window "Editor"', 'webarea "Other"']},
        ):
            changed = Observation([{**field, **change}], after.screen)
            assert observed_popup(before, changed, opener) is None
        assert (
            observed_popup(before, Observation([field, {**field, "name": "Second"}], after.screen), opener)
            is None
        )
        assert observed_popup(after, after, opener) is None

    def test_broader_operation_is_rejected_before_any_input_and_is_not_offered_again(self):
        desktop = Desktop()
        model = Model("1", "CLICK", "broader_or_unproven", "BLOCKED")
        events = list(
            JevLoop(desktop, model, sleep=lambda _: None).run(
                "Delete selected history only", RunOptions(review_goal_scope=True)
            )
        )
        assert not desktop.actions
        assert any(e.get("recovery") == "replan_operation_scope" for e in events)
        assert "1" not in model.requests[-1]["questions"]["target"]["criteria"]

    def test_scope_rejection_is_bound_to_window_target_and_current_form_values(self):
        button = node(name="Delete data")
        check = node(role="checkbox", name="Cookies", states=["checked"])
        seen = Observation([button, check], {"window_id": "w1"})
        rejected = [state_key(seen, "CLICK", button)]
        for changed, expected in (
            (seen, False),
            (Observation([button, {**check, "states": []}], seen.screen), True),
            (Observation(seen.nodes, {"window_id": "w2"}), True),
        ):
            space = action_space([button])
            filter_rejections(space, changed, rejected)
            assert bool(space["by_target"]) == expected

    def test_review_cannot_authorize_a_write_after_form_changes_during_model_call(self):
        desktop = Desktop()
        model = Model("1", "CLICK", "within_request", "BLOCKED")
        original = model.ask

        def ask(request):
            answer = original(request)
            if "goal_scope" in request["questions"]:
                desktop.nodes.append(node(role="checkbox", name="Other data", states=["checked"]))
            return answer

        model.ask = ask
        events = list(
            JevLoop(desktop, model, sleep=lambda _: None).run(
                "Delete selected history only", RunOptions(review_goal_scope=True)
            )
        )
        assert not desktop.actions
        assert any(e.get("recovery") == "replan_changed_review_context" for e in events)

    def test_wait_hands_off_only_to_proven_owned_dialog_and_resumes_parent_after_close(self):
        class ModalDesktop(Desktop):
            closed = False

            def observe(self, scope):
                if scope.window_id == "dialog" and self.closed:
                    raise DesktopError("Closed", "WINDOW_NOT_FOUND")
                return super().observe(scope)

            def owned_dialog(self, scope):
                return (
                    Scope(app="Portal", window_id="dialog")
                    if scope.window_id == "w1" and not self.closed
                    else None
                )

            def execute(self, scope, operation, target, text):
                if scope.window_id == "dialog":
                    self.closed = True
                return super().execute(scope, operation, target, text)

        desktop = ModalDesktop()
        events = list(
            JevLoop(desktop, Model("WAIT", "1", "CLICK", "DONE"), sleep=lambda _: None).run(
                "Save", RunOptions(scope=Scope(app="Fixture", window_id="w1"), follow_owned_dialogs=True)
            )
        )
        assert len(desktop.actions) == 1 and desktop.actions[0][0].window_id == "dialog"
        assert [e["recovery"] for e in events if "recovery" in e] == [
            "follow_owned_dialog",
            "owned_dialog_closed",
        ]
        assert events[-1]["screen"]["window_id"] == "w1"

    def test_backend_ownership_lookup_never_chooses_unrelated_or_ambiguous_windows(self):
        for windows in (
            [],
            [{"id": "w1", "app_name": "Fixture"}],
            [{"id": "x", "app_name": "A"}, {"id": "y", "app_name": "B"}],
        ):
            transport = Transport(lambda _, windows=windows: {"ok": True, "data": windows})
            desktop = AgentDesktop(DesktopConfig(), transport)
            try:
                result = desktop.owned_dialog(Scope(app="Fixture", window_id="w1"))
            except DesktopError:
                assert windows
            else:
                assert result is None and not windows
            assert transport.calls[0][1][:3] == ["list-windows", "--owned-by", "w1"]
