from __future__ import annotations

import json
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import actor_run
import entry
import phase_run


class PhaseRunTest(unittest.TestCase):
    def setup_case(self, directory):
        spec = {
            "run_id": "phased",
            "model": {"name": "m", "model": "m", "base_url": "http://unused", "api_key": "test"},
            "harness": {
                "max_steps": 20,
                "max_wall_seconds": 10800,
                "evaluation_reserve_seconds": 600,
                "orchestration": {
                    "enabled": True,
                    "max_cycles": 20,
                    "subagent_steps_per_cycle": 50,
                    "max_model_calls": 1300,
                    "max_tool_calls": 1000,
                },
            },
        }
        vm = SimpleNamespace(
            vm_ip="127.0.0.1",
            server_port=5000,
            execute=Mock(),
            env=SimpleNamespace(_step_no=5, action_history=["old"], _traj_no=1, setup_controller=object()),
        )
        calls, setups = [], []
        phases = [
            {
                "name": f"phase-{i}",
                "instruction": f"instruction-{i}",
                "setup": lambda *args, **kwargs: setups.append(True),
                "evaluate": lambda env: 0.35,
                "gate_min_score": 0.35,
                "pause_after_setup_seconds": 0,
            }
            for i in range(2)
        ]

        def run(**kwargs):
            config = json.loads(kwargs["app_config"].read_text())
            calls.append((kwargs["query"], kwargs["max_steps"], config["orchestration"]))
            return {
                "returncode": 0,
                "terminal_status": "finish",
                "step_count": 2,
                "usage": {"model_calls": 80, "tool_calls": 40, "cleanup_tool_calls": 0},
            }

        return spec, vm, phases, calls, setups, run

    def test_native_phases_share_vm_cycle_and_call_budgets_without_double_weighting(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp) / "jobs/job-00/episodes/069"
            directory.mkdir(parents=True)
            spec, vm, phases, calls, setups, run = self.setup_case(directory)
            with patch.object(actor_run, "run_agent", side_effect=run):
                info, raw = phase_run.run_phases(
                    vm, spec, {"id": "069"}, directory, time.perf_counter(), phases
                )
            self.assertEqual(
                [(q, steps) for q, steps, _ in calls], [("instruction-0", 20), ("instruction-1", 18)]
            )
            self.assertEqual(calls[1][2]["max_model_calls"], 1220)
            self.assertEqual(calls[1][2]["max_tool_calls"], 960)
            self.assertEqual(info["step_count"], 4)
            self.assertEqual(info["usage"]["model_calls"], 160)
            self.assertEqual(raw["score"], 0.7)
            self.assertEqual(len(setups), 1)
            self.assertEqual(vm.env.action_history, [])
            self.assertEqual(vm.env._traj_no, 2)
            self.assertEqual(vm.execute.call_count, 2)
            self.assertTrue(all(call.args == ("DONE",) for call in vm.execute.call_args_list))

    def test_wall_stop_recovers_only_parent_steps_and_parent_usage(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            spec, vm, phases, _, _, _ = self.setup_case(directory)

            def interrupted(**kwargs):
                parent = kwargs["log_dir"] / "session"
                child = parent / "query_0001/subagents/child"
                child.mkdir(parents=True)
                (parent / "steps.jsonl").write_text('{"session_step":1}\n{"torn":')
                (child / "steps.jsonl").write_text('{"session_step":99}\n' * 5)
                for folder, calls in ((parent / "query_0001", 3), (child, 99)):
                    (folder / "orchestration.jsonl").write_text(json.dumps({
                        "state": {"usage": {"model_calls": calls, "tool_calls": 1, "cleanup_tool_calls": 0}},
                    }) + '\n{"torn":')
                return {"timed_out": True, "returncode": -15}

            with patch.object(actor_run, "run_agent", side_effect=interrupted):
                info, raw = phase_run.run_phases(vm, spec, {"id": "069"}, directory,
                                                time.perf_counter(), phases)
            self.assertEqual(info["step_count"], 2)
            self.assertEqual(info["usage"], {"model_calls": 6, "tool_calls": 2, "cleanup_tool_calls": 0})
            self.assertEqual(raw["score"], 0.7)
            vm.execute.assert_not_called()

    def test_native_gate_stops_before_next_setup_or_agent(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            spec, vm, phases, calls, setups, run = self.setup_case(directory)
            phases[0]["evaluate"] = lambda env: 0.2
            with patch.object(actor_run, "run_agent", side_effect=run):
                info, raw = phase_run.run_phases(
                    vm, spec, {"id": "069"}, directory, time.perf_counter(), phases
                )
            self.assertEqual(len(calls), 1)
            self.assertEqual(setups, [])
            self.assertEqual(raw["score"], 0.2)
            self.assertEqual(info["phase_stop_reason"], "phase_gate")

    def test_next_phase_reports_whole_task_time_remaining_after_prior_phase(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            spec, vm, phases, calls, _, run = self.setup_case(directory)
            with (
                patch.object(actor_run, "run_agent", side_effect=run),
                patch.object(phase_run.time, "perf_counter", side_effect=[100, 100, 100, 1600, 1600, 1600]),
            ):
                phase_run.run_phases(vm, spec, {"id": "069"}, directory, 100, phases)
            self.assertEqual(calls[0][2]["max_seconds"], 10200)
            self.assertEqual(calls[1][2]["max_seconds"], 8700)

    def test_phase_exception_propagates_instead_of_becoming_a_zero(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            spec, vm, phases, _, _, run = self.setup_case(directory)
            phases[0]["evaluate"] = Mock(side_effect=RuntimeError("evaluator unavailable"))
            with patch.object(actor_run, "run_agent", side_effect=run):
                with self.assertRaisesRegex(RuntimeError, "evaluator unavailable"):
                    phase_run.run_phases(vm, spec, {"id": "069"}, directory, time.perf_counter(), phases)

    def test_old_job_judge_failure_does_not_poison_later_task_phases(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp) / "episodes/069"
            directory.mkdir(parents=True)
            log = Path(tmp) / "worker/evaluation_model.jsonl"
            log.parent.mkdir()
            history = '{"status":"error","task":"previous"}\n'
            log.write_text(history)
            spec, vm, phases, calls, _, run = self.setup_case(directory)
            with patch.object(actor_run, "run_agent", side_effect=run):
                _, raw = phase_run.run_phases(vm, spec, {"id": "069"}, directory, time.perf_counter(), phases)
            self.assertEqual(raw["score"], 0.7)
            self.assertEqual(len(calls), 2)
            self.assertEqual(log.read_text(), history)

    def test_swallowed_phase_judge_error_stops_before_next_phase(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp) / "episodes/069"
            directory.mkdir(parents=True)
            log = Path(tmp) / "worker/evaluation_model.jsonl"
            log.parent.mkdir()
            spec, vm, phases, calls, setups, run = self.setup_case(directory)

            def swallowed_error(env):
                log.write_text('{"status":"error","error_type":"TimeoutError"}\n')
                return 0.35

            phases[0]["evaluate"] = swallowed_error
            with patch.object(actor_run, "run_agent", side_effect=run):
                with self.assertRaisesRegex(RuntimeError, "Official evaluator model failed"):
                    phase_run.run_phases(vm, spec, {"id": "069"}, directory, time.perf_counter(), phases)
            self.assertEqual(len(calls), 1)
            self.assertEqual(setups, [])

    def test_worker_passes_task_start_before_setup_to_phase_error_checks(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            spec, vm, phases, calls, setups, run = self.setup_case(root)
            log = root / "worker/evaluation_model.jsonl"

            def reset(task):
                log.parent.mkdir()
                log.write_text('{"status":"error","error_type":"TimeoutError"}\n')

            class PhasedTask(dict):
                def get_phases(self):
                    return phases

            vm.reset = reset
            spec["log"] = {}
            with (
                patch.object(actor_run, "run_agent", side_effect=run),
                patch.object(entry.worker_limits, "arm", return_value=lambda: None),
                patch.object(entry.recorder, "write_trajectory"),
            ):
                result = entry.run_task(vm, spec, PhasedTask(id="069", instruction="test"), root)
            self.assertEqual(result["status"], "failed_unscored")
            self.assertIsNone(result["score"])
            self.assertIn("Official evaluator model failed", result["exception"]["message"])
            self.assertEqual(len(calls), 1)
            self.assertEqual(setups, [])
