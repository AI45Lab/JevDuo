"""Local batch state persistence."""

from __future__ import annotations

import json
from pathlib import Path

from northcua.batch.contracts import BatchTaskState, BatchTaskStatus


class BatchStateStore:
    """JSONL-backed local state store for resumable batch runs."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._states: dict[str, BatchTaskState] = {}
        if self.path.exists():
            for line in self.path.read_text(encoding="utf-8").splitlines():
                if line.strip():
                    state = BatchTaskState.model_validate_json(line)
                    self._states[state.task_id] = state

    def get(self, task_id: str) -> BatchTaskState | None:
        """Return stored state for a task id.

        Args:
            task_id: Manifest task id to look up.

        Returns:
            Latest state snapshot, or ``None`` when the task has not been seen.
        """

        return self._states.get(task_id)

    def upsert(self, state: BatchTaskState) -> None:
        """Persist a new state snapshot for one task.

        Args:
            state: Task state to store.
        """

        self._states[state.task_id] = state
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(state.model_dump_json() + "\n")

    def all(self) -> list[BatchTaskState]:
        """Return latest state for every known task.

        Returns:
            In-memory latest snapshots in insertion order.
        """

        return list(self._states.values())

    def succeeded(self, task_id: str) -> bool:
        """Return whether a task already reached a successful terminal state.

        Args:
            task_id: Batch task id to inspect.

        Returns:
            ``True`` when the latest state is succeeded.
        """

        state = self.get(task_id)
        return bool(state and state.status == BatchTaskStatus.SUCCEEDED)

    def write_compacted(self) -> None:
        """Rewrite JSONL with only the latest state per task."""

        lines = [json.dumps(state.model_dump(mode="json"), ensure_ascii=False) for state in self._states.values()]
        self.path.write_text("\n".join(lines) + ("\n" if lines else ""), encoding="utf-8")
