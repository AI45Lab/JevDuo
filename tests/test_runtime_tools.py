"""Tool attempt journals preserve failure and cancellation without payload leakage."""

import asyncio
import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import AsyncMock

from northcua.agentkit.orchestration.contracts import OrchestrationState, SubagentRequest, SubgoalContract
from northcua.agentkit.orchestration.runtime import Journal, RunBudget, ScopedRuntime
from northcua.core.config_base import OrchestrationConfig


class RuntimeToolTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.journal = Journal(Path(self.tmp.name) / "journal.jsonl")
        state = OrchestrationState(round_num=1, started_at=time.time())
        self.budget = RunBudget(OrchestrationConfig(max_seconds=30), state, self.journal)

    def runtime(self, execute, seconds=5):
        request = SubagentRequest(
            session_id="s",
            round_num=1,
            attempt_id="a",
            task="isolated test",
            contract=SubgoalContract(
                subgoal_id="one",
                agent_id="fixture",
                goal="one operation",
                success_criteria=["operation completed"],
                allowed_tools=["fixture"],
                budget={"max_model_calls": 1, "max_tool_calls": 1, "max_seconds": seconds},
            ),
            observation={"observation_id": "o1"},
        )
        runtime = ScopedRuntime(request, self.budget, AsyncMock(), execute, AsyncMock())
        self.addAsyncCleanup(runtime.close)
        return runtime

    def records(self):
        return [json.loads(line) for line in self.journal.path.read_text().splitlines()]

    def assert_failed(self, error_type):
        records = self.records()
        self.assertEqual([r["event"] for r in records], ["call_started", "tool_started", "tool_failed"])
        failure = records[-1]
        self.assertEqual(failure["error_type"], error_type)
        self.assertEqual(failure["attempt_id"], "a")
        self.assertEqual(failure["tool_name"], "fixture")
        self.assertGreaterEqual(failure["elapsed_seconds"], 0)
        self.assertEqual(self.budget.state.usage.tool_calls, 1)
        self.assertNotIn("private", self.journal.path.read_text())

    async def test_executor_exception_is_paired_with_a_failed_record_and_reraised(self):
        execute = AsyncMock(side_effect=OSError("private response"))
        runtime = self.runtime(execute)
        with self.assertRaises(OSError):
            await runtime.execute("fixture", {"value": "private input"}, sensitive=True)
        self.assert_failed("OSError")
        execute.assert_awaited_once()

    async def test_cancellation_records_failure_without_replaying_the_operation(self):
        entered = asyncio.Event()
        calls = []

        async def execute(*args):
            calls.append(args)
            entered.set()
            await asyncio.Event().wait()

        runtime = self.runtime(execute)
        task = asyncio.create_task(runtime.execute("fixture", {"value": "private input"}, sensitive=True))
        await asyncio.wait_for(entered.wait(), 2)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assert_failed("CancelledError")
        self.assertEqual(len(calls), 1)

    async def test_parent_deadline_still_bounds_a_long_running_tool(self):
        calls = []

        async def execute(*args):
            calls.append(args)
            await asyncio.Event().wait()

        runtime = self.runtime(execute, seconds=0.5)
        with self.assertRaises(TimeoutError):
            await asyncio.wait_for(runtime.execute("fixture", {}, sensitive=True), 2)
        self.assert_failed("TimeoutError")
        self.assertEqual(len(calls), 1)

    async def test_success_stays_redacted_and_records_its_duration(self):
        runtime = self.runtime(AsyncMock(return_value={"value": "private output"}))
        result = await runtime.execute("fixture", {"value": "private input"}, sensitive=True)
        self.assertEqual(result, {"value": "private output"})
        record = self.records()[-1]
        self.assertEqual(record["event"], "tool_finished")
        self.assertEqual(record["result"], {"redacted": True})
        self.assertEqual(record["tool_name"], "fixture")
        self.assertGreaterEqual(record["elapsed_seconds"], 0)
        self.assertNotIn("private", self.journal.path.read_text())
