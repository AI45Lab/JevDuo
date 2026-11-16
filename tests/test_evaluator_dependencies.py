"""055 dependency preparation: guest effects are mocked; live preflight is separate."""

import contextlib
import io
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import evaluator_dependencies as dependencies


class EvaluatorDependenciesTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.receipt = Path(self.tmp.name) / "dependencies/055.json"
        self.controller = SimpleNamespace(client_password="guest-test-only")

    def test_other_tasks_do_not_install_or_start_a_preflight(self):
        with patch.object(dependencies, "execute_background") as execute:
            dependencies.prepare_evaluator_dependencies(self.controller, "054", self.receipt)
        execute.assert_not_called()
        self.assertFalse(self.receipt.exists())

    def test_valid_receipt_is_required_and_sanitized(self):
        detail = {"ok": True, "renderer": "/usr/bin/melt", "installed": True, "sample_bytes": 2048}
        with patch.object(dependencies, "execute_background", return_value={
            "returncode": 0, "output": json.dumps(detail),
        }) as execute:
            dependencies.prepare_evaluator_dependencies(self.controller, "055", self.receipt)
        self.assertEqual(execute.call_count, 1)
        self.assertEqual(execute.call_args.kwargs, {"timeout": 720, "check": False})
        record = json.loads(self.receipt.read_text())
        self.assertEqual(record["status"], "passed")
        self.assertEqual(record["sample_bytes"], 2048)
        self.assertNotIn(self.controller.client_password, self.receipt.read_text())

    def test_failed_install_is_not_repeated_or_converted_to_success(self):
        with patch.object(
            dependencies, "execute_background", side_effect=RuntimeError("install failed"),
        ) as ex:
            with self.assertRaisesRegex(RuntimeError, "install failed"):
                dependencies.prepare_evaluator_dependencies(self.controller, "055", self.receipt)
        self.assertEqual(ex.call_count, 1)
        self.assertEqual(json.loads(self.receipt.read_text())["status"], "failed")

    def test_invalid_or_empty_video_receipt_blocks_acting(self):
        for detail in ({"ok": True}, {"ok": True, "renderer": "/usr/bin/melt", "sample_bytes": 0}):
            with self.subTest(detail=detail), patch.object(dependencies, "execute_background", return_value={
                "returncode": 0, "output": json.dumps(detail),
            }), self.assertRaisesRegex(RuntimeError, "invalid success receipt"):
                dependencies.prepare_evaluator_dependencies(self.controller, "055", self.receipt)
            self.assertEqual(json.loads(self.receipt.read_text())["status"], "failed")

    def test_guest_failure_details_are_preserved_without_password(self):
        with patch.object(dependencies, "execute_background", return_value={
            "returncode": 1, "output": "package unavailable", "error": "guest-test-only: error",
        }), self.assertRaisesRegex(RuntimeError, "preflight failed"):
            dependencies.prepare_evaluator_dependencies(self.controller, "055", self.receipt)
        receipt = json.loads(self.receipt.read_text())
        self.assertEqual(receipt["guest_failure"]["returncode"], 1)
        self.assertEqual(receipt["guest_failure"]["output"], "package unavailable")
        self.assertNotIn("guest-test-only", self.receipt.read_text())

    def guest(self, *, installed, fail_install=False, lists_locked=False):
        calls = []
        available = installed

        def find(candidate):
            return "/usr/bin/melt" if available and candidate in {"melt", "/usr/bin/melt"} else None

        def run(command, **kwargs):
            nonlocal available
            calls.append((command, kwargs))
            if "apt-get" in command:
                if lists_locked and len(calls) == 1:
                    kwargs["stderr"].write(
                        b"E: Could not get lock /var/lib/apt/lists/lock. It is held by process 1839 "
                        b"(packagekitd)\nE: Unable to lock directory /var/lib/apt/lists/\n"
                    )
                    raise subprocess.CalledProcessError(100, command)
                if fail_install:
                    raise subprocess.CalledProcessError(100, command)
                if "install" in command:
                    available = True
            else:
                output = next(arg.removeprefix("avformat:") for arg in command if arg.startswith("avformat:"))
                Path(output).write_bytes(b"mock-video")
            return SimpleNamespace(returncode=0)

        stream = io.StringIO()
        with patch("shutil.which", side_effect=find), patch("subprocess.run", side_effect=run), patch(
            "tempfile.mkdtemp", return_value=self.tmp.name,
        ), patch("os.geteuid", return_value=1000), patch.object(
            sys, "argv", ["preflight", self.controller.client_password],
        ), patch("time.sleep"), contextlib.redirect_stdout(stream):
            exec(compile(dependencies._MELT_PREFLIGHT, "guest-preflight", "exec"), {})
        return calls, json.loads(stream.getvalue())

    def test_guest_existing_renderer_only_runs_encoding_check(self):
        calls, receipt = self.guest(installed=True)
        self.assertEqual(len(calls), 1)
        self.assertNotIn("apt-get", calls[0][0])
        self.assertFalse(receipt["installed"])
        self.assertGreater(receipt["sample_bytes"], 0)

    def test_guest_missing_renderer_installs_once_then_checks_encoding(self):
        calls, receipt = self.guest(installed=False)
        self.assertEqual(len(calls), 3)
        self.assertEqual(sum("install" in command for command, _ in calls), 1)
        self.assertIn("melt", calls[1][0])
        self.assertEqual(calls[1][1]["input"], b"guest-test-only\n")
        self.assertTrue(receipt["installed"])

    def test_guest_install_failure_never_reaches_encoder(self):
        with self.assertRaises(subprocess.CalledProcessError):
            self.guest(installed=False, fail_install=True)

    def test_guest_waits_only_for_confirmed_lists_lock_then_installs_once(self):
        calls, receipt = self.guest(installed=False, lists_locked=True)
        self.assertEqual(len(calls), 4)
        self.assertEqual(sum("install" in command for command, _ in calls), 1)
        self.assertTrue(receipt["ok"])

    def test_guest_lists_lock_has_a_bounded_wait(self):
        with patch("time.monotonic", side_effect=[0, 181]), self.assertRaises(subprocess.CalledProcessError):
            self.guest(installed=False, lists_locked=True)
