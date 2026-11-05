"""协议 v0.2 手写校验器（零依赖）。

与 schema/*.json 的关系：schema 文件是对外（人类阅读 / 未来 MCP tool 定义）的真源；
本文件是运行时的强制执行者。二者与 samples/ 由 tests/protocol 的 golden 测试共同锚定。
若未来解除依赖冻结引入 jsonschema，应补一条「schema 文件本身可通过」的一致性测试。

所有 *_from_dict 严格校验：未知字段、字段缺失、类型不符（含 bool 冒充 int）、
枚举越界、数值越界均抛 ProtocolValidationError，path 定位到出错字段。
"""

from __future__ import annotations

import re
from collections.abc import Callable
from typing import Any

from .models import (
    GUI_ACTION_TYPES,
    PROTOCOL_VERSION,
    WANT_OBSERVATION_VALUES,
    AccessibilityTree,
    Action,
    BrowserSnapshot,
    Budget,
    CliAction,
    FailAction,
    FinishAction,
    GuiAction,
    Observation,
    Receipt,
    Screenshot,
    WaitAction,
)

RECEIPT_KINDS = ("cli", "gui", "wait")
A11Y_SOURCES = ("vm", "jev", "none")
BROWSER_SNAPSHOT_SOURCES = ("cdp", "none")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


class ProtocolValidationError(ValueError):
    def __init__(self, path: str, message: str):
        self.path = path
        super().__init__(f"{path}: {message}")


def _fail(path: str, message: str) -> None:
    raise ProtocolValidationError(path, message)


