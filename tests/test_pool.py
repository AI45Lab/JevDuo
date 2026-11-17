"""pool/settings 的纯逻辑单测：切片、任务解析、spec 渲染、rjob 状态解析。不起 VM、不调模型。"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import pool  # noqa: E402
import recorder  # noqa: E402
import rjob  # noqa: E402
import settings  # noqa: E402
import task_run  # noqa: E402


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
            "harness": {"max_steps": 5, "max_wall_seconds": 60,
                                "orchestration": {"max_cycles": 5, "max_seconds": 60,
                                                  "max_model_calls": 1300, "max_tool_calls": 1000}},
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

    def test_subagent_credentials_are_carried_in_private_spec_and_masked(self):
        config = {
            "harness": {"orchestration": {"max_cycles": 20, "max_seconds": 60,
                                              "max_model_calls": 100, "max_tool_calls": 1000}},
            "benchmark": {"name": "osworld21", "task_root": "/r", "vm": "/v"},
            "log": {},
        }
        from unittest.mock import patch

        with patch.dict(os.environ, {"TYPESAFE_API_KEY": "private-jev-key", "TYPESAFE_MODEL": "jev-latest"}):
            spec = settings.render_spec(config, run_id="r", job_id=0, tasks=["061"],
                                        api_name="m", api={"base_url": "http://test", "model": "m"},
                                        run_root=Path("/tmp/test"))
        self.assertEqual(spec["subagent_env"]["TYPESAFE_API_KEY"], "private-jev-key")
        self.assertEqual(settings.masked(spec)["subagent_env"]["TYPESAFE_API_KEY"], "***")

    def test_missing_key_raises(self):
        config = {
            "models": {"default": "m", "apis": {"m": {"model": "m", "base_url": "u",
                                                       "api_key_env": "JEVDUO_ABSENT_KEY"}}},
            "harness": {"orchestration": {"max_cycles": 20, "max_seconds": 60,
                                              "max_model_calls": 100, "max_tool_calls": 1000}},
            "benchmark": {"name": "osworld21", "task_root": "/r", "vm": "/v"},
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
    def test_worker_uses_the_current_checkout(self):
        config = {"cluster": {"image": "unused"}}
        regular = pool.submit_argv(config, run_id="r", job_id=0, spec_path=Path("/spec.json"))
        self.assertEqual(regular[-2:], [str(pool.ROOT / "infra/worker.sh"), "/spec.json"])

    def test_status_line(self):
        self.assertEqual(rjob.parse_status("rjob jevduo-x: Succeeded"), "succeeded")
        self.assertEqual(rjob.parse_status("replica jevduo-x-abc: FAILED"), "failed")

    def test_count_dict_not_confused(self):
        output = "{'active': 0, 'failed': 0, 'succeeded': 0}\nrjob jevduo-x: Running"
        self.assertEqual(rjob.parse_status(output), "running")

    def test_unknown(self):
        self.assertEqual(rjob.parse_status("weird output"), "unknown")

    def test_actual_platform_status_with_showname_and_logger_prefix(self):
        for word in ("Running", "Succeeded", "Failed", "Stopped"):
            with self.subTest(word=word):
                output = (f"11-16 12:45:41 [INFO] rjob jevduo-r-00-92432325 "
                          f"(showname=jevduo-r-00): {word}\n"
                          "{'active': 0, 'failed': 0, 'succeeded': 0, 'stopped': 1}")
                self.assertEqual(rjob.parse_status(output), word.lower())

    def test_stopped_finishes_watch_without_polling_again(self):
        client = Mock()
        client.get.return_value = "rjob actual-name (showname=display): Stopped"
        sleep = Mock()
        self.assertEqual(rjob.watch(client, "actual-name", sleep=sleep), "stopped")
        sleep.assert_not_called()

    def test_status_query_failure_retries_reads_until_deadline(self):
        client = Mock()
        client.get.side_effect = [rjob.RjobError("temporary outage"), "rjob actual: Succeeded"]
        sleep = Mock()
        self.assertEqual(rjob.watch(client, "actual", timeout_seconds=60, sleep=sleep), "succeeded")
        self.assertEqual(client.get.call_count, 2)
        sleep.assert_called_once()
        client.get.side_effect = rjob.RjobError("continued outage")
        with patch.object(rjob.time, "monotonic", side_effect=[0, 61]):
            with self.assertRaisesRegex(TimeoutError, "query_error"):
                rjob.watch(client, "actual", timeout_seconds=60, sleep=sleep)


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


class PoolRunTest(unittest.TestCase):
    """One ordinary entry supports fresh reruns without replaying uncertain submissions."""

    def config(self):
        return {
            "project": {"run_root": "runs"},
            "models": {"default": "m", "apis": {"m": {"model": "m", "base_url": "http://test"}}},
            "benchmark": {"name": "osworld21", "task_root": "/tasks", "vm": "/image"},
            "harness": {"max_steps": 20, "max_wall_seconds": 10800, "orchestration": {
                "max_cycles": 20, "max_seconds": 10800, "max_model_calls": 1300, "max_tool_calls": 1000,
            }},
            "pool": {"tasks_per_job": 1, "max_parallel": 1},
            "cluster": {"image": "test", "private_machine": False}, "log": {},
        }

    def run_job(self, root, client, **overrides):
        return pool.run_one(self.config(), run_id="r", job_id=0, tasks=["005"], api_name="m",
                            api={"base_url": "http://test", "model": "m"},
                            run_root=root, keep=False, client=client, **overrides)

    def test_actual_name_is_persisted_and_used_for_monitoring_and_cleanup(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            calls = []

            def command(argv):
                calls.append(argv)
                if argv[1] == "submit":
                    record = json.loads((root / "jobs/job-00/job.json").read_text())
                    self.assertEqual(record["status"], "submission_unknown")
                    return 0, "11-16 [INFO] created rjob_name: actual-92432325"
                if argv[1] == "get":
                    return 0, "rjob actual-92432325 (showname=jevduo-r-00): Succeeded"
                return 0, "worker log"

            result = self.run_job(root, rjob.Client(command))
            self.assertEqual(result["rjob"], "actual-92432325")
            self.assertEqual(result["status"], "succeeded")
            self.assertEqual(result["requested_name"], "jevduo-r-00")
            self.assertEqual([argv[1] for argv in calls], ["submit", "get", "get", "logs", "stop", "delete"])
            self.assertTrue(all("actual-92432325" in argv for argv in calls[1:]))

    def test_unknown_submission_is_never_replayed_or_cleaned_up(self):
        for outcome in ("connection ended without receipt", rjob.RjobError("CLI error"),
                        subprocess.TimeoutExpired(["rjob", "submit"], 600)):
            with self.subTest(outcome=type(outcome).__name__), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                calls = []

                def command(argv, calls=calls, outcome=outcome):
                    calls.append(argv)
                    self.assertEqual(argv[1], "submit")
                    if isinstance(outcome, Exception):
                        raise outcome
                    return 0, outcome

                client = rjob.Client(command)
                first = self.run_job(root, client)
                spec = (root / "job_specs/job-00.json").read_bytes()
                second = self.run_job(root, client)
                self.assertEqual(first["status"], "submission_unknown")
                self.assertEqual(second["status"], "submission_unknown")
                self.assertEqual(len(calls), 1)
                self.assertEqual((root / "job_specs/job-00.json").read_bytes(), spec)

    def test_saved_attempt_is_observed_without_rewriting_spec_or_restarting(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            directory = root / "jobs/job-00"
            directory.mkdir(parents=True)
            (directory / "job.json").write_text(json.dumps({
                "job_id": 0, "rjob": "saved-actual", "tasks": ["005"], "status": "running",
            }))
            with patch.object(settings, "render_spec", side_effect=AssertionError("do not rewrite")):
                client = Mock()
                client.get.return_value = "rjob saved-actual (showname=display): Stopped"
                client.logs.return_value = "worker log"
                result = self.run_job(root, client)
            self.assertEqual(result["status"], "stopped")
            client.submit.assert_not_called()
            client.stop.assert_not_called()
            client.delete.assert_not_called()
            self.assertEqual(client.get.call_args_list[0].args, ("saved-actual",))

    def test_retry_plan_preserves_old_directories_and_observes_unfinished_jobs(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for job_id, tasks, status in ((0, ["005"], "failed"), (1, ["018", "024"], "running"),
                                         (2, ["054"], "submission_unknown")):
                directory = root / "jobs" / f"job-{job_id:02d}"
                directory.mkdir(parents=True)
                (directory / "job.json").write_text(json.dumps({
                    "tasks": tasks, "status": status,
                }))
            self.assertEqual(pool._plan_jobs(root, ["005", "018", "054", "055"], 1), [
                (1, ["018", "024"]), (2, ["054"]), (3, ["005"]), (4, ["055"]),
            ])

    def test_local_pre_submit_failure_can_be_retried_after_fixing_credentials(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            client = Mock()
            with patch.object(settings, "render_spec", side_effect=settings.ConfigError("missing API key")):
                result = self.run_job(root, client)
            self.assertEqual(result["status"], "pre_submit_error")
            client.submit.assert_not_called()
            client.get.assert_not_called()
            client.stop.assert_not_called()
            client.delete.assert_not_called()
            self.assertEqual(pool._plan_jobs(root, ["005"], 1), [(1, ["005"])])
            client.submit.return_value = "created rjob_name: retry-actual"
            client.get.return_value = "rjob retry-actual: Succeeded"
            client.logs.return_value = "log"
            result = pool.run_one(self.config(), run_id="r", job_id=1, tasks=["005"], api_name="m",
                                  api={"base_url": "http://test", "model": "m"},
                                  run_root=root, keep=True, client=client)
            self.assertEqual(result["status"], "succeeded")
            client.submit.assert_called_once()

    def test_cleaned_timeout_is_finished_but_unresolved_timeout_is_observed(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for job_id, cleanup in ((0, {"stopped": True, "deleted": True}), (1, {"kept": True})):
                directory = root / "jobs" / f"job-{job_id:02d}"
                directory.mkdir(parents=True)
                (directory / "job.json").write_text(json.dumps({
                    "tasks": ["005" if job_id == 0 else "018"], "status": "timeout", "cleanup": cleanup,
                }))
            self.assertEqual(pool._plan_jobs(root, ["005", "018"], 1), [(1, ["018"]), (2, ["005"])])

    def test_invalid_cluster_argument_fails_before_submission_marker(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = self.config()
            config["cluster"]["cpu"] = "invalid"
            client = Mock()
            result = pool.run_one(config, run_id="r", job_id=0, tasks=["005"], api_name="m",
                                  api={"base_url": "http://test", "model": "m"},
                                  run_root=root, keep=True, client=client)
            self.assertEqual(result["status"], "pre_submit_error")
            self.assertNotIn("submission_started_at", result)
            client.submit.assert_not_called()
            self.assertEqual(pool._plan_jobs(root, ["005"], 1), [(1, ["005"])])

    def test_cli_selected_tasks_and_failed_retry_use_new_job_directories(self):
        import yaml

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config_path = root / "external-config.yaml"
            config_path.write_text(yaml.safe_dump(self.config()))
            calls = []

            def command(argv):
                calls.append(argv)
                if argv[1] == "submit":
                    spec_path = Path(argv[-1])
                    spec = json.loads(spec_path.read_text())
                    self.assertEqual(len(spec["tasks"]), 1)
                    self.assertEqual(spec["harness"]["max_wall_seconds"], 120)
                    task = spec["tasks"][0]
                    job = f"job-{spec['job_id']:02d}"
                    _episode(spec_path.parent.parent, job, task,
                             status="scored" if task == "005" or spec["job_id"] == 2 else "failed_unscored",
                             score=0.0 if task == "005" else 0.5, finished_at=spec["job_id"] + 1)
                    return 0, f"created rjob_name: actual-{spec['job_id']}"
                if argv[1] == "get":
                    return 0, f"rjob {argv[2]} (showname=display): Succeeded"
                return 0, "log"

            client = rjob.Client(command)
            args = ["--config", str(config_path), "--run-id", "rerun", "--tasks", "005,018",
                    "--jobs", "1", "--per-job", "1", "--max-wall", "120", "--keep", "1"]
            with patch.object(pool, "ROOT", root), patch.object(rjob, "Client", return_value=client):
                self.assertEqual(pool.main(args), 1)
                old = (root / "runs/rerun/jobs/job-01/episodes/018/episode.json").read_bytes()
                self.assertEqual(pool.main([*args, "--retry-failed", "1"]), 0)
            self.assertEqual(sum(argv[1] == "submit" for argv in calls), 3)
            self.assertEqual((root / "runs/rerun/jobs/job-01/episodes/018/episode.json").read_bytes(), old)
            summary = json.loads((root / "runs/rerun/summary.json").read_text())
            self.assertEqual((summary["scored"], summary["failed_unscored"]), (2, 0))
            self.assertFalse((root / "runs/rerun/source-snapshot").exists())

    def test_missing_episode_or_stopped_job_is_not_cli_success(self):
        import yaml

        for status in ("Stopped", "Succeeded"):
            with self.subTest(status=status), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                config_path = root / "config.yaml"
                config_path.write_text(yaml.safe_dump(self.config()))
                client = Mock()
                client.submit.return_value = "created rjob_name: actual"
                client.get.return_value = f"rjob actual (showname=display): {status}"
                client.logs.return_value = "log"
                with patch.object(pool, "ROOT", root), patch.object(rjob, "Client", return_value=client):
                    self.assertEqual(pool.main(["--config", str(config_path), "--run-id", "r",
                                                "--tasks", "005"]), 1)
                client.submit.assert_called_once()


class ToolPairRepairTest(unittest.TestCase):
    """出站前的 tool 回执补齐：位置必须紧跟所属 assistant 轮次。

    真网关已验证：补齐后紧跟轮次 → 200；追加到列表末尾 → 400。
    """

    @staticmethod
    def _shapes(messages):
        out = []
        for message in messages:
            role = message.get("role")
            if role == "assistant":
                out.append(("assistant", tuple(c["id"] for c in message.get("tool_calls") or [])))
            elif role == "tool":
                out.append(("tool", message.get("tool_call_id")))
            else:
                out.append((role, None))
        return out

    def test_missing_result_inserted_after_turn(self):
        messages = [
            {"role": "user", "content": "hi"},
            {"role": "assistant", "tool_calls": [{"id": "a"}, {"id": "b"}]},
            {"role": "tool", "tool_call_id": "a", "content": "ok"},
            {"role": "user", "content": "continue"},
        ]
        fixed, count = task_run._repair_tool_pairs(messages)
        self.assertEqual(count, 1)
        self.assertEqual(self._shapes(fixed), [
            ("user", None), ("assistant", ("a", "b")),
            ("tool", "a"), ("tool", "b"), ("user", None)])

    def test_whole_turn_missing(self):
        messages = [
            {"role": "user", "content": "hi"},
            {"role": "assistant", "tool_calls": [{"id": "x"}, {"id": "y"}]},
            {"role": "user", "content": "continue"},
        ]
        fixed, count = task_run._repair_tool_pairs(messages)
        self.assertEqual(count, 2)
        self.assertEqual(self._shapes(fixed), [
            ("user", None), ("assistant", ("x", "y")),
            ("tool", "x"), ("tool", "y"), ("user", None)])

    def test_misplaced_result_is_moved_back(self):
        # 回执存在但错位（网关按紧邻校验，仍会 400）：必须搬回所属轮次
        messages = [
            {"role": "user", "content": "hi"},
            {"role": "assistant", "tool_calls": [{"id": "a"}, {"id": "b"}]},
            {"role": "tool", "tool_call_id": "a", "content": "ok"},
            {"role": "user", "content": "interrupt"},
            {"role": "tool", "tool_call_id": "b", "content": "late"},
        ]
        fixed, count = task_run._repair_tool_pairs(messages)
        self.assertEqual(count, 1)
        self.assertEqual(self._shapes(fixed), [
            ("user", None), ("assistant", ("a", "b")),
            ("tool", "a"), ("tool", "b"), ("user", None)])
        # 搬走的原件不能留在原地
        self.assertEqual(sum(1 for m in fixed if m.get("tool_call_id") == "b"), 1)

    def test_complete_history_untouched(self):
        messages = [{"role": "assistant", "tool_calls": [{"id": "z"}]},
                    {"role": "tool", "tool_call_id": "z"}]
        fixed, count = task_run._repair_tool_pairs(messages)
        self.assertEqual(count, 0)
        self.assertIs(fixed, messages)

    def test_idempotent(self):
        messages = [{"role": "assistant", "tool_calls": [{"id": "q"}]},
                    {"role": "user", "content": "next"}]
        fixed, _ = task_run._repair_tool_pairs(messages)
        _, count = task_run._repair_tool_pairs(fixed)
        self.assertEqual(count, 0)


class EvaluationReasonTest(unittest.TestCase):
    def test_replay_failure_surfaces(self):
        raw = {"score": 0.0, "failure_reason": "replay_failed",
               "replay_validation": {"passed": False, "reason": "melt_not_found"}}
        self.assertEqual(recorder.evaluation_failure_reason(raw), "replay_failed")

    def test_nested_reason_fallback(self):
        raw = {"score": 0.0, "replay_validation": {"reason": "missing_mlt"}}
        self.assertEqual(recorder.evaluation_failure_reason(raw), "replay:missing_mlt")

    def test_plain_score_has_no_reason(self):
        self.assertEqual(recorder.evaluation_failure_reason(0.5), "")

    def test_successful_replay_does_not_become_a_failure_label(self):
        raw = {"score": 0.15, "replay_validation": {"passed": True, "reason": "ok"}}
        self.assertEqual(recorder.evaluation_failure_reason(raw), "")

    def test_explicit_evaluator_failure_still_takes_precedence(self):
        raw = {"failure_reason": "evaluation_data_missing",
               "replay_validation": {"passed": True, "reason": "ok"}}
        self.assertEqual(recorder.evaluation_failure_reason(raw), "evaluation_data_missing")


if __name__ == "__main__":
    unittest.main()
