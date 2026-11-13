"""The repair controller must stop only the newly submitted failed rjob."""

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import recovery_batch


class RecoveryBatchTest(unittest.TestCase):
    def test_transient_status_read_is_retried_without_stopping_or_resubmitting(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            calls = {"submit": 0, "get": 0, "stop": 0}
            submitted = []
            source = root / "source-snapshot"
            original_root = recovery_batch.pool.ROOT

            class Client:
                def submit(self, argv):
                    calls["submit"] += 1
                    submitted.append(argv)
                    return "created rjob_name: repair-read-12345"

                def get(self, name):
                    calls["get"] += 1
                    if calls["get"] == 1:
                        raise recovery_batch.rjob.RjobError("scripted control-plane read outage")
                    return "rjob repair-read-12345: Succeeded"

                def stop(self, name):
                    calls["stop"] += 1

                def logs(self, name):
                    return "scripted worker log"

            config = {
                "models": {"default": "m", "apis": {"m": {"model": "m", "base_url": "http://test"}}},
                "benchmark": {"name": "osworld21", "task_root": "/tasks", "vm": "/image"},
                "harness": {"max_steps": 500, "max_wall_seconds": 10800},
                "cluster": {"image": "test", "private_machine": False}, "log": {},
            }
            with (
                patch.object(recovery_batch.rjob, "Client", Client),
                patch.object(recovery_batch.time, "sleep"),
            ):
                result = recovery_batch.execute(config, "repair-read", root, 0, "074", repo_root=source)
            # Two monitor reads and one final evidence collection read.
            self.assertEqual(calls, {"submit": 1, "get": 3, "stop": 0})
            self.assertEqual(result["status"], "succeeded")
            self.assertEqual(result["episode_status"], "missing_episode")
            self.assertIsNone(result["score"])
            self.assertTrue((root / "jobs/job-00/monitor-errors.jsonl").exists())
            self.assertEqual(submitted[0][-2], str(source / "infra/worker.sh"))
            self.assertEqual(recovery_batch.pool.ROOT, original_root)

    def test_legacy_model_timeout_cannot_count_as_a_budget_stop(self):
        self.assertEqual(recovery_batch.agent_failure({"terminal_status": "fail", "terminal_decision": {
            "reason": "budget_exhausted", "final_answer": "LLM call timed out after 180.0 seconds.",
        }}), "LLM call timed out after 180.0 seconds.")
        self.assertIsNone(recovery_batch.agent_failure({"terminal_status": "fail", "terminal_decision": {
            "reason": "budget_exhausted", "final_answer": "global_model_calls_limit",
        }}))

    def test_agent_error_stops_actual_metadata_name_without_deleting_any_job(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            stops = []

            class Client:
                def submit(self, argv):
                    episode = root / "jobs/job-00/episodes/061"
                    episode.mkdir(parents=True)
                    (episode / "agent_result.json").write_text(json.dumps({
                        "terminal_status": "error", "terminal_decision": {"final_answer": "transport failed"},
                    }))
                    return "created rjob_name: repair-pilot-12345"

                def stop(self, name):
                    stops.append(name)

                def get(self, name):
                    return "rjob repair-pilot-12345: Stopped"

                def logs(self, name):
                    return "sanitized worker evidence"

            config = {
                "models": {"default": "m", "apis": {"m": {"model": "m", "base_url": "http://test"}}},
                "benchmark": {"name": "osworld21", "task_root": "/tasks", "vm": "/image"},
                "harness": {"max_steps": 500, "max_wall_seconds": 10800},
                "cluster": {"image": "test", "private_machine": False}, "log": {},
            }
            with patch.object(recovery_batch.rjob, "Client", Client):
                result = recovery_batch.execute(config, "repair-pilot", root, 0, "061")
            self.assertEqual(stops, ["repair-pilot-12345"])
            self.assertEqual(result["status"], "failed_unscored")
            self.assertFalse((root / "jobs/job-00/episodes/061/episode.json").exists())

    def test_missing_submission_name_is_not_replayed_or_stopped_by_display_name(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)

            class Client:
                def submit(self, argv):
                    return "connection ended without a metadata name"

                def stop(self, name):
                    raise AssertionError("Unknown submission must not target an inferred job")

            config = {
                "models": {"default": "m", "apis": {"m": {"model": "m", "base_url": "http://test"}}},
                "benchmark": {"name": "osworld21", "task_root": "/tasks", "vm": "/image"},
                "harness": {"max_steps": 500, "max_wall_seconds": 10800},
                "cluster": {"image": "test", "private_machine": False}, "log": {},
            }
            with patch.object(recovery_batch.rjob, "Client", Client):
                result = recovery_batch.execute(config, "repair-pilot", root, 0, "061")
            self.assertEqual(result["status"], "submission_unknown")
