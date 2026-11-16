import json
import tempfile
import unittest
from pathlib import Path

import recorder


class PhaseArtifactsTest(unittest.TestCase):
    def test_phase_trajectories_and_trace_links_preserve_both_agent_runs(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for index in (1, 2):
                directory = root / "agent" / f"phase-{index:02d}" / "session"
                directory.mkdir(parents=True)
                (directory / "steps.jsonl").write_text(json.dumps({"session_step": 1,
                                                                  "assistant_text": f"phase {index}"}) + "\n")
                (directory / "llm_trace.jsonl").write_text(json.dumps({"phase": index}) + "\n")
            recorder.write_trajectory(root, root / "agent")
            rows = [json.loads(line) for line in (root / "traj.jsonl").read_text().splitlines()]
            self.assertEqual([row["phase_index"] for row in rows], [1, 2])
            self.assertEqual([row["assistant_text"] for row in rows], ["phase 1", "phase 2"])
            for index in (1, 2):
                link = root / "phases" / f"phase-{index:02d}" / "llm_calls.jsonl"
                self.assertTrue(link.is_symlink())
                self.assertEqual(json.loads(link.read_text())["phase"], index)
            self.assertEqual(len(json.loads((root / "llm_calls.index.json").read_text())), 2)
