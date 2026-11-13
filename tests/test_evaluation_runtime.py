"""Configure judge credentials without leaking them or accepting swallowed errors."""

import json
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import actor_run
import entry
import evaluation_runtime


class EvaluatorRuntimeTest(unittest.TestCase):
    def test_original_judge_prompt_is_preserved_and_small_output_budget_is_raised(self):
        original = Mock(return_value='{"verdict":"yes"}')
        facade = types.SimpleNamespace(generate_text=original, create_backend=Mock())
        evaluators = types.ModuleType("desktop_env.evaluators")
        evaluators.model_client = facade
        package = types.ModuleType("desktop_env")
        package.evaluators = evaluators
        spec = {"evaluation_model": {"provider": "openai_compatible", "model": "k3.1-pro",
                                    "base_url": "http://gateway/v1", "api_key": "test-secret",
                                    "min_output_tokens": 8192}}
        with tempfile.TemporaryDirectory() as directory, patch.dict(sys.modules, {
            "desktop_env": package, "desktop_env.evaluators": evaluators,
        }), patch.dict("os.environ"):
            root = Path(directory)
            evaluation_runtime.configure(spec, root)
            options = {"max_tokens": 200, "temperature": 0}
            result = facade.generate_text("unaltered official rubric", options=options)
            self.assertEqual(result, '{"verdict":"yes"}')
            self.assertEqual(original.call_args.args, ("unaltered official rubric",))
            self.assertEqual(original.call_args.kwargs["options"]["max_tokens"], 8192)
            self.assertEqual(options["max_tokens"], 200)
            log = (root / "worker/evaluation_model.jsonl").read_text()
            self.assertNotIn("test-secret", log)
            self.assertEqual(json.loads(log)["status"], "ok")
            evaluation_runtime.check_failures(root)

    def test_judge_outage_cannot_be_swallowed_into_a_numeric_zero(self):
        original = Mock(side_effect=TimeoutError("judge unavailable"))
        facade = types.SimpleNamespace(generate_text=original, create_backend=Mock())
        evaluators = types.ModuleType("desktop_env.evaluators")
        evaluators.model_client = facade
        package = types.ModuleType("desktop_env")
        package.evaluators = evaluators
        spec = {"evaluation_model": {"provider": "openai_compatible", "model": "k3.1-pro",
                                    "base_url": "http://gateway/v1", "api_key": "test-secret"}}
        with tempfile.TemporaryDirectory() as directory, patch.dict(sys.modules, {
            "desktop_env": package, "desktop_env.evaluators": evaluators,
        }), patch.dict("os.environ"):
            root = Path(directory)
            evaluation_runtime.configure(spec, root)
            with self.assertRaises(TimeoutError):
                facade.generate_text("official rubric")
            # Even if a task catches that TimeoutError and returns 0, the worker fails unscored.
            with self.assertRaisesRegex(RuntimeError, "Official evaluator model failed"):
                evaluation_runtime.check_failures(root)

    def test_serial_tasks_keep_their_own_judge_status_and_preserve_history(self):
        for old_log in (False, True):
            with self.subTest(old_log=old_log), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                log_path = root / "worker/evaluation_model.jsonl"
                history = '{"status":"error","task":"旧任务"}\n' if old_log else ""
                if history:
                    log_path.parent.mkdir()
                    log_path.write_text(history, encoding="utf-8")
                original = Mock(side_effect=[TimeoutError("judge unavailable"), "yes", "no"])
                facade = types.SimpleNamespace(generate_text=original, create_backend=Mock())
                evaluators = types.ModuleType("desktop_env.evaluators")
                evaluators.model_client = facade
                package = types.ModuleType("desktop_env")
                package.evaluators = evaluators
                spec = {
                    "run_id": "judge-isolation", "job_id": 0, "output_root": str(root),
                    "tasks": ["001", "002", "003", "004"],
                    "benchmark": {"task_root": "/unused", "vm": "/unused"},
                    "harness": {"max_steps": 1, "max_wall_seconds": 60},
                    "model": {"name": "scripted", "api_key": "test-only"}, "log": {},
                    "evaluation_model": {
                        "provider": "openai_compatible", "model": "scripted",
                        "base_url": "http://unused", "api_key": "test-only",
                    },
                }

                def score(task, facade=facade):
                    if task["id"] == "004":
                        return 0.25  # A task that needs no judge must also remain scored.
                    try:
                        answer = facade.generate_text("official rubric")
                    except TimeoutError:
                        return 0.0  # Simulate an evaluator swallowing a judge outage.
                    return 0.5 if answer == "yes" else 0.0

                vm = Mock(vm_ip="test-vm", server_port=5000)
                vm.fetch_task.side_effect = lambda task_id: {"id": task_id, "instruction": "test"}
                vm.score.side_effect = score
                with (
                    patch.dict(sys.modules, {"desktop_env": package, "desktop_env.evaluators": evaluators}),
                    patch.dict("os.environ"),
                    patch.object(entry, "Vm", return_value=vm),
                    patch.object(actor_run, "run_agent", return_value={
                        "returncode": 0, "step_count": 1, "terminal_status": "finish",
                    }),
                    patch.object(actor_run, "write_configs", return_value=root / "unused.json"),
                    patch.object(entry.worker_limits, "arm", return_value=lambda: None),
                    patch.object(entry.recorder, "write_trajectory"),
                ):
                    result = entry.run_job(spec)
                self.assertEqual(result["failed_unscored"], 1)
                self.assertEqual(result["scored"], 3)
                episodes = [json.loads((root / "episodes" / task / "episode.json").read_text())
                            for task in spec["tasks"]]
                self.assertEqual([episode["score"] for episode in episodes], [None, 0.5, 0.0, 0.25])
                self.assertEqual([episode["status"] for episode in episodes],
                                 ["failed_unscored", "scored", "scored", "scored"])
                log = log_path.read_text(encoding="utf-8")
                self.assertTrue(log.startswith(history))
                records = [json.loads(line) for line in log.splitlines()]
                self.assertEqual([record["status"] for record in records],
                                 (["error"] if old_log else []) + ["error", "ok", "ok"])
                self.assertNotIn("test-only", log)
                vm.close.assert_called_once()
