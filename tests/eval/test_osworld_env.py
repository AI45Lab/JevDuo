"""osworld 适配器：代码生成 / 凭据装载 / 任务定位 / 假 DesktopEnv 全流程。"""

from __future__ import annotations

import json
import struct
import xml.etree.ElementTree as ET
from pathlib import Path
from types import ModuleType

import pytest

from jevduo.protocol.models import (
    Budget,
    CliAction,
    FailAction,
    FinishAction,
    GuiAction,
    WaitAction,
)
from jevduo_eval.osworld import (
    OsWorldEnv,
    action_code,
    gui_code,
    png_size,
)


def _png(width: int = 8, height: int = 4) -> bytes:
    return b"\x89PNG\r\n\x1a\n" + b"\x00" * 8 + struct.pack(">II", width, height) + b"tail"


# ------------------------------------------------------------------ codegen
def test_gui_code_matches_legacy_pyautogui_semantics():
    assert gui_code(GuiAction("click", {"x": 140, "y": 212})) == "pyautogui.click(x=140, y=212)"
    assert gui_code(GuiAction("double_click", {"x": 1, "y": 2})) == "pyautogui.doubleClick(x=1, y=2)"
    assert gui_code(GuiAction("hotkey", {"sequence": [["ctrl", "l"], ["enter"]]})) == (
        "pyautogui.hotkey('ctrl', 'l'); __import__('time').sleep(0.2); pyautogui.press('enter')"
    )
    assert gui_code(GuiAction("scroll", {"x": 300, "y": 150, "dy": -5})) == (
        "pyautogui.moveTo(300, 150); pyautogui.scroll(-5)"
    )
    assert gui_code(GuiAction("type", {
        "text": "42", "input_mode": "replace", "click": [650, 102], "press_f2": True,
    })) == ("pyautogui.click(x=650, y=102); pyautogui.press('f2'); "
            "pyautogui.hotkey('ctrl', 'a'); pyautogui.write('42', interval=0.03)")
    assert gui_code(GuiAction("type", {
        "text": "cmd", "input_mode": "insert", "click": None, "press_f2": False,
    })) == "pyautogui.write('cmd', interval=0.03)"


def test_action_code_cli_wait_and_terminals():
    assert action_code(WaitAction()) == "WAIT"
    assert action_code(FinishAction()) == "DONE"
    assert action_code(FailAction("x")) == "FAIL"
    code = action_code(CliAction(command="ls -la", timeout_s=30, want_observation="screenshot"))
    assert "bash" in code and "subprocess.run" in code and "'ls -la'" in code
    with pytest.raises(ValueError):
        gui_code(GuiAction("drag", {"x": 1}))


@pytest.mark.parametrize("text", ["First line\nSecond line", "Unicode: 合成测试", "Column\tValue"])
def test_multiline_and_unicode_use_bounded_clipboard_without_typing_enter(text):
    code = gui_code(GuiAction("type", {"text": text, "input_mode": "replace"}))
    assert "pyautogui.write" not in code
    assert "xclip" in code and "timeout=10" in code
    assert "DEVNULL" in code and "hotkey('ctrl','v')" in code


def test_png_size_parses_ihdr():
    assert png_size(_png(1920, 1080)) == (1920, 1080)


def test_read_only_refresh_keeps_cdp_receipt_and_prior_evidence(tmp_path, env, monkeypatch):
    snapshot = {"elements": [{"index": 1, "role": "entry", "name": "Receipt Date"}]}
    monkeypatch.setattr("jevduo_eval.osworld.capture_browser_snapshot",
                        lambda controller: (snapshot, {"status": "ok"}))
    task = _task_file(tmp_path)
    first = env.reset(task)
    budget = Budget(steps_remaining=10, wall_seconds_remaining=60)
    receipt, previous = env.execute(GuiAction("click", {"x": 1, "y": 2}), budget=budget, task=task)
    steps, commands = list(env.env.steps), list(env.env.controller.commands)
    before = Path(previous.screenshot.path).read_bytes()
    refreshed = env.observe(task, budget=budget, previous=previous)
    assert refreshed.protocol_version == "0.2" and refreshed.last_receipt is receipt
    assert refreshed.step_idx == previous.step_idx
    assert env.env.steps == steps
    assert env.env.controller.commands[:-1] == commands
    assert "_NET_ACTIVE_WINDOW" in env.env.controller.commands[-1]  # Read-only window observation.
    tree = ET.fromstring(Path(refreshed.accessibility_tree.path).read_text())
    assert tree.get("foreground_title") == "main-window"
    assert json.loads(tree.get("foreground_class")) == ["app", "App"]
    assert refreshed.browser_snapshot.source == "cdp"
    assert json.loads(Path(refreshed.browser_snapshot.path).read_text()) == snapshot
    assert refreshed.screenshot.path != previous.screenshot.path != first.screenshot.path
    assert Path(previous.screenshot.path).read_bytes() == before


