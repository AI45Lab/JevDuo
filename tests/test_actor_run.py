"""Single and phased callers share the same bounded actor/terminal contract."""

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import actor_run


class ActorRunTest(unittest.TestCase):
    def test_terminal_actions_errors_and_budget_stops_are_delivered_once(self):
        cases = [
            ({"terminal_status": "finish"}, "DONE", "done"),
            ({"terminal_status": "fail"}, "FAIL", "fail"),
            ({"hit_step_limit": True}, None, "max_steps"),
            ({"terminal_decision": {"reason": "budget_exhausted"}}, None, "orchestration_budget_exhausted"),
            ({"timed_out": True, "returncode": -15}, None, "wall_time_limit"),
            ({"error": "transport failed"}, None, None),
        ]
        for result, action, reason in cases:
            with self.subTest(result=result), tempfile.TemporaryDirectory() as tmp:
                directory = Path(tmp)
                vm = Mock(vm_ip="vm", server_port=5000)
                info = {"returncode": 0, "step_count": 2, **result}
                harness = {"max_steps": 6, "max_wall_seconds": 100, "evaluation_reserve_seconds": 20,
                           "orchestration": {"enabled": True, "max_seconds": 100}}
                with (
                    patch.object(actor_run, "run_agent", return_value=info) as run,
                    patch.object(actor_run.time, "perf_counter", side_effect=[10, 12]),
                ):
                    kwargs = {"vm": vm, "model": {"model": "test", "base_url": "http://unused",
                                                   "api_key": "private-test-key"},
                              "harness": harness, "query": "test", "session_id": "test",
                              "directory": directory, "log_dir": directory / "agent", "started": 0}
                    if reason is None:
                        with self.assertRaisesRegex(RuntimeError, "transport failed"):
                            actor_run.run_actor(**kwargs)
                    else:
                        returned, stop_reason = actor_run.run_actor(**kwargs)
                        self.assertIs(returned, info)
                        self.assertEqual(stop_reason, reason)
                if action:
                    vm.execute.assert_called_once_with(action, pause=1)
                else:
                    vm.execute.assert_not_called()
                self.assertEqual(run.call_args.kwargs["timeout_seconds"], 68)
                self.assertEqual(run.call_args.kwargs["result_path"], directory / "agent_result.json")
                app = json.loads((directory / "config/app.yaml").read_text())
                self.assertEqual(app["orchestration"]["max_seconds"], 70)
                self.assertEqual(harness["orchestration"]["max_seconds"], 100)
                self.assertNotIn("private-test-key", (directory / "config/agent.yaml").read_text())
