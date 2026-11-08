"""Selection logic for upcoming tasks that can be prepared safely."""

from __future__ import annotations

from pydantic import BaseModel

from northcua.batch.contracts import BatchManifest, BatchTaskSpec
from northcua.batch.preparation.contracts import PreparedSandboxPolicy
from northcua.batch.preparation.sqlite_store import SQLitePreparedSandboxStore
from northcua.sandboxes.contracts import SandboxProviderSpec


class PreparedSandboxCandidate(BaseModel):
    """One task attempt selected for background preparation.

    Attributes:
        task: Batch task spec that will eventually be claimed.
        attempt: One-based task attempt number to prepare.
        sandbox_spec: Per-task or default sandbox provider spec.
    """

    task: BatchTaskSpec
    attempt: int
    sandbox_spec: SandboxProviderSpec


class PreparedSandboxPlanner:
    """Plan bounded prepared-sandbox work from batch state and manifest order."""

    def __init__(
        self,
        *,
        manifest: BatchManifest,
        store: SQLitePreparedSandboxStore,
        policy: PreparedSandboxPolicy,
        max_attempts_by_task: dict[str, int],
    ) -> None:
        self._manifest = manifest
        self._store = store
        self._policy = policy
        self._max_attempts_by_task = max_attempts_by_task
        self._tasks_by_id = {task.task_id: task for task in manifest.tasks}

    def candidates(self, *, run_concurrency: int, runner_id: str) -> list[PreparedSandboxCandidate]:
        """Return upcoming task attempts until the configured buffer is full.

        Args:
            run_concurrency: Effective task concurrency used to resolve a
                zero-as-default buffer.
            runner_id: Runner process requesting reservations.

        Returns:
            Ordered prepared-sandbox candidates matching manifest order.
        """

        if not self._policy.enabled:
            return []
        buffer = self._policy.effective_buffer(run_concurrency=run_concurrency)
        live_limit = max(run_concurrency, 1) + buffer
        desired_unassigned_slots = max(buffer - self._store.active_count(), 0)
        live_slots = max(live_limit - self._store.live_count(), 0)
        remaining_slots = min(desired_unassigned_slots, live_slots)
        if remaining_slots <= 0:
            return []
        keys = self._store.claimable_task_ids(
            max_attempts_by_task=self._max_attempts_by_task,
            limit=remaining_slots,
        )
        candidates: list[PreparedSandboxCandidate] = []
        for task_id, attempt in keys:
            task = self._tasks_by_id.get(task_id)
            if task is None:
                continue
            sandbox_spec = task.sandbox or self._manifest.defaults.sandbox
            candidates.append(
                PreparedSandboxCandidate(
                    task=task,
                    attempt=attempt,
                    sandbox_spec=sandbox_spec,
                )
            )
        return candidates
