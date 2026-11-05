"""包级冒烟：版本号与协议公开 API 可导入。"""

import jevduo
from jevduo.protocol import PROTOCOL_VERSION, action_from_dict, observation_from_dict, receipt_from_dict


def test_version():
    assert jevduo.__version__ == "0.1.0"


def test_protocol_exports():
    assert PROTOCOL_VERSION == "0.2"
    assert callable(action_from_dict)
    assert callable(observation_from_dict)
    assert callable(receipt_from_dict)
