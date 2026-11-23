"""Finite decision protocol and cluster credential routing; no hosted requests."""

from __future__ import annotations

import copy
import json
import os
import unittest
from pathlib import Path
from unittest.mock import patch

from northcua.agents.desktop.subagents.jev_chat import (
    ChatDecisionSettings,
    chat_endpoint,
    parse_answer,
    validate_questions,
)
from northcua.core.config_base import GuiJevDecisionConfig

import appcfg
import settings
import task_run


class DecisionProtocolTest(unittest.TestCase):
    def setUp(self):
        self.questions = {"target": {"type": "choice", "criteria": {"1": "Body", "DONE": "Verified"}}}
        self.answer = {"type": "choice", "choice": "1", "confidence": 0.8,
                       "probabilities": {"1": 0.8, "DONE": 0.2}}

    def response(self, answer=None, *, content=None, reason="stop"):
        if content is None:
            content = json.dumps({"answers": {"target": answer or self.answer}})
        return {"choices": [{"finish_reason": reason, "message": {"role": "assistant", "content": content}}]}

    def test_valid_answer_keeps_actual_probabilities_without_synthetic_one_hot(self):
        result = parse_answer(self.response(), self.questions)
        self.assertEqual(result, {"answers": {"target": self.answer}})
        risk = {"destructive": {"type": "noul"}}
        content = json.dumps({"answers": {"destructive": {"type": "noul", "noul": 0.25}}})
        result = parse_answer(self.response(content=content), risk)
        self.assertEqual(result["answers"]["destructive"]["noul"], 0.25)

    def test_bad_labels_probabilities_fields_and_types_fail_closed(self):
        for changes in (
            {"choice": "CLICK"}, {"choice": ["1"]}, {"type": "text"},
            {"probabilities": {"1": 0.3}}, {"probabilities": {"1": 0.5, "invented": 0.5}},
            {"probabilities": {"DONE": 1}}, {"confidence": True}, {"confidence": 1.1},
            {"probabilities": {"1": float("nan")}}, {"explanation": "extra output"},
        ):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                parse_answer(self.response({**self.answer, **changes}), self.questions)

    def test_truncation_tools_fences_duplicate_keys_and_wrong_questions_are_rejected(self):
        cases = [self.response(reason="length"), self.response(reason="tool_calls"),
                 self.response(content='```json\n{"answers":{}}\n```'),
                 self.response(content='{"answers":{},"answers":{}}'),
                 self.response(content='{"answers":{"other":{}}}'),
                 self.response(content='{"answers":{},"extra":1}'),
                 self.response(content="")]
        tool = self.response()
        tool["choices"][0]["message"]["tool_calls"] = [{"id": "not-an-action-channel"}]
        cases.append(tool)
        for response in cases:
            with self.subTest(response=response), self.assertRaises(ValueError):
                parse_answer(response, self.questions)

    def test_request_validation_and_endpoint_do_not_guess_or_rewrite_gateway(self):
        self.assertIs(validate_questions({"questions": self.questions}), self.questions)
        for question in ({}, {"q": {"type": "text"}}, {"q": {"type": "choice", "criteria": {}}}):
            with self.subTest(question=question), self.assertRaises(ValueError):
                validate_questions({"questions": question})
        for base in ("http://100.103.152.177:8080/v1", "http://100.103.152.177:8080/v1/chat/completions/"):
            self.assertEqual(chat_endpoint(base), "http://100.103.152.177:8080/v1/chat/completions")
        for base in ("file:///tmp/key", "https://user:password@host/v1", "http://host/v1?key=private"):
            with self.subTest(base=base), self.assertRaises(ValueError):
                chat_endpoint(base)

    def test_chat_configuration_needs_no_typesafe_secret_and_hides_its_own_key(self):
        config = GuiJevDecisionConfig(backend="chat_completions", model="k3.1-pro", base_url="http://gateway/v1")
        with patch.dict(os.environ, {"JEVDUO_JEV_CHAT_API_KEY": "private-chat-secret"}):
            connection = ChatDecisionSettings.from_env(config)
            self.assertNotIn("private-chat-secret", repr(connection))
            self.assertEqual(connection.api_key, "private-chat-secret")
            self.assertEqual(task_run._redact("a private-chat-secret b"), "a *** b")
        with patch.dict(os.environ):
            os.environ.pop("JEVDUO_JEV_CHAT_API_KEY", None)
            with self.assertRaisesRegex(ValueError, "unset"):
                ChatDecisionSettings.from_env(config)


