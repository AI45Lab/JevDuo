import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from http_support import server
from jev_desktop import ChatTextModel, TextModelError, TextRequest
from jev_desktop.text_model import read_text
from jev_desktop.text_request import build_text_request
from support import node


def completion(text="Generated title", **message):
    return {
        "choices": [{"finish_reason": "stop", "message": {"role": "assistant", "content": text, **message}}]
    }


class TextModelTests(unittest.TestCase):
    def test_http_payload_auth_and_exact_content(self):
        sent = []
        text = '  月球\n"未来"  '

        def dispatch(path, body, headers):
            sent.append((path, body, headers["Authorization"]))
            return 200, completion(text)

        request = TextRequest("write a title", {"what": "Title"}, {"app": "Fixture"}, [])
        with server(dispatch) as url:
            writer = ChatTextModel("text-key", model="writer-fixture", endpoint=url + "/v1/chat/completions")
            self.assertEqual(writer.generate(request), text)
        self.assertEqual(len(sent), 1)
        path, body, auth = sent[0]
        self.assertEqual(path, "/v1/chat/completions")
        self.assertEqual(auth, "Bearer text-key")
        self.assertEqual(body["model"], "writer-fixture")
        self.assertFalse(body["stream"])
        self.assertEqual([m["role"] for m in body["messages"]], ["system", "user"])
        context = json.loads(body["messages"][1]["content"])
        self.assertEqual(
            context,
            {
                "goal": request.goal,
                "field": request.field,
                "screen": request.screen,
                "history": [],
                "operation": "TYPE_TEXT",
            },
        )
        self.assertNotIn("tools", body)

    def test_incomplete_refused_or_tool_responses_are_not_insertable_text(self):
        invalid = [
            None,
            [],
            {},
            {"choices": []},
            {"choices": [None]},
            completion(content=None),
            completion(tool_calls=[{"id": "tool"}]),
            completion(function_call={"name": "tool"}),
            completion(refusal="private refusal"),
            completion(content=[{"type": "text", "text": "partial"}]),
            {"choices": [completion()["choices"][0], completion()["choices"][0]]},
        ]
        for reason in (None, "length", "tool_calls", "function_call", "content_filter"):
            invalid.append(
                {
                    "choices": [
                        {
                            "finish_reason": reason,
                            "message": {"role": "assistant", "content": "private incomplete text"},
                        }
                    ]
                }
            )
        for value in invalid:
            with self.subTest(value=value):
                with self.assertRaises(TextModelError) as caught:
                    read_text(value)
                self.assertNotIn("private", str(caught.exception))
        for value in ("", " \n "):
            self.assertIsNone(read_text(completion(value)))

    def test_http_failures_keep_status_without_echoing_provider_content(self):
        for status, payload in ((401, b"private echoed request"), (302, {}), (200, b"private invalid JSON")):
            with self.subTest(status=status):
                calls = []

                def dispatch(*args, calls=calls, status=status, payload=payload):
                    calls.append(args)
                    return status, payload

                with server(dispatch) as url:
                    with self.assertRaises(TextModelError) as caught:
                        ChatTextModel("text-key", model="writer", endpoint=url).generate(
                            TextRequest("goal", {}, {}, [])
                        )
                self.assertEqual(len(calls), 1)
                self.assertNotIn("private", str(caught.exception))
                if status == 401:
                    self.assertIn("HTTP 401", str(caught.exception))

    def test_optional_configuration_and_explicit_overrides(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertIsNone(ChatTextModel.from_env())
            with self.assertRaisesRegex(ValueError, "must name a model"):
                ChatTextModel.from_env(endpoint="http://localhost:9/chat/completions")
        with patch.dict(
            os.environ,
            {
                "TEXT_MODEL": "env-model",
                "TEXT_MODEL_API_KEY": "text-key",
                "TEXT_MODEL_BASE_URL": "invalid overridden endpoint",
            },
            clear=True,
        ):
            writer = ChatTextModel.from_env(
                model="explicit-model", endpoint="http://localhost:9/chat/completions"
            )
            self.assertEqual(writer.model, "explicit-model")
            self.assertEqual(writer.api_key, "text-key")
            with self.assertRaisesRegex(ValueError, "TEXT_MODEL_BASE_URL"):
                ChatTextModel.from_env()
        with self.assertRaisesRegex(ValueError, "TEXT_MODEL_API_KEY"):
            ChatTextModel(None, model="writer", endpoint="https://provider.example/chat/completions")

    def test_no_values_and_history_exclude_action_payloads(self):
        n = node(value="PRIVATE-FIELD-VALUE")
        screen = {"app": "Fixture", "context": [{"role": "textfield", "value": "PRIVATE-CONTEXT"}]}
        history = [
            {
                "step": 1,
                "operation": "TYPE_TEXT",
                "argv": ["private payload"],
                "details": {"value": "private payload"},
            }
        ]
        request = build_text_request("goal", screen, n, history, values=False)
        self.assertNotIn("PRIVATE", str(request))
        self.assertNotIn("private payload", str(request))
        self.assertEqual(request.history, [{"step": 1, "operation": "TYPE_TEXT"}])


class TextCliTests(unittest.TestCase):
    def test_cli_two_models_then_verified_write_with_separate_credentials(self):
        self.run_text_cli(use_flags=True)

    def test_cli_enables_text_model_from_environment(self):
        self.run_text_cli(use_flags=False)

    def run_text_cli(self, *, use_flags):
        commands, requests, written = [], [], []
        content = '  月球\n"未来"  '

        def dispatch(path, body, headers):
            if path == "/writer":
                requests.append(("writer", body, headers["Authorization"]))
                return 200, completion(content)
            if path == "/decisions":
                requests.append(("jev", body, headers["Authorization"]))
                head = next(iter(body["questions"]))
                choice = "TYPE_TEXT" if head == "operation" else "DONE" if written else "1"
                return 200, {
                    "answers": {head: {"choice": choice, "confidence": 1, "probabilities": {choice: 1}}}
                }
            self.assertEqual(path, "/execute")
            self.assertFalse(body["shell"])
            args = body["command"][1:]
            commands.append(args)
            data = {"disposition": {"delivery": "delivered_verified"}}
            if args[0] == "set-value":
                written.append(args[2])
                data.update(post_state={"value": args[2]}, details={"value": args[2]})
            if args[0] == "snapshot":
                data.update(
                    app="Fixture",
                    window={"id": "w1", "title": "Document"},
                    complete=True,
                    tree={
                        "role": "window",
                        "children": [
                            node(
                                role="textfield",
                                name="Title",
                                value=written[-1] if written else "PRIVATE-VALUE",
                                available_actions=["SetValue"],
                            )
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
                "TYPESAFE_API_KEY": "jev-key",
                "TYPESAFE_BASE_URL": url + "/decisions",
                "TEXT_MODEL_API_KEY": "writer-key",
                "TEXT_MODEL": "overridden-model" if use_flags else "writer-fixture",
                "TEXT_MODEL_BASE_URL": "invalid overridden endpoint" if use_flags else url + "/writer",
            }
            model_flags = (
                ["--text-model", "writer-fixture", "--text-model-url", url + "/writer"] if use_flags else []
            )
            process = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "jev_desktop",
                    "--vm-url",
                    url,
                    "--bin",
                    "/vm/binary",
                    *model_flags,
                    "--no-values",
                    "--app",
                    "Fixture",
                    "给标题栏填写月球主题标题",
                ],
                env=env,
                capture_output=True,
                text=True,
                timeout=10,
                cwd=tempfile.gettempdir(),
            )
        self.assertEqual(process.returncode, 0, process.stdout + process.stderr)
        self.assertEqual(process.stderr, "")
        self.assertEqual(written, [content])
        self.assertEqual([call[0] for call in requests], ["jev", "jev", "writer", "jev"])
        self.assertEqual(
            [call[2] for call in requests],
            ["Bearer jev-key", "Bearer jev-key", "Bearer writer-key", "Bearer jev-key"],
        )
        writer = requests[2][1]
        self.assertEqual(writer["model"], "writer-fixture")
        self.assertEqual(json.loads(writer["messages"][1]["content"])["goal"], "给标题栏填写月球主题标题")
        self.assertNotIn("PRIVATE-VALUE", json.dumps(requests))
        self.assertNotIn(content, process.stdout)
        events = [json.loads(line) for line in process.stdout.splitlines()]
        self.assertEqual(events[0]["turn"]["text_source"], "text_model")
        self.assertEqual(events[0]["turn"]["post_state"]["value"], "[redacted]")
        self.assertEqual(events[-1]["stop"], "done")
        self.assertEqual(sum(args[0] == "set-value" for args in commands), 1)

    def test_missing_text_model_configuration_fails_before_desktop_commands(self):
        env = {
            **os.environ,
            "PYTHONPATH": str(Path(__file__).resolve().parents[1] / "src"),
            "TYPESAFE_API_KEY": "fixture",
            "TEXT_MODEL_API_KEY": "",
            "TEXT_MODEL_BASE_URL": "",
            "TEXT_MODEL": "",
        }
        process = subprocess.run(
            [sys.executable, "-m", "jev_desktop", "--text-model", "writer", "goal"],
            env=env,
            capture_output=True,
            text=True,
            timeout=10,
        )
        self.assertEqual(process.returncode, 1)
        self.assertIn("TEXT_MODEL_API_KEY unset", process.stdout)
        self.assertNotIn("SPAWN_FAILED", process.stdout)
