import unittest

from jev_desktop.snapshot_retry import snapshot
from support import failure, success


def busy():
    result = failure("TIMEOUT", delivery="not_delivered", retry="safe")
    result["error"]["details"] = {"kind": "frames_unanswered"}
    return result


class SnapshotRetryTests(unittest.TestCase):
    def invoke(self, replies, timeout_ms=15000):
        calls, pauses, now = [], [], [0.0]
        def cli(*argv):
            calls.append(argv)
            return replies.pop(0) if len(replies) > 1 else replies[0]
        def sleep(delay):
            pauses.append(delay)
            now[0] += delay
        result = snapshot(cli, ["snapshot", "--timeout-ms", str(timeout_ms)], timeout_ms,
                          sleep=sleep, clock=lambda: now[0])
        return result, calls, pauses

    def test_busy_owner_gets_only_reads_then_the_real_closed_window_result(self):
        closed = failure("WINDOW_NOT_FOUND")
        result, calls, pauses = self.invoke([busy(), busy(), closed])
        self.assertEqual(result, closed)
        self.assertEqual(pauses, [0.25, 0.5])
        self.assertEqual(len(calls), 3)
        self.assertTrue(all(call[0] == "snapshot" for call in calls))
        self.assertEqual([call[-1] for call in calls], ["15000", "14750", "14250"])

    def test_reads_succeed_after_busy_and_never_exceed_the_sleep_budget(self):
        self.assertTrue(self.invoke([busy(), success()])[0]["ok"])
        result, calls, pauses = self.invoke([busy()], timeout_ms=1000)
        self.assertEqual(result["error"]["code"], "TIMEOUT")
        self.assertLessEqual(sum(pauses), 1)
        self.assertEqual(len(calls), 3)

    def test_other_failures_remain_immediate(self):
        for response in [failure("TIMEOUT"), failure("WINDOW_NOT_FOUND"), failure("REMOTE_EXECUTION_UNCONFIRMED")]:
            with self.subTest(response=response):
                result, calls, pauses = self.invoke([response, success()])
                self.assertEqual(result, response)
                self.assertEqual(len(calls), 1)
                self.assertEqual(pauses, [])

    def test_unproven_capture_gets_only_bounded_reads_before_a_proven_capture(self):
        pending = success({"identity_unproven": True, "complete": True})
        proven = success({"complete": True})
        result, calls, pauses = self.invoke([pending, pending, proven])
        self.assertEqual(result, proven)
        self.assertEqual([call[-1] for call in calls], ["15000", "14750", "14250"])
        self.assertEqual(pauses, [0.25, 0.5])
        self.assertTrue(all(call[0] == "snapshot" for call in calls))

    def test_expired_identity_wait_keeps_the_unproven_result(self):
        pending = success({"identity_unproven": True, "complete": True})
        result, calls, pauses = self.invoke([pending], timeout_ms=1000)
        self.assertEqual(result, pending)
        self.assertEqual(len(calls), 3)
        self.assertLessEqual(sum(pauses), 1)
