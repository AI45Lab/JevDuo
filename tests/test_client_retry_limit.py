"""Check SDK request counts, rather than only configuration values."""

import unittest
from unittest.mock import patch

import httpx
from nexau.archs.llm.llm_config import LLMConfig
from openai import InternalServerError, OpenAI

from task_run import _install_client_retry_limit


class ClientRetryLimitTest(unittest.TestCase):
    def test_zero_disables_hidden_sdk_retries(self):
        requests = []

        def respond(request):
            requests.append(request)
            return httpx.Response(503, json={"error": {"message": "scripted outage"}})

        with patch.object(LLMConfig, "to_client_kwargs", LLMConfig.to_client_kwargs):
            _install_client_retry_limit()
            config = LLMConfig(model="scripted", api_key="test-only",
                               base_url="https://unused.invalid/v1", max_retries=0)
            with OpenAI(**config.to_client_kwargs(), http_client=httpx.Client(
                transport=httpx.MockTransport(respond),
            )) as client:
                with self.assertRaises(InternalServerError):
                    client.chat.completions.create(
                        model="scripted", messages=[{"role": "user", "content": "test"}],
                    )
        self.assertEqual(len(requests), 1)

    def test_idempotent_and_preserves_nonzero_retry_setting(self):
        with patch.object(LLMConfig, "to_client_kwargs", LLMConfig.to_client_kwargs):
            _install_client_retry_limit()
            installed = LLMConfig.to_client_kwargs
            _install_client_retry_limit()
            self.assertIs(LLMConfig.to_client_kwargs, installed)
            config = LLMConfig(model="scripted", api_key="test-only",
                               base_url="https://unused.invalid/v1", max_retries=2)
            self.assertEqual(config.to_client_kwargs()["max_retries"], 2)