def test_empty_screenshot_retries_only_observation(tmp_path, env, monkeypatch):
    from unittest.mock import Mock

    task = _task_file(tmp_path)
    previous = env.reset(task)
    valid = env.env._get_obs()
    observe = Mock(side_effect=[{"screenshot": None}, {"screenshot": b""}, valid])
    monkeypatch.setattr(env.env, "_get_obs", observe)
    monkeypatch.setattr("jevduo_eval.osworld.time.sleep", lambda seconds: None)
    budget = Budget(steps_remaining=5, wall_seconds_remaining=30)
    refreshed = env.observe(task, budget=budget, previous=previous)
    assert observe.call_count == 3 and env.env.steps == []
    assert refreshed.step_idx == previous.step_idx and refreshed.last_receipt is None
    assert Path(refreshed.screenshot.path).read_bytes() == valid["screenshot"]


def test_repeated_empty_screenshot_is_explicit_observation_failure(tmp_path, env, monkeypatch):
    from unittest.mock import Mock

    task = _task_file(tmp_path)
    previous = env.reset(task)
    observe = Mock(return_value={"screenshot": None})
    monkeypatch.setattr(env.env, "_get_obs", observe)
    monkeypatch.setattr("jevduo_eval.osworld.time.sleep", lambda seconds: None)
    with pytest.raises(RuntimeError, match="no screenshot after three read-only"):
        env.observe(task, budget=previous.budget, previous=previous)
    assert observe.call_count == 3 and env.env.steps == []


def test_right_click_codegen_uses_real_mouse_button():
    assert gui_code(GuiAction("right_click", {"x": 20, "y": 30})) == "pyautogui.rightClick(x=20, y=30)"


# ------------------------------------------------------------------ adapter
class FakeController:
    def __init__(self):
        self.retry_times = 3
        self.commands = []
        self.files = {"/home/user/out.txt": b"DATA"}

    def execute_python_command(self, command):
        self.commands.append(command)
        if "_NET_ACTIVE_WINDOW" in command:
            return {"returncode": 0, "output": json.dumps({
                "foreground_title": "main-window", "foreground_class": ["app", "App"]})}
        return {"returncode": 0, "output": json.dumps(
            {"returncode": 0, "stdout": "ok-out", "stderr": ""})}

    def get_file(self, path):
        return self.files.get(path)


class FakeDesktopEnv:
    instances = []

    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.controller = FakeController()
        self.steps = []
        self.closed = False
        FakeDesktopEnv.instances.append(self)

    def reset(self, task_config):
        self.task_config = task_config

    def _get_obs(self):
        return {"screenshot": _png(1920, 1080), "accessibility_tree":
                '<application name="app" active="true">'
                '<push_button name="OK" screencoord="(1,2)" size="(30,10)"/></application>'}

    def step(self, code, pause=1):
        self.steps.append(code)
        # 真实 DesktopEnv 经 controller 执行代码；回执由此产生
        self.controller.execute_python_command(code)
        return self._get_obs(), None, None, None

    def evaluate(self):
        return {"score": 1.0}

    def close(self):
        self.closed = True


@pytest.fixture
def env(tmp_path):
    return OsWorldEnv(
        benchmark="osworld1",
        task_root=tmp_path / "osw1",
        vm_path=tmp_path / "vm.qcow2",
        run_root=tmp_path / "run",
        startup_wait_seconds=0.0,
        env_factory=lambda **kwargs: FakeDesktopEnv(),
    )


def _task_file(tmp_path):
    examples = tmp_path / "osw1" / "evaluation_examples" / "examples" / "chrome"
    examples.mkdir(parents=True)
    task = {"id": "t-1", "instruction": "打开文件", "config": [
        {"type": "open", "parameters": {"path": "/home/user/in.txt"}}]}
    (examples / "t-1.json").write_text(json.dumps(task), encoding="utf-8")
    return task


