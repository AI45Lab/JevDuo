"""往返一致测试：样例 → dataclass → dict 必须与原文完全相等。

这条测试把「序列化格式」钉死：任何字段改名、默认值漂移、键序引入的差异都会在此暴露。
"""

from __future__ import annotations

import json

import pytest

from jevduo.protocol import action_from_dict, iter_samples, observation_from_dict, receipt_from_dict

_DISPATCH = {
    "action": action_from_dict,
    "observation": observation_from_dict,
    "receipt": receipt_from_dict,
}

SAMPLES = list(iter_samples())


@pytest.mark.parametrize("name,data", SAMPLES, ids=[name for name, _ in SAMPLES])
def test_roundtrip_dict_equality(name, data):
    obj = _DISPATCH[name.split("_", 1)[0]](data)
    assert obj.to_dict() == data


@pytest.mark.parametrize("name,data", SAMPLES, ids=[name for name, _ in SAMPLES])
def test_roundtrip_json_stable(name, data, tmp_path):
    obj = _DISPATCH[name.split("_", 1)[0]](data)
    dumped = json.dumps(obj.to_dict(), ensure_ascii=False, sort_keys=True)
    original = json.dumps(data, ensure_ascii=False, sort_keys=True)
    assert dumped == original
    # 二次往返：从 to_dict 再解析回来，dataclass 相等（tuple/list 归一化由 from_dict 处理）
    again = _DISPATCH[name.split("_", 1)[0]](json.loads(dumped))
    assert again == obj
