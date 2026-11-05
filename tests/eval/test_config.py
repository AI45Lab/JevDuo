"""infra/config.json：唯一配置文件（无占位符、绝对路径、必填键齐全、角色化命名）。

守的是三条纪律：
1. 三段结构 harness / eval / log 齐全，且**没有** `_comment` 之类的说明字段；
2. 慢模型（大脑）与快模型（小脑）都是**命名 API 登记表**，default 必须在 apis 里；
3. 路径全绝对、凭据内联、endpoint 可留空走发现文件。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from jevduo_eval.config_schema import ConfigError, api_names, select_api
from jevduo_eval.rjob import load_config

CONFIG_PATH = Path(__file__).resolve().parents[2] / "infra" / "config.example.json"


def _walk_keys(node) -> list[str]:
    """递归收集所有键名（查有没有混进说明性字段）。"""
    if isinstance(node, dict):
        return [key for key in node] + [k for value in node.values() for k in _walk_keys(value)]
    if isinstance(node, list):
        return [k for value in node for k in _walk_keys(value)]
    return []


def test_example_config_has_three_sections_and_no_comment_fields():
    config = load_config(CONFIG_PATH)
    assert set(config) == {"harness", "eval", "log"}
    # 说明性字段（_comment 之类）不留在配置里：注释属于文档，不属于配置
    assert [key for key in _walk_keys(config) if key.startswith("_")] == []


def test_slow_model_entries_use_known_knobs():
    """配置里的旋钮必须落在 harness 认识的档位内，否则运行期才炸。"""
    config = load_config(CONFIG_PATH)
    for name, api in config["harness"]["slow_model"]["apis"].items():
        assert api["base_url"].startswith("http"), f"{name}: base_url"
        assert api["reasoning_effort"] in {
            None, "off", "low", "medium", "high", "max",
        }, f"{name}: reasoning_effort"
        assert api["max_tokens"] > 0 and api["timeout_seconds"] > 0, f"{name}: 旋钮必须为正"


def test_slow_model_rejects_unknown_reasoning_effort(tmp_path):
    config = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    name = next(iter(config["harness"]["slow_model"]["apis"]))
    config["harness"]["slow_model"]["apis"][name]["reasoning_effort"] = "xhigh"
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps(config), encoding="utf-8")
    with pytest.raises(ConfigError, match="reasoning_effort"):
        load_config(bad)


def test_models_are_named_api_registries():
    config = load_config(CONFIG_PATH)
    slow = config["harness"]["slow_model"]
    fast = config["harness"]["fast_model"]
    # default 必须指向登记表里的某个条目
    assert slow["default"] in slow["apis"]
    assert fast["default"] in fast["apis"]
    assert api_names(config, "slow_model") == sorted(slow["apis"])
    assert api_names(config, "fast_model") == sorted(fast["apis"])
    # 慢模型条目是凭据三件套 + 旋钮；快模型条目声明自己的传输协议
    for api in slow["apis"].values():
        assert set(api) >= {"base_url", "api_key", "model"}
    for api in fast["apis"].values():
        assert api["backend"] in {"jev", "kev", "planner"}
        if api["backend"] != "planner":
            assert api.get("endpoint") or api.get("endpoint_file")


def test_paths_are_absolute_and_credentials_inlined():
    config = load_config(CONFIG_PATH)
    PATH_KEYS = (
        "eval.repo_root", "eval.deploy_root", "log.results_root",
        "eval.benchmarks.osworld1.root", "eval.benchmarks.osworld1.vm",
        "eval.benchmarks.osworld2.root", "eval.benchmarks.osworld2.vm",
        "eval.benchmarks.osworld2.asset_root",
        "harness.fast_model.image_stage.write", "harness.fast_model.image_stage.read",
    )
    for key in PATH_KEYS:
        value = config
        for part in key.split("."):
            value = value[part]
        assert value.startswith("/"), f"{key} 必须是绝对路径: {value}"
    assert config["eval"]["inner_image"].startswith("registry.")
    assert config["eval"]["rjob"]["image"].startswith("registry.")
    assert "auths" in config["eval"]["registry"]
    assert config["log"]["results_root"].endswith("jevduo/rjob_logs")
    assert config["eval"]["repo_root"].endswith("/jevduo")


def test_mounts_cover_both_volumes_without_duplicate_sources():
    """两个卷各挂一次；同一 source 挂两次会被平台拒（"the value of source path repeats"）。

    evobox 卷在 worker 里挂 canonical 视角——OSW2.1 的 env.sh 与 prepared 软链层写的是
    canonical 路径，只有这个视角能解析；旧环境的 env.sh 是自相对的，两种视角都行。
    adpc 卷（部署副本与结果目录）维持 user 视角。
    """
    config = load_config(CONFIG_PATH)
    mounts = config["eval"]["mounts"]
    sources = [m["source"] for m in mounts]
    assert len(sources) == len(set(sources)), f"同一 source 不能挂两次: {mounts}"
    targets = {m["target"] for m in mounts}
    assert "/mnt/shared-storage-gpfs2/evobox-share-gpfs2" in targets
    assert "/mnt/shared-storage-user/adpc-share" in targets
    # 配置里引用的 evobox 路径必须与 worker 的挂载视角一致
    benchmarks = config["eval"]["benchmarks"]
    for key in ("osworld1", "osworld2", "osworld21"):
        for field in ("root", "vm", "asset_root"):
            value = benchmarks[key].get(field)
            if value:
                assert value.startswith("/mnt/shared-storage-gpfs2/evobox-share-gpfs2/"), value
    assert config["harness"]["fast_model"]["image_stage"]["write"].startswith(
        "/mnt/shared-storage-gpfs2/evobox-share-gpfs2/")


def test_budget_and_route_come_from_config():
    config = load_config(CONFIG_PATH)
    assert config["harness"]["budget"]["max_steps"] > 0
    assert config["harness"]["budget"]["max_wall_seconds"] > 0
    assert config["harness"]["route"]["policy"] in {"auto", "gui", "cli"}


def test_load_config_rejects_missing_section(tmp_path):
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps({"eval": {}, "log": {}}), encoding="utf-8")
    with pytest.raises(ConfigError, match="必填键"):
        load_config(bad)


def test_load_config_rejects_empty_image(tmp_path):
    config = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    config["eval"]["rjob"]["image"] = ""
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps(config), encoding="utf-8")
    with pytest.raises(ConfigError, match="image"):
        load_config(bad)


def test_load_config_rejects_unknown_fast_backend(tmp_path):
    config = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    name = next(iter(config["harness"]["fast_model"]["apis"]))
    config["harness"]["fast_model"]["apis"][name]["backend"] = "omni"
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps(config), encoding="utf-8")
    with pytest.raises(ConfigError, match="backend"):
        load_config(bad)


def test_select_api_picks_named_entry_or_default():
    config = load_config(CONFIG_PATH)
    slow_default, slow_api = select_api(config, "slow_model")
    assert slow_default == config["harness"]["slow_model"]["default"]
    assert slow_api["model"]
    # 按名字挑另一套 API（换大脑/换小脑的入口）
    fast_name, fast_api = select_api(config, "fast_model", "kev")
    assert fast_name == "kev"
    assert fast_api["backend"] == "kev"


def test_select_api_error_lists_available_names():
    config = load_config(CONFIG_PATH)
    with pytest.raises(ConfigError, match="可选"):
        select_api(config, "fast_model", "no-such-api")


def test_osworld21_section_is_validated():
    config = load_config(CONFIG_PATH)
    section = config["eval"]["benchmarks"]["osworld21"]
    for key in ("root", "vm", "asset_root", "shared_env"):
        assert section[key].startswith("/"), f"{key} 必须是绝对路径"
    assert section["proxy"].startswith("http")
    assert isinstance(section["volume_size"], int) and section["volume_size"] > 0


def test_missing_osworld21_paths_are_reported(tmp_path):
    config = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    del config["eval"]["benchmarks"]["osworld21"]["shared_env"]
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps(config), encoding="utf-8")
    with pytest.raises(ConfigError, match="osworld21.shared_env"):
        load_config(bad)


def test_osworld21_inner_image_must_be_release_pinned(tmp_path):
    config = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    config["eval"]["benchmarks"]["osworld21"]["inner_image"] = "registry.example/osw21:latest"
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps(config), encoding="utf-8")
    with pytest.raises(ConfigError, match="latest"):
        load_config(bad)


def test_cli_model_flags_reach_spec_overrides(monkeypatch):
    """启动命令按名字切大脑/小脑：--slow-model / --fast-model 进 spec 的 models 段。"""
    from jevduo_eval.__main__ import main

    captured: dict = {}

    def fake_run_job(**kwargs):
        captured.update(kwargs)
        return {"status": "succeeded"}

    monkeypatch.setattr("jevduo_eval.rjob.run_job", fake_run_job)
    code = main(["rjob", "run", "--run-id", "r1", "--benchmark", "mock",
                 "--config", str(CONFIG_PATH),
                 "--slow-model", "kimi-k3", "--fast-model", "kev"])
    assert code == 0
    assert captured["spec_overrides"]["models"] == {"slow": "kimi-k3", "fast": "kev"}


def test_real_config_is_gitignored_with_example_committed():
    repo = Path(__file__).resolve().parents[2]
    ignore = (repo / ".gitignore").read_text(encoding="utf-8")
    assert "infra/config.json" in ignore, "含密钥的真实配置必须 gitignore"


def test_endpoint_discovery_prefers_explicit_then_file(tmp_path):
    from jevduo_eval.entry import read_endpoint_file, resolve_fast_model_endpoint

    f = tmp_path / "endpoint.json"
    f.write_text(json.dumps({"base_url": "http://10.9.9.9:8891/"}), encoding="utf-8")
    assert read_endpoint_file(str(f)) == "http://10.9.9.9:8891"
    # 显式值优先于文件
    assert resolve_fast_model_endpoint(
        {"endpoint": "http://1.2.3.4:1", "endpoint_file": str(f)}) == "http://1.2.3.4:1"
    # 为空 → 文件发现
    assert resolve_fast_model_endpoint({"endpoint": "", "endpoint_file": str(f)}) \
        == "http://10.9.9.9:8891"
    # 两者皆无 → 明确报错
    with pytest.raises(ValueError, match="endpoint_file"):
        resolve_fast_model_endpoint({"endpoint": ""})


def test_endpoint_file_symlink_view_fallback(tmp_path, monkeypatch):
    """给定路径不可读时，自动尝试另一挂载视角的等价路径（前缀翻译）。"""
    from jevduo_eval import entry as entry_mod

    user_dir = tmp_path / "user-view"
    canonical_dir = tmp_path / "canonical-view"
    real = user_dir / "endpoint.json"
    real.parent.mkdir(parents=True)
    real.write_text(json.dumps({"base_url": "http://5.5.5.5:8891"}), encoding="utf-8")

    monkeypatch.setattr(entry_mod, "_EVOBOX_USER", str(user_dir))
    monkeypatch.setattr(entry_mod, "_EVOBOX_CANONICAL", str(canonical_dir))
    # 传入的规范视角路径不存在（符号链接断裂场景）→ 翻译到 user 视角命中
    assert entry_mod.read_endpoint_file(str(canonical_dir / "endpoint.json")) == "http://5.5.5.5:8891"


def test_endpoint_file_respects_readiness_flag(tmp_path):
    """发现文件标记未就绪时快速失败，不调用半启动的服务。"""
    from jevduo_eval.entry import read_endpoint_file

    f = tmp_path / "svc.json"
    f.write_text(json.dumps({"status": "starting", "ready": False,
                             "base_url": "http://1.1.1.1:1"}), encoding="utf-8")
    with pytest.raises(ValueError, match="未就绪"):
        read_endpoint_file(str(f))
    f.write_text(json.dumps({"status": "ready", "ready": True,
                             "base_url": "http://2.2.2.2:2"}), encoding="utf-8")
    assert read_endpoint_file(str(f)) == "http://2.2.2.2:2"