def test_fetch_task_appends_input_files(tmp_path, env):
    _task_file(tmp_path)
    task = env.fetch_task("t-1", "chrome")
    assert '"/home/user/in.txt"' in task["instruction"]


def test_fetch_task_domainless_search(tmp_path, env):
    _task_file(tmp_path)
    assert env.fetch_task("t-1").get("id") == "t-1"


def test_reset_execute_score_lifecycle(tmp_path, env):
    task = _task_file(tmp_path)
    observation = env.reset(task)
    assert observation.screenshot.width == 1920
    assert observation.accessibility_tree.present and observation.accessibility_tree.source == "vm"
    assert Path(observation.screenshot.path).is_file()
    budget = Budget(steps_remaining=10, wall_seconds_remaining=60.0)
    receipt, obs = env.execute(GuiAction("click", {"x": 1, "y": 2}), budget=budget, task=task)
    assert receipt.kind == "gui" and receipt.acknowledged
    assert obs.step_idx == 1 and obs.last_receipt is not None
    cli_receipt, _ = env.execute(
        CliAction(command="ls", timeout_s=30, want_observation="screenshot"),
        budget=budget, task=task)
    assert cli_receipt.stdout == "ok-out" and cli_receipt.kind == "cli"
    fake = FakeDesktopEnv.instances[-1]
    assert "subprocess.run" in fake.steps[-1]
    # 终态送达 VM（评分钩子依赖）
    env.execute(FinishAction(outputs=("/home/user/out.txt",)), budget=budget, task=task)
    assert fake.steps[-1] == "DONE"
    env.execute(FailAction("放弃"), budget=budget, task=task)
    assert fake.steps[-1] == "FAIL"
    assert env.score(task) == {"score": 1.0}
    records = env.export_outputs(["/home/user/out.txt"], tmp_path / "run" / "ep" / "t-1")
    assert records[0]["exported"] and Path(records[0]["local_path"]).read_bytes() == b"DATA"
    env.close()
    assert fake.closed


def test_browser_snapshot_is_a_hashed_observation_file(tmp_path, env, monkeypatch):
    monkeypatch.setattr(
        "jevduo_eval.osworld.capture_browser_snapshot",
        lambda controller: ({"elements": [{"name": "Save"}], "url": "https://example.test"},
                            {"available": True}),
    )
    task = _task_file(tmp_path)
    observation = env.reset(task)
    reference = observation.browser_snapshot
    assert reference is not None and reference.present and reference.source == "cdp"
    assert reference.node_count == 1 and len(reference.digest) == 64
    assert json.loads(Path(reference.path).read_text())["elements"][0]["name"] == "Save"


def test_ack_discipline_requires_receipt(tmp_path, env):
    task = _task_file(tmp_path)
    env.reset(task)
    fake = FakeDesktopEnv.instances[-1]
    original = fake.controller.execute_python_command
    fake.controller.execute_python_command = lambda command: None
    budget = Budget(steps_remaining=5, wall_seconds_remaining=30.0)
    with pytest.raises(RuntimeError, match="acknowledgement"):
        env.execute(GuiAction("click", {"x": 1, "y": 2}), budget=budget, task=task)
    fake.controller.execute_python_command = original


def test_setup_cache_seeding(tmp_path):
    import shutil as _shutil

    cache = tmp_path / "share-cache" / "t-1"
    cache.mkdir(parents=True)
    (cache / "951cf1e8_profile.tar.gz").write_bytes(b"tar")
    (cache / "thunder-prefs.js").write_text("prefs", encoding="utf-8")
    env = OsWorldEnv(
        benchmark="osworld1", task_root=tmp_path / "osw1", vm_path=tmp_path / "vm.qcow2",
        run_root=tmp_path / "run", setup_cache_root=tmp_path / "share-cache",
        startup_wait_seconds=0.0, env_factory=lambda **kwargs: FakeDesktopEnv(),
    )
    task = {"id": "t-1", "instruction": "x", "config": []}
    env.reset(task)
    seeded = tmp_path / "run" / "evaluation_cache" / "t-1"
    assert sorted(p.name for p in seeded.iterdir()) == [
        "951cf1e8_profile.tar.gz", "thunder-prefs.js"]
    # 无缓存任务静默跳过
    env.reset({"id": "t-2", "instruction": "x", "config": []})
    assert not (tmp_path / "run" / "evaluation_cache" / "t-2").exists()
    del _shutil


