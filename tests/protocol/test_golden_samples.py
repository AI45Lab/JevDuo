"""golden 样例测试：每份样例必须通过协议校验器；样例目录本身被清单锁死。"""

from __future__ import annotations

import pytest

from jevduo.protocol import (
    ProtocolValidationError,
    action_from_dict,
    iter_samples,
    observation_from_dict,
    receipt_from_dict,
)

_DISPATCH = {
    "action": action_from_dict,
    "observation": observation_from_dict,
    "receipt": receipt_from_dict,
}

SAMPLES = list(iter_samples())

# 样例清单锁：增删样例必须有意为之（新增消息分支时同步更新此清单）。
EXPECTED_SAMPLE_NAMES = {
    "action_gui_click",
    "action_gui_scroll",
    "action_gui_type",
    "action_cli",
    "action_cli_no_observation",
    "action_fail",
    "action_finish",
    "action_finish_empty",
    "action_wait",
    "observation_no_a11y",
    "observation_osw1_step0",
    "observation_osw2_cdp",
    "observation_osw2_jev",
    "receipt_cli",
    "receipt_cli_error",
    "receipt_gui",
    "receipt_wait",
}


def test_sample_catalog_locked():
    assert {name for name, _ in SAMPLES} == EXPECTED_SAMPLE_NAMES


@pytest.mark.parametrize("name,data", SAMPLES, ids=[name for name, _ in SAMPLES])
def test_sample_validates(name, data):
    category = name.split("_", 1)[0]
    _DISPATCH[category](data)  # 不抛异常即通过


def test_malformed_sample_filename_rejected(tmp_path, monkeypatch):
    from jevduo.protocol import samples as samples_mod

    bad = tmp_path / "bogus_thing.json"
    bad.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(samples_mod, "SAMPLES_DIR", tmp_path)
    with pytest.raises(ValueError, match="必须以"):
        list(samples_mod.iter_samples())


def test_validation_error_is_value_error_with_path():
    err = ProtocolValidationError("a.b", "必须是 string")
    assert isinstance(err, ValueError)
    assert err.path == "a.b"
    assert "a.b" in str(err)
