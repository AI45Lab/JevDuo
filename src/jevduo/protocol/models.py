"""协议 v0.2 消息模型。

schema/ 下同名 JSON Schema 是唯一真源；本文件是它的 Python 包装层。
约束（MCP-ready，自第一天生效）：
- 序列化结果必须是纯 JSON：二进制一律文件引用（path + sha256），禁止内嵌；
- 字段显式：to_dict 输出全部字段（含 None），不做稀疏序列化；
- kind 字段标记动作 union 分支，由 validate.py 在反序列化时严格校验。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

PROTOCOL_VERSION = "0.2"

# GUI 动作类型枚举。v0.1 为暂定集，M1 移植 compile 语义时如有出入以移植结果为准并 bump 版本。
GUI_ACTION_TYPES = (
    "click",
    "double_click",
    "right_click",
    "drag",
    "hotkey",
    "type",
    "scroll",
    "key",
    "mouse_down",
    "mouse_up",
)
WANT_OBSERVATION_VALUES = ("screenshot", "none")


@dataclass(frozen=True, slots=True)
class Screenshot:
    """截图引用。captured=False 表示复用了之前某步的截图（CLI 连发时 want_observation="none"）。"""

    path: str
    sha256: str
    width: int
    height: int
    captured: bool = True

    def to_dict(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "sha256": self.sha256,
            "width": self.width,
            "height": self.height,
            "captured": self.captured,
        }


@dataclass(frozen=True, slots=True)
class AccessibilityTree:
    """a11y tree 引用。present=False（含 JEV 降级）时 digest/node_count/path 必须为 None。"""

    present: bool
    source: str  # "vm" | "jev" | "none"
    digest: str | None = None
    node_count: int | None = None
    path: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "present": self.present,
            "source": self.source,
            "digest": self.digest,
            "node_count": self.node_count,
            "path": self.path,
        }


@dataclass(frozen=True, slots=True)
class BrowserSnapshot:
    """可选的浏览器 DOM 快照引用；CDP 不可用时为 None。"""

    present: bool
    source: str  # "cdp" | "none"
    digest: str | None = None
    node_count: int | None = None
    path: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "present": self.present,
            "source": self.source,
            "digest": self.digest,
            "node_count": self.node_count,
            "path": self.path,
        }


@dataclass(frozen=True, slots=True)
class Budget:
    steps_remaining: int
    wall_seconds_remaining: float

    def to_dict(self) -> dict[str, Any]:
        return {
            "steps_remaining": self.steps_remaining,
            "wall_seconds_remaining": self.wall_seconds_remaining,
        }


@dataclass(frozen=True, slots=True)
class Receipt:
    """执行回执。stdout/stderr 为截尾后的内容（≤12000 字符），全文见 full_output_ref。"""

    returncode: int
    stdout: str
    stderr: str
    kind: str  # "cli" | "gui" | "wait"
    acknowledged: bool
    full_output_ref: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "returncode": self.returncode,
            "stdout": self.stdout,
            "stderr": self.stderr,
            "kind": self.kind,
            "acknowledged": self.acknowledged,
            "full_output_ref": self.full_output_ref,
        }


@dataclass(frozen=True, slots=True)
class Observation:
    """环境 → harness 的观察消息。"""

    task_id: str
    instruction: str
    step_idx: int
    budget: Budget
    screenshot: Screenshot
    accessibility_tree: AccessibilityTree
    last_receipt: Receipt | None
    browser_snapshot: BrowserSnapshot | None = None
    protocol_version: str = PROTOCOL_VERSION

    def to_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "instruction": self.instruction,
            "step_idx": self.step_idx,
            "budget": self.budget.to_dict(),
            "screenshot": self.screenshot.to_dict(),
            "accessibility_tree": self.accessibility_tree.to_dict(),
            "last_receipt": self.last_receipt.to_dict() if self.last_receipt is not None else None,
            "browser_snapshot": self.browser_snapshot.to_dict() if self.browser_snapshot else None,
            "protocol_version": self.protocol_version,
        }


@dataclass(frozen=True, slots=True)
class GuiAction:
    """OSWorld 原生 GUI 动作。parameters 的内容由 M1 的 compile 层解释。"""

    action_type: str
    parameters: dict[str, Any]
    target_id: str | None = None
    description: str | None = None
    kind: str = field(default="gui", init=False)

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "action_type": self.action_type,
            "parameters": dict(self.parameters),
            "target_id": self.target_id,
            "description": self.description,
        }


@dataclass(frozen=True, slots=True)
class CliAction:
    """VM 内 shell 动作：bash -lc，cwd=/home/user。1 条 CLI = 1 步，与 GUI 共用步数预算。"""

    command: str
    timeout_s: int
    want_observation: str  # "screenshot" | "none"
    kind: str = field(default="cli", init=False)

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "command": self.command,
            "timeout_s": self.timeout_s,
            "want_observation": self.want_observation,
        }


@dataclass(frozen=True, slots=True)
class WaitAction:
    kind: str = field(default="wait", init=False)

    def to_dict(self) -> dict[str, Any]:
        return {"kind": self.kind}


@dataclass(frozen=True, slots=True)
class FinishAction:
    """终态：任务完成，outputs 为 VM 内 deliverable 路径（可为空）。"""

    outputs: tuple[str, ...] = ()
    kind: str = field(default="finish", init=False)

    def to_dict(self) -> dict[str, Any]:
        return {"kind": self.kind, "outputs": list(self.outputs)}


@dataclass(frozen=True, slots=True)
class FailAction:
    """终态：harness 主动放弃，reason 必填。"""

    reason: str
    kind: str = field(default="fail", init=False)

    def to_dict(self) -> dict[str, Any]:
        return {"kind": self.kind, "reason": self.reason}


Action = GuiAction | CliAction | WaitAction | FinishAction | FailAction
