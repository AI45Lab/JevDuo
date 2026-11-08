"""pool/settings 的纯逻辑单测：切片、任务解析、spec 渲染、rjob 状态解析。不起 VM、不调模型。"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import rjob  # noqa: E402
import settings  # noqa: E402


class ChunkTest(unittest.TestCase):
    def test_round_robin_spreads_long_tail(self):
        tasks = [f"{n:03d}" for n in range(1, 11)]
        chunks = settings.chunk(tasks, 3)
        self.assertEqual(len(chunks), 4)
        self.assertEqual(sorted(sum(chunks, [])), tasks)
        self.assertEqual(max(len(c) for c in chunks) - min(len(c) for c in chunks), 1)

    def test_empty(self):
        self.assertEqual(settings.chunk([], 5), [])


class ResolveTasksTest(unittest.TestCase):
    CONFIG = {"models": {"default": "m", "apis": {"m": {"model": "m", "base_url": "u"}}}}

    def test_default_all(self):
        self.assertEqual(len(settings.resolve_tasks(self.CONFIG, tasks="", limit=None, shard=None)), 108)

    def test_shard_interleaves(self):
        refs = settings.resolve_tasks(self.CONFIG, tasks="", limit=10, shard="2/3")
        self.assertEqual(refs, ["002", "005", "008"])

    def test_rejects_out_of_range(self):
        with self.assertRaises(settings.ConfigError):
            settings.resolve_tasks(self.CONFIG, tasks="109", limit=None, shard=None)


class SpecTest(unittest.TestCase):
    def test_render_and_mask(self):
        config = {
            "models": {"default": "m", "apis": {"m": {"model": "m", "base_url": "http://x/v1",
                                                       "api_key_env": "JEVDUO_TEST_KEY"}}},
            "harness": {"max_steps": 5, "max_wall_seconds": 60},
            "benchmark": {"name": "osworld21", "task_root": "/r", "vm": "/v"},
            "log": {"screenshots": True, "llm_calls": True},
        }
        os.environ["JEVDUO_TEST_KEY"] = "secret-value"
        name, api = settings.select_model(config, None)
        spec = settings.render_spec(config, run_id="r", job_id=0, tasks=["001"],
                                    api_name=name, api=api, run_root=Path("/tmp/run"))
        self.assertEqual(spec["model"]["api_key"], "secret-value")
        self.assertEqual(spec["output_root"], "/tmp/run/jobs/job-00")
        self.assertEqual(settings.masked(spec)["model"]["api_key"], "***")

    def test_missing_key_raises(self):
        config = {
            "models": {"default": "m", "apis": {"m": {"model": "m", "base_url": "u",
                                                       "api_key_env": "JEVDUO_ABSENT_KEY"}}},
            "harness": {}, "benchmark": {"name": "osworld21", "task_root": "/r", "vm": "/v"},
            "log": {},
        }
        name, api = settings.select_model(config, None)
        os.environ.pop("JEVDUO_ABSENT_KEY", None)
        with self.assertRaises(settings.ConfigError):
            settings.render_spec(config, run_id="r", job_id=0, tasks=["001"],
                                 api_name=name, api=api, run_root=Path("/tmp/run"))

    def test_write_spec_permissions(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = settings.write_spec({"job_id": 3}, Path(tmp))
            self.assertEqual(json.loads(path.read_text())["job_id"], 3)
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)


class RjobStatusTest(unittest.TestCase):
    def test_status_line(self):
        self.assertEqual(rjob.parse_status("rjob jevduo-x: Succeeded"), "succeeded")
        self.assertEqual(rjob.parse_status("replica jevduo-x-abc: FAILED"), "failed")

    def test_count_dict_not_confused(self):
        output = "{'active': 0, 'failed': 0, 'succeeded': 0}\nrjob jevduo-x: Running"
        self.assertEqual(rjob.parse_status(output), "running")

    def test_unknown(self):
        self.assertEqual(rjob.parse_status("weird output"), "unknown")


if __name__ == "__main__":
    unittest.main()
