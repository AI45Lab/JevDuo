"""rjob 客户端：submit 参数构造 / 状态解析 / watch / 一站式 run_job 生命周期。

收尾与自动删除的用例见 test_rjob_cleanup.py。
"""

from __future__ import annotations

import json

import pytest

from tandem_eval.rjob import (
    RjobClient,
    RjobError,
    build_job_spec,
    build_submit_args,
    fetch_artifacts,
    job_name,
    parse_status,
    run_job,
    watch,
)

from .rjob_fakes import CONFIG, REAL_RUNNING, REAL_SUCCEEDED, FakeRunner, with_paths


def test_parse_status_variants():
    assert parse_status("Name: x\nStatus: Succeeded") == "succeeded"
    assert parse_status("状态: 成功") == "succeeded"
    assert parse_status("phase: Running") == "running"
    assert parse_status("waiting for scheduling") == "running"
    assert parse_status("Status: Failed") == "failed"
    assert parse_status("Deleted") == "deleted"
    assert parse_status("totally opaque") == "unknown"


def test_parse_status_real_captured_outputs():
    # 真机 Succeeded 输出：状态行优先命中，不被计数字典里的 'failed': 0 干扰
    assert parse_status(REAL_SUCCEEDED) == "succeeded"
    # 运行中任务同样带 'failed': 0 计数，必须解析为 running 而非 failed
    assert parse_status(REAL_RUNNING) == "running"
    # 只有计数字典、无状态行：剔除后无关键词 → unknown（继续轮询）
    assert parse_status("task t: {'active': 1, 'failed': 0, 'pending': 0}") == "unknown"


def test_submit_args_include_mount_and_worker_entrypoint():
    args = build_submit_args(config=CONFIG, run_id="r1")
    joined = " ".join(args)
    assert "--name=tandem-r1" in joined
    assert "--charged-group=evobox_proxy" in joined
    assert "--private-machine=group" in joined
    assert "--mount=gpfs://gpfs2/share:/mnt/share" in joined
    assert "--image=reg.example/outer:1" in joined
    assert joined.endswith("/dev-deploy/infra/worker.sh /dev-deploy/job_specs/r1.json")


def test_build_job_spec_uses_config_paths(tmp_path):
    config = with_paths(CONFIG, deploy_root=str(tmp_path / "deploy"),
                        results_root=str(tmp_path / "results"))
    spec = build_job_spec(config=config, run_id="r1",
                          repo_root=tmp_path, spec_overrides={"benchmark": "osworld1"})
    assert spec["output_root"] == str(tmp_path / "results" / "r1")
    assert spec["repo_root"] == str(tmp_path / "deploy")
    assert spec["benchmark"] == "osworld1"
    assert spec["env"]["root"] == CONFIG["eval"]["benchmarks"]["osworld1"]["root"]
    assert spec["secrets"]["fast_model"]["endpoint"] == "http://10.1.1.1:8891"
    assert spec["secrets"]["slow_model"]["api_key"] == "sk-x"
    assert spec["secrets"]["registry"]["auths"]
    # 角色与厂商解耦：spec 里记的是"谁在跑"，不是"哪家的模型"
    assert spec["harness"]["models"] == {"slow": "kimi-k3", "fast": "jev"}
    assert spec["env"]["shared_env"] == CONFIG["eval"]["shared_env"]
    assert spec["harness_config"]["fast_backend"] == "jev"
    assert spec["harness_config"]["slow_max_tokens"] == 4096
    assert spec["budget"] == {"max_steps": 5, "max_wall_seconds": 60.0}
    written = json.loads((tmp_path / "deploy" / "job_specs" / "r1.json").read_text())
    assert written == spec


def test_build_job_spec_selects_models_by_name(tmp_path):
    """换大脑/小脑只改名字：地址与协议跟着选中的 API 条目走。"""
    config = with_paths(CONFIG, deploy_root=str(tmp_path / "deploy"))
    spec = build_job_spec(config=config, run_id="r1", repo_root=tmp_path,
                          spec_overrides={"benchmark": "osworld2",
                                          "models": {"slow": "kimi-k3", "fast": "kev"}})
    assert spec["harness"]["models"] == {"slow": "kimi-k3", "fast": "kev"}
    assert spec["harness_config"]["fast_backend"] == "kev"
    assert spec["secrets"]["fast_model"]["endpoint"] == ""  # kev 留空 = 走发现文件
    assert spec["secrets"]["fast_model"]["endpoint_file"].endswith("kev.json")
    assert spec["secrets"]["slow_model"]["api_key"] == "sk-x"


def test_build_job_spec_rejects_unknown_model_name(tmp_path):
    from tandem_eval.config_schema import ConfigError

    config = with_paths(CONFIG, deploy_root=str(tmp_path / "deploy"))
    with pytest.raises(ConfigError, match="可选"):
        build_job_spec(config=config, run_id="r1", repo_root=tmp_path,
                       spec_overrides={"benchmark": "mock", "models": {"fast": "nope"}})