class ChatSpecTest(unittest.TestCase):
    def fixture(self):
        return {
            "models": {"default": "slow", "apis": {
                "slow": {"model": "different-slow", "base_url": "http://slow/v1"},
                "k3.1-pro": {"model": "k3.1-pro", "base_url": "http://100.103.152.177:8080/v1",
                             "api_key_env": "TEST_SELECTOR_KEY", "timeout_seconds": 70, "max_retries": 0,
                             "reasoning_effort": "off", "direct_api": True},
            }},
            "harness": {"orchestration": {
                "max_cycles": 20, "max_seconds": 60, "max_model_calls": 100, "max_tool_calls": 100,
                "gui_jev": {"enabled": True,
                            "decision": {"backend": "chat_completions", "model": "k3.1-pro"}},
            }},
            "benchmark": {"name": "osworld21", "task_root": "/r", "vm": "/v"}, "log": {},
        }

    def render(self, config):
        return settings.render_spec(config, run_id="test", job_id=0, tasks=["001"], api_name="slow",
                                    api=config["models"]["apis"]["slow"], run_root=Path("/tmp/test"))

    def test_native_gateway_http_opt_in_is_forwarded_in_spec(self):
        config = self.fixture()
        config["harness"]["orchestration"]["gui_jev"]["decision"] = {"backend": "typesafe"}
        env = {"TYPESAFE_API_KEY": "private-native-value", "TYPESAFE_MODEL": "jev-latest",
               "TYPESAFE_BASE_URL": "http://100.103.152.177:8080/v1/systemone",
               "TYPESAFE_ALLOW_HTTP": "1"}
        with patch.dict(os.environ, env, clear=True):
            spec = self.render(config)
        self.assertEqual(spec["subagent_env"], env)
        self.assertNotIn("private-native-value", json.dumps(settings.masked(spec)))

    def test_independent_alias_uses_existing_model_entry_and_private_env_only(self):
        config = self.fixture()
        before = copy.deepcopy(config)
        with patch.dict(os.environ, {"TEST_SELECTOR_KEY": "private-selector-value"}):
            spec = self.render(config)
        self.assertEqual(config, before)
        self.assertEqual(spec["model"]["model"], "different-slow")
        resolved = spec["harness"]["orchestration"]["gui_jev"]["decision"]
        self.assertEqual(resolved["model"], "k3.1-pro")
        self.assertEqual(resolved["base_url"], "http://100.103.152.177:8080/v1")
        self.assertEqual(resolved["timeout_seconds"], 70)
        self.assertEqual(resolved["max_retries"], 0)
        self.assertEqual(resolved["reasoning_effort"], "off")
        self.assertEqual(spec["subagent_env"]["JEVDUO_JEV_CHAT_API_KEY"], "private-selector-value")
        app = appcfg.app_config(vm_ip="vm", server_port=5000, max_steps=10,
                                artifact_root=Path("/tmp/artifacts"), harness=spec["harness"])
        self.assertNotIn("private-selector-value", json.dumps(app))
        self.assertNotIn("private-selector-value", json.dumps(settings.masked(spec)))
        self.assertEqual(GuiJevDecisionConfig.model_validate(resolved).backend, "chat_completions")

    def test_bad_alias_or_missing_key_fails_before_job_or_vm(self):
        config = self.fixture()
        config["harness"]["orchestration"]["gui_jev"]["decision"]["model"] = "absent"
        with self.assertRaisesRegex(settings.ConfigError, "未知模型"):
            self.render(config)
        config = self.fixture()
        with patch.dict(os.environ):
            os.environ.pop("TEST_SELECTOR_KEY", None)
            with self.assertRaisesRegex(settings.ConfigError, "TEST_SELECTOR_KEY"):
                self.render(config)

    def test_disabled_plugin_does_not_require_the_selector_credentials(self):
        config = self.fixture()
        config["harness"]["orchestration"]["gui_jev"]["enabled"] = False
        spec = self.render(config)
        self.assertNotIn("JEVDUO_JEV_CHAT_API_KEY", spec["subagent_env"])
