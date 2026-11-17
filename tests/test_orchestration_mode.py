"""Desktop has one execution path, including driver and frozen-worker boundaries."""

import argparse
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import appcfg
import entry
import settings
import task_run


class OrchestrationModeTest(unittest.TestCase):
    def test_invalid_mode_fails_before_credentials_jobs_or_vm_creation(self):
        for orchestration in (None, {}, {"enabled": False}, {"enabled": True}):
            with self.subTest(orchestration=orchestration):
                harness = {} if orchestration is None else {"orchestration": orchestration}
                with patch.object(settings, "_key") as key:
                    with self.assertRaisesRegex(ValueError, "orchestration"):
                        settings.render_spec(
                            {"harness": harness, "benchmark": {}}, run_id="test", job_id=0,
                            tasks=["001"], api_name="test", api={}, run_root=Path("/unused"),
                        )
                    key.assert_not_called()
                with patch.object(entry, "Vm") as vm:
                    with self.assertRaisesRegex(ValueError, "orchestration"):
                        entry.run_job({"harness": harness})
                    vm.assert_not_called()

    def test_generated_limits_share_the_same_cycle_unit(self):
        harness = {"orchestration": {
            "max_cycles": 20, "max_seconds": 60, "max_model_calls": 100, "max_tool_calls": 1000,
        }}
        config = appcfg.app_config(
            vm_ip="unused", server_port=5000, max_steps=3, artifact_root=Path("/unused"), harness=harness,
        )
        self.assertEqual(config["orchestration"]["max_cycles"], 3)
        self.assertNotIn("enabled", config["orchestration"])
        self.assertEqual(harness["orchestration"]["max_cycles"], 20)


class ActorMetadataTest(unittest.IsolatedAsyncioTestCase):
    async def test_legacy_actor_result_is_rejected_and_application_still_stops(self):
        from northcua.agents.desktop.application import NorthCUAApplication

        for metadata in ({}, {"step_unit": "model_tool_step", "usage": {}}):
            with self.subTest(metadata=metadata), tempfile.TemporaryDirectory() as tmp:
                app = SimpleNamespace(
                    artifact_store=SimpleNamespace(root=Path(tmp)),
                    run_query=AsyncMock(
                        return_value=SimpleNamespace(terminal_decision={"metadata": metadata}),
                    ),
                    stop=AsyncMock(),
                )
                args = argparse.Namespace(
                    app_config="unused", log_dir=tmp, session_id="test", query="test", max_steps=1,
                    no_tool_pair_repair=True,
                )
                with (
                    patch.object(NorthCUAApplication, "from_app_config", return_value=app),
                    patch.object(task_run, "_install_client_retry_limit"),
                    patch.object(task_run, "_install_tool_pair_repair"),
                ):
                    with self.assertRaisesRegex(RuntimeError, "orchestration_cycle"):
                        await task_run.run(args)
                app.stop.assert_awaited_once()
