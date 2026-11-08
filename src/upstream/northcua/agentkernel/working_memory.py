"""Task-scoped, controller-owned working memory independent of prompt history.

SQLite transactions preserve an immutable note audit and survive process restart.
One artifact root identifies a session; each round is a separate task scope.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Iterable
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from nexau.core.messages import Message
    from northcua.core.models import ToolExecutionRecord

MEMORY_METADATA_KEY = "northcua_working_memory"
DEFAULT_PROMPT_CHAR_BUDGET = 12000


class WorkingMemoryStore:
    def __init__(self, artifact_root: Path) -> None:
        self.path = Path(artifact_root) / "working_memory.sqlite3"

    def _connect(self) -> sqlite3.Connection:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.path, timeout=30)
        try:
            connection.execute("PRAGMA synchronous=FULL")
            connection.execute(
                "CREATE TABLE IF NOT EXISTS notes ("
                "event_id TEXT PRIMARY KEY, round_num INTEGER NOT NULL, memory_key TEXT NOT NULL, "
                "kind TEXT NOT NULL, text TEXT NOT NULL, reason TEXT NOT NULL, session_step INTEGER NOT NULL)"
            )
        except BaseException:
            connection.close()
            raise
        return connection

    def ingest(self, executions: Iterable[ToolExecutionRecord], *, round_num: int, session_step: int) -> None:
        records = []
        for index, execution in enumerate(executions):
            if execution.is_error or execution.tool_name.rsplit("__", 1)[-1] not in {"note", "web_note"}:
                continue
            output = execution.tool_output
            if isinstance(output, str):
                try:
                    output = json.loads(output)
                except ValueError:
                    continue
            if not isinstance(output, dict) or output.get("status") != "recorded":
                continue
            text = str(output.get("note_text", "")).strip()
            if not text:
                continue
            identity = f"{round_num}:{session_step}:{execution.tool_call_id or index}"
            event_id = hashlib.sha256(identity.encode()).hexdigest()
            key = str(execution.tool_input.get("key") or event_id)
            records.append(
                (
                    event_id,
                    round_num,
                    key,
                    str(output.get("kind", "fact")),
                    text,
                    str(output.get("reason", "")),
                    session_step,
                )
            )
        if not records:
            return
        connection = self._connect()
        try:
            with connection:
                connection.executemany("INSERT OR IGNORE INTO notes VALUES (?, ?, ?, ?, ?, ?, ?)", records)
        finally:
            connection.close()

    def active_entries(self, round_num: int) -> list[dict[str, str]]:
        if not self.path.exists():
            return []
        connection = self._connect()
        try:
            rows = connection.execute(
                "SELECT memory_key, kind, text, reason FROM (SELECT *, "
                "ROW_NUMBER() OVER(PARTITION BY memory_key ORDER BY session_step DESC, rowid DESC) AS rank "
                "FROM notes WHERE round_num=?) WHERE rank=1 ORDER BY session_step",
                (round_num,),
            ).fetchall()
            return [dict(zip(("key", "kind", "text", "reason"), row)) for row in rows]
        finally:
            connection.close()

    def inject(
        self, messages: list[Message], *, round_num: int, max_chars: int = DEFAULT_PROMPT_CHAR_BUDGET
    ) -> list[Message]:
        """Project only the current active view; never mutate replay history.

        Newest entries fit first. The serialized memory view is bounded, and the
        context manager includes this pinned message in its normal token budget.
        """
        from nexau.core.messages import Message, Role, TextBlock

        result = [message for message in messages if not message.metadata.get(MEMORY_METADATA_KEY)]
        entries = self.active_entries(round_num)
        if not entries or max_chars < 128:
            return result
        lines = []
        remaining = max_chars - 128
        for entry in reversed(entries):
            line = json.dumps(entry, ensure_ascii=False)
            if len(line) + 1 > remaining:
                continue
            lines.append(line)
            remaining -= len(line) + 1
        text = "Active working memory (agent notes; task instructions remain authoritative):\n"
        text += "\n".join(reversed(lines))
        if len(lines) < len(entries):
            text += f"\n[{len(entries) - len(lines)} entries omitted by memory budget]"
        memory = Message(role=Role.USER, content=[TextBlock(text=text)], metadata={MEMORY_METADATA_KEY: True})
        # Keep task and current observation positions intact for the context adapter.
        task_index = next((i for i, message in enumerate(result) if message.role == Role.USER), -1)
        result.insert(task_index + 1, memory)
        return result
