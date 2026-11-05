"""负例测试：非法消息必须被校验器拒绝，且错误信息定位到字段路径。

覆盖：未知字段 / 缺字段 / 类型不符 / bool 冒充 int / 枚举与数值越界 / 一致性约束。
"""

from __future__ import annotations

import pytest

from jevduo.protocol import (
    ProtocolValidationError,
    action_from_dict,
    observation_from_dict,
    receipt_from_dict,
)

VALID_ACTION_CLI = {
    "kind": "cli",
    "command": "ls",
    "timeout_s": 30,
    "want_observation": "screenshot",
}
VALID_ACTION_GUI = {
    "kind": "gui",
    "action_type": "click",
    "parameters": {"x": 1, "y": 2},
    "target_id": None,
    "description": None,
}
VALID_RECEIPT = {
    "returncode": 0,
    "stdout": "",
    "stderr": "",
    "kind": "gui",
    "acknowledged": True,
    "full_output_ref": None,
}


def _observation(**overrides):
    base = {
        "task_id": "t-1",
        "instruction": "做某事",
        "step_idx": 0,
        "budget": {"steps_remaining": 10, "wall_seconds_remaining": 60.0},
        "screenshot": {
            "path": "s.png",
            "sha256": "0" * 64,
            "width": 100,
            "height": 200,
            "captured": True,
        },
        "accessibility_tree": {
            "present": False,
            "source": "none",
            "digest": None,
            "node_count": None,
            "path": None,
        },
        "last_receipt": None,
        "protocol_version": "0.1",
    }
    base.update(overrides)
    return base


@pytest.mark.parametrize(
    "label,fn,payload,fragment",
    [
        # --- Action: 结构 ---
        ("action-unknown-kind", action_from_dict, {"kind": "teleport"}, "action.kind"),
        ("action-missing-kind", action_from_dict, {}, "缺少必填字段"),
        ("action-not-object", action_from_dict, [1, 2], "必须是 JSON object"),
        # --- Action: cli ---
        ("cli-unknown-field", action_from_dict, {**VALID_ACTION_CLI, "shell": "/bin/zsh"}, "未知字段"),
        ("cli-timeout-zero", action_from_dict, {**VALID_ACTION_CLI, "timeout_s": 0}, "必须 >="),
        ("cli-timeout-over", action_from_dict, {**VALID_ACTION_CLI, "timeout_s": 121}, "必须 <="),
        ("cli-timeout-bool", action_from_dict, {**VALID_ACTION_CLI, "timeout_s": True}, "必须是 integer"),
        ("cli-want-bad-enum", action_from_dict, {**VALID_ACTION_CLI, "want_observation": "always"}, "之一"),
        ("cli-empty-command", action_from_dict, {**VALID_ACTION_CLI, "command": ""}, "不能为空字符串"),
        # --- Action: gui ---
        ("gui-bad-action-type", action_from_dict, {**VALID_ACTION_GUI, "action_type": "pinch"}, "之一"),
        (
            "gui-parameters-not-jsonable",
            action_from_dict,
            {**VALID_ACTION_GUI, "parameters": {"x": {1, 2}}},
            "纯 JSON",
        ),
        # --- Action: 终态 ---
        ("fail-empty-reason", action_from_dict, {"kind": "fail", "reason": ""}, "不能为空字符串"),
        ("finish-output-not-string", action_from_dict, {"kind": "finish", "outputs": [42]}, "必须是 string"),
        # --- Receipt ---
        ("receipt-unknown-kind", receipt_from_dict, {**VALID_RECEIPT, "kind": "python"}, "之一"),
        ("receipt-ack-string", receipt_from_dict, {**VALID_RECEIPT, "acknowledged": "yes"}, "必须是 boolean"),
        ("receipt-stdout-too-long", receipt_from_dict, {**VALID_RECEIPT, "stdout": "x" * 12001}, "12000"),
        ("receipt-stdout-int", receipt_from_dict, {**VALID_RECEIPT, "stdout": 123}, "必须是 string"),
        # --- Observation ---
    ("obs-bad-version", observation_from_dict, _observation(protocol_version="0.1"), "protocol_version"),
        ("obs-negative-step", observation_from_dict, _observation(step_idx=-1), "必须 >="),
        ("obs-unknown-top-field", observation_from_dict, _observation(extra=1), "未知字段"),
        (
            "obs-budget-unknown",
            observation_from_dict,
            _observation(
                budget={"steps_remaining": 1, "wall_seconds_remaining": 1.0, "tokens": 5}
            ),
            "未知字段",
        ),
        (
            "obs-steps-remaining-negative",
            observation_from_dict,
            _observation(budget={"steps_remaining": -1, "wall_seconds_remaining": 1.0}),
            "必须 >=",
        ),
        (
            "obs-receipt-inside-bad",
            observation_from_dict,
            _observation(last_receipt={**VALID_RECEIPT, "kind": "bad"}),
            "kind",
        ),
    ],
)
def test_rejects(label, fn, payload, fragment):
    with pytest.raises(ProtocolValidationError) as excinfo:
        fn(payload)
    assert fragment in str(excinfo.value), f"{label}: {excinfo.value}"


def test_rejects_short_sha256():
    bad = _observation()
    bad["screenshot"]["sha256"] = "abc123"
    with pytest.raises(ProtocolValidationError, match="64 位小写十六进制"):
        observation_from_dict(bad)


def test_rejects_a11y_present_without_digest():
    bad = _observation(
        accessibility_tree={
            "present": True,
            "source": "vm",
            "digest": None,
            "node_count": 5,
            "path": "a.json",
        }
    )
    with pytest.raises(ProtocolValidationError, match="digest 与 node_count 必填"):
        observation_from_dict(bad)


def test_rejects_a11y_present_source_none():
    bad = _observation(
        accessibility_tree={
            "present": True,
            "source": "none",
            "digest": "d" * 64,
            "node_count": 5,
            "path": "a.json",
        }
    )
    with pytest.raises(ProtocolValidationError, match="source 不能是 none"):
        observation_from_dict(bad)


def test_rejects_a11y_absent_with_leftovers():
    bad = _observation(
        accessibility_tree={
            "present": False,
            "source": "none",
            "digest": None,
            "node_count": 7,
            "path": None,
        }
    )
    with pytest.raises(ProtocolValidationError, match="必须为 null"):
        observation_from_dict(bad)


def test_rejects_cdp_present_without_required_references():
    bad = _observation(browser_snapshot={"present": True, "source": "cdp"})
    with pytest.raises(ProtocolValidationError, match="digest/node_count/path"):
        observation_from_dict(bad)


def test_rejects_cdp_absent_with_leftovers():
    bad = _observation(
        browser_snapshot={"present": False, "source": "none", "path": "obs_stage/t/dom.json"}
    )
    with pytest.raises(ProtocolValidationError, match="必须为 null"):
        observation_from_dict(bad)
