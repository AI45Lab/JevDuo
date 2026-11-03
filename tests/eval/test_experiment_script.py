"""Named experiment entrypoint and artifact layout."""

import json
import os
import subprocess
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "run_experiment.sh"


def test_dry_run_creates_manifest_and_batch_log(tmp_path):
    env = {**os.environ, "TANDEM_EXPERIMENT_ROOT": str(tmp_path)}
    result = subprocess.run(
        [
            "bash", str(SCRIPT),
            "--experiment", "kimi12-retry-20261030",
            "--tasks", "024,029",
            "--concurrency", "12",
            "--slow-model", "kimi-k3",
            "--slow-key-ids", "1,2,3,4",
            "--max-steps", "500",
            "--max-wall", "14400",
            "--dry-run",
        ],
        capture_output=True,
        text=True,
        env=env,
    )
    assert result.returncode == 0, result.stderr
    experiment = tmp_path / "kimi12-retry-20261030"
    manifest = json.loads((experiment / "manifest.json").read_text())
    assert manifest["experiment"] == "kimi12-retry-20261030"
    assert manifest["tasks"] == ["024", "029"]
    assert manifest["concurrency"] == 12
    assert manifest["slow_model"] == "kimi-k3"
    assert manifest["slow_key_ids"] == [1, 2, 3, 4]
    assert manifest["max_steps"] == 500
    assert manifest["max_wall_seconds"] == 14400.0
    assert manifest["watch_timeout_seconds"] == 15000.0
    assert manifest["git_describe"]
    assert "dry-run" in (experiment / "batch.log").read_text()


def test_invalid_experiment_name_is_rejected(tmp_path):
    env = {**os.environ, "TANDEM_EXPERIMENT_ROOT": str(tmp_path)}
    result = subprocess.run(
        ["bash", str(SCRIPT), "--experiment", "bad name", "--tasks", "024", "--dry-run"],
        capture_output=True,
        text=True,
        env=env,
    )
    assert result.returncode == 2
    assert "invalid experiment name" in result.stderr
    assert not (tmp_path / "bad name").exists()


def test_dry_run_selects_goal_mode_without_changing_script_default(tmp_path):
    result = subprocess.run([
        "bash", str(SCRIPT), "--experiment", "goal-smoke", "--tasks", "008", "--execution-mode", "goal",
        "--stage-max-actions", "2", "--dry-run"], capture_output=True, text=True,
        env={**os.environ, "TANDEM_EXPERIMENT_ROOT": str(tmp_path)})
    assert result.returncode == 0, result.stderr
    manifest = json.loads((tmp_path / "goal-smoke/manifest.json").read_text())
    assert manifest["execution_mode"] == "goal" and manifest["stage_max_actions"] == 2


def test_dry_run_uses_example_when_private_config_is_missing(tmp_path):
    env = {
        **os.environ,
        "TANDEM_EXPERIMENT_ROOT": str(tmp_path),
        "TANDEM_CONFIG_FILE": str(tmp_path / "missing.json"),
    }
    result = subprocess.run(
        ["bash", str(SCRIPT), "--experiment", "ci-dry-run", "--tasks", "024", "--dry-run"],
        capture_output=True,
        text=True,
        env=env,
    )
    assert result.returncode == 0, result.stderr
    assert (tmp_path / "ci-dry-run" / "manifest.json").is_file()