@pytest.mark.parametrize("benchmark", ["osworld1", "osworld2", "osworld21"])
def test_real_benchmarks_carry_credentials(tmp_path, benchmark):
    """每个真实基准的 spec 都必须带凭据段。

    基准名在白名单里漏一个，secrets 就会是空的——worker 在拉 inner 镜像前
    因没有 registry 凭据直接退出（2026-10-22 跑 osworld21 时踩过）。
    """
    config = with_paths(CONFIG, deploy_root=str(tmp_path / "deploy"))
    spec = build_job_spec(config=config, run_id="r1", repo_root=tmp_path,
                          spec_overrides={"benchmark": benchmark})
    secrets = spec["secrets"]
    assert secrets["slow_model"]["api_key"]
    assert secrets["fast_model"]["endpoint_file"]
    assert secrets["registry"]["auths"]


def test_build_job_spec_for_osworld21(tmp_path):
    """OSW2.1 用自己的 runtime/env/镜像，不复用 OSW2 的。"""
    config = with_paths(CONFIG, deploy_root=str(tmp_path / "deploy"))
    spec = build_job_spec(config=config, run_id="r1", repo_root=tmp_path,
                          spec_overrides={"benchmark": "osworld21"})
    section = CONFIG["eval"]["benchmarks"]["osworld21"]
    assert spec["env"]["shared_env"] == section["shared_env"]
    assert spec["env"]["proxy"] == section["proxy"]
    assert spec["env"]["volume_size"] == section["volume_size"]
    assert spec["code"]["images"]["inner"] == section["inner_image"]
    old = build_job_spec(config=config, run_id="r2", repo_root=tmp_path,
                         spec_overrides={"benchmark": "osworld2"})
    assert old["code"]["images"]["inner"] == CONFIG["eval"]["inner_image"]


def test_build_job_spec_rejects_missing_osworld21_inner_image(tmp_path):
    config = with_paths(CONFIG, deploy_root=str(tmp_path / "deploy"))
    config["eval"]["benchmarks"]["osworld21"]["inner_image"] = ""
    with pytest.raises(ValueError, match="inner_image"):
        build_job_spec(config=config, run_id="r1", repo_root=tmp_path,
                       spec_overrides={"benchmark": "osworld21"})


def test_cli_budget_override_merges_with_config(tmp_path):
    """预算来自 config，命令行只覆盖给定的那一项。"""
    config = with_paths(CONFIG, deploy_root=str(tmp_path / "deploy"))
    spec = build_job_spec(config=config, run_id="r1", repo_root=tmp_path,
                          spec_overrides={"benchmark": "mock",
                                          "budget": {"max_steps": 3}})
    assert spec["budget"] == {"max_steps": 3, "max_wall_seconds": 60.0}


def test_goal_recovery_budget_is_taken_from_execution_config(tmp_path):
    config = with_paths(CONFIG, deploy_root=str(tmp_path / "deploy"))
    config["harness"]["execution"] = {
        "mode": "goal", "recovery_max_calls": 3, "recovery_max_seconds": 240,
    }
    spec = build_job_spec(config=config, run_id="goal", repo_root=tmp_path,
                          spec_overrides={"benchmark": "mock"})
    assert spec["harness_config"]["execution_mode"] == "goal"
    assert spec["harness_config"]["goal_recovery_max_calls"] == 3
    assert spec["harness_config"]["goal_recovery_max_seconds"] == 240


def test_watch_until_terminal_and_timeout():
    runner = FakeRunner({"get": ["running", "succeeded"]})
    client = RjobClient(runner)
    assert watch(client, "j", poll_seconds=0, sleep=lambda _: None) == "succeeded"
    always_running = FakeRunner({"get": ["running"]})
    with pytest.raises(TimeoutError):
        watch(RjobClient(always_running), "j", poll_seconds=0, timeout_seconds=0,
              sleep=lambda _: None)


def test_run_job_full_lifecycle_submits_watches_fetches_cleans(tmp_path):
    run_root = tmp_path / "rjob_logs" / "r1"  # 唯一落盘位置
    config = with_paths(CONFIG, results_root=str(tmp_path / "rjob_logs"),
                        deploy_root=str(tmp_path / "deploy"))
    script = {
        "submit": ["submitted"],
        "get": ["running", "succeeded"],
        "logs": ["log-line"],
        "stop": ["stopped"],
    }
    runner = FakeRunner(script)
    result = run_job(config=config,
                     repo_root=tmp_path, run_id="r1", spec_overrides={},
                     client=RjobClient(runner),
                     poll_seconds=0, sleep=lambda _: None, sync=False)
    assert result["status"] == "succeeded"
    assert result["run_root"] == str(run_root)
    submit_index = next(i for i, c in enumerate(runner.calls) if c[:2] == ["rjob", "submit"])
    delete_index = next(
        (i for i, c in enumerate(runner.calls[:submit_index]) if c[:2] == ["rjob", "delete"]), None)
    assert delete_index is not None, "提交前应先防御性 delete 同名残留"
    submit_call = runner.calls[submit_index]
    assert any(a.startswith("--mount=") for a in submit_call)
    # 平台日志写进 run 目录本身（唯一落盘位置）
    assert (run_root / "platform.tail.log").read_text() == "log-line"
    assert (run_root / "platform.status.txt").is_file()
    assert any(c[:2] == ["rjob", "stop"] for c in runner.calls)


