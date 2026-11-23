import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from http_support import server

from jev_desktop import DesktopConfig, LocalTransport, RemoteTransport, TypeSafeModel
from jev_desktop.http import post_json
from jev_desktop.model import model_endpoint, resolve_model
from jev_desktop.transport import remote_endpoint


class TransportTests(unittest.TestCase):
    def test_slow_body_cannot_extend_request_deadline(self):
        for proxy in (False, True):
            with self.subTest(proxy=proxy):
                with server(lambda *_: (200, {"answers": {}}), interval=0.02) as url:
                    start = time.monotonic()
                    with self.assertRaises(TimeoutError):
                        post_json(url, {}, timeout=0.06, proxy=proxy)
                    self.assertLess(time.monotonic() - start, 1.06)

    def test_local_success_nonzero_error_malformed_and_timeout(self):
        transport = LocalTransport()
        for script, ok in (
            ('print(\'{"ok":true,"data":{}}\')', True),
            ('print(\'{"ok":false,"error":{"code":"TIMEOUT"}}\');exit(1)', False),
            ("print('private invalid output')", False),
            ("print('{\"ok\":true}');exit(1)", False),
            ("import time;time.sleep(2)", False),
        ):
            with self.subTest(script=script):
                # Shared-storage interpreter startup alone can exceed 100 ms.
                timeout = 0.1 if script == "import time;time.sleep(2)" else 2
                result = transport.invoke(sys.executable, ["-c", script], timeout)
                self.assertEqual(result["ok"], ok)
                self.assertNotIn("private", str(result))
                if not ok and result["error"]["code"] == "SPAWN_FAILED":
                    self.assertEqual(result["error"]["disposition"]["retry"], "unsafe")

    def test_vm_request_argv_and_nonzero_structured_error(self):
        requests = []

        def dispatch(path, body, headers):
            requests.append((path, body))
            return 200, {"returncode": 1, "output": json.dumps({"ok": False, "error": {"code": "STALE_REF"}})}

        with server(dispatch) as url:
            result = RemoteTransport(url).invoke(
                "/vm/agent-desktop", ["set-value", "@s1:e1", 'a"b\n$HOME'], 2
            )
        self.assertEqual(result["error"]["code"], "STALE_REF")
        self.assertEqual(
            requests,
            [
                (
                    "/execute",
                    {"command": ["/vm/agent-desktop", "set-value", "@s1:e1", 'a"b\n$HOME'], "shell": False},
                )
            ],
        )

    def test_vm_bad_envelopes_and_redirects_fail_without_retry(self):
        cases = [
            (200, {}),
            (200, {"returncode": 1, "output": '{"ok":true}'}),
            (200, {"returncode": 0, "output": '{"ok":false}'}),
            (200, {"returncode": True, "output": '{"ok":true}'}),
            (200, b"private response invalid"),
            (503, {}),
            (302, {}),
        ]
        for status, payload in cases:
            with self.subTest(status=status, payload=payload):
                calls = []

                def dispatch(*args, calls=calls, status=status, payload=payload):
                    calls.append(args)
                    return status, payload

                with server(dispatch) as url:
                    result = RemoteTransport(url).invoke("binary", ["click", "@s1:e1"], 2)
                self.assertEqual(len(calls), 1)
                self.assertEqual(
                    result["error"]["disposition"], {"delivery": "delivery_uncertain", "retry": "unsafe"}
                )
                self.assertNotIn("private", str(result))

    def test_vm_timeout_is_uncertain_and_never_retried(self):
        calls = []

        def dispatch(*args):
            calls.append(args)
            time.sleep(0.3)
            return 200, {"returncode": 0, "output": '{"ok":true}'}

        with server(dispatch) as url:
            start = time.monotonic()
            result = RemoteTransport(url).invoke("binary", ["click", "@s1:e1"], 0.05)
            elapsed = time.monotonic() - start
        self.assertLess(elapsed, 1.05)
        self.assertEqual(len(calls), 1)
        self.assertEqual(result["error"]["code"], "REMOTE_EXECUTION_UNCONFIRMED")

    def test_endpoint_validation_and_model_alias(self):
        for endpoint in (
            "http://localhost:9/test",
            "http://127.0.0.2/test",
            "http://[::1]:9/test",
            "https://api.example/test",
        ):
            self.assertEqual(model_endpoint(endpoint), endpoint)
        for endpoint in (
            "file:///tmp/x",
            "http://remote.example/test",
            "http://127.0.0.2.example/test",
            "https://user:secret@api.example/test",
            "https:///test",
            "https://api.example:bad",
        ):
            with self.assertRaises(ValueError):
                model_endpoint(endpoint)
        for endpoint in (
            "http://user:secret@localhost",
            "http://localhost/other",
            "http://localhost/?key=secret",
        ):
            with self.assertRaises(ValueError):
                remote_endpoint(endpoint)
        self.assertEqual(remote_endpoint("http://localhost:9"), "http://localhost:9/execute")
        self.assertEqual(
            resolve_model("jev-latest", "https://openrouter.ai/api/alpha/decisions"), "~typesafe/jev-latest"
        )
        self.assertEqual(resolve_model("jev-latest", "https://other.example/openrouter.ai"), "jev-latest")

    def test_remote_http_model_requires_explicit_opt_in(self):
        endpoint = "http://100.103.152.177:8080/v1/systemone"
        env = {"TYPESAFE_API_KEY": "fixture", "TYPESAFE_BASE_URL": endpoint}
        with patch.dict(os.environ, env, clear=True):
            with self.assertRaises(ValueError):
                TypeSafeModel.from_env()
            os.environ["TYPESAFE_ALLOW_HTTP"] = "1"
            model = TypeSafeModel.from_env()
            self.assertEqual(model.endpoint, endpoint)
            self.assertEqual(model.model, "jev-latest")
            for invalid in ("http://user:secret@remote/test", "file:///tmp/x", "http://remote:bad/test"):
                with self.assertRaises(ValueError):
                    model_endpoint(invalid, allow_http=True)

    def test_invalid_timeouts_are_rejected_before_commands(self):
        for bad in (float("nan"), float("inf"), -1, 60001, True):
            with self.assertRaises(ValueError):
                DesktopConfig(verify_ms=bad)
        for bad in (0, 300001, 1.2, True):
            with self.assertRaises(ValueError):
                DesktopConfig(snapshot_timeout_ms=bad)
            with self.assertRaises(ValueError):
                DesktopConfig(action_timeout_ms=bad)

    def test_explicit_config_overrides_invalid_environment_before_validation(self):
        with patch.dict(os.environ, {"AGENT_DESKTOP_BIN": "", "AGENT_DESKTOP_SNAPSHOT_TIMEOUT_MS": "bad"}):
            config = DesktopConfig.from_env(overrides={"binary": "chosen-binary", "snapshot_timeout_ms": 42})
        self.assertEqual(config.binary, "chosen-binary")
        self.assertEqual(config.snapshot_timeout_ms, 42)


