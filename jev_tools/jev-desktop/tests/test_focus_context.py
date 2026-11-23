import unittest

from jev_desktop import DesktopError, JevLoop, RunOptions, Scope
from jev_desktop.actions import extend_space
from jev_desktop.goal_scope import preparation_operation, review_request, state_key
from jev_desktop.native_intent import filter_intents, intent
from jev_desktop.policy import action_space, build_request
from support import Desktop, Model, node


class FocusContextTests(unittest.TestCase):
    def test_unverified_focus_request_is_not_mistaken_for_focus_or_repeated_on_the_same_screen(self):
        desktop = Desktop([node(role="combobox", name="Input", available_actions=["SetFocus"], states=["editable"])])
        original_observe = desktop.observe
        def observe(scope):
            seen = original_observe(scope)
            seen.screen['headed_text'] = True
            return seen
        desktop.observe = observe
        desktop.result = {"ok": True, "delivery": "delivered_unverified"}
        model = Model("1", "FOCUS", "WINDOW", "PRESS_KEY:escape", "DONE")
        events = list(JevLoop(desktop, model, sleep=lambda _: None).run("Navigate", RunOptions(replan_no_effect=True)))
        self.assertEqual([a[1] for a in desktop.actions], ["FOCUS", "PRESS_KEY:escape"])
        self.assertEqual(events[-1]['stop'], 'done')
        self.assertNotIn('1', model.requests[2]['questions']['target']['criteria'])

    def test_same_name_buttons_keep_distinct_cycle_receivers_and_unknown_blocks_stay_conservative(self):
        from jev_desktop.native_intent import matches
        left = node(name="Save", path=["left"], bounds={"x": 1, "y": 1, "width": 20, "height": 20})
        right = node(name="Save", path=["right"], bounds={"x": 100, "y": 1, "width": 20, "height": 20})
        window = node(role="window", window_id="w1", focus_observation_complete=True,
                      reported_focus=[{**left, "states": ["focused"]}])
        scope = Scope(app="Fixture", window_id="w1")
        for operation, receiver in (("CLICK", left), ("PRESS_KEY:return", window)):
            previous = intent(scope, operation, receiver, None, [left, right])
            exact = {**previous, "exact_state": True}
            self.assertTrue(matches(exact, intent(scope, "CLICK", left, None, [left, right])))
            self.assertFalse(matches(exact, intent(scope, "CLICK", right, None, [left, right])))
            self.assertTrue(matches(previous, intent(scope, "CLICK", right, None, [left, right])))

    def test_target_only_state_change_invalidates_a_safe_rejection(self):
        import copy
        seen = Desktop().observe(Scope())
        original = state_key(seen, "CLICK", seen.nodes[0])
        for key, value in (("states", ["disabled"]), ("description", "Ready"), ("available_actions", ["SetFocus"])):
            changed = copy.deepcopy(seen)
            changed.nodes[0][key] = value
            self.assertNotEqual(original, state_key(changed, "CLICK", changed.nodes[0]))

    def test_no_effect_freshness_replan_returns_unused_queued_text(self):
        class Changing(Desktop):
            def observe(self, scope):
                seen = super().observe(scope)
                seen.screen['context'] = [{"role": "statictext", "name": "Old" if len(self.observations) == 1 else "New"}]
                return seen
        desktop = Changing([node(role="textfield", available_actions=["SetValue"])])
        events = list(JevLoop(desktop, Model("1", "TYPE_TEXT", "1", "TYPE_TEXT", "DONE"), sleep=lambda _: None).run(
            "Fill", RunOptions(text=["needed"], replan_no_effect=True)))
        self.assertEqual(events[-1]['stop'], 'done')
        self.assertEqual([a[3] for a in desktop.actions], ["needed"])

    def test_predispatch_closed_pinned_window_stops_cleanly_without_sending(self):
        class Closing(Desktop):
            def observe(self, scope):
                if self.observations:
                    raise DesktopError("closed", "WINDOW_NOT_FOUND")
                seen = super().observe(scope)
                seen.screen.update(headed_text=True, context=[{"role": "button", "name": "Apply", "states": ["focused"]}])
                return seen
        for choices, options in ((["1", "CLICK"], {"replan_no_effect": True}),
                                 (["WINDOW", "PRESS_KEY:tab"], {"review_goal_scope": True}),
                                 (["1", "CLICK", "within_request"], {"review_goal_scope": True})):
            desktop = Closing()
            events = list(JevLoop(desktop, Model(*choices), sleep=lambda _: None).run(
                "Apply", RunOptions(scope=Scope(app="Fixture", window_id="w1"), **options)))
            self.assertEqual(events[-1]['stop'], 'window_closed')
            self.assertEqual(desktop.actions, [])

    def test_button_activation_aliases_share_the_receiver_in_both_directions(self):
        button = node(states=["focused"])
        window = node(role="window", window_id="w1", focus_observation_complete=True,
                      reported_focus=[button])
        scope = Scope(app="Fixture", window_id="w1")
        for previous_operation, receiver in (("CLICK", button), ("PRESS_KEY:return", window), ("PRESS_KEY:space", window)):
            previous = intent(scope, previous_operation, receiver, None, [button])
            space = {"by_target": {
                "1": {"node": button, "operations": ["CLICK"]},
                "WINDOW": {"node": window, "operations": ["PRESS_KEY:return", "PRESS_KEY:space", "PRESS_KEY:tab"]},
            }}
            filter_intents(space, scope, [button], [previous])
            self.assertNotIn("1", space["by_target"])
            self.assertEqual(space["by_target"]["WINDOW"]["operations"], ["PRESS_KEY:tab"])

    def test_nonkeyboard_rejection_changes_with_new_scope_and_static_evidence(self):
        import copy
        seen = Desktop().observe(Scope())
        original = state_key(seen, "CLICK", seen.nodes[0])
        for field, value in (("root", "@s1:e9"), ("complete", False), ("unclassified_surfaces", ["unknown"]),
                             ("context", [{"role": "statictext", "name": "Only 1 item selected"}])):
            changed = copy.deepcopy(seen)
            changed.screen[field] = value
            self.assertNotEqual(original, state_key(changed, "CLICK", changed.nodes[0]))

    def test_rejected_supplied_queue_is_available_to_a_later_authorized_field(self):
        desktop = Desktop([node(role="textfield", name="A", available_actions=["SetValue"]),
                           node(ref_id="@s1:e2", role="textfield", name="B", available_actions=["SetValue"])])
        model = Model("1", "TYPE_TEXT", "broader_or_unproven", "2", "TYPE_TEXT", "within_request", "DONE")
        events = list(JevLoop(desktop, model, sleep=lambda _: None).run("Fill B", RunOptions(text=["needed"], review_goal_scope=True)))
        self.assertEqual(events[-1]["stop"], "done")
        self.assertEqual([(a[2]["name"], a[3]) for a in desktop.actions], [("B", "needed")])

    def test_safe_refusal_expires_after_an_observed_focus_repair(self):
        class Repairable(Desktop):
            repaired = False
            def observe(self, scope):
                seen = super().observe(scope)
                seen.screen.update(headed_text=True, context=[{"role": "button", "name": "Ready" if self.repaired else "Before", "states": ["focused"]}])
                return seen
            def execute(self, scope, operation, target, text):
                super().execute(scope, operation, target, text)
                if operation == "FOCUS_WINDOW":
                    self.repaired = True
                if operation == "PRESS_KEY:tab" and not self.repaired:
                    return {"ok": False, "delivery": "not_delivered", "error": {"code": "ACTION_FAILED", "disposition": {"delivery": "not_delivered", "retry": "safe"}}}
                return {"ok": True, "delivery": "delivered_verified"}
        desktop = Repairable()
        model = Model("WINDOW", "PRESS_KEY:tab", "WINDOW", "FOCUS_WINDOW", "WINDOW", "PRESS_KEY:tab", "DONE")
        events = list(JevLoop(desktop, model, sleep=lambda _: None).run("Navigate", RunOptions(replan_safe_failures=True)))
        self.assertEqual(events[-1]["stop"], "done")
        self.assertEqual([a[1] for a in desktop.actions], ["PRESS_KEY:tab", "FOCUS_WINDOW", "PRESS_KEY:tab"])

    def test_model_time_context_change_replans_before_dispatch_without_scope_review(self):
        class Changing(Desktop):
            def observe(self, scope):
                seen = super().observe(scope)
                seen.screen["context"] = [{"role": "statictext", "name": "Old" if len(self.observations) == 1 else "New"}]
                return seen
        desktop = Changing()
        events = list(JevLoop(desktop, Model("1", "CLICK", "BLOCKED"), sleep=lambda _: None).run("Apply", RunOptions(replan_no_effect=True)))
        self.assertEqual(desktop.actions, [])
        self.assertTrue(any(e.get("recovery") == "replan_changed_action_context" for e in events))

    def test_unverified_activation_is_not_repeated_at_the_same_unchanged_focus(self):
        class Focused(Desktop):
            def observe(self, scope):
                seen = super().observe(scope)
                seen.screen.update(headed_text=True, context=[{"role": "button", "name": "Apply", "states": ["focused"]}])
                return seen
        desktop = Focused()
        desktop.result = {"ok": True, "delivery": "delivered_unverified"}
        model = Model("WINDOW", "PRESS_KEY:return", "within_request", "WINDOW", "PRESS_KEY:shift+f10", "DONE")
        events = list(JevLoop(desktop, model, sleep=lambda _: None).run("Open options", RunOptions(review_goal_scope=True, replan_no_effect=True)))
        self.assertEqual([a[1] for a in desktop.actions], ["PRESS_KEY:return", "PRESS_KEY:shift+f10"])
        self.assertNotIn("PRESS_KEY:return", model.requests[4]["questions"]["operation"]["criteria"])
        self.assertTrue(any(e.get("diagnostic") == "unverified_activation_without_observed_change" for e in events))

    def test_changed_static_counter_preserves_legitimate_repeated_activation(self):
        class Counter(Desktop):
            count = 0
            def observe(self, scope):
                seen = super().observe(scope)
                seen.screen.update(headed_text=True, context=[
                    {"role": "button", "name": "Increment", "states": ["focused"]},
                    {"role": "statictext", "name": str(self.count)},
                ])
                return seen
            def execute(self, scope, operation, target, text):
                super().execute(scope, operation, target, text)
                self.count += 1
                return {"ok": True, "delivery": "delivered_unverified"}
        for choices, reviewed in ((["1", "CLICK", "1", "CLICK", "DONE"], False), (["WINDOW", "PRESS_KEY:return", "within_request", "WINDOW", "PRESS_KEY:return", "within_request", "DONE"], True)):
            desktop = Counter()
            events = list(JevLoop(desktop, Model(*choices), sleep=lambda _: None).run("Increment twice", RunOptions(review_goal_scope=reviewed, replan_no_effect=True)))
            self.assertEqual(desktop.count, 2)
            self.assertTrue(all(e["turn"]["changed"] for e in events if "turn" in e))

    def test_preparation_requires_unique_complete_nonediting_focus_and_keeps_activation_reviewed(self):
        def window(role, **extra):
            return node(role="window", window_id="w1", focus_observation_complete=True,
                        reported_focus=[{"role": role, "name": "Observed", "states": ["focused"]}], **extra)
        for operation in ("PRESS_KEY:tab", "PRESS_KEY:shift+tab", "PRESS_KEY:shift+f10"):
            self.assertTrue(preparation_operation(operation, window("button")))
        self.assertTrue(preparation_operation("PRESS_KEY:shift+f10", window("option")))
        self.assertFalse(preparation_operation("PRESS_KEY:tab", window("option")))
        for role in ("textfield", "combobox", "terminal", "custom"):
            for operation in ("PRESS_KEY:tab", "PRESS_KEY:shift+tab", "PRESS_KEY:shift+f10"):
                self.assertFalse(preparation_operation(operation, window(role)))
        for operation in ("PRESS_KEY:return", "PRESS_KEY:space", "PRESS_KEY:delete", "PRESS_KEY:ctrl+shift+p"):
            self.assertFalse(preparation_operation(operation, window("button")))
        for changes in ({"reported_focus": []}, {"reported_focus": [{"role": "button", "states": ["focused"]}]*2}, {"window_id": None}, {"focus_observation_complete": False}):
            target = window("button")
            target.update(changes)
            self.assertFalse(preparation_operation("PRESS_KEY:shift+f10", target))

    def test_focus_change_invalidates_a_prior_keyboard_scope_rejection(self):
        class Focused(Desktop):
            focus = "A"
            def observe(self, scope):
                seen = super().observe(scope)
                seen.screen.update(headed_text=True, context=[{"role": "button", "name": self.focus, "states": ["focused"]}])
                return seen
            def execute(self, scope, operation, target, text):
                result = super().execute(scope, operation, target, text)
                if operation == "PRESS_KEY:tab":
                    self.focus = "B"
                return result
        desktop = Focused()
        model = Model("WINDOW", "PRESS_KEY:return", "broader_or_unproven", "WINDOW", "PRESS_KEY:tab", "WINDOW", "PRESS_KEY:return", "within_request", "DONE")
        events = list(JevLoop(desktop, model, sleep=lambda _: None).run("Activate B", RunOptions(review_goal_scope=True)))
        self.assertEqual(events[-1]["stop"], "done")
        self.assertEqual([a[1] for a in desktop.actions], ["PRESS_KEY:tab", "PRESS_KEY:return"])
        self.assertNotIn("PRESS_KEY:return", model.requests[4]["questions"]["operation"]["criteria"])
        self.assertIn("PRESS_KEY:return", model.requests[6]["questions"]["operation"]["criteria"])
        self.assertEqual(model.requests[7]["state"]["current_focus"][0]["name"], "B")

    def test_preparation_key_is_not_sent_if_focus_changes_to_an_editor_before_delivery(self):
        class MovingFocus(Desktop):
            def observe(self, scope):
                seen = super().observe(scope)
                focus = {"role": "button", "name": "A", "states": ["focused"]} if len(self.observations) == 1 else {"role": "textfield", "name": "Editor", "states": ["focused", "editable"]}
                seen.screen.update(headed_text=True, context=[focus])
                return seen
        desktop = MovingFocus()
        events = list(JevLoop(desktop, Model("WINDOW", "PRESS_KEY:tab", "BLOCKED"), sleep=lambda _: None).run("Navigate", RunOptions(review_goal_scope=True)))
        self.assertEqual(events[-1]["stop"], "blocked")
        self.assertEqual(desktop.actions, [])
        self.assertTrue(any(e.get("recovery") == "replan_changed_keyboard_focus" for e in events))

    def test_reported_focus_is_explicit_even_when_its_ref_is_not_executable(self):
        seen = Desktop().observe(Scope())
        seen.screen.update(headed_text=True, context=[
            {"role": "option", "name": "Extension row", "states": ["focused"], "ref_actionable": False},
            {"role": "button", "name": "Hidden", "states": ["focused", "hidden"]},
            {"role": "textfield", "name": "Secret", "value": "PRIVATE", "states": ["focused", "secure"]},
        ])
        space = action_space(seen.nodes)
        extend_space(space, seen.screen, RunOptions())
        request = build_request("Install", seen.screen, space, [], values=False)
        self.assertEqual([n["name"] for n in request["state"]["current_focus"]], ["Extension row", "Secret"])
        self.assertNotIn("PRIVATE", str(request))
        self.assertIn("current_focus", request["questions"]["target"]["criteria"]["WINDOW"]["purpose"])
        self.assertEqual(len(request["state"]["current_focus"]), 2)

    def test_scope_review_receives_the_actual_operation_meaning_without_approving_it(self):
        request = {"state": {"current_focus": [{"role": "option", "name": "Extension row"}]}}
        reviewed = review_request(request, "PRESS_KEY:shift+f10", node(role="window"), None)
        self.assertIn("Open the context menu", reviewed["state"]["proposed_operation_meaning"])
        self.assertEqual(reviewed["state"]["current_focus"], request["state"]["current_focus"])
        self.assertEqual(set(reviewed["questions"]["goal_scope"]["criteria"]), {"within_request", "broader_or_unproven"})
