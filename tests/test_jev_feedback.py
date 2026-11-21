"""Native evidence and cross-contract progress; no VM or model provider."""

import json
import unittest

import pytest

pytest.importorskip("jev_desktop")

from northcua.agents.desktop.subagents.jev_feedback import bounded_json, feedback
from northcua.agents.desktop.subagents.jev_progress import repeated_blocker, unreconciled_actions


class FeedbackTests(unittest.TestCase):
    def test_latest_action_evidence_prefers_post_observation(self):
        value = feedback([{
            "turn": {"operation": "TYPE_TEXT", "ok": True, "changed": True,
                     "verification_scope": "element_value", "application_commit": "not_verified"},
            "screen": {"window_id": "w1", "visible_context": ["old"]},
            "after_screen": {"window_id": "w1", "visible_context": ["new"]},
        }, {"stop": "exception"}])
        self.assertEqual(value["screen"]["visible_context"], ["new"])
        self.assertEqual(value["turns"][0]["application_commit"], "not_verified")

    def test_resource_uncertainty_blocks_rebound_indices_and_other_windows(self):
        from jev_desktop import RunOptions
        from jev_desktop.actions import extend_space
        from jev_desktop.policy import action_space

        value = feedback([{
            "turn": {"operation": "OPEN_RESOURCE:0", "target": 'window "Document"',
                     "ok": False, "delivery": "delivery_uncertain"},
            "screen": {"window_id": "old-window"},
        }, {"stop": "action_failed"}])
        blocked = unreconciled_actions([{"subagent_result": {"summary": bounded_json(value)}}])
        self.assertEqual(len(blocked), 16)
        space = extend_space(
            action_space([]), {"app": "Editor", "window_id": "new-window", "headed_text": True},
            RunOptions(resources=("/tmp/other",), blocked_actions=blocked, reconcile_observed_popups=True),
        )
        self.assertNotIn("OPEN_RESOURCE:0", space["by_target"]["WINDOW"]["operations"])
        self.assertIn("FOCUS_WINDOW", space["by_target"]["WINDOW"]["operations"])

    def test_execution_diagnostics_survive_large_screen_and_history_truncation(self):
        diagnostic = {
            "command": "snapshot",
            "failure_kind": "timeout",
            "error_type": "ReadTimeout",
            "snapshot_timeout_ms": 60000,
            "timeout_seconds": 65,
        }
        value = feedback(
            [
                {
                    "stop": "exception",
                    "error": {"code": "REMOTE_EXECUTION_UNCONFIRMED"},
                    "screen": {"visible_context": ["x" * 10000] * 200},
                }
            ],
            execution_diagnostics=[diagnostic] * 20,
        )
        value["turns"] = [{"target": "x" * 2000}] * 200
        receipt = {"operation": "TYPE_TEXT", "delivery": "delivery_uncertain", "window_id": "w1"}
        value["interrupted_action"] = receipt
        value["unresolved_delivery"] = True
        self.assertEqual(value["recovery"]["snapshot_timeout_ms"], 60000)
        summary = json.loads(bounded_json(value))
        self.assertEqual(summary["execution_diagnostics"], [diagnostic] * 8)
        self.assertEqual(summary["blocked_reason"], "REMOTE_EXECUTION_UNCONFIRMED")
        self.assertEqual(summary["interrupted_action"], receipt)
        self.assertTrue(summary["unresolved_delivery"])

    def test_non_object_historical_summaries_do_not_crash_recovery(self):
        for value in (None, [], 42, "message"):
            cycle = {
                "decision": {"contract": {"agent_id": "gui_jev"}},
                "subagent_result": {"summary": json.dumps(value)},
            }
            self.assertEqual(unreconciled_actions([cycle]), ())
            self.assertIsNone(repeated_blocker([cycle] * 3, {}))

    def cycle(self, *, context="same", changed=False, native_input=None):
        summary = feedback(
            [
                {
                    "turn": {"operation": "CLICK", "target": 'menu "File"', "ok": True, "changed": changed},
                    "screen": {
                        "app": "Editor",
                        "window": "Document",
                        "window_id": "w1",
                        "visible_context": [context],
                    },
                },
                {"stop": "blocked"},
            ]
        )
        return {
            "decision": {"contract": {"agent_id": "gui_jev", "agent_input": native_input or {}}},
            "subagent_result": {"outcome": "blocked", "summary": bounded_json(summary)},
        }

    def test_changing_subgoal_ids_does_not_reset_a_repeated_native_failure(self):
        history = [self.cycle() for _ in range(3)]
        self.assertIsNotNone(repeated_blocker(history, {}))
        self.assertIsNone(repeated_blocker(history[:2], {}))
        self.assertIsNone(repeated_blocker(history, {"input_mode": "insert"}))
        self.assertIsNone(repeated_blocker([self.cycle(), self.cycle(context="new"), self.cycle()], {}))
        self.assertIsNone(repeated_blocker([self.cycle(), self.cycle(changed=True), self.cycle()], {}))

    def test_uncertain_native_delivery_survives_summary_size_limits(self):
        value = feedback(
            [
                {
                    "turn": {
                        "operation": "TYPE_TEXT",
                        "target": 'textfield "Name"',
                        "ref": "@s1:e1",
                        "ok": False,
                        "delivery": "delivery_uncertain",
                    },
                    "screen": {"window_id": "w1", "visible_context": ["x" * 10000] * 200},
                },
                {"stop": "action_failed", "error": {"code": "ACTION_FAILED"}},
            ]
        )
        summary = bounded_json(value)
        self.assertLessEqual(len(summary), 32000)
        actions = unreconciled_actions([{"subagent_result": {"summary": summary}}])
        self.assertEqual(actions[0]["target"], 'textfield "Name"')
        self.assertEqual(actions[0]["window_id"], "w1")
        self.assertEqual(actions[0]["delivery"], "delivery_uncertain")
        self.assertEqual(json.loads(summary)["blocked_reason"], "ACTION_FAILED")

    def test_dispatched_text_without_post_observation_is_not_replayable(self):
        turn = {
            "operation": "INSERT_TEXT",
            "target": 'textfield "Body"',
            "ok": True,
            "delivery": "delivered_unverified",
            "changed": None,
        }
        value = feedback([{"turn": turn, "screen": {"window_id": "w1"}}, {"stop": "exception"}])
        self.assertEqual(value["unsafe_to_replay"][0]["operation"], "INSERT_TEXT")
        turn["changed"] = True
        value = feedback([{"turn": turn}, {"stop": "done"}])
        self.assertEqual(value["unsafe_to_replay"], [])
        for operation in ("CLICK", "PRESS_KEY:return"):
            turn.update(operation=operation, changed=None)
            value = feedback([{"turn": turn}, {"stop": "exception"}])
            self.assertEqual(value["unsafe_to_replay"][0]["operation"], operation)

    def test_lost_or_redacted_target_identity_blocks_the_whole_window(self):
        for target in ("x" * 3000, 'textfield "[redacted]"'):
            value = feedback(
                [
                    {
                        "turn": {
                            "operation": "TYPE_TEXT",
                            "target": target,
                            "ok": False,
                            "delivery": "delivery_uncertain",
                        },
                        "screen": {"window_id": "w1"},
                    },
                    {"stop": "exception"},
                ]
            )
            actions = unreconciled_actions([{"subagent_result": {"summary": bounded_json(value)}}])
            self.assertEqual(actions[0], {"operation": "*", "target": "*", "window_id": "w1"})
