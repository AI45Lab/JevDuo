"""pool/settings 的纯逻辑单测：切片、任务解析、spec 渲染、rjob 状态解析。不起 VM、不调模型。"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import pool  # noqa: E402
import recorder  # noqa: E402
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


def _episode(run_root: Path, job: str, task: str, *, status: str = "scored",
             score=None, finished_at: float = 1.0) -> None:
    directory = run_root / "jobs" / job / "episodes" / task
    directory.mkdir(parents=True, exist_ok=True)
    record = {"task_id": task, "status": status, "score": score,
              "steps": 1, "seconds": 1.0, "stop_reason": "done", "model": "m",
              "finished_at": finished_at}
    (directory / "episode.json").write_text(json.dumps(record), encoding="utf-8")


class ResumeTest(unittest.TestCase):
    """续跑：以磁盘 episode.json 为准，不依赖 entry 写的 job summary。"""

    def test_collect_and_merge_from_disk(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _episode(root, "job-00", "001", score=1.0)
            _episode(root, "job-01", "002", status="failed_unscored", score=None)
            episodes = recorder.collect_episodes(root)
            self.assertEqual([item["task_id"] for item in episodes], ["001", "002"])
            merged = recorder.merge_episodes(root, episodes)
            self.assertEqual((merged["total"], merged["scored"], merged["failed_unscored"]), (2, 1, 1))
            self.assertEqual(merged["score_mean"], 1.0)

    def test_latest_record_wins(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _episode(root, "job-00", "003", status="failed_unscored", finished_at=1.0)
            _episode(root, "job-01", "003", status="scored", score=0.5, finished_at=2.0)
            episodes = recorder.collect_episodes(root)
            self.assertEqual(len(episodes), 1)
            self.assertEqual(episodes[0]["status"], "scored")

    def test_csv_keeps_zero_score(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _episode(root, "job-00", "004", score=0.0)
            recorder.merge_episodes(root, recorder.collect_episodes(root))
            self.assertIn("004,scored,0.0", (root / "summary.csv").read_text(encoding="utf-8"))

    def test_skip_scored_by_default(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _episode(root, "job-00", "005", score=1.0)
            _episode(root, "job-00", "006", status="failed_unscored")
            args = type("A", (), {"resume": 1, "retry_failed": 0})()
            done, retry = pool._resume_state(root, args)
            # 默认：跑过的题都算完成，包括上次未评分的（避免反复重试环境问题）
            self.assertEqual(done, {"005", "006"})
            self.assertEqual(retry, [])

    def test_retry_failed_reruns_them(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _episode(root, "job-00", "005", score=1.0)
            _episode(root, "job-00", "006", status="failed_unscored")
            args = type("A", (), {"resume": 1, "retry_failed": 1})()
            done, retry = pool._resume_state(root, args)
            # 只跳过已评分的，未评分的进重跑队列
            self.assertEqual(done, {"005"})
            self.assertEqual(retry, ["006"])

    def test_resume_disabled(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _episode(root, "job-00", "007", score=1.0)
            args = type("A", (), {"resume": 0, "retry_failed": 0})()
            self.assertEqual(pool._resume_state(root, args), (set(), []))


class AdoptTest(unittest.TestCase):
    """接管判断：只有明确 running 才接管；不存在的 job 解析为 unknown，必须重新提交。"""

    class _Client:
        def __init__(self, output: str):
            self._output = output

        def get(self, name: str) -> str:
            return self._output

    def test_running_is_adopted(self):
        self.assertTrue(pool._is_running(self._Client("rjob jevduo-x: Running"), "jevduo-x"))

    def test_unknown_is_not_adopted(self):
        # rjob get 对不存在的 job 返回空输出且退出 0
        self.assertFalse(pool._is_running(self._Client(""), "jevduo-x"))

    def test_terminal_is_not_adopted(self):
        for word in ("Succeeded", "Failed", "Stopped"):
            self.assertFalse(pool._is_running(
                self._Client(f"rjob jevduo-x: {word}"), "jevduo-x"))


if __name__ == "__main__":
    unittest.main()
