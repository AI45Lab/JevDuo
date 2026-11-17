"""Exercise package selection without rebuilding the real environment or using the network."""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


class SetupEnvTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        (self.root / "scripts").mkdir()
        self.script = self.root / "scripts/setup_env.sh"
        shutil.copy2(Path(__file__).resolve().parents[1] / "scripts/setup_env.sh", self.script)
        upstream = self.root / "src/upstream"
        upstream.mkdir(parents=True)
        (upstream / "pyproject.toml").write_text("[project]\nname = 'fixture'\n")
        self.log = self.root / "uv-calls.jsonl"
        self.uv = self.root / "fake-uv"
        self.uv.write_text(
            f"#!{sys.executable}\n"
            "import json, os, sys\n"
            "from pathlib import Path\n"
            "args = sys.argv[1:]\n"
            "if args == ['--version']:\n"
            "    print('uv 0.13.6')\n"
            "    raise SystemExit(0)\n"
            "with Path(os.environ['FAKE_UV_LOG']).open('a') as out:\n"
            "    out.write(json.dumps(args) + '\\n')\n"
            "if args[0] == 'sync':\n"
            "    python = Path(os.environ['UV_PROJECT_ENVIRONMENT']) / 'bin/python'\n"
            "    python.parent.mkdir(parents=True)\n"
            "    python.write_text('#!/bin/sh\\ncat >/dev/null\\n')\n"
            "    python.chmod(0o755)\n"
        )
        self.uv.chmod(0o755)
        self.default = self.root / "jev_tools/jev-desktop"

    def package(self, path):
        (path / "src/jev_desktop").mkdir(parents=True)
        (path / "pyproject.toml").write_text("[project]\nname = 'jev-desktop'\n")
        (path / "src/jev_desktop/__init__.py").write_text("")

    def invoke(self, package=None):
        env = {k: v for k, v in os.environ.items() if k != "JEVDUO_JEV_PACKAGE"}
        env.update(JEVDUO_UV=str(self.uv), FAKE_UV_LOG=str(self.log))
        if package is not None:
            env["JEVDUO_JEV_PACKAGE"] = str(package)
        return subprocess.run(
            ["bash", str(self.script)], cwd=self.root / "scripts", env=env,
            text=True, capture_output=True, timeout=15,
        )

    def installed_package(self):
        calls = [json.loads(line) for line in self.log.read_text().splitlines()]
        installs = [call for call in calls if call[:2] == ["pip", "install"]]
        self.assertEqual(len(installs), 1)
        self.assertIn("--no-deps", installs[0])
        return installs[0][installs[0].index("-e") + 1]

    def test_installs_in_repository_package_by_default(self):
        self.package(self.default)
        result = self.invoke()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.installed_package(), str(self.default))

    def test_absolute_override_replaces_missing_default(self):
        custom = self.root / "custom/jev"
        self.package(custom)
        result = self.invoke(custom)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.installed_package(), str(custom))

    def test_relative_override_is_resolved_from_repository_root(self):
        custom = self.root / "custom/jev"
        self.package(custom)
        result = self.invoke("custom/jev")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.installed_package(), str(custom))

    def assert_rejects_without_deleting_environment(self):
        venv = self.root / "venv"
        venv.mkdir()
        marker = venv / "existing-environment"
        marker.write_text("keep")
        result = self.invoke()
        self.assertEqual(result.returncode, 2)
        self.assertIn("缺少完整的 jev-desktop 包", result.stderr)
        self.assertEqual(marker.read_text(), "keep")
        self.assertFalse(self.log.exists())

    def test_missing_package_is_rejected_before_removing_venv(self):
        self.assert_rejects_without_deleting_environment()

    def test_metadata_without_sources_is_rejected_before_removing_venv(self):
        self.default.mkdir(parents=True)
        (self.default / "pyproject.toml").write_text("[project]\nname = 'jev-desktop'\n")
        self.assert_rejects_without_deleting_environment()
