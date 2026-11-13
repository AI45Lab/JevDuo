"""A scored wall stop retains completed cycles despite a missing agent result."""

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import actor_run
import entry


class WorkerStepRecoveryTest(unittest.TestCase):
    def test_actor_error_retains_reported_steps_without_a_readable_log(self):
        for status, message in (("error", "model failure"), ("finish", "terminal delivery failed")):
            with self.subTest(status=status), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                vm = Mock(vm_ip="test-vm", server_port=5000)
                vm.execute.side_effect = RuntimeError(message)
                spec = {"run_id": "error", "harness": {"max_steps": 10, "max_wall_seconds": 60},
                        "model": {"name": "scripted", "api_key": "test-only"}, "log": {}}
                with (
                    patch.object(actor_run, "run_agent", return_value={
                        "returncode": 0, "step_count": 2, "terminal_status": status,
                        "terminal_decision": {"final_answer": message},
                    }),
                    patch.object(actor_run, "write_configs", return_value=root / "app.yaml"),
                    patch.object(entry.worker_limits, "arm", return_value=lambda: None),
                    patch.object(entry.recorder, "write_trajectory"),
                ):
                    result = entry.run_task(vm, spec, {"id": "014", "instruction": "test"}, root)
                self.assertEqual(result["status"], "failed_unscored")
                self.assertEqual(result["steps"], 2)
                self.assertIn(message, result["exception"]["message"])
                if status == "error":
                    vm.execute.assert_not_called()
                else:
                    vm.execute.assert_called_once_with("DONE", pause=1)
                vm.score.assert_not_called()

    def test_counts_all_phase_cycles_but_ignores_a_torn_trailing_record(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for name in ("phase-01", "phase-02"):
                path = root / "agent" / name / "session" / "steps.jsonl"
                path.parent.mkdir(parents=True)
                path.write_text('{"session_step":1}\n{"session_step":2}\n{"session_step":')
            self.assertEqual(entry._steps_from_disk(root), 4)

    def test_wall_stop_counts_complete_persisted_steps_and_runs_official_score(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            scores = []

            def run_agent(**kwargs):
                path = kwargs["log_dir"] / "session" / "steps.jsonl"
                path.parent.mkdir(parents=True)
                path.write_text('{"session_step":1}\n{"session_step":2}\n{"session_step":')
                return {"timed_out": True, "returncode": -15}

            def score(task):
                scores.append(task["id"])
                return 0.25

            vm = SimpleNamespace(reset=lambda task: None, vm_ip="test-vm", server_port=5000, score=score)
            spec = {"run_id": "scripted-wall", "harness": {"max_steps": 500, "max_wall_seconds": 10800},
                    "model": {"name": "scripted", "api_key": "test-only"}, "log": {}}
            with (
                patch.object(actor_run, "run_agent", run_agent),
                patch.object(actor_run, "write_configs", return_value=root / "app.yaml"),
                patch.object(entry.worker_limits, "arm", return_value=lambda: None),
                patch.object(entry.recorder, "write_trajectory"),
            ):
                result = entry.run_task(vm, spec, {"id": "014", "instruction": "test"}, root)
            self.assertEqual(result["status"], "scored")
            self.assertEqual(result["stop_reason"], "wall_time_limit")
            self.assertEqual(result["steps"], 2)
            self.assertEqual(result["score"], 0.25)
            self.assertEqual(scores, ["014"])