def test_run_job_timeout_still_fetches_and_cleans(tmp_path):
    config = with_paths(CONFIG, results_root=str(tmp_path / "rjob_logs"),
                        deploy_root=str(tmp_path / "deploy"))
    runner = FakeRunner({"get": ["running"], "stop": ["stopped"],
                         "submit": ["ok"], "logs": ["log"]})
    result = run_job(config=config,
                     repo_root=tmp_path, run_id="r2", spec_overrides={},
                     client=RjobClient(runner),
                     poll_seconds=0, timeout_seconds=0, sleep=lambda _: None, sync=False)
    assert result["status"] == "timeout"
    assert (tmp_path / "rjob_logs" / "r2" / "platform.tail.log").is_file()
    assert any(c[:2] == ["rjob", "stop"] for c in runner.calls)
    assert result["cleanup"]["deleted"] is True, "超时也必须删 rjob"


def test_client_raises_on_nonzero_exit():
    client = RjobClient(lambda argv: (1, "boom"))
    with pytest.raises(RjobError, match="退出码 1"):
        client.get("j")




def test_fetch_artifacts_writes_platform_logs_into_run_root(tmp_path):
    run_root = tmp_path / "run"
    (run_root / "worker").mkdir(parents=True)
    (run_root / "worker" / "dockerd.log").write_text("d", encoding="utf-8")
    (run_root / "summary.json").write_text("{}", encoding="utf-8")
    runner = FakeRunner({"get": ["status"], "logs": ["logs"]})
    out = fetch_artifacts(RjobClient(runner), "j", run_root)
    assert out == run_root
    assert (run_root / "platform.status.txt").read_text() == "status"
    assert (run_root / "platform.tail.log").read_text() == "logs"
    assert (run_root / "worker" / "dockerd.log").is_file()  # run 数据原样保留
    assert job_name("r1") == "tandem-r1"


def test_fetch_artifacts_tolerates_cli_crash(tmp_path):
    """rjob logs CLI 在日志未聚合时会崩（真机 2026-10-20）：错误写文件，run 数据照常。"""
    run_root = tmp_path / "run"
    (run_root / "worker").mkdir(parents=True)
    (run_root / "worker" / "entry.log").write_text("ok", encoding="utf-8")
    runner = FakeRunner({"get": [REAL_SUCCEEDED]}, fail_on={"logs"})
    out = fetch_artifacts(RjobClient(runner), "j", run_root)
    tail = (out / "platform.tail.log").read_text(encoding="utf-8")
    assert "[rjob logs 失败" in tail and "NoneType" in tail
    assert (out / "worker" / "entry.log").is_file()  # 结果镜像不受影响


def test_run_job_fetch_failure_never_skips_cleanup(tmp_path, monkeypatch):
    """平台日志补写失败不得跳过 cleanup（资源泄漏）；错误记入 result。"""
    config = with_paths(CONFIG, results_root=str(tmp_path / "rjob_logs"),
                        deploy_root=str(tmp_path / "deploy"))
    runner = FakeRunner({"get": [REAL_SUCCEEDED], "submit": ["ok"],
                         "logs": ["log"], "stop": ["stopped"]})

    def exploding_fetch(*args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr("tandem_eval.rjob.fetch_artifacts", exploding_fetch)
    result = run_job(config=config,
                     repo_root=tmp_path, run_id="r3", spec_overrides={},
                     client=RjobClient(runner),
                     poll_seconds=0, sleep=lambda _: None, sync=False)
    assert result["status"] == "succeeded"
    assert "fetch_error" in result
    assert any(c[:2] == ["rjob", "stop"] for c in runner.calls)  # cleanup 仍然执行
    assert result["cleanup"]["deleted"] is True, "fetch 失败也不许跳过删除"


def test_env_section_seeds_setup_cache_for_osw1():
    from tandem_eval.rjob_spec import _env_section

    section = _env_section("osworld1", CONFIG)
    assert section["root"] == "/shared/osw1"
    assert section["setup_cache_root"] == "/shared/osw1/cache"
    assert "setup_cache_root" not in _env_section("osworld2", CONFIG)


def test_rsync_never_deletes_job_specs():
    """job_specs 在部署副本里而非源仓库，必须排除出 rsync --delete（并发 run 互踩根因）。"""
    from tandem_eval.rjob import RSYNC_EXCLUDES

    assert "job_specs/" in RSYNC_EXCLUDES
