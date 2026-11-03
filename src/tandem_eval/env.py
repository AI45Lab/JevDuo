"""环境抽象：把 VM/DesktopEnv 适配为协议消息（Observation / Action / Receipt）。

M2 提供 Environment 契约 + MockEnv（全链路测试用）；真实 OSWorld 环境适配在 M3 落地
（读取 infra/envs/rjob.json 注入的共享目录路径与 OSWORLD_PYTHON）。
"""

from __future__ import annotations

import hashlib
import io
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

from PIL import Image

from tandem.protocol.models import (
    AccessibilityTree,
    Action,
    Budget,
    Observation,
    Receipt,
    Screenshot,
)


class Environment(Protocol):
    """runner 依赖的全部环境能力。真实实现（M3）封装 VM 生命周期与动作执行。"""

    name: str

    def reset(self, task: dict) -> Observation: ...

    def execute(self, action: Action, *, budget: Budget, task: dict) -> tuple[Receipt, Observation]: ...

    def observe(self, task: dict, *, budget: Budget, previous: Observation) -> Observation: ...

    def fetch_task(self, task_id: str) -> dict: ...

    def export_outputs(self, paths: list[str], episode_dir) -> list[dict]: ...

    def score(self, task: dict) -> dict: ...

    def close(self) -> None: ...


@dataclass
class MockEnv:
    """脚本化环境：按预置剧本返回观察与回执，验证 runner/recorder 全链路。

    base_dir 非空时把截图/a11y 真实落盘（observation 引用绝对路径），
    并暴露 last_screenshot / last_a11y 供 runner 写入 episode 目录。
    """

    instruction: str = "把桌面上的会议纪要另存为 PDF"
    a11y_xml: str = (
        '<application name="app" active="true">'
        '<push_button name="确定" screencoord="(100,200)" size="(80,24)"/></application>'
    )
    cli_stdout: str = "done"
    receipt_returncode: int = 0
    base_dir: Path | None = None
    shots: dict[str, bytes] = field(default_factory=dict)
    screenshot_counter: int = 0
    last_screenshot: bytes = b""
    last_a11y: str = ""

    @property
    def name(self) -> str:
        return "mock"

    def fetch_task(self, task_id: str, domain: str = "") -> dict:
        return {
            "id": task_id,
            "instruction": self.instruction,
            "domain": domain or "mock",
            "config": [],
        }

    def _screenshot_ref(self, task_id: str, step_idx: int) -> tuple[str, str]:
        key = f"{task_id}:{step_idx}"
        if key not in self.shots:
            image = io.BytesIO()
            Image.new("RGB", (1920, 1080), "white").save(image, format="PNG")
            self.shots[key] = image.getvalue()
        digest = hashlib.sha256(self.shots[key]).hexdigest()
        if self.base_dir is not None:
            target = self.base_dir / "screenshots" / f"step_{step_idx:02d}.png"
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(self.shots[key])
        return key, digest

    def _observation(
        self, task: dict, step_idx: int, budget: Budget, receipt: Receipt | None
    ) -> Observation:
        width, height = 1920, 1080
        key, digest = self._screenshot_ref(task["id"], step_idx)
        self.last_screenshot = self.shots[key]
        self.last_a11y = self.a11y_xml
        a11y_path = f"a11y/step_{step_idx:02d}.xml"
        if self.base_dir is not None:
            target = self.base_dir / a11y_path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(self.a11y_xml, encoding="utf-8")
            a11y_path = str(target)
        return Observation(
            task_id=task["id"],
            instruction=task["instruction"],
            step_idx=step_idx,
            budget=budget,
            screenshot=Screenshot(
                path=(
                    str(self.base_dir / "screenshots" / f"step_{step_idx:02d}.png")
                    if self.base_dir
                    else f"screenshots/step_{step_idx:02d}.png"
                ),
                sha256=digest,
                width=width,
                height=height,
            ),
            accessibility_tree=AccessibilityTree(
                present=True,
                source="vm",
                digest=hashlib.sha256(self.a11y_xml.encode()).hexdigest(),
                node_count=1,
                path=a11y_path,
            ),
            last_receipt=receipt,
        )

    def reset(self, task: dict) -> Observation:
        self.screenshot_counter = 0
        return self._observation(
            task, 0, Budget(steps_remaining=10**6, wall_seconds_remaining=10**6), None
        )

    def execute(self, action: Action, *, budget: Budget, task: dict) -> tuple[Receipt, Observation]:
        kind = getattr(action, "kind", "gui")
        receipt = Receipt(
            returncode=self.receipt_returncode,
            stdout=self.cli_stdout if kind == "cli" else "",
            stderr="",
            kind=kind if kind in {"cli", "gui", "wait"} else "gui",
            acknowledged=True,
        )
        step_idx = self.screenshot_counter
        self.screenshot_counter += 1
        return receipt, self._observation(task, step_idx, budget, receipt)

    def observe(self, task, *, budget, previous):
        return self._observation(task, previous.step_idx, budget, previous.last_receipt)

    def export_outputs(self, paths: list[str], episode_dir) -> list[dict]:
        records = []
        for index, path in enumerate(paths):
            target = episode_dir / "deliverables" / f"{index:02d}_{path.rsplit('/', 1)[-1]}"
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(b"mock-output")
            records.append({"vm_path": path, "exported": True, "local_path": str(target)})
        return records

    def score(self, task: dict) -> dict:
        # 与真实 evaluator 同构：dict 必含 "score"（official_score 契约）
        return {"source": "mock", "score": 1.0}

    def close(self) -> None:
        return None


def budget_of(*, max_steps: int, max_wall_seconds: float, steps_used: int,
              started_monotonic: float, now_monotonic: float) -> Budget:
    """runner 每步构造预算观察；墙钟由 runner 计时（eval 侧权威）。"""
    return Budget(
        steps_remaining=max(0, max_steps - steps_used),
        wall_seconds_remaining=max(0.0, max_wall_seconds - (now_monotonic - started_monotonic)),
    )
