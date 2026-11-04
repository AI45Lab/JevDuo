"""OSW1/OSW2/OSW2.1 统一环境适配器：DesktopEnv ↔ 协议消息。

- 代码生成（gui_code/cli_code）语义对齐旧仓库 gui/actions.py + execution.py（DEVIATIONS #6）；
- 回执确认纪律：包装 controller.execute_python_command，无回执不重放（旧 execution.py:30-35）；
- want_observation="none" 当前仍返回完整观察（截图+a11y 由 controller 一次读取，成本低），
  协议字段保留，严格跳过推迟到真机 profiling 后（DEVIATIONS #20）；
- 终态动作（finish/fail）经 env.step("DONE"/"FAIL") 送达 VM（评分钩子依赖它，旧 runtime 同款）。
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import struct
import sys
import time
import xml.etree.ElementTree as ET
from collections.abc import Callable
from pathlib import Path
from typing import Any

from tandem.protocol.models import (
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

from .browser import capture_browser_snapshot

RECEIPT_TAIL = 12000

_FOREGROUND_QUERY = """import json
from Xlib.display import Display
d=Display()
p=d.screen().root.get_full_property(d.intern_atom('_NET_ACTIVE_WINDOW'),0)
info={'foreground_title':'','foreground_class':[]}
if p is not None and int(p.value[0]):
 w=d.create_resource_object('window',int(p.value[0]))
 title=w.get_full_property(d.intern_atom('_NET_WM_NAME'),0)
 info={'foreground_title':title.value.decode('utf-8','replace') if title else str(w.get_wm_name() or ''),
       'foreground_class':list(w.get_wm_class() or [])}