class ModelTests(unittest.TestCase):
    def test_retry_only_transient_http_and_keep_request(self):
        calls, pauses = [], []

        def dispatch(path, body, headers):
            calls.append((body, headers["Authorization"]))
            return (503, {}) if len(calls) < 3 else (200, {"answers": {}})

        with server(dispatch) as url:
            model = TypeSafeModel("fixture", endpoint=url, sleep=pauses.append)
            self.assertEqual(model.ask({"state": {"goal": "goal"}, "questions": {}}), {"answers": {}})
        self.assertEqual(pauses, [0.5, 1])
        self.assertEqual(len(calls), 3)
        self.assertTrue(all(call == calls[0] for call in calls))
        self.assertEqual(calls[0][1], "Bearer fixture")

    def test_permanent_failure_redirect_invalid_json_and_transport_never_retry(self):
        for status, payload in (
            (400, b"private echoed input"),
            (302, {}),
            (200, b"private invalid JSON"),
            (200, []),
            (200, {"answers": None}),
        ):
            with self.subTest(status=status, payload=payload):
                calls = []

                def dispatch(*args, calls=calls, status=status, payload=payload):
                    calls.append(args)
                    return status, payload

                with server(dispatch) as url:
                    with self.assertRaises(RuntimeError) as caught:
                        TypeSafeModel("fixture", endpoint=url).ask({"questions": {}})
                self.assertEqual(len(calls), 1)
                self.assertNotIn("private", str(caught.exception))
        with patch("jev_desktop.model.post_json", side_effect=OSError("private network exception")) as post:
            with self.assertRaisesRegex(RuntimeError, "could not be reached"):
                TypeSafeModel("fixture").ask({})
            self.assertEqual(post.call_count, 1)

    def test_missing_api_key_and_scoped_alias(self):
        with self.assertRaises(ValueError):
            TypeSafeModel("")
        self.assertEqual(TypeSafeModel("fixture", model="typesafe/version").model, "typesafe/version")


