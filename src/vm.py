"""OSWorld 2.1 虚拟机生命周期（worker 侧，跑在冻结的 OSWORLD_PYTHON 里）。

只暴露评测需要的最小面：fetch_task / reset / vm_ip / server_port / execute / score / close。
不解析 a11y、不做 CDP 快照——那些是 agent 子进程自己的事。
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path
from typing import Any


class Vm:
    def __init__(self, *, task_root: str | Path, vm_path: str | Path,
                 run_root: str | Path, asset_root: str | Path | None = None,
                 startup_wait_seconds: float = 30.0):
        self.task_root = Path(task_root).resolve()
        self.vm_path = str(vm_path)
        self.run_root = Path(run_root)
        self.asset_root = Path(asset_root) if asset_root else None
        self.startup_wait_seconds = float(startup_wait_seconds)
        self.env: Any = None
        # 任务模块 import 期就把资产 URL 固化，必须在 fetch_task 之前设好
        self._bootstrap()

    def _bootstrap(self) -> None:
        root = str(self.task_root)
        # 基础镜像里的旧 desktop_env（/opt/osworld-v2）会顶掉共享 runtime 的版本：
        # 旧版 SetupController.execute 没有 check 参数，task setup 会直接报
        # "unexpected keyword argument 'check'"。这里把不该出现的路径与已加载模块清掉。
        self._pin_desktop_env()
        if root not in sys.path:
            sys.path.insert(0, root)
        if self.asset_root is not None:
            self.__dict__["asset_url"] = str(self.asset_root)
            import os

            os.environ["OSWORLD_FILE_BASE_URL"] = str(self.asset_root)
        self.env = None

    def _pin_desktop_env(self) -> None:
        """保证 desktop_env 从 task_root 加载，清掉其它位置的同名包。

        sys.path 里的空串（或 "."）表示当前目录，同样可能藏着旧副本；镜像的
        WORKDIR 就是 /opt/osworld-v2，必须按解析后的真实路径判断。
        """
        root = self.task_root
        kept = []
        for entry in sys.path:
            base = Path(entry or ".").resolve()
            if (base / "desktop_env").is_dir() and not base.is_relative_to(root):
                continue
            kept.append(entry)
        sys.path[:] = kept
        for name in [item for item in sys.modules
                     if item == "desktop_env" or item.startswith("desktop_env.")]:
            module = sys.modules[name]
            origin = getattr(module, "__file__", "") or ""
            if origin and not Path(origin).is_relative_to(root):
                del sys.modules[name]

    def _check_desktop_env(self) -> None:
        """导入后断言：desktop_env 必须来自 task_root，否则后续错误极难定位。"""
        module = sys.modules.get("desktop_env")
        origin = getattr(module, "__file__", "") or ""
        if origin and not Path(origin).is_relative_to(self.task_root):
            raise RuntimeError(
                f"desktop_env 从 {origin} 加载，而不是 {self.task_root}；"
                "环境里有旧副本抢先，setup 会报 'unexpected keyword argument check'")

    @property
    def vm_ip(self) -> str:
        return self.env.vm_ip

    @property
    def server_port(self) -> int:
        return int(self.env.server_port)

    def fetch_task(self, task_id: str) -> dict:
        path = self.task_root / "evaluation_examples" / "task_class" / f"task_{task_id}.py"
        if not path.is_file():
            raise FileNotFoundError(f"任务文件不存在: {path}")
        import os

        os.chdir(self.task_root)
        from task_loader import load_task_from_file

        task = load_task_from_file(str(path))
        task.setdefault("id", task_id)
        input_paths = [
            item["parameters"]["path"] for item in task.get("config", [])
            if item.get("type") == "open" and "path" in item.get("parameters", {})
        ]
        if input_paths:
            task["instruction"] = task["instruction"] + (
                "\nProvided input files: " + json.dumps(input_paths)
            )
        return task

    def reset(self, task: dict) -> None:
        if self.env is not None:
            self.env.close()
            self.env = None
        import os

        os.chdir(self.task_root)
        from desktop_env.desktop_env import DesktopEnv

        self._check_desktop_env()

        self.env = DesktopEnv(
            provider_name="docker",
            path_to_vm=self.vm_path,
            action_space="pyautogui",
            screen_size=(1920, 1080),
            headless=True,
            os_type="Ubuntu",
            require_a11y_tree=True,
            cache_dir=str(self.run_root / "evaluation_cache"),
        )
        self.env.reset(task_config=task)
        time.sleep(self.startup_wait_seconds)

    def execute(self, action: str, pause: int = 1) -> None:
        """下发终态动作 DONE / FAIL（评分钩子依赖它）。"""
        if self.env is not None:
            self.env.step(action, pause=pause)

    def score(self, task: dict) -> Any:
        if self.env is None:
            return None
        return self.env.evaluate()

    def close(self) -> None:
        if self.env is not None:
            try:
                self.env.close()
            finally:
                self.env = None
