"""Durable task conversations whose existing messages remain immutable."""

from __future__ import annotations

import json
import os
import sqlite3
from contextlib import closing
from copy import deepcopy
from datetime import datetime
from pathlib import Path

from nexau.core.messages import Message, Role, TextBlock


class Conversation:
    """Keep the raw transcript; the existing context manager projects compaction checkpoints."""

    def __init__(self, path: Path | None = None) -> None:
        self.path = path
        self.messages: list[Message] = []
        self.events: set[str] = set()
        self.sequence = 0
        if path is not None:
            path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            descriptor = os.open(path, os.O_CREAT | os.O_WRONLY, 0o600)
            os.close(descriptor)
            with self._connect() as connection:
                connection.execute(
                    "CREATE TABLE IF NOT EXISTS messages ("
                    "sequence INTEGER PRIMARY KEY, event_id TEXT UNIQUE, payload TEXT NOT NULL)"
                )
                for sequence, event_id, payload in connection.execute(
                    "SELECT sequence, event_id, payload FROM messages ORDER BY sequence"
                ):
                    self.messages.extend(Message.model_validate(item) for item in json.loads(payload))
                    self.sequence = sequence
                    if event_id is not None:
                        self.events.add(event_id)

    def _connect(self):
        assert self.path is not None
        connection = sqlite3.connect(self.path, timeout=30)
        connection.execute("PRAGMA synchronous=FULL")
        return closing(connection)

    def extend(self, messages: list[Message], *, event_id: str | None = None) -> None:
        if event_id is not None and event_id in self.events:
            return
        frozen = deepcopy(messages)
        for message in frozen:
            # NexAU serializes an unset timestamp as the current time. Freeze
            # it once so persistence and replay use identical message metadata.
            if message.created_at is None:
                message.created_at = datetime.now()
        if self.path is not None:
            with self._connect() as connection, connection:
                connection.execute("BEGIN IMMEDIATE")
                latest = connection.execute("SELECT COALESCE(MAX(sequence), 0) FROM messages").fetchone()[0]
                if latest != self.sequence:
                    raise RuntimeError("Conversation has another writer; reload the task before continuing")
                connection.execute(
                    "INSERT INTO messages VALUES (?, ?, ?)",
                    (
                        self.sequence + 1,
                        event_id,
                        json.dumps([message.model_dump(mode="json") for message in frozen], ensure_ascii=False),
                    ),
                )
        self.sequence += 1
        self.messages.extend(frozen)
        if event_id is not None:
            self.events.add(event_id)

    def append(self, message: Message, *, event_id: str | None = None) -> None:
        self.extend([message], event_id=event_id)

    def snapshot(self) -> list[Message]:
        return deepcopy(self.messages)


def event_message(payload: dict) -> Message:
    return Message(role=Role.USER, content=[TextBlock(text=json.dumps(payload, ensure_ascii=False))])
