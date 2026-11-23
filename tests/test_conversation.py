"""Recovery and immutability of task-scoped model conversations."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from northcua.agentkit.orchestration.conversation import Conversation, event_message


class ConversationTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "conversations" / "slow.sqlite3"

    def test_restart_preserves_messages_and_idempotent_events(self):
        conversation = Conversation(self.path)
        original = event_message({"task": "Keep exact task text\n中文"})
        conversation.append(original, event_id="initial")
        original.content[0].text = "changed by caller"
        snapshot = conversation.snapshot()
        snapshot[0].content[0].text = "changed by reader"
        restored = Conversation(self.path)
        restored.append(event_message({"task": "must not replace history"}), event_id="initial")
        self.assertEqual(len(restored.snapshot()), 1)
        self.assertEqual(json.loads(restored.snapshot()[0].content[0].text)["task"],
                         "Keep exact task text\n中文")
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)

    def test_concurrent_stale_writer_fails_before_changing_memory_or_disk(self):
        first = Conversation(self.path)
        stale = Conversation(self.path)
        first.append(event_message({"event": "first"}))
        with self.assertRaisesRegex(RuntimeError, "another writer"):
            stale.append(event_message({"event": "stale"}))
        self.assertEqual(stale.snapshot(), [])
        self.assertEqual(Conversation(self.path).snapshot(), first.snapshot())
