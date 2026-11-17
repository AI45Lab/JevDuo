"""Framework transport boundaries; no VM, Jev model, or native process."""

import asyncio
import json
import unittest
from unittest.mock import patch

import httpx
import pytest

pytest.importorskip("jev_desktop")

from jev_desktop import DesktopConfig
from northcua.agents.desktop.subagents.jev_environment import JevEnvironment, snapshot_timeout
from northcua.core.config import DesktopPolicyConfig


class ExecutionTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.environment = JevEnvironment(
            DesktopPolicyConfig(backend="osworld", osworld={"vm_ip": "current-vm", "server_port": 5000}),
            DesktopConfig(binary="/vm/agent-desktop"),
        )

    async def invoke(self, handler, argv, timeout=30):
        client_type = httpx.AsyncClient
        with patch.object(
            httpx,
            "AsyncClient",
            side_effect=lambda **kwargs: client_type(transport=httpx.MockTransport(handler), **kwargs),
        ):
            return await self.environment.execute({"argv": argv, "timeout": timeout})

    async def test_recovered_snapshots_receive_their_actual_budget(self):
        for native_ms, supplied, expected in [
            (20000, 30, 30),
            (40000, 30, 45),
            (60000, 35, 65),
            (300000, 30, 305),
        ]:
            with self.subTest(native_ms=native_ms):
                requests = []

                def server(request, requests=requests, expected=expected):
                    requests.append(request)
                    self.assertEqual(request.extensions["timeout"]["read"], expected)
                    return httpx.Response(
                        200, json={"output": '{"ok":true,"data":{"complete":true}}', "returncode": 0}
                    )

                result = await self.invoke(
                    server,
                    ["--session", "s", "--agent-id", "a", "snapshot", "--timeout-ms", str(native_ms)],
                    supplied,
                )
                self.assertTrue(result["ok"])
                self.assertEqual(len(requests), 1)
                self.assertEqual(result["execution_diagnostic"]["snapshot_timeout_ms"], native_ms)
                self.assertTrue(result["execution_diagnostic"]["complete"])

    def test_snapshot_option_parsing_does_not_treat_written_text_as_a_command(self):
        self.assertEqual(snapshot_timeout(["--headed", "snapshot", "--timeout-ms=60000"]), 60000)
        self.assertIsNone(snapshot_timeout(["set-value", "@s:e1", "snapshot", "--timeout-ms", "60000"]))
        self.assertIsNone(snapshot_timeout(["snapshot"]))
        for args in (
            ["--timeout-ms"],
            ["--timeout-ms", "no"],
            ["--timeout-ms", "0"],
            ["--timeout-ms", "300001"],
            ["--timeout-ms=20", "--timeout-ms=30"],
        ):
            with self.subTest(args=args), self.assertRaises(ValueError):
                snapshot_timeout(["snapshot", *args])

    async def test_unknown_write_delivery_is_not_retried_and_keeps_no_raw_error(self):
        calls = []

        def server(request):
            calls.append(request)
            self.assertEqual(request.extensions["timeout"]["read"], 30)
            raise httpx.ReadTimeout("private text and secret endpoint", request=request)

        result = await self.invoke(server, ["set-value", "@s:e1", "private text"])
        self.assertEqual(len(calls), 1)
        self.assertEqual(
            result["error"]["disposition"], {"delivery": "delivery_uncertain", "retry": "unsafe"}
        )
        self.assertEqual(result["execution_diagnostic"]["failure_kind"], "timeout")
        self.assertEqual(result["execution_diagnostic"]["error_type"], "ReadTimeout")
        self.assertNotIn("private", json.dumps(result))
        self.assertNotIn("endpoint", json.dumps(result))

    async def test_response_failure_stages_are_distinguishable_without_logging_payloads(self):
        cases = [
            (httpx.Response(503, text="private response"), "http_status"),
            (httpx.Response(200, text="private malformed json"), "response_json"),
            (httpx.Response(200, json={"output": "private"}), "execution_envelope"),
            (httpx.Response(200, json={"output": "private", "returncode": 1}), "native_output"),
            (httpx.Response(200, json={"output": '{"ok":true}', "returncode": True}), "execution_envelope"),
        ]
        for response, kind in cases:
            with self.subTest(kind=kind):
                calls = []

                def server(request, calls=calls, response=response):
                    calls.append(request)
                    return response

                result = await self.invoke(server, ["snapshot", "--timeout-ms", "40000"])
                self.assertFalse(result["ok"])
                self.assertEqual(result["execution_diagnostic"]["failure_kind"], kind)
                self.assertEqual(result["execution_diagnostic"]["http_status"], response.status_code)
                self.assertNotIn("private", json.dumps(result))
                self.assertEqual(len(calls), 1)

    async def test_identity_failure_remains_visible_in_native_evidence(self):
        def server(request):
            return httpx.Response(
                200,
                json={
                    "output": json.dumps(
                        {
                            "ok": True,
                            "data": {"identity_unproven": True, "complete": False, "truncated": True},
                        }
                    ),
                    "returncode": 0,
                },
            )

        result = await self.invoke(server, ["snapshot", "--timeout-ms", "60000"])
        diagnostic = result["execution_diagnostic"]
        self.assertTrue(diagnostic["identity_unproven"])
        self.assertFalse(diagnostic["complete"])
        self.assertTrue(diagnostic["truncated"])

    async def test_parent_cancellation_does_not_retry_or_turn_into_transport_success(self):
        entered = asyncio.Event()
        calls = []

        async def server(request):
            calls.append(request)
            entered.set()
            await asyncio.Event().wait()

        task = asyncio.create_task(self.invoke(server, ["snapshot", "--timeout-ms", "60000"]))
        await asyncio.wait_for(entered.wait(), 1)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertEqual(len(calls), 1)