class CliTests(unittest.TestCase):
    def test_remote_cli_full_loop_and_exit_status(self):
        for stop in ("DONE", "BLOCKED"):
            with self.subTest(stop=stop):
                commands, requests = [], []

                def dispatch(path, body, headers, fixture=(stop, commands, requests)):
                    stop, commands, requests = fixture
                    if path == "/decisions":
                        requests.append(body)
                        head = next(iter(body["questions"]))
                        choice = "CLICK" if head == "operation" else stop if "click" in commands else "1"
                        return 200, {
                            "answers": {
                                head: {"choice": choice, "confidence": 0, "probabilities": {choice: 1}}
                            }
                        }
                    args = body["command"][1:]
                    commands.append(args[0])
                    data = {"disposition": {"delivery": "delivered_verified"}}
                    if args[0] == "snapshot":
                        data.update(
                            app="Fixture",
                            window={"id": "w1", "title": "Test"},
                            complete=True,
                            tree={
                                "role": "window",
                                "children": [
                                    {
                                        "role": "button",
                                        "name": "Apply",
                                        "ref_id": "@s1:e1",
                                        "available_actions": ["Click"],
                                    }
                                ],
                            },
                        )
                    if args[0] == "list-surfaces":
                        data["surfaces"] = []
                    return 200, {"returncode": 0, "output": json.dumps({"ok": True, "data": data})}

                with server(dispatch) as url:
                    env = {
                        **os.environ,
                        "PYTHONPATH": str(Path(__file__).resolve().parents[1] / "src"),
                        "TYPESAFE_API_KEY": "fixture",
                        "TYPESAFE_BASE_URL": url + "/decisions",
                        "AGENT_DESKTOP_BIN": "",
                        "AGENT_DESKTOP_SNAPSHOT_TIMEOUT_MS": "invalid overridden setting",
                    }
                    process = subprocess.run(
                        [
                            sys.executable,
                            "-m",
                            "jev_desktop",
                            "--vm-url",
                            url,
                            "--bin",
                            "/vm/binary",
                            "--snapshot-timeout-ms",
                            "42",
                            "--app",
                            "Fixture",
                            "--window-id",
                            "w1",
                            "click Apply",
                        ],
                        capture_output=True,
                        text=True,
                        env=env,
                        timeout=10,
                        cwd=tempfile.gettempdir(),
                    )
                self.assertEqual(
                    process.returncode, 0 if stop == "DONE" else 1, process.stdout + process.stderr
                )
                events = [json.loads(line) for line in process.stdout.splitlines()]
                self.assertEqual(events[-1]["stop"], stop.lower())
                self.assertEqual(events[0]["turn"]["argv"], ["click", "@s1:e1"])
                self.assertEqual(commands.count("click"), 1)
                self.assertEqual(len(requests), 3)
                self.assertEqual(process.stderr, "")

    def test_cli_usage_and_validation(self):
        env = {**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[1] / "src")}
        for args, code in (
            (["--help"], 0),
            ([], 2),
            (["--snapshot-timeout-ms", "bad", "goal"], 2),
            (["--unknown", "goal"], 2),
            (["--text"], 2),
        ):
            process = subprocess.run(
                [sys.executable, "-m", "jev_desktop", *args], env=env, capture_output=True, timeout=10
            )
            self.assertEqual(process.returncode, code, process.stdout + process.stderr)