print(json.dumps(info))
d.close()
"""


def png_size(data: bytes) -> tuple[int, int]:
    width, height = struct.unpack(">II", data[16:24])
    return width, height


def gui_code(action: GuiAction) -> str:
    params = action.parameters
    if action.action_type in {"click", "double_click", "right_click"}:
        method = {"click": "click", "double_click": "doubleClick", "right_click": "rightClick"}[
            action.action_type]
        return f"pyautogui.{method}(x={params['x']}, y={params['y']})"
    if action.action_type == "hotkey":
        steps = []
        for keys in params["sequence"]:
            keys_text = ", ".join(repr(key) for key in keys)
            steps.append(f"pyautogui.{'press' if len(keys) == 1 else 'hotkey'}({keys_text})")
        return "; __import__('time').sleep(0.2); ".join(steps)
    if action.action_type == "scroll":
        return f"pyautogui.moveTo({params['x']}, {params['y']}); pyautogui.scroll({params['dy']})"
    if action.action_type == "type":
        steps = []
        if params.get("click"):
            steps.append(f"pyautogui.click(x={params['click'][0]}, y={params['click'][1]})")
        if params.get("press_f2"):
            steps.append("pyautogui.press('f2')")
        if params.get("input_mode", "replace") == "replace":
            steps.append("pyautogui.hotkey('ctrl', 'a')")
        if params["text"].isascii() and not any(c in params["text"] for c in "\r\n\t"):
            steps.append(f"pyautogui.write({params['text']!r}, interval=0.03)")
        else:
            steps.append("__import__('subprocess').run(['xclip','-selection','clipboard','-t','UTF8_STRING'],"
                         f"input={params['text']!r}.encode('utf-8'),check=True,timeout=10,"
                         "stdout=__import__('subprocess').DEVNULL,stderr=__import__('subprocess').DEVNULL)")
            steps.append("pyautogui.hotkey('ctrl','v')")
        return "; ".join(steps)
    raise ValueError(f"不支持的 GUI 动作类型: {action.action_type}")


def cli_code(command: str, timeout_s: int) -> str:
    return (
        "import subprocess,json; p=subprocess.run(['bash','-lc',"
        + repr(command)
        + "],cwd='/home/user',capture_output=True,text=True,timeout="
        + str(timeout_s)
        + "); "
        "print(json.dumps({'returncode':p.returncode,'stdout':p.stdout,'stderr':p.stderr}))"
    )


def action_code(action: Action) -> str:
    """协议动作 → VM 内 python 代码（WAIT/DONE/FAIL 为 DesktopEnv 保留字）。"""
    if isinstance(action, WaitAction):
        return "WAIT"
    if isinstance(action, FinishAction):
        return "DONE"
    if isinstance(action, FailAction):
        return "FAIL"
    if isinstance(action, CliAction):
        return cli_code(action.command, action.timeout_s)
    if isinstance(action, GuiAction):
        return gui_code(action)
    raise TypeError(f"未知动作类型: {type(action).__name__}")


class OsWorldEnv:
    def __init__(self, *, benchmark: str, task_root: Path, vm_path: Path,
                 run_root: Path, asset_root: Path | None = None,
                 setup_cache_root: Path | None = None,
                 volume_size: int | None = None,
                 startup_wait_seconds: float = 30.0, env_factory: Callable[..., Any] | None = None):
        self.benchmark = benchmark
        self.task_root = Path(task_root)
        self.vm_path = Path(vm_path)
        self.run_root = Path(run_root)
        self.asset_root = Path(asset_root) if asset_root else None
        self.setup_cache_root = Path(setup_cache_root) if setup_cache_root else None
        self.volume_size = volume_size
        self.startup_wait_seconds = startup_wait_seconds
        self._env_factory = env_factory or self._import_desktop_env
        # bootstrap 必须先于 fetch_task：任务模块在 import 时（fetch_task 阶段）
        # 就于顶层调用 asset() 固化资产 URL——晚设 OSWORLD_FILE_BASE_URL 会让
        # 模块级 URL 烘焙成默认 HF 地址（task_001 真机 404 的根因；函数体内调用
        # 的 task_066 恰好躲过）
        self._bootstrap_runtime()

    def _bootstrap_runtime(self) -> None:
        root = str(self.task_root.resolve())
        if root not in sys.path:
            sys.path.insert(0, root)
        if self.asset_root is not None:
            os.environ["OSWORLD_FILE_BASE_URL"] = str(self.asset_root)
        self.env: Any = None
        self._step_seq = 0
        self._frame_seq = 0
        self.last_screenshot: bytes = b""
        self.last_a11y: str = ""
        self.last_browser_snapshot: dict | None = None
        self._cdp_reported = False

    @property
    def name(self) -> str:
        return f"osworld-{self.benchmark}"

    # ------------------------------------------------------------------ 装载
    def _import_desktop_env(self, **kwargs):
        self._bootstrap_runtime()
        os.chdir(self.task_root)
        from desktop_env.desktop_env import DesktopEnv

        if self.volume_size is not None:
            kwargs["volume_size"] = self.volume_size
        return DesktopEnv(**kwargs)

    def _task_file(self, task_id: str, domain: str) -> Path:
        if self.benchmark == "osworld1":
            base = self.task_root / "evaluation_examples" / "examples"
            if domain:
                return base / domain / f"{task_id}.json"
            matches = list(base.glob(f"*/{task_id}.json"))
            if len(matches) != 1:
                raise FileNotFoundError(f"OSW1 任务 {task_id} 无法唯一定位（命中 {len(matches)} 个）")
            return matches[0]
        return self.task_root / "evaluation_examples" / "task_class" / f"task_{task_id}.py"

    def fetch_task(self, task_id: str, domain: str = "") -> dict:
        path = self._task_file(task_id, domain)
        if self.benchmark == "osworld1":
            task = json.loads(path.read_text(encoding="utf-8"))
        else:
            sys.path.insert(0, str(self.task_root.resolve()))
            from task_loader import load_task_from_file

            task = load_task_from_file(str(path))
        task.setdefault("id", task_id)
        task.setdefault("domain", domain)
        input_paths = [
            item["parameters"]["path"] for item in task.get("config", [])
            if item.get("type") == "open" and "path" in item.get("parameters", {})
        ]
        if input_paths:
            task["instruction"] = task["instruction"] + (
                "\nProvided input files: " + json.dumps(input_paths)
            )
        return task

    # ------------------------------------------------------------------ 观察
    def _observation(self, task: dict, step_idx: int, budget: Budget,
                     receipt: Receipt | None) -> Observation:
        stage = self.run_root / "obs_stage" / str(task["id"])
        stage.mkdir(parents=True, exist_ok=True)
        self._frame_seq += 1
        frame_name = f"step_{step_idx:02d}_obs_{self._frame_seq:04d}"
        shot_path = stage / f"{frame_name}.png"
        shot_path.write_bytes(self.last_screenshot)
        a11y_path: str | None = None
        digest = node_count = None
        present = bool(self.last_a11y.strip())
        if present:
            a11y_path = str(stage / f"{frame_name}.xml")
            Path(a11y_path).write_text(self.last_a11y, encoding="utf-8")
            digest = hashlib.sha256(self.last_a11y.encode()).hexdigest()
            node_count = self.last_a11y.count("<")
        width, height = png_size(self.last_screenshot)
        browser_snapshot = None
        if self.last_browser_snapshot is not None:
            serialized = json.dumps(self.last_browser_snapshot, ensure_ascii=False)
            browser_path = stage / f"{frame_name}.dom.json"
            browser_path.write_text(serialized, encoding="utf-8")
            browser_snapshot = BrowserSnapshot(
                present=True,
                source="cdp",
                digest=hashlib.sha256(serialized.encode()).hexdigest(),
                node_count=len(self.last_browser_snapshot.get("elements", [])),
                path=str(browser_path),
            )
        return Observation(
            task_id=str(task["id"]),
            instruction=str(task["instruction"]),
            step_idx=step_idx,
            budget=budget,
            screenshot=Screenshot(path=str(shot_path), sha256=hashlib.sha256(
                self.last_screenshot).hexdigest(), width=width, height=height),
            accessibility_tree=AccessibilityTree(
                present=present, source="vm" if present else "none",
                digest=digest, node_count=node_count, path=a11y_path),
            last_receipt=receipt,
            browser_snapshot=browser_snapshot,
        )

    def _capture(self) -> None:
        for attempt in range(3):
            obs = self.env._get_obs()
            if obs.get("screenshot"):
                break
            if attempt < 2:
                time.sleep(1)
        else:
            raise RuntimeError("Environment returned no screenshot after three read-only observations")
        self.last_screenshot = obs["screenshot"]
        self.last_a11y = obs.get("accessibility_tree") or ""
        result = self.env.controller.execute_python_command(_FOREGROUND_QUERY)
        if result is None or result.get("returncode", 0):
            raise RuntimeError("Read-only foreground-window observation failed")
        info = json.loads(result.get("output") or "{}")
        if self.last_a11y:
            tree = ET.fromstring(self.last_a11y)
            tree.set("foreground_title", info.get("foreground_title", ""))
            tree.set("foreground_class", json.dumps(info.get("foreground_class", [])))
            self.last_a11y = ET.tostring(tree, encoding="unicode")
        self.last_browser_snapshot, diagnostic = capture_browser_snapshot(self.env.controller)
        if not self._cdp_reported:
            self._cdp_reported = True
            public = {key: value for key, value in diagnostic.items() if key != "url"}
            print(json.dumps({"event": "cdp_snapshot", **public}, ensure_ascii=False), flush=True)

    def observe(self, task, *, budget, previous):
        """Refresh screenshot, A11y and CDP without executing or replaying any action."""
        self._capture()
        return self._observation(task, previous.step_idx, budget, previous.last_receipt)

    # ------------------------------------------------------------------ 协议
    def _seed_setup_cache(self, task_id: str) -> None:
        """把共享盘已缓存的 setup 文件播种进本次 evaluation_cache（旧 runtime.py:532-539），
        避免任务 setup 走外网下载（HF 下载在 worker 内经代理，慢且可能失败）。"""
        if self.setup_cache_root is None:
            return
        source = self.setup_cache_root / task_id
        if not source.is_dir():
            return
        target = self.run_root / "evaluation_cache" / task_id
        target.mkdir(parents=True, exist_ok=True)
        for item in source.iterdir():
            if item.is_file():
                shutil.copy2(item, target / item.name)

    def reset(self, task: dict) -> Observation:
        if self.env is not None:
            self.env.close()
        self._seed_setup_cache(str(task["id"]))
        self.env = self._env_factory(
            provider_name="docker", path_to_vm=str(self.vm_path), action_space="pyautogui",
            screen_size=(1920, 1080), headless=True, os_type="Ubuntu",
            require_a11y_tree=True, cache_dir=str(self.run_root / "evaluation_cache"),
        )
        self.env.reset(task_config=task)
        self._cdp_reported = False
        time.sleep(self.startup_wait_seconds)
        self._capture()
        self._step_seq = 0
        return self._observation(
            task, 0, Budget(steps_remaining=10**6, wall_seconds_remaining=10**6), None
        )

    def _step_with_ack(self, code: str, *, kind: str) -> Receipt:
        controller = self.env.controller
        original = controller.execute_python_command
        original_retries = controller.retry_times
        received: dict | None = None

        def acknowledged(command):
            nonlocal received
            received = original(command)
            if received is None or "returncode" not in received:
                raise RuntimeError(
                    "Action effect unknown: no execution acknowledgement; not replayed")
            return received

        controller.retry_times = 1
        controller.execute_python_command = acknowledged
        try:
            self.env.step(code, pause=1)
        finally:
            controller.execute_python_command = original
            controller.retry_times = original_retries
        result = received or {}
        if kind == "cli" and result.get("returncode") == 0:
            parsed = json.loads(result.get("output", "").strip() or "{}")
            stdout, stderr, returncode = parsed["stdout"], parsed["stderr"], parsed["returncode"]
        else:
            stdout = result.get("output", "")
            stderr = result.get("error", "")
            returncode = int(result.get("returncode", 0))
        output_ref = None
        if kind == "cli":
            output = self.run_root / "cli_outputs" / f"action_{self._step_seq:04d}.json"
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_text(json.dumps({"stdout": stdout, "stderr": stderr,
                                          "returncode": returncode}), encoding="utf-8")
            output_ref = str(output)
        return Receipt(
            returncode=returncode, stdout=stdout[-RECEIPT_TAIL:], stderr=stderr[-RECEIPT_TAIL:],
            kind=kind if kind in {"cli", "gui", "wait"} else "gui", acknowledged=True,
            full_output_ref=output_ref,
        )

    def execute(self, action: Action, *, budget: Budget, task: dict) -> tuple[Receipt, Observation]:
        if isinstance(action, (FinishAction, FailAction)):
            receipt = Receipt(returncode=0, stdout="", stderr="", kind="gui", acknowledged=True)
            self.env.step("DONE" if isinstance(action, FinishAction) else "FAIL", pause=1)
            return receipt, self._observation(task, self._step_seq, budget, receipt)
        if isinstance(action, WaitAction):
            self.env.step("WAIT", pause=1)
            receipt = Receipt(returncode=0, stdout="", stderr="", kind="wait", acknowledged=True)
        elif isinstance(action, CliAction):
            receipt = self._step_with_ack(action_code(action), kind="cli")
        else:
            receipt = self._step_with_ack(action_code(action), kind="gui")
        self._capture()
        self._step_seq += 1
        return receipt, self._observation(task, self._step_seq, budget, receipt)

    def export_outputs(self, paths: list[str], episode_dir) -> list[dict]:
        records = []
        directory = Path(episode_dir) / "deliverables"
        directory.mkdir(parents=True, exist_ok=True)
        for index, path in enumerate(dict.fromkeys(paths)):
            content = self.env.controller.get_file(path)
            record = {"vm_path": path, "exported": content is not None}
            if content is not None:
                target = directory / f"{index:02d}_{Path(path).name}"
                target.write_bytes(content)
                record.update(local_path=str(target), bytes=len(content))
            records.append(record)
        return records

    def score(self, task: dict) -> dict:
        return self.env.evaluate()

    def close(self) -> None:
        if self.env is not None:
            self.env.close()
            self.env = None
