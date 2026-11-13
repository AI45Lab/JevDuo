"""A scoring helper must not swallow the worker's hard task deadline."""

import os
import subprocess
import sys
import unittest
from pathlib import Path


class WorkerDeadlineTest(unittest.TestCase):
    def test_deadline_escapes_an_evaluator_exception_handler(self):
        result = subprocess.run(
            [sys.executable, "-c", """
import signal
import time
import worker_limits
previous = signal.getsignal(signal.SIGALRM)
cancel = worker_limits.arm(0.05)
try:
    try:
        time.sleep(5)
    except Exception:
        raise AssertionError('evaluator swallowed the task deadline')
except worker_limits.TaskWallTimeLimit as error:
    assert str(error) == 'task_wall_time_limit'
else:
    raise AssertionError('deadline did not fire')
finally:
    cancel()
assert signal.getsignal(signal.SIGALRM) == previous
assert signal.getitimer(signal.ITIMER_REAL)[0] == 0
"""],
            env={**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[1] / "src")},
            capture_output=True, text=True, timeout=10,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
