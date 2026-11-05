"""OSW2.1 私有凭据注入与公共服务配置，不调用模型或 VM。"""

from __future__ import annotations

import json
import stat
from pathlib import Path

import pytest

from jevduo_eval.config_schema import validate
from jevduo_eval.entry import _mask_secrets, run_from_spec
from jevduo_eval.rjob_spec import build_job_spec

from .rjob_fakes import CONFIG, with_paths


def test_osworld21_credentials_stay_private_and_profiles_are_attributable(tmp_path):
    config = with_paths(CONFIG, deploy_root=str(tmp_path / "deploy"))
    section = config["eval"]["benchmarks"]["osworld21"]
    section["gitlab"] = {"private_token": "private-gitlab-test-token"}
    for role in ("judge", "user_simulator"):
        section[role] = {"provider": "openai_compatible", "model": "alternative-test-model",
                         "base_url": "https://example.invalid/v1", "api_key": role + "-private-key"}
    section["judge"].update(reasoning_effort="low", max_tokens=8192)
    spec = build_job_spec(config=config, run_id="services", repo_root=tmp_path,
                          spec_overrides={"benchmark": "osworld21"})
    environment = spec["secrets"]["environment"]
    assert environment["GITLAB_PRIVATE_TOKEN"] == section["gitlab"]["private_token"]
    assert environment["OSWORLD_EVAL_MODEL_NAME"] == "alternative-test-model"
    assert environment["OSWORLD_USER_SIM_MODEL"] == "alternative-test-model"
    assert environment["OSWORLD_USER_SIM_API_KEY"] == "user_simulator-private-key"
    assert environment["OSWORLD_EVAL_MODEL_REASONING_EFFORT"] == "low"
    assert environment["OSWORLD_EVAL_MODEL_MAX_OUTPUT_TOKENS"] == "8192"
    assert spec["code"]["resources"]["judge"]["configuration"] == "alternative"
    recorded = json.dumps(_mask_secrets(spec))
    for role in ("judge", "user_simulator"):
        assert section[role]["api_key"] not in recorded
    assert section["gitlab"]["private_token"] not in recorded
    path = tmp_path / "deploy/job_specs/services.json"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert json.loads(path.read_text()) == spec


def test_osworld21_missing_private_configuration_does_not_copy_planner_key(tmp_path):
    config = with_paths(CONFIG, deploy_root=str(tmp_path / "deploy"))
    spec = build_job_spec(config=config, run_id="public", repo_root=tmp_path,
                          spec_overrides={"benchmark": "osworld21"})
    assert spec["secrets"]["environment"] == {}
    assert spec["code"]["resources"] == {}


@pytest.mark.parametrize("inline_token", ["", "inline-test-token"])
def test_osworld21_shared_gitlab_token_file(tmp_path, inline_token):
    token_path = tmp_path / "gitlab.token"
    token_path.write_text("shared-test-token\n", encoding="utf-8")
    config = with_paths(CONFIG, deploy_root=str(tmp_path / "deploy"))
    config["eval"]["benchmarks"]["osworld21"]["gitlab"] = {
        "private_token": inline_token, "private_token_file": str(token_path),
    }
    spec = build_job_spec(config=config, run_id="shared-token", repo_root=tmp_path,
                          spec_overrides={"benchmark": "osworld21"})
    assert spec["secrets"]["environment"]["GITLAB_PRIVATE_TOKEN"] == (inline_token or "shared-test-token")
    recorded = json.dumps(_mask_secrets(spec))
    assert "shared-test-token" not in recorded
    assert "inline-test-token" not in recorded
    path = tmp_path / "deploy/job_specs/shared-token.json"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_shared_gitlab_token_file_requires_absolute_path():
    path = Path(__file__).resolve().parents[2] / "infra/config.example.json"
    config = json.loads(path.read_text())
    config["eval"]["benchmarks"]["osworld21"]["gitlab"]["private_token_file"] = "relative.token"
    assert "eval.benchmarks.osworld21.gitlab.private_token_file（必须是绝对路径）" in validate(config)


def test_osworld21_direct_api_hosts_do_not_change_older_benchmarks(tmp_path):
    config = with_paths(CONFIG, deploy_root=str(tmp_path / "deploy"))
    config["eval"]["benchmarks"]["osworld21"]["no_proxy"] = "api.example.invalid"
    spec = build_job_spec(config=config, run_id="direct", repo_root=tmp_path,
                          spec_overrides={"benchmark": "osworld21"})
    assert spec["env"]["no_proxy"] == "api.example.invalid"
    old = build_job_spec(config=config, run_id="old", repo_root=tmp_path,
                         spec_overrides={"benchmark": "osworld2"})
    assert "no_proxy" not in old["env"]


@pytest.mark.parametrize("role", ["judge", "user_simulator"])
def test_configured_evaluation_api_requires_its_own_model_and_endpoint(role):
    path = Path(__file__).resolve().parents[2] / "infra/config.example.json"
    config = json.loads(path.read_text())
    config["eval"]["benchmarks"]["osworld21"][role] = {"api_key": "private-test-key"}
    problems = validate(config)
    assert f"eval.benchmarks.osworld21.{role}.base_url" in problems
    assert f"eval.benchmarks.osworld21.{role}.model" in problems


def test_credentials_are_injected_before_environment_loading(tmp_path, monkeypatch):
    from jevduo_eval import entry

    class EnvironmentReached(Exception):
        pass

    monkeypatch.delenv("GITLAB_PRIVATE_TOKEN", raising=False)

    def build_env(spec):
        import os

        assert os.environ["GITLAB_PRIVATE_TOKEN"] == "private-test-token"
        raise EnvironmentReached

    monkeypatch.setattr(entry, "build_env", build_env)
    with pytest.raises(EnvironmentReached):
        run_from_spec({"benchmark": "osworld21", "output_root": str(tmp_path),
                       "secrets": {"environment": {"GITLAB_PRIVATE_TOKEN": "private-test-token"}}})
    monkeypatch.delenv("GITLAB_PRIVATE_TOKEN")
    assert "private-test-token" not in (tmp_path / "code_version.json").read_text()