@pytest.mark.parametrize("benchmark", ["osworld2", "osworld21"])
def test_task_module_level_asset_urls_resolve_locally(tmp_path, monkeypatch, benchmark):
    """OSW2 任务模块在 import 时（fetch_task 阶段）顶层调用 asset()；
    OSWORLD_FILE_BASE_URL 必须在那之前就绪（task_001 真机 404 根因）。"""
    import os
    import sys

    monkeypatch.setattr(sys, "path", sys.path.copy())
    monkeypatch.setenv("OSWORLD_FILE_BASE_URL", "before")
    for name in ("desktop_env", "desktop_env.file_source", "task_loader",
                 "task_class", "task_class.task_001"):
        # 同时记录原先不存在的模块，确保临时导入也能在测试结束时撤销。
        monkeypatch.setitem(sys.modules, name, sys.modules.get(name))
        monkeypatch.delitem(sys.modules, name, raising=False)
    root = tmp_path / "osw2"
    (root / "desktop_env").mkdir(parents=True)
    (root / "desktop_env" / "__init__.py").write_text("", encoding="utf-8")
    (root / "desktop_env" / "file_source.py").write_text(
        "import os\n"
        "DEFAULT = 'https://huggingface.co/datasets/xlangai/osworld_v2_assets/resolve/main'\n"
        "def asset(path):\n"
        "    return os.path.join(os.environ.get('OSWORLD_FILE_BASE_URL', DEFAULT), path)\n",
        encoding="utf-8")
    task_class = root / "task_class"
    task_class.mkdir()
    (task_class / "task_001.py").write_text(
        "from desktop_env.file_source import asset\n"
        "THUNDERBIRD_PROFILE_URL = asset('task_001/profile.tar.gz')\n"
        "def get_instruction():\n"
        "    return {'id': '001', 'instruction': '做', 'config': [], 'THUMB': THUNDERBIRD_PROFILE_URL}\n",
        encoding="utf-8")
    (root / "task_loader.py").write_text(
        "import importlib\n"
        "def load_task_from_file(path):\n"
        "    name = path.rsplit('/', 1)[-1][:-3]\n"
        "    module = importlib.import_module(f'task_class.{name}')\n"
        "    return module.get_instruction()\n",
        encoding="utf-8")

    assets = tmp_path / "assets"
    env = OsWorldEnv(benchmark=benchmark, task_root=root, vm_path=tmp_path / "vm",
                     run_root=tmp_path / "run", asset_root=assets,
                     startup_wait_seconds=0.0, env_factory=lambda **kw: FakeDesktopEnv())
    task = env.fetch_task("001")  # 先于 reset：模块级 asset() 必须已指向本地
    assert task["THUMB"] == str(assets / "task_001" / "profile.tar.gz")
    assert os.environ.get("OSWORLD_FILE_BASE_URL") == str(assets)


def test_task_object_is_passed_through_to_desktop_env(tmp_path, monkeypatch):
    import sys

    monkeypatch.setattr(sys, "path", sys.path.copy())
    calls = []

    class NativeTask(dict):
        def setup(self, controller):
            calls.append("setup")

        def evaluate(self, env):
            calls.append("evaluate")
            return {"score": 0.25}

    task = NativeTask(id="003", instruction="test", config=[])
    loader = ModuleType("task_loader")
    loader.load_task_from_file = lambda path: task
    monkeypatch.setitem(sys.modules, "task_loader", loader)

    class NativeDesktop(FakeDesktopEnv):
        def reset(self, task_config):
            super().reset(task_config)
            task_config.setup(self.controller)

        def evaluate(self):
            return self.task_config.evaluate(self)

    env = OsWorldEnv(benchmark="osworld21", task_root=tmp_path / "runtime", vm_path=tmp_path / "vm",
                     run_root=tmp_path / "run", startup_wait_seconds=0, env_factory=NativeDesktop)
    loaded = env.fetch_task("003")
    assert loaded is task
    env.reset(loaded)
    assert env.env.task_config is task
    assert env.score(loaded) == {"score": 0.25}
    assert calls == ["setup", "evaluate"]
    env.close()
    assert env.env is None
