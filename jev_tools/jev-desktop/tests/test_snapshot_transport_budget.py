import unittest

from jev_desktop import AgentDesktop, DesktopConfig, Scope
from support import Transport, node, success


class SnapshotTransportBudgetTest(unittest.TestCase):
    def test_escalated_observation_keeps_transport_longer_than_native_deadline(self):
        for configured, escalated in ((15000, 30000), (30000, 60000)):
            with self.subTest(configured=configured):
                snapshots = []

                def handler(argv, snapshots=snapshots):
                    if argv[0] == "list-surfaces":
                        return success({"surfaces": []})
                    snapshots.append(argv)
                    return success(
                        {
                            "app": "Editor",
                            "window": {"id": "w1"},
                            "complete": len(snapshots) > 1,
                            "tree": {"role": "window", "children": [node()]},
                        }
                    )

                transport = Transport(handler)
                desktop = AgentDesktop(DesktopConfig(snapshot_timeout_ms=configured), transport)
                desktop.observe(Scope(window_id="w1"))
                calls = [(argv, timeout) for _, argv, timeout in transport.calls if argv[0] == "snapshot"]
                self.assertEqual(len(calls), 2)
                self.assertEqual(calls[1][0][-1], str(escalated))
                self.assertGreater(calls[1][1], escalated / 1000)

    def test_explicit_snapshot_deadline_does_not_change_action_transport_budget(self):
        transport = Transport()
        desktop = AgentDesktop(DesktopConfig(snapshot_timeout_ms=15000), transport)
        desktop.cli("snapshot", "--timeout-ms=60000")
        desktop.cli("snapshot", "--timeout-ms", "invalid")
        desktop.cli("set-value", "@s1:e1", "--timeout-ms=60000")
        self.assertEqual([call[2] for call in transport.calls], [65, 30, 30])
