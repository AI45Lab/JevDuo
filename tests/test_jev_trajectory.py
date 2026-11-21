"""Durable Jev event files: full data, private credentials, torn tails and no overwrite."""

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from northcua.agents.desktop.subagents.jev_trajectory import JevTrajectory, TrajectoryWriteError


class JevTrajectoryTest(unittest.TestCase):
    def test_full_records_exceed_handoff_limits_and_credentials_remain_private(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "attempt"
            trace = JevTrajectory(path, {"attempt_id": "a"}, secrets=('a"private\\key',))
            for index in range(45):
                trace.record("loop_event", {"data": {"turn": {"step": index + 1,
                             "text": "正文" * 20000, "secret": 'a"private\\key'}}})
            raw = trace.path.read_text()
            records = [json.loads(line) for line in raw.splitlines()]
            self.assertEqual(len(records), 45)
            self.assertEqual(records[-1]["sequence"], 45)
            self.assertEqual(records[-1]["data"]["turn"]["text"], "正文" * 20000)
            self.assertEqual(records[0]["data"]["turn"]["secret"], "[credential-redacted]")
            self.assertEqual(trace.path.stat().st_mode & 0o777, 0o600)
            self.assertEqual(path.stat().st_mode & 0o777, 0o700)
            with self.assertRaises(FileExistsError):
                JevTrajectory(path, {})

    def test_short_credential_redaction_does_not_corrupt_numeric_json(self):
        with tempfile.TemporaryDirectory() as directory:
            trace = JevTrajectory(Path(directory) / "attempt", {}, secrets=("1",))
            trace.record("event", {"number": 1, "text": "value1"})
            record = json.loads(trace.path.read_text())
            self.assertEqual(record["number"], 1)
            self.assertEqual(record["text"], "value[credential-redacted]")

    def test_credentials_are_also_redacted_inside_raw_json_response_text(self):
        with tempfile.TemporaryDirectory() as directory:
            secret = 'private"\\key中文'
            trace = JevTrajectory(Path(directory) / "attempt", {}, secrets=(secret,))
            for ascii_only in (False, True):
                trace.record("model_response", {"response_body": json.dumps({"echo": secret},
                                                                           ensure_ascii=ascii_only)})
            for line in trace.path.read_text().splitlines():
                body = json.loads(json.loads(line)["response_body"])
                self.assertEqual(body["echo"], "[credential-redacted]")

    def test_torn_tail_is_preserved_and_error_retains_native_receipt(self):
        with tempfile.TemporaryDirectory() as directory:
            trace = JevTrajectory(Path(directory) / "attempt", {})
            trace.record("start", {})
            with trace.path.open("ab") as stream:
                stream.write(b'{"incomplete":')
            before = trace.path.read_bytes()
            receipt = {"ok": True, "data": {"disposition": {"delivery": "delivered_verified"}}}
            with self.assertRaises(TrajectoryWriteError) as caught:
                trace.record("native_finished", {"result": receipt})
            self.assertEqual(caught.exception.result, receipt)
            self.assertEqual(trace.path.read_bytes(), before)

    def test_fsync_failure_does_not_trigger_any_hidden_write_retry(self):
        with tempfile.TemporaryDirectory() as directory:
            trace = JevTrajectory(Path(directory) / "attempt", {})
            with patch.object(os, "fsync", side_effect=OSError("disk error")) as sync:
                with self.assertRaises(TrajectoryWriteError):
                    trace.record("start", {})
                with self.assertRaises(TrajectoryWriteError):
                    trace.record("later", {})
            self.assertEqual(sync.call_count, 1)
