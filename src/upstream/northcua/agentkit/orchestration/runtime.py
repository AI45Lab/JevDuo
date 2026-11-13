"""Budgeted handles and an append-only recovery journal, without another agent loop."""

from __future__ import annotations

import asyncio
import json
import os
import time
from collections.abc import AsyncIterator, Awaitable, Callable, Iterator
from contextlib import asynccontextmanager
from pathlib import Path

from nexau.core.messages import Message

from northcua.agentkernel.context.strategies import ContextBudgetExceededError
from northcua.core.config_base import OrchestrationConfig
from northcua.core.json_types import JsonObject

from .contracts import OrchestrationState, SubagentRequest, SubgoalBudget, Usage


class BudgetExceeded(ContextBudgetExceededError):
    pass


def read_records(path: Path) -> Iterator[JsonObject]:
    """Fail closed on incomplete records; never append new work onto a torn log."""
    if not path.exists():
        return
    with path.open(encoding="utf-8") as stream:
        for line in stream:
            if not line.endswith("\n"):
                raise RuntimeError(f"Incomplete JSONL record in {path}; preserve evidence and start a new session")
            if not line.strip():
                continue
            yield json.loads(line)


class Journal:
    def __init__(self, path: Path) -> None:
        self.path = path

    def write(self, event: str, state: OrchestrationState, **payload: object) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # Never append onto a torn record or silently discard existing evidence.
        with self.path.open("ab+") as stream:
            stream.seek(0, os.SEEK_END)
            if stream.tell():
                stream.seek(-1, os.SEEK_END)
                if stream.read(1) != b"\n":
                    raise RuntimeError("Truncated orchestration journal; preserve it and start a new session")
            record = {"event": event, "state": state.model_dump(mode="json"), **payload}
            stream.write((json.dumps(record, ensure_ascii=False) + "\n").encode())
            stream.flush()
            os.fsync(stream.fileno())


class RunBudget:
    def __init__(self, config: OrchestrationConfig, state: OrchestrationState, journal: Journal) -> None:
        self.config, self.state, self.journal = config, state, journal

    def seconds_left(self) -> float:
        return self.config.max_seconds - (time.time() - self.state.started_at)

    def check(self) -> None:
        if self.seconds_left() <= 0:
            raise BudgetExceeded("wall_time_limit")

    def charge(self, kind: str, local: Usage | None = None, limit: int | None = None, *, reserve: int = 0) -> None:
        self.check()
        if getattr(self.state.usage, kind) >= getattr(self.config, f"max_{kind}") - reserve:
            raise BudgetExceeded(f"global_{kind}_limit")
        if local is not None and limit is not None and getattr(local, kind) >= limit:
            raise BudgetExceeded(f"subgoal_{kind}_limit")
        setattr(self.state.usage, kind, getattr(self.state.usage, kind) + 1)
        if local is not None:
            setattr(local, kind, getattr(local, kind) + 1)
        # Persist attempted usage before the effect, including failed calls.
        self.journal.write("call_started", self.state, kind=kind)
        self.check()


