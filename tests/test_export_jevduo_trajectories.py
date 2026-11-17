"""JevDuo archive protocol checks: phase order, trace joins, and evidence boundaries."""

import base64
import contextlib
import importlib.util
import io
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from urllib.parse import unquote

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "export_jevduo_trajectories.py"
spec = importlib.util.spec_from_file_location("export_jevduo_trajectories", SCRIPT)
exporter = importlib.util.module_from_spec(spec)
spec.loader.exec_module(exporter)
archived_path, collect, render = exporter.archived_path, exporter.collect, exporter.render


class JevDuoExportTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.ep = self.root / "episodes" / "069"
        self.agent = self.ep / "agent" / "phase-01" / "renamed-session"
        self.agent.mkdir(parents=True)
        self.image = self.agent / "query_0001" / "observations" / "screen #1.png"
        self.image.parent.mkdir(parents=True)
        self.image.write_bytes(
            base64.b64decode(
                "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aX1sAAAAASUVORK5CYII="
            )
        )
        self.old = (
            "/old/run/episodes/069/agent/phase-01/renamed-session/query_0001/observations/screen #1.png"
        )
        self.write(
            self.ep / "episode.json",
            {
                "task_id": "069",
                "steps": 2,
                "score": 0.8,
                "status": "scored",
                "stop_reason": "phase_gate",
                "seconds": 9,
            },
        )
        self.write(self.ep / "evaluation.json", {"score": 0})
        self.write(
            self.ep / "task.json",
            {"instruction": "inspect </script><script>window.bad=true</script> & <title>"},
        )
        self.contract = {
            "agent_id": "general",
            "subgoal_id": "same-subgoal",
            "goal": "Inspect file",
            "success_criteria": ["read"],
            "constraints": ["preserve"],
        }
        self.cycle1 = self.cycle("c1", "a1", 1, 1)
        self.cycle2 = self.cycle("c2", "a2", 2, 1)
        self.rows(self.ep / "traj.jsonl", [self.cycle1, self.cycle2])
        self.rows(
            self.agent / "steps.jsonl",
            [{**self.cycle1, "session_step": 1, "llm_thinking": "extra reasoning"}],
        )
        self.trace = self.agent / "query_0001" / "orchestration.jsonl"
        self.events = [
            self.dispatch("c1", "a1"),
            self.start("a1", "run_command", {"command": "read"}),
            self.finish("a1", "run_command", {"ok": True, "stdout": "read result"}),
            {"event": "dispatch_finished", "cycle_id": "c1"},
            self.dispatch("c2", "a2"),
            self.start("a2", "agent_desktop", {"redacted": True}),
            self.finish("a2", "agent_desktop", {"redacted": True}),
        ]
        self.rows(self.trace, self.events)

    def write(self, path, obj):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(obj), encoding="utf-8")

    def rows(self, path, rows):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")

    def cycle(self, cid, aid, phase, number):
        return {
            "step": number,
            "phase_index": phase,
            "status": "continue",
            "screenshot": self.old,
            "orchestration": {
                "cycle_id": cid,
                "decision": {"contract": self.contract, "verification": "verification " + cid},
                "subagent_result": {
                    "attempt_id": aid,
                    "outcome": "achieved",
                    "summary": '{"message":"reported result"}',
                    "last_observation": {"screenshot_path": self.old},
                },
            },
        }

    def dispatch(self, cid, aid):
        return {
            "event": "dispatch_started",
            "cycle_id": cid,
            "state": {
                "pending": {
                    "attempt_id": aid,
                    "contract": self.contract,
                    "observation": {"screenshot_path": self.old},
                }
            },
        }

    def start(self, aid, tool, args):
        return {"event": "tool_started", "attempt_id": aid, "tool_name": tool, "arguments": args}

    def finish(self, aid, tool, result):
        return {"event": "tool_finished", "attempt_id": aid, "tool_name": tool, "result": result}

    def load(self, **kwargs):
        with contextlib.redirect_stdout(io.StringIO()):
            return collect(self.root, self.root / "viewer.html", **kwargs)

    def test_phase_order_exact_cycle_join_and_full_step_enrichment(self):
        data = self.load()
        t = data["tasks"][0]
        self.assertEqual(t["cycle_count"], 2)
        self.assertEqual([s["phase"] for s in t["entries"]], [1, 2])
        self.assertEqual([s["number"] for s in t["entries"]], [1, 1])
        self.assertEqual(t["entries"][0]["reasoning"], "extra reasoning")
        self.assertEqual(t["entries"][1]["attempts"][0]["actions"][0]["name"], "agent_desktop")
        self.assertNotIn("next_verification", t["entries"][0])
        self.assertEqual(t["entries"][0]["summary"], {"message": "reported result"})
        self.assertEqual(t["tool_count"], 2)
        self.assertEqual(t["redacted_count"], 1)
        self.assertEqual(t["unmatched_start_count"], 0)

    def test_next_parent_verification_stays_in_phase(self):
        self.cycle2["phase_index"] = 1
        self.rows(self.ep / "traj.jsonl", [self.cycle1, self.cycle2])
        self.assertEqual(self.load()["tasks"][0]["entries"][0]["next_verification"], "verification c2")

    def test_conflicting_attempt_cycle_ids_do_not_override_cycle_identity(self):
        self.rows(self.trace, [self.dispatch("unknown-cycle", "a1"), self.start("a1", "x", {})])
        t = self.load()["tasks"][0]
        self.assertEqual(t["entries"][0]["attempts"], [])
        self.assertEqual(t["entries"][-1]["kind"], "trace_only")
        self.assertTrue(any("冲突" in w for w in t["warnings"]))

    def test_duplicate_cycle_ids_do_not_consume_tools_into_first_cycle(self):
        self.rows(self.ep / "traj.jsonl", [self.cycle1, self.cycle1])
        t = self.load()["tasks"][0]
        self.assertEqual(t["entries"][0]["attempts"], [])
        self.assertEqual(t["entries"][1]["attempts"], [])
        self.assertEqual(sum(s["kind"] == "trace_only" for s in t["entries"]), 2)

    def test_unpersisted_attempt_and_unfinished_tool_are_preserved(self):
        self.rows(
            self.trace,
            self.events + [self.dispatch("not-persisted", "a3"), self.start("a3", "run_command", None)],
        )
        t = self.load()["tasks"][0]
        self.assertEqual(t["cycle_count"], 2)
        self.assertEqual(len(t["entries"]), 3)
        self.assertEqual(t["entries"][-1]["kind"], "trace_only")
        self.assertEqual(t["unmatched_start_count"], 1)
        self.assertEqual(t["tool_count"], 3)

    def test_interleaved_attempts_and_null_arguments_pair_correctly(self):
        self.rows(
            self.trace,
            [
                self.dispatch("c1", "a1"),
                self.start("a1", "x", None),
                self.dispatch("c2", "a2"),
                self.start("a2", "x", {}),
                self.finish("a2", "x", "second"),
                self.finish("a1", "x", "first"),
            ],
        )
        t = self.load()["tasks"][0]
        self.assertEqual(t["entries"][0]["attempts"][0]["actions"][0]["result"], "first")
        self.assertEqual(t["unmatched_start_count"], 0)

    def test_ambiguous_concurrent_calls_do_not_get_guessed_results(self):
        self.rows(
            self.trace,
            [
                self.dispatch("c1", "a1"),
                self.start("a1", "x", {"n": 1}),
                self.start("a1", "x", {"n": 2}),
                self.finish("a1", "x", "ambiguous"),
            ],
        )
        t = self.load()["tasks"][0]
        self.assertEqual(t["tool_count"], 2)
        self.assertEqual(t["unmatched_start_count"], 2)
        self.assertEqual(t["unmatched_result_count"], 1)
        self.assertIsNone(t["entries"][0]["attempts"][0]["actions"][0]["result"])

    def test_moved_images_deduplicate_and_embed_with_safe_html(self):
        data = self.load()
        self.assertEqual(len(data["images"]), 1)
        self.assertEqual((self.root / unquote(data["images"][0]["src"])).resolve(), self.image)
        data = self.load(embed=True)
        self.assertTrue(data["images"][0]["src"].startswith("data:image/png;base64,"))
        document = render(data)
        self.assertNotIn("</script><script>window.bad", document)
        payload = document.split('<script id="trajectory-data" type="application/json">')[1].split(
            "</script>"
        )[0]
        self.assertEqual(json.loads(payload), data)

    def test_serial_calls_after_ambiguous_group_pair_independently(self):
        self.rows(
            self.trace,
            [
                self.dispatch("c1", "a1"),
                self.start("a1", "x", {"n": 1}),
                self.start("a1", "x", {"n": 2}),
                self.finish("a1", "x", "unknown1"),
                self.finish("a1", "x", "unknown2"),
                self.start("a1", "x", {"n": 3}),
                self.finish("a1", "x", "third"),
            ],
        )
        t = self.load()["tasks"][0]
        actions = t["entries"][0]["attempts"][0]["actions"]
        self.assertEqual(t["tool_count"], 3)
        self.assertEqual(t["unmatched_start_count"], 2)
        self.assertEqual(t["unmatched_result_count"], 2)
        self.assertEqual(actions[-1]["result"], "third")
        self.assertEqual(actions[-1]["status"], "finished")

    def test_tool_failed_closes_call_without_poisoning_later_pairs(self):
        self.rows(
            self.trace,
            [
                self.dispatch("c1", "a1"),
                self.start("a1", "x", {"n": 1}),
                {
                    "event": "tool_failed",
                    "attempt_id": "a1",
                    "tool_name": "x",
                    "error_type": "TimeoutError",
                    "elapsed_seconds": 2,
                },
                self.start("a1", "x", {"n": 2}),
                self.finish("a1", "x", "second"),
            ],
        )
        t = self.load()["tasks"][0]
        actions = t["entries"][0]["attempts"][0]["actions"]
        self.assertEqual([a["status"] for a in actions], ["failed", "finished"])
        self.assertEqual(actions[0]["result"]["error_type"], "TimeoutError")
        self.assertEqual(actions[1]["result"], "second")
        self.assertEqual(t["failed_tool_count"], 1)
        self.assertEqual(t["unmatched_start_count"], 0)

    def model_record(self, aid, observations):
        return {
            "agent_path": "subagents/general/" + aid,
            "llm_trace": {
                "request": {
                    "messages": [
                        {
                            "role": "user",
                            "content": [
                                {"type": "text", "text": json.dumps({"observation": o})},
                                {
                                    "type": "image_url",
                                    "image_url": {"url": "data:image/png;base64,DO_NOT_COPY_HISTORY"},
                                },
                            ],
                        }
                        for o in observations
                    ]
                },
                "assistant_text": "model proposal",
                "assistant_tool_calls": [
                    {"id": "proposal", "name": "run_command", "arguments": {"command": "inspect"}}
                ],
            },
        }

    def test_general_intermediate_frames_use_latest_explicit_observation_per_inference(self):
        second = self.image.with_name("second.png")
        second.write_bytes(self.image.read_bytes())
        first_obs = {"observation_id": "obs1", "screenshot_path": self.old}
        second_obs = {"observation_id": "obs2", "screenshot_path": str(second)}
        self.rows(
            self.agent / "llm_trace.jsonl",
            [
                self.model_record("a1", [first_obs]),
                self.model_record("a1", [first_obs, second_obs]),
                self.model_record("a2", [second_obs]),
            ],
        )
        data = self.load()
        t = data["tasks"][0]
        self.assertEqual(t["model_observation_count"], 3)
        contexts = [f["context"] for f in t["entries"][0]["frames"] if "context" in f]
        self.assertEqual([c["observation_id"] for c in contexts], ["obs1", "obs2"])
        self.assertTrue(contexts[1]["source"].endswith(":2"))
        self.assertEqual(contexts[0]["model_tool_proposals"][0]["id"], "proposal")
        self.assertEqual(len([f for f in t["entries"][1]["frames"] if "context" in f]), 1)
        self.assertNotIn("DO_NOT_COPY_HISTORY", render(data))
        self.assertEqual(len(data["images"]), 2)

    def test_model_observation_without_journal_joins_only_exact_result_attempt(self):
        self.trace.unlink()
        self.rows(
            self.agent / "llm_trace.jsonl",
            [self.model_record("a1", [{"observation_id": "obs1", "screenshot_path": self.old}])],
        )
        t = self.load()["tasks"][0]
        self.assertEqual(t["model_observation_count"], 1)
        self.assertEqual(t["entries"][0]["attempts"][0]["id"], "a1")
        self.assertEqual(t["tool_count"], 0)
        self.assertTrue(t["entries"][0]["attempts"][0]["warnings"])

    def test_skip_model_observations_and_unknown_attempt_do_not_guess(self):
        self.rows(
            self.agent / "llm_trace.jsonl",
            [self.model_record("unknown", [{"observation_id": "obs1", "screenshot_path": self.old}])],
        )
        t = self.load()["tasks"][0]
        self.assertEqual(t["model_observation_count"], 0)
        self.assertTrue(any("未唯一关联" in w for w in t["warnings"]))
        t = self.load(model_observations=False)["tasks"][0]
        self.assertTrue(any("跳过 llm_trace" in w for w in t["warnings"]))

    def test_jev_retained_turns_preserve_original_steps_and_truncation_limits(self):
        self.cycle1["orchestration"]["decision"]["contract"] = {"agent_id": "gui_jev"}
        turn = {
            "step": 9,
            "operation": "TYPE_TEXT",
            "target": "Search mail",
            "argv": ["set-value", "@ref", "[redacted]"],
            "ok": True,
        }
        self.cycle1["orchestration"]["subagent_result"]["summary"] = json.dumps(
            {"turns": [turn], "context_truncated": True}
        )
        self.cycle2["orchestration"]["decision"]["contract"] = {"agent_id": "gui_jev"}
        self.cycle2["orchestration"]["subagent_result"]["summary"] = json.dumps({"evidence_truncated": True})
        self.rows(self.ep / "traj.jsonl", [self.cycle1, self.cycle2])
        t = self.load()["tasks"][0]
        self.assertEqual(t["jev_turn_count"], 1)
        self.assertEqual(t["entries"][0]["jev"]["turns"], [turn])
        self.assertIn("context_truncated=true", " ".join(t["entries"][0]["jev"]["notes"]))
        self.assertEqual(t["entries"][1]["jev"]["turns"], [])
        self.assertIn("evidence_truncated=true", " ".join(t["entries"][1]["jev"]["notes"]))

    def test_missing_images_and_symlinks_cannot_escape_episode(self):
        outside = self.root / "outside.png"
        outside.write_bytes(self.image.read_bytes())
        (self.ep / "escape.png").symlink_to(outside)
        self.assertIsNone(archived_path(self.ep, "escape.png"))
        self.assertIsNone(archived_path(self.ep, "/old/episodes/069/../../outside.png"))
        self.image.unlink()
        self.assertTrue(self.load()["images"][0]["missing"])

    def test_official_zero_missing_score_and_fallback_provenance(self):
        t = self.load()["tasks"][0]
        self.assertEqual(t["score"], 0)
        self.assertEqual(t["score_source"], "evaluation.json:score")
        (self.ep / "evaluation.json").unlink()
        self.assertEqual(self.load()["tasks"][0]["score_source"], "episode.json:score")
        self.write(self.ep / "episode.json", {"task_id": "069"})
        self.assertIsNone(self.load()["tasks"][0]["score"])

    def test_missing_trace_and_missing_trajectory_keep_scored_task(self):
        self.trace.unlink()
        self.assertIn("trace", " ".join(self.load()["tasks"][0]["entries"][0]["warnings"]))
        (self.ep / "traj.jsonl").unlink()
        (self.agent / "steps.jsonl").unlink()
        t = self.load()["tasks"][0]
        self.assertEqual(t["score"], 0)
        self.assertEqual(t["entries"], [])

    def test_full_steps_fallback_and_task_selection(self):
        (self.ep / "traj.jsonl").unlink()
        t = self.load(task_ids=["069"])["tasks"][0]
        self.assertEqual(t["entries"][0]["cycle_id"], "c1")
        with self.assertRaisesRegex(ValueError, "Unknown task IDs"):
            self.load(task_ids=["069", "999"])

    def test_cli_requires_force_before_overwriting(self):
        output = self.root / "precious.html"
        output.write_text("keep", encoding="utf-8")
        script = Path(__file__).resolve().parents[1] / "scripts" / "export_jevduo_trajectories.py"
        run = subprocess.run(
            [sys.executable, str(script), str(self.root), "-o", str(output)], capture_output=True, text=True
        )
        self.assertNotEqual(run.returncode, 0)
        self.assertIn("Output exists", run.stderr)
        self.assertEqual(output.read_text(), "keep")

    def test_native_run_and_job_roots_keep_archived_evidence(self):
        job = self.root / "jobs" / "job-00"
        job.mkdir(parents=True)
        shutil.move(str(self.ep.parent), job / "episodes")
        data = self.load(task_ids=["069"])
        task = data["tasks"][0]
        self.assertEqual(task["source"], "jobs/job-00/episodes/069")
        self.assertEqual(task["score"], 0)
        self.assertEqual(task["cycle_count"], 2)
        self.assertEqual(task["tool_count"], 2)
        self.assertFalse(data["images"][0]["missing"])
        self.assertEqual(task["entries"][1]["attempts"][0]["id"], "a2")
        with contextlib.redirect_stdout(io.StringIO()):
            job_data = collect(job, job / "viewer.html", embed=True)
        self.assertEqual(job_data["tasks"][0]["source"], "episodes/069")
        self.assertTrue(job_data["images"][0]["src"].startswith("data:image/png;base64,"))

    def test_repeated_tasks_keep_separate_sources_and_scores(self):
        for name, score in [("job-00", 0.5), ("job-01", None)]:
            episode = self.root / "jobs" / name / "episodes" / "069"
            self.write(
                episode / "episode.json",
                {
                    "task_id": "069",
                    "score": score,
                    "status": "scored" if score is not None else "failed_unscored",
                },
            )
            self.rows(episode / "traj.jsonl", [self.cycle(name, "same-attempt", 1, 1)])
        data = self.load(task_ids=["069"])
        tasks = data["tasks"]
        self.assertEqual([t["score"] for t in tasks], [0, 0.5, None])
        self.assertEqual(
            [t["source"] for t in tasks],
            [
                "episodes/069",
                "jobs/job-00/episodes/069",
                "jobs/job-01/episodes/069",
            ],
        )
        self.assertEqual([t["cycle_count"] for t in tasks], [2, 1, 1])
        self.assertEqual([t["tool_count"] for t in tasks], [2, 0, 0])
        self.assertTrue(all(any("多份执行记录" in w for w in t["warnings"]) for t in tasks))

    def test_cli_requires_input_and_exports_native_run_from_another_directory(self):
        missing = subprocess.run([sys.executable, str(SCRIPT)], capture_output=True, text=True)
        self.assertNotEqual(missing.returncode, 0)
        self.assertIn("root", missing.stderr)
        job = self.root / "jobs" / "job-00"
        job.mkdir(parents=True)
        shutil.move(str(self.ep.parent), job / "episodes")
        output = self.root / "export" / "standalone.html"
        run = subprocess.run(
            [
                sys.executable,
                str(SCRIPT),
                str(self.root),
                "--task",
                "069",
                "--embed-images",
                "--skip-model-observations",
                "-o",
                str(output),
            ],
            cwd=self.root,
            capture_output=True,
            text=True,
        )
        self.assertEqual(run.returncode, 0, run.stderr)
        document = output.read_text(encoding="utf-8")
        payload = document.split('<script id="trajectory-data" type="application/json">')[1].split(
            "</script>"
        )[0]
        data = json.loads(payload)
        self.assertEqual(data["tasks"][0]["source"], "jobs/job-00/episodes/069")
        self.assertTrue(data["images"][0]["src"].startswith("data:image/png;base64,"))
        self.assertNotIn("__TRAJECTORY_DATA__", document)
        self.assertFalse((self.root / "trajectories.html").exists())


if __name__ == "__main__":
    unittest.main()
