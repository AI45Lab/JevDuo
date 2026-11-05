"""manifest：索引加载与 job spec manifest 解析。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from jevduo_eval.manifest import OSWORLD21_TASK_IDS, TaskRef, load_index, resolve_manifest, write_resolved

# OSW2.1 的 prepared/runtime 是指向 canonical 路径的软链层：worker 内可见（canonical 视角），
# 开发机只有 user 视角的同一份 runtime。两处都没有时跳过该检查（例如 CI）。
_OSW21_TASK_CLASS_CANDIDATES = (
    Path("/mnt/shared-storage-gpfs2/evobox-share-gpfs2/xuyuanxun/shared_resources/sa_cua"
         "/osworld21/prepared/runtime/evaluation_examples/task_class"),
    Path("/mnt/shared-storage-user/evobox-share-gpfs2/xuyuanxun/shared_resources/sa_cua"
         "/osworld21/runtime/evaluation_examples/task_class"),
)
_OSW21_TASK_CLASS = next((p for p in _OSW21_TASK_CLASS_CANDIDATES if p.is_dir()), None)


def test_load_index_domain_dict_format():
    import tempfile
    from pathlib import Path

    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "idx.json"
        path.write_text(json.dumps({"chrome": ["a", "b"], "os": ["c"]}), encoding="utf-8")
        refs = load_index(path)
    assert refs == [TaskRef("a", "chrome"), TaskRef("b", "chrome"), TaskRef("c", "os")]


def test_load_index_flat_list_formats():
    import tempfile
    from pathlib import Path

    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "idx.json"
        path.write_text(json.dumps(["x", {"id": "y", "domain": "os"}]), encoding="utf-8")
        refs = load_index(path)
    assert refs == [TaskRef("x", ""), TaskRef("y", "os")]


def test_resolve_manifest_explicit_tasks_and_limit():
    spec = {"benchmark": "osworld1", "manifest": {"tasks": ["t1", "t2"]}}
    assert [r.task_id for r in resolve_manifest(spec)] == ["t1", "t2"]
    spec = {"benchmark": "mock", "manifest": {"tasks": ["t1", "t2", "t3"], "limit": 2}}
    assert [r.task_id for r in resolve_manifest(spec)] == ["t1", "t2"]


def test_resolve_manifest_mock_default():
    refs = resolve_manifest({"benchmark": "mock"})
    assert len(refs) == 1 and refs[0].task_id == "mock-000"


def test_osworld21_full_list_and_sharding():
    refs = resolve_manifest({"benchmark": "osworld21"})
    assert tuple(ref.task_id for ref in refs) == OSWORLD21_TASK_IDS
    assert len(OSWORLD21_TASK_IDS) == len(set(OSWORLD21_TASK_IDS)) == 108
    assert all(not ref.domain for ref in refs)
    assert {"026", "035", "041", "069", "087", "095"} <= set(OSWORLD21_TASK_IDS)
    shards = [resolve_manifest({"benchmark": "osworld21", "manifest": {
        "shard": {"index": index, "total": 4}}}) for index in range(1, 5)]
    assert sorted(ref.task_id for shard in shards for ref in shard) == sorted(OSWORLD21_TASK_IDS)


def test_osworld21_explicit_tasks_limit_and_unsupported_task():
    spec = {"benchmark": "osworld21", "manifest": {"tasks": ["054", "003"], "limit": 1}}
    assert [ref.task_id for ref in resolve_manifest(spec)] == ["054"]
    with pytest.raises(ValueError, match="outside 001–108"):
        resolve_manifest({"benchmark": "osworld21", "manifest": {"tasks": ["109"]}})


def test_resolve_manifest_from_index_root():
    import tempfile
    from pathlib import Path

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        examples = root / "osworld1" / "evaluation_examples"
        examples.mkdir(parents=True)
        (examples / "test_nogdrive.json").write_text(
            json.dumps({"chrome": ["id-1", "id-2"]}), encoding="utf-8"
        )
        spec = {"benchmark": "osworld1", "index_root": str(root), "manifest": {}}
        refs = resolve_manifest(spec)
    assert [r.task_id for r in refs] == ["id-1", "id-2"]


def test_write_resolved(tmp_path):
    write_resolved(tmp_path, [TaskRef("a", "chrome"), TaskRef("b", "")],
                   {"run_id": "r1", "benchmark": "mock"})
    data = json.loads((tmp_path / "episodes" / "manifest.resolved.json").read_text())
    assert data["count"] == 2
    assert data["tasks"][0]["episode_dir"] == "chrome-a"
    assert data["tasks"][1]["episode_dir"] == "b"


def test_shard_interleaves_after_limit():
    spec = {"benchmark": "mock", "manifest": {
        "tasks": [f"t{i}" for i in range(20)], "limit": 10,
        "shard": {"index": 1, "total": 5}}}
    refs = resolve_manifest(spec)
    assert [r.task_id for r in refs] == ["t0", "t5"]  # 10 题 → 交错 5 片各 2 题
    spec["manifest"]["shard"] = {"index": 5, "total": 5}
    assert [r.task_id for r in resolve_manifest(spec)] == ["t4", "t9"]
    spec["manifest"]["shard"] = {"index": 6, "total": 5}
    import pytest as _pytest
    with _pytest.raises(ValueError, match="1..5"):
        resolve_manifest(spec)


def test_index_root_derived_from_env_section(tmp_path):
    """spec 未带 index_root 时从 env.root 推导（--limit 索引路径在真实基准上的默认来源）。"""
    import tempfile
    from pathlib import Path

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        examples = root / "evaluation_examples"
        examples.mkdir()
        (examples / "test_v2.json").write_text(
            json.dumps({"tasks": ["001", "002", "003"]}), encoding="utf-8")
        spec = {"benchmark": "osworld2", "env": {"root": str(root)}, "manifest": {}}
        refs = resolve_manifest(spec)
    assert [r.task_id for r in refs] == ["001", "002", "003"]


@pytest.mark.skipif(_OSW21_TASK_CLASS is None, reason="OSW2.1 runtime 未挂载（worker 内才有 canonical 视角）")
def test_osworld21_tasks_exist_in_runtime():
    """完整清单里的每个题号都必须在 runtime 的 task_class 里有任务文件。"""
    missing = [tid for tid in OSWORLD21_TASK_IDS
               if not (_OSW21_TASK_CLASS / f"task_{tid}.py").is_file()]
    assert missing == [], f"子集里的题号在 runtime 中不存在: {missing}"
