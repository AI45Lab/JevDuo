"""Artifact readers share one tolerant policy without changing runtime journals."""

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

import entry
import recorder


class RedactionTest(unittest.TestCase):
    def test_key_and_bearer_bodies_are_removed_with_prefixes_preserved(self):
        body = "syntheticOnly_123-abc.def"
        cases = [
            ("sk-" + body, "sk-[REDACTED]"),
            ("SK-" + body, "SK-[REDACTED]"),
            ("Bearer " + body, "Bearer [REDACTED]"),
            ("bearer\t" + body, "bearer\t[REDACTED]"),
            ("Bearer sk-" + body, "Bearer [REDACTED]"),
        ]
        for value, expected in cases:
            with self.subTest(prefix=value.split(body)[0]):
                self.assertEqual(recorder._redact('failure "' + value + '" next'),
                                 'failure "' + expected + '" next')
        mixed = "sk-" + body + " and Bearer " + body
        self.assertEqual(recorder._redact(mixed), "sk-[REDACTED] and Bearer [REDACTED]")
        self.assertEqual(recorder._redact("密钥sk-" + body), "密钥sk-[REDACTED]")

    def test_plain_messages_idempotence_and_redaction_before_truncation(self):
        plain = "task-run failed; ask-owner; Authorization: Bearer"
        self.assertEqual(recorder._redact(plain), plain)
        key = "sk-" + "syntheticOnly_123456789"
        redacted = recorder._redact(key)
        self.assertEqual(recorder._redact(redacted), redacted)
        self.assertEqual(recorder._redact(key, limit=7), "sk-[RED")
        self.assertEqual(recorder._redact(key, limit=0), "")
        self.assertEqual(recorder._redact("ordinary text", limit=8), "ordinary")

    def test_finalized_exception_message_and_traceback_do_not_retain_tokens(self):
        key = "sk-" + "syntheticOnly_key_123456789"
        token = "syntheticOnly_bearer_123456789"
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            try:
                raise RuntimeError('upstream rejected "' + key + '" and Bearer ' + token)
            except RuntimeError as error:
                recorder.finalize(root, task_id="001", stop_reason="exception", score=None,
                                  status="failed_unscored", steps=0, exception=error)
            raw = (root / "episode.json").read_text()
            self.assertNotIn(key, raw)
            self.assertNotIn(token, raw)
            info = json.loads(raw)["exception"]
            self.assertIn("[REDACTED]", info["message"])
            self.assertIn("[REDACTED]", "".join(info["traceback"]))


class ArtifactReaderTest(unittest.TestCase):
    def test_discovery_prefers_shallow_logs_then_lexical_order(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for name, records in (("z/steps.jsonl", 3), ("a/steps.jsonl", 2), ("a/child/steps.jsonl", 7)):
                path = root / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text('{"session_step":1}\n' * records)
            self.assertEqual(recorder.count_steps(root), 2)
            recorder.write_trajectory(root, root, llm_calls=False)
            self.assertEqual(len((root / "traj.jsonl").read_text().splitlines()), 2)

    def test_explicit_phase_log_is_used_for_recovery_and_trajectory(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            phase = root / "agent/phase-01"
            phase.mkdir(parents=True)
            (phase / "steps.jsonl").write_text('{"session_step":99}\n')
            selected = phase / "session/steps.jsonl"
            selected.parent.mkdir()
            selected.write_text('{"session_step":1}\n{"session_step":2}\n{"torn":')
            local = root / "phases/phase-01"
            local.mkdir(parents=True)
            result = local / "agent_result.json"
            result.write_text(json.dumps({"step_log_path": str(selected)}, indent=2))
            self.assertEqual(entry._steps_from_disk(root), 2)
            recorder.write_trajectory(root, root / "agent", llm_calls=False)
            rows = [json.loads(line) for line in (root / "traj.jsonl").read_text().splitlines()]
            self.assertEqual([row["step"] for row in rows], [1, 2])
            selected.unlink()
            # A missing explicit log must never fall back to the unrelated step 99.
            self.assertEqual(entry._steps_from_disk(root), 0)
            recorder.write_trajectory(root, root / "agent", llm_calls=False)
            self.assertEqual((root / "traj.jsonl").read_text(), "")

    def test_reader_handles_bad_bytes_invalid_records_and_torn_tail(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "steps.jsonl"
            path.write_bytes(b'\nnull\n[]\n123\nnot json\n{"assistant_text":"ok\xff"}\n{"torn":')
            self.assertEqual(list(recorder.read_jsonl(path)), [{"assistant_text": "ok\ufffd"}])
            self.assertEqual(list(recorder.read_jsonl(path.with_name("missing.jsonl"))), [])

    def test_open_and_midstream_errors_preserve_already_read_records(self):
        def interrupted():
            yield '{"session_step":1}\n'
            raise OSError("scripted read failure")

        path = Path("/unused/steps.jsonl")
        with patch.object(Path, "open", side_effect=OSError("unreadable")):
            self.assertEqual(list(recorder.read_jsonl(path)), [])
        stream = MagicMock()
        stream.__enter__.return_value = interrupted()
        with patch.object(Path, "open", return_value=stream):
            self.assertEqual(list(recorder.read_jsonl(path)), [{"session_step": 1}])
        stream.__enter__.return_value = interrupted()
        with patch.object(Path, "open", return_value=stream):
            with self.assertRaises(OSError):
                list(recorder.read_jsonl(path, required=True))
        with self.assertRaises(FileNotFoundError):
            list(recorder.read_jsonl(None, required=True))


class SummaryTest(unittest.TestCase):
    def test_job_and_run_share_counts_but_keep_their_order_and_csv_contracts(self):
        episodes = [
            {"task_id": "002", "status": "scored", "score": 0.0},
            {"task_id": "001", "status": "failed_unscored", "score": None},
            {"task_id": "003", "status": "scored", "score": 1.0},
        ]
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            recorder.write_job_summary(root / "job-00", episodes)
            job = json.loads((root / "job-00/summary.json").read_text())
            run = recorder.merge_episodes(root, episodes)
            for key, expected in (("total", 3), ("scored", 2), ("failed_unscored", 1), ("score_mean", 0.5)):
                self.assertEqual(job[key], expected)
                self.assertEqual(run[key], expected)
            self.assertEqual(job["job_id"], "job-00")
            self.assertEqual([item["task_id"] for item in job["episodes"]], ["002", "001", "003"])
            self.assertEqual([item["task_id"] for item in run["episodes"]], ["001", "002", "003"])
            csv = (root / "summary.csv").read_text()
            self.assertIn("001,failed_unscored,,", csv)
            self.assertIn("002,scored,0.0,", csv)
        for items in ([], [episodes[1]]):
            with self.subTest(items=items):
                self.assertIsNone(recorder.episode_summary(items)["score_mean"])