def _as_obj(value: Any, path: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        _fail(path, "必须是 JSON object")
    return value


def _as_str(value: Any, path: str, *, allow_empty: bool = False) -> str:
    if not isinstance(value, str):
        _fail(path, "必须是 string")
    if not allow_empty and not value:
        _fail(path, "不能为空字符串")
    return value


def _as_bool(value: Any, path: str) -> bool:
    if not isinstance(value, bool):
        _fail(path, "必须是 boolean")
    return value


def _as_int(value: Any, path: str, *, minimum: int | None = None, maximum: int | None = None) -> int:
    # bool 是 int 的子类，必须显式排除，防止 true 冒充 1。
    if isinstance(value, bool) or not isinstance(value, int):
        _fail(path, "必须是 integer")
    if minimum is not None and value < minimum:
        _fail(path, f"必须 >= {minimum}")
    if maximum is not None and value > maximum:
        _fail(path, f"必须 <= {maximum}")
    return value


def _as_num(value: Any, path: str, *, minimum: float | None = None) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        _fail(path, "必须是 number")
    if minimum is not None and value < minimum:
        _fail(path, f"必须 >= {minimum}")
    return float(value)


def _as_enum(value: Any, allowed: tuple[str, ...], path: str) -> str:
    if value not in allowed:
        _fail(path, f"必须是 {allowed} 之一，得到 {value!r}")
    return value


def _as_nullable(value: Any, inner: Callable[[Any, str], Any], path: str) -> Any:
    if value is None:
        return None
    return inner(value, path)


def _as_str_list(value: Any, path: str) -> list[str]:
    if not isinstance(value, list):
        _fail(path, "必须是 array")
    return [_as_str(item, f"{path}[{i}]") for i, item in enumerate(value)]


def _as_jsonable(value: Any, path: str) -> Any:
    """递归校验可跨进程序列化（未来 MCP 边界），通过则原样返回。"""
    if value is None or isinstance(value, (str, bool)):
        return value
    if isinstance(value, int) or isinstance(value, float):
        return value
    if isinstance(value, list):
        return [_as_jsonable(item, f"{path}[{i}]") for i, item in enumerate(value)]
    if isinstance(value, dict):
        out: dict[str, Any] = {}
        for key, item in value.items():
            if not isinstance(key, str):
                _fail(path, f"object 的键必须是 string，得到 {key!r}")
            out[key] = _as_jsonable(item, f"{path}.{key}")
        return out
    _fail(path, f"必须是纯 JSON 值，得到 {type(value).__name__}")


def _take(obj: dict[str, Any], required: list[str], optional: list[str], path: str) -> None:
    unknown = sorted(set(obj) - set(required) - set(optional))
    if unknown:
        _fail(path, f"未知字段: {unknown}")
    missing = [key for key in required if key not in obj]
    if missing:
        _fail(path, f"缺少必填字段: {missing}")


def receipt_from_dict(data: Any) -> Receipt:
    path = "receipt"
    obj = _as_obj(data, path)
    _take(obj, ["returncode", "stdout", "stderr", "kind", "acknowledged"], ["full_output_ref"], path)
    kind = _as_enum(obj["kind"], RECEIPT_KINDS, f"{path}.kind")
    stdout = _as_str(obj["stdout"], f"{path}.stdout", allow_empty=True)
    stderr = _as_str(obj["stderr"], f"{path}.stderr", allow_empty=True)
    if len(stdout) > 12000 or len(stderr) > 12000:
        _fail(path, "stdout/stderr 截尾后不得超过 12000 字符（全文应存 full_output_ref）")
    full_ref = _as_nullable(
        obj["full_output_ref"], lambda v, p: _as_str(v, p), f"{path}.full_output_ref"
    )
    return Receipt(
        returncode=_as_int(obj["returncode"], f"{path}.returncode"),
        stdout=stdout,
        stderr=stderr,
        kind=kind,
        acknowledged=_as_bool(obj["acknowledged"], f"{path}.acknowledged"),
        full_output_ref=full_ref,
    )


def _screenshot_from_dict(data: Any, path: str) -> Screenshot:
    obj = _as_obj(data, path)
    _take(obj, ["path", "sha256", "width", "height"], ["captured"], path)
    sha256 = _as_str(obj["sha256"], f"{path}.sha256")
    if not _SHA256_RE.fullmatch(sha256):
        _fail(f"{path}.sha256", "必须是 64 位小写十六进制")
    return Screenshot(
        path=_as_str(obj["path"], f"{path}.path"),
        sha256=sha256,
        width=_as_int(obj["width"], f"{path}.width", minimum=1),
        height=_as_int(obj["height"], f"{path}.height", minimum=1),
        captured=_as_bool(obj.get("captured", True), f"{path}.captured"),
    )


def _a11y_from_dict(data: Any, path: str) -> AccessibilityTree:
    obj = _as_obj(data, path)
    _take(obj, ["present", "source"], ["digest", "node_count", "path"], path)
    present = _as_bool(obj["present"], f"{path}.present")
    source = _as_enum(obj["source"], A11Y_SOURCES, f"{path}.source")
    digest = _as_nullable(obj["digest"], lambda v, p: _as_str(v, p), f"{path}.digest")
    node_count = _as_nullable(
        obj["node_count"], lambda v, p: _as_int(v, p, minimum=0), f"{path}.node_count"
    )
    tree_path = _as_nullable(obj["path"], lambda v, p: _as_str(v, p), f"{path}.path")
    if present:
        if source == "none":
            _fail(f"{path}", "present=true 时 source 不能是 none")
        if digest is None or node_count is None:
            _fail(path, "present=true 时 digest 与 node_count 必填")
    else:
        if digest is not None or node_count is not None or tree_path is not None:
            _fail(path, "present=false 时 digest/node_count/path 必须为 null")
    return AccessibilityTree(
        present=present, source=source, digest=digest, node_count=node_count, path=tree_path
    )


def _browser_snapshot_from_dict(data: Any, path: str) -> BrowserSnapshot | None:
    if data is None:
        return None
    obj = _as_obj(data, path)
    _take(obj, ["present", "source"], ["digest", "node_count", "path"], path)
    present = _as_bool(obj["present"], f"{path}.present")
    source = _as_enum(obj["source"], BROWSER_SNAPSHOT_SOURCES, f"{path}.source")
    digest = _as_nullable(obj.get("digest"), lambda v, p: _as_str(v, p), f"{path}.digest")
    node_count = _as_nullable(
        obj.get("node_count"), lambda v, p: _as_int(v, p, minimum=0), f"{path}.node_count"
    )
    snapshot_path = _as_nullable(obj.get("path"), lambda v, p: _as_str(v, p), f"{path}.path")
    if present:
        if source != "cdp" or digest is None or node_count is None or snapshot_path is None:
            _fail(path, "present=true 时 source=cdp 且 digest/node_count/path 必填")
    elif source != "none" or any(value is not None for value in (digest, node_count, snapshot_path)):
        _fail(path, "present=false 时 source=none 且 digest/node_count/path 必须为 null")
    return BrowserSnapshot(present, source, digest, node_count, snapshot_path)


def observation_from_dict(data: Any) -> Observation:
    path = "observation"
    obj = _as_obj(data, path)
    _take(
        obj,
        [
            "task_id",
            "instruction",
            "step_idx",
            "budget",
            "screenshot",
            "accessibility_tree",
            "last_receipt",
            "protocol_version",
        ],
        ["browser_snapshot"],
        path,
    )
    budget_obj = _as_obj(obj["budget"], f"{path}.budget")
    _take(budget_obj, ["steps_remaining", "wall_seconds_remaining"], [], f"{path}.budget")
    budget = Budget(
        steps_remaining=_as_int(budget_obj["steps_remaining"], f"{path}.budget.steps_remaining", minimum=0),
        wall_seconds_remaining=_as_num(
            budget_obj["wall_seconds_remaining"], f"{path}.budget.wall_seconds_remaining", minimum=0.0
        ),
    )
    last_receipt = (
        None if obj["last_receipt"] is None else receipt_from_dict(obj["last_receipt"])
    )
    return Observation(
        task_id=_as_str(obj["task_id"], f"{path}.task_id"),
        instruction=_as_str(obj["instruction"], f"{path}.instruction"),
        step_idx=_as_int(obj["step_idx"], f"{path}.step_idx", minimum=0),
        budget=budget,
        screenshot=_screenshot_from_dict(obj["screenshot"], f"{path}.screenshot"),
        accessibility_tree=_a11y_from_dict(obj["accessibility_tree"], f"{path}.accessibility_tree"),
        last_receipt=last_receipt,
        browser_snapshot=_browser_snapshot_from_dict(
            obj.get("browser_snapshot"), f"{path}.browser_snapshot"
        ),
        protocol_version=_as_enum(obj["protocol_version"], (PROTOCOL_VERSION,), f"{path}.protocol_version"),
    )


def _gui_action_from_dict(obj: dict[str, Any]) -> GuiAction:
    path = "action(gui)"
    _take(obj, ["kind", "action_type", "parameters"], ["target_id", "description"], path)
    return GuiAction(
        action_type=_as_enum(obj["action_type"], GUI_ACTION_TYPES, f"{path}.action_type"),
        parameters=_as_jsonable(obj["parameters"], f"{path}.parameters"),
        target_id=_as_nullable(obj.get("target_id"), lambda v, p: _as_str(v, p), f"{path}.target_id"),
        description=_as_nullable(
            obj.get("description"), lambda v, p: _as_str(v, p, allow_empty=True), f"{path}.description"
        ),
    )


def _cli_action_from_dict(obj: dict[str, Any]) -> CliAction:
    path = "action(cli)"
    _take(obj, ["kind", "command", "timeout_s", "want_observation"], [], path)
    return CliAction(
        command=_as_str(obj["command"], f"{path}.command"),
        timeout_s=_as_int(obj["timeout_s"], f"{path}.timeout_s", minimum=1, maximum=120),
        want_observation=_as_enum(
            obj["want_observation"], WANT_OBSERVATION_VALUES, f"{path}.want_observation"
        ),
    )


def action_from_dict(data: Any) -> Action:
    path = "action"
    obj = _as_obj(data, path)
    if "kind" not in obj:
        _fail(path, "缺少必填字段: ['kind']")
    kind = obj["kind"]
    if kind == "gui":
        return _gui_action_from_dict(obj)
    if kind == "cli":
        return _cli_action_from_dict(obj)
    if kind == "wait":
        _take(obj, ["kind"], [], path)
        return WaitAction()
    if kind == "finish":
        _take(obj, ["kind", "outputs"], [], path)
        return FinishAction(outputs=tuple(_as_str_list(obj["outputs"], f"{path}.outputs")))
    if kind == "fail":
        _take(obj, ["kind", "reason"], [], path)
        return FailAction(reason=_as_str(obj["reason"], f"{path}.reason"))
    _fail(f"{path}.kind", f"必须是 ('gui', 'cli', 'wait', 'finish', 'fail') 之一，得到 {kind!r}")
