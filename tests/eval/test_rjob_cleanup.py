"""rjob 收尾：跑完自动删除 rjob（无论成功、失败、超时、异常）。

被测约定：
- 默认删除（stop 释放资源 → delete 永久清理），`--keep` 才保留现场；
- stop 是异步的，delete 必须重试到删掉为止；
- 删不掉不能抛异常盖掉主流程结果，但如实回报 deleted=False；
- 收尾 delete 必须发生在 fetch 之后（先取平台日志，再删 rjob）。
"""

from __future__ import annotations

from jevduo_eval.rjob import RjobClient, cleanup, run_job

from .rjob_fakes import CONFIG, REAL_SUCCEEDED, FakeRunner, with_paths


def _delete_calls(runner: FakeRunner) -> list[int]:
    return [i for i, c in enumerate(runner.calls) if c[:2] == ["rjob", "delete"]]


def _submit_index(runner: FakeRunner) -> int:
    return next(i for i, c in enumerate(runner.calls) if c[:2] == ["rjob", "submit"])


def _run_config(tmp_path) -> dict:
    return with_paths(CONFIG, results_root=str(tmp_path / "rjob_logs"),
                      deploy_root=str(tmp_path / "deploy"))


def test_cleanup_deletes_rjob_by_default():
    """收尾默认删除 rjob——跑完（无论成败）不在集群上留残留。"""
    runner = FakeRunner({"stop": ["ok"], "delete": ["ok"]})
    outcome = cleanup(RjobClient(runner), "j")
    assert outcome == {"stopped": True, "deleted": True}
    assert any(c[:2] == ["rjob", "delete"] for c in runner.calls)


def test_cleanup_can_keep_rjob_without_deleting():
    runner = FakeRunner({"stop": ["ok"]})
    outcome = cleanup(RjobClient(runner), "j", delete=False)
    assert outcome["deleted"] is False
    assert not any(c[:2] == ["rjob", "delete"] for c in runner.calls)


def test_cleanup_retries_delete_while_job_still_terminating():
    """stop 是异步的：刚 stop 完 delete 可能被平台拒——必须重试到删掉。"""
    runner = FakeRunner({"stop": ["ok"], "delete": ["ok"]}, fail_times={"delete": 2})
    outcome = cleanup(RjobClient(runner), "j", sleep=lambda _: None)
    assert outcome["deleted"] is True
    assert runner.failed_counts["delete"] == 2


def test_cleanup_reports_delete_failure_without_raising():
    """删不掉不能抛异常盖掉主流程结果，但必须如实回报 deleted=False。"""
    runner = FakeRunner({"stop": ["ok"]}, fail_on={"delete"})
    outcome = cleanup(RjobClient(runner), "j", retries=2, sleep=lambda _: None)
    assert outcome == {"stopped": True, "deleted": False}


def test_run_job_deletes_rjob_after_success(tmp_path):
    """成功路径：收尾 delete 必须发生在 fetch 之后（先取平台日志，再删 rjob）。"""
    runner = FakeRunner({"submit": ["ok"], "get": [REAL_SUCCEEDED], "logs": ["log"],
                         "stop": ["stopped"], "delete": ["ok"]})
    result = run_job(config=_run_config(tmp_path),
                     repo_root=tmp_path, run_id="r1", spec_overrides={},
                     client=RjobClient(runner),
                     poll_seconds=0, sleep=lambda _: None, sync=False)
    assert result["status"] == "succeeded"
    assert result["cleanup"] == {"stopped": True, "deleted": True}
    deletes = _delete_calls(runner)
    assert len(deletes) >= 2, "提交前防御性 delete + 收尾 delete"
    fetch_index = next(i for i, c in enumerate(runner.calls) if c[:2] == ["rjob", "logs"])
    assert deletes[-1] > fetch_index, "收尾 delete 必须晚于 fetch"


def test_run_job_deletes_rjob_after_platform_failure(tmp_path):
    """平台侧报错（rjob get 失败）也要删 rjob，且状态如实记为 error。"""
    runner = FakeRunner({"submit": ["ok"], "logs": ["log"], "stop": ["stopped"],
                         "delete": ["ok"]}, fail_on={"get"})
    result = run_job(config=_run_config(tmp_path),
                     repo_root=tmp_path, run_id="r2", spec_overrides={},
                     client=RjobClient(runner),
                     poll_seconds=0, sleep=lambda _: None, sync=False)
    assert result["status"] == "error"
    assert "error" in result
    assert result["cleanup"]["deleted"] is True


def test_run_job_deletes_rjob_when_submit_fails(tmp_path):
    """submit 失败也走收尾——绝不在集群上留半成品残留。"""
    runner = FakeRunner({"stop": ["stopped"], "delete": ["ok"]}, fail_on={"submit"})
    result = run_job(config=_run_config(tmp_path),
                     repo_root=tmp_path, run_id="r3", spec_overrides={},
                     client=RjobClient(runner),
                     poll_seconds=0, sleep=lambda _: None, sync=False)
    assert result["status"] == "error"
    assert result["cleanup"]["deleted"] is True
    assert _delete_calls(runner)[-1] > _submit_index(runner)


def test_run_job_keep_preserves_rjob(tmp_path):
    """--keep：提交之后不 stop 不 delete，保留现场供事后排查。"""
    runner = FakeRunner({"submit": ["ok"], "get": [REAL_SUCCEEDED], "logs": ["log"],
                         "stop": ["stopped"], "delete": ["ok"]})
    result = run_job(config=_run_config(tmp_path),
                     repo_root=tmp_path, run_id="r4", spec_overrides={},
                     client=RjobClient(runner),
                     poll_seconds=0, sleep=lambda _: None, sync=False, keep=True)
    assert result["cleanup"] == {"stopped": False, "deleted": False, "kept": True}
    after_submit = runner.calls[_submit_index(runner):]
    assert not any(c[:2] == ["rjob", "stop"] for c in after_submit)
    assert not any(c[:2] == ["rjob", "delete"] for c in after_submit)
