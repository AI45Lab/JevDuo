"""Token accounting across SDK retries, Jev HTTP, absent cache fields and interruption."""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from northcua.agentinfra.providers.token_usage import ModelUsageLedger, normalize_usage

SCRIPT = Path(__file__).resolve().parents[1] / "scripts/summarize_model_usage.py"
SPEC = importlib.util.spec_from_file_location("usage_export", SCRIPT)
export = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(export)


class ModelUsageTest(unittest.TestCase):
    def test_native_jev_usage_preserves_counts_and_unknown_cache(self):
        usage = normalize_usage({"input_tokens": 481, "output_tokens": 53})
        self.assertEqual(usage["input_tokens"], 481)
        self.assertEqual(usage["output_tokens"], 53)
        self.assertIsNone(usage["cache_input_tokens"])
        self.assertIsNone(usage["total_tokens"])
        self.assertIsNone(normalize_usage({"input_tokens": True})["input_tokens"])
        self.assertIsNone(normalize_usage({"output_tokens": -1})["output_tokens"])

    def test_export_keeps_user_simulator_tokens_outside_actor_totals(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for name, agent, amount in (("model_usage.jsonl", "slow", 10),
                                        ("user_simulator_usage.jsonl", "user_simulator", 20)):
                ledger = ModelUsageLedger(root / 'episodes/024' / name,
                                          {"agent_id": agent, "purpose": "test"})
                call = ledger.start("scripted")
                ledger.response(call, {"usage": {"prompt_tokens": amount, "completion_tokens": 1}})
            subprocess.run([sys.executable, str(SCRIPT), str(root)], check=True, capture_output=True)
            report = json.loads((root/'model-usage/summary.json').read_text())
            self.assertEqual(report['overall']['calls'], 1)
            self.assertEqual(report['overall']['tokens']['input_tokens']['reported_sum'], 10)
            self.assertEqual(report['external_services'][0]['tokens']['input_tokens']['reported_sum'], 20)
            calls = [json.loads(line) for line in (root/'model-usage/calls.jsonl').read_text().splitlines()]
            self.assertEqual([r['scope'] for r in calls], ['actor', 'external_service'])

    def test_usage_keeps_missing_fields_distinct_from_zero_and_reasoning_is_subset(self):
        usage = normalize_usage({"prompt_tokens": 100, "completion_tokens": 30, "total_tokens": 130,
                                 "prompt_tokens_details": {"cached_tokens": 80},
                                 "completion_tokens_details": {"reasoning_tokens": 20}})
        self.assertEqual(usage["input_tokens"], 100)
        self.assertEqual(usage["cache_input_tokens"], 80)
        self.assertEqual(usage["uncached_input_tokens"], 20)
        self.assertEqual(usage["total_tokens"], 130)
        self.assertEqual(usage["output_tokens"], 30)
        self.assertEqual(usage["reasoning_tokens"], 20)
        self.assertIsNone(normalize_usage({"prompt_tokens": 100})["cache_input_tokens"])
        self.assertEqual(normalize_usage({"prompt_cache_hit_tokens": 0})["cache_input_tokens"], 0)
        self.assertEqual(normalize_usage({"prompt_cache_hit_tokens": 7})["cache_input_tokens"], 7)
        self.assertIsNone(normalize_usage({"prompt_tokens": True})["input_tokens"])
        conflict = normalize_usage({"prompt_tokens": 10, "prompt_cache_hit_tokens": 9,
                                    "prompt_tokens_details": {"cached_tokens": 3}})
        self.assertIsNone(conflict["cache_input_tokens"])
        self.assertTrue(conflict["cache_fields_conflict"])

    def test_summary_counts_attempts_once_without_turning_unknown_usage_into_zero(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = root / "jobs/job-00/episodes/002/agent/session/model_usage.jsonl"
            ledger = ModelUsageLedger(path, {"agent_id": "slow", "purpose": "routing"})
            first = ledger.start("k3.1-pro", attempt=1)
            ledger.response(first, {"usage": {"prompt_tokens": 100, "completion_tokens": 30,
                                              "prompt_tokens_details": {"cached_tokens": 80}}})
            ledger.failure(first, ValueError("invalid JSON is still a paid response"))
            second = ledger.start("k3.1-pro", attempt=2)
            ledger.failure(second, TimeoutError())
            ledger.start("k3.1-pro", attempt=3)  # Killed while waiting; preserve unknown usage.
            calls, warnings = export.collect(root)
            self.assertEqual(warnings, [])
            self.assertEqual(len(calls), 3)
            self.assertTrue(all(call["task_id"] == "002" for call in calls))
            result = export.aggregate(calls)
            self.assertEqual(result["calls"], 3)
            self.assertEqual(result["tokens"]["input_tokens"], {
                "reported_sum": 100, "known_calls": 1, "missing_calls": 2, "complete": False})
            self.assertEqual(result["tokens"]["cache_input_tokens"]["reported_sum"], 80)
            self.assertEqual(result["statuses"]["pending_or_interrupted"], 1)
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            with path.open("ab") as stream:
                stream.write(b'{"torn":')
            recovered, warnings = export.collect(root)
            self.assertEqual(len(recovered), 3)
            self.assertEqual(len(warnings), 1)
            with self.assertRaisesRegex(RuntimeError, "Truncated"):
                ledger.start("cannot-append")
            self.assertEqual(export.summarize(calls, ("agent_id",))[0]["agent_id"], "slow")

    def test_no_tokens_are_inferred_from_http_error_or_usage_less_response(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            ledger = ModelUsageLedger(root / "model_usage.jsonl",
                                      {"agent_id": "gui_jev", "purpose": "selection"})
            for body in ({"error": {"code": "busy"}}, {"choices": []}):
                call = ledger.start("k3.1-pro")
                ledger.response(call, body, http_status=429)
            calls, _ = export.collect(root)
            result = export.aggregate(calls)
            self.assertIsNone(result["tokens"]["cache_input_tokens"]["reported_sum"])
            self.assertEqual(result["tokens"]["cache_input_tokens"]["missing_calls"], 2)
            self.assertEqual(json.loads((root / "model_usage.jsonl").read_text().splitlines()[0])["model"],
                             "k3.1-pro")