class ScopedRuntime:
    """Bound to one attempt and the parent's environment; owns no session counters."""

    def __init__(
        self,
        request: SubagentRequest,
        budget: RunBudget,
        observe: Callable[[], Awaitable[JsonObject]],
        execute: Callable[[str, JsonObject], Awaitable[JsonObject]],
        infer: Callable[[str, Callable[[], None]], Awaitable[str]],
        cleanup: Callable[[str, JsonObject], Awaitable[JsonObject]] | None = None,
        *,
        reserve_model_calls: int = 0,
        infer_tools: Callable[[list[Message], list[JsonObject], Callable[[], None]], Awaitable[Message]] | None = None,
    ) -> None:
        self.request, self.budget = request, budget
        self._observe, self._execute, self._infer = observe, execute, infer
        self._cleanup = cleanup
        self._infer_tools = infer_tools
        self.reserve_model_calls = reserve_model_calls
        self.usage = Usage()
        self.last_observation: JsonObject = {}
        self.deadline = time.monotonic() + min(request.contract.budget.max_seconds, budget.seconds_left())
        self.closed = False
        self._lock = asyncio.Lock()
        self._tasks: set[asyncio.Task] = set()

    def remaining_budget(self) -> SubgoalBudget:
        self._check()
        return SubgoalBudget(
            max_model_calls=max(
                0,
                min(
                    self.request.contract.budget.max_model_calls - self.usage.model_calls,
                    self.budget.config.max_model_calls - self.budget.state.usage.model_calls - self.reserve_model_calls,
                ),
            ),
            max_tool_calls=max(
                0,
                min(
                    self.request.contract.budget.max_tool_calls - self.usage.tool_calls,
                    self.budget.config.max_tool_calls - self.budget.state.usage.tool_calls,
                ),
            ),
            max_seconds=min(self.deadline - time.monotonic(), self.budget.seconds_left()),
        )

    def _check(self) -> None:
        if self.closed:
            raise RuntimeError("Subagent runtime is closed")
        self.budget.check()
        if time.monotonic() >= self.deadline:
            raise BudgetExceeded("subgoal_wall_time_limit")

    def _charge_model(self) -> None:
        self._check()
        self.budget.charge(
            "model_calls",
            self.usage,
            self.request.contract.budget.max_model_calls,
            reserve=self.reserve_model_calls,
        )
        self._check()

    @asynccontextmanager
    async def _operation(self) -> AsyncIterator[None]:
        task = asyncio.current_task()
        assert task is not None
        self._tasks.add(task)
        try:
            async with self._lock:
                self._check()
                yield
        finally:
            self._tasks.discard(task)

    async def close(self) -> bool:
        """Cancel and drain cooperative operations before observing the environment again."""
        self.closed = True
        tasks = [task for task in self._tasks if task is not asyncio.current_task() and not task.done()]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        return bool(tasks)

    async def observe(self) -> JsonObject:
        async with self._operation():
            async with asyncio.timeout(self.remaining_budget().max_seconds):
                self.last_observation = await self._observe()
                return self.last_observation

    async def execute(self, tool_name: str, arguments: JsonObject, *, sensitive: bool = False) -> JsonObject:
        async with self._operation():
            if tool_name not in self.request.contract.allowed_tools:
                raise ValueError("tool_not_allowed")
            self.budget.charge("tool_calls", self.usage, self.request.contract.budget.max_tool_calls)
            self.budget.journal.write(
                "tool_started",
                self.budget.state,
                attempt_id=self.request.attempt_id,
                tool_name=tool_name,
                arguments={"redacted": True} if sensitive else arguments,
            )
            started = time.monotonic()
            try:
                self._check()
                async with asyncio.timeout(self.remaining_budget().max_seconds):
                    result = await self._execute(tool_name, arguments)
            except BaseException as error:
                self.budget.journal.write(
                    "tool_failed",
                    self.budget.state,
                    attempt_id=self.request.attempt_id,
                    tool_name=tool_name,
                    error_type=type(error).__name__,
                    elapsed_seconds=round(time.monotonic() - started, 3),
                )
                raise
            self.budget.journal.write(
                "tool_finished",
                self.budget.state,
                attempt_id=self.request.attempt_id,
                tool_name=tool_name,
                elapsed_seconds=round(time.monotonic() - started, 3),
                result={"redacted": True} if sensitive else result,
            )
            return result

    async def infer(self, prompt: str) -> str:
        async with self._operation():
            async with asyncio.timeout(self.remaining_budget().max_seconds):
                return await self._infer(prompt, self._charge_model)

    async def call_model(self, name: str, call: Callable[[], Awaitable[JsonObject]]) -> JsonObject:
        """Account a trusted plugin's external model using the same subgoal/global quotas."""
        async with self._operation():
            self._charge_model()
            self.budget.journal.write(
                "plugin_model_started", self.budget.state, model=name, attempt_id=self.request.attempt_id
            )
            try:
                async with asyncio.timeout(self.remaining_budget().max_seconds):
                    result = await call()
            except BaseException as error:
                self.budget.journal.write(
                    "plugin_model_failed",
                    self.budget.state,
                    model=name,
                    attempt_id=self.request.attempt_id,
                    error_type=type(error).__name__,
                )
                raise
            self.budget.journal.write(
                "plugin_model_finished", self.budget.state, model=name, attempt_id=self.request.attempt_id
            )
            return result

    async def infer_tools(self, messages: list[Message], tools: list[JsonObject]) -> Message:
        """One multimodal model turn; returned calls are executed only via execute()."""
        async with self._operation():
            if self._infer_tools is None:
                raise RuntimeError("subagent_tool_inference_unavailable")
            async with asyncio.timeout(self.remaining_budget().max_seconds):
                return await self._infer_tools(messages, tools, self._charge_model)

    async def cleanup(self, tool_name: str, arguments: JsonObject) -> JsonObject:
        """Two bounded teardown calls, accounted separately from normal execution quotas.

        The environment callback must allow only resource restoration. Plugins are trusted;
        this is not a sandbox. Cleanup remains available until close(), including at deadline.
        """
        async with self._lock:
            if self.closed or self._cleanup is None or self.usage.cleanup_tool_calls >= 2:
                raise RuntimeError("cleanup_unavailable")
            if tool_name not in self.request.contract.allowed_tools:
                raise ValueError("tool_not_allowed")
            self.usage.cleanup_tool_calls += 1
            self.budget.state.usage.cleanup_tool_calls += 1
            self.budget.journal.write(
                "cleanup_started", self.budget.state, attempt_id=self.request.attempt_id, tool_name=tool_name
            )
            try:
                async with asyncio.timeout(5):
                    result = await self._cleanup(tool_name, arguments)
            except BaseException as error:
                self.budget.journal.write(
                    "cleanup_failed",
                    self.budget.state,
                    attempt_id=self.request.attempt_id,
                    error_type=type(error).__name__,
                )
                raise
            self.budget.journal.write(
                "cleanup_finished", self.budget.state, attempt_id=self.request.attempt_id, ok=result.get("ok")
            )
            return result
