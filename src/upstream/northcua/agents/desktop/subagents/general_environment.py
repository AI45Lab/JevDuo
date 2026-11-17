"""Bounded, single-delivery GUI and shell commands in the current OSWorld VM."""

from __future__ import annotations

import ast
import asyncio
import json
import textwrap
from pathlib import PurePosixPath
from typing import Literal

import httpx
from pydantic import Field, StrictInt, model_validator

from northcua.agentkit.orchestration.contracts import ContractModel
from northcua.core.config import DesktopPolicyConfig


class GuiCode(ContractModel):
    code: str = Field(min_length=1, max_length=32768)


class ShellCommand(ContractModel):
    command: str = Field(min_length=1, max_length=65536)
    cwd: str = "/home/user"
    timeout: StrictInt = Field(default=60, ge=1, le=90)

    @model_validator(mode="after")
    def absolute_cwd(self):
        if not PurePosixPath(self.cwd).is_absolute():
            raise ValueError("cwd must be an absolute path inside the task VM")
        return self


class Wait(ContractModel):
    seconds: StrictInt = Field(default=1, ge=1, le=60)


class Finish(ContractModel):
    status: Literal["success", "failure"]
    answer: str = Field(min_length=1)


class Screenshot(ContractModel):
    pass


TOOL_MODELS = {
    "execute_pyautogui_code": (
        GuiCode,
        "Execute straight-line PyAutoGUI calls using absolute screenshot pixels. "
        "pyautogui, pyperclip and time are pre-imported. Use literal arguments. "
        "For Unicode text use pyperclip.copy(text), then pyautogui.hotkey('ctrl', 'v').",
    ),
    "run_command": (
        ShellCommand,
        "Run Bash directly inside the CURRENT TASK VM. Returns real stdout, stderr, "
        "returncode, timed_out and truncation flags. Never runs on the controller.",
    ),
    "take_screenshot": (Screenshot, "Observe the current task desktop without changing it."),
    "wait_for_state": (Wait, "Wait briefly without interacting with the desktop."),
    "finish_task": (
        Finish,
        "Return a subgoal completion claim or failure to the parent for fresh verification.",
    ),
}

GUI_METHODS = frozenset(
    {
        "click",
        "doubleClick",
        "tripleClick",
        "rightClick",
        "middleClick",
        "moveTo",
        "moveRel",
        "dragTo",
        "dragRel",
        "mouseDown",
        "mouseUp",
        "scroll",
        "hscroll",
        "vscroll",
        "write",
        "typewrite",
        "press",
        "hotkey",
        "keyDown",
        "keyUp",
        "sleep",
    }
)


def validate_gui_code(code: str) -> None:
    """Structural language restriction, not a guess about task authorization."""
    tree = ast.parse(code)
    if not tree.body:
        raise ValueError("Empty GUI code")
    for statement in tree.body:
        if isinstance(statement, ast.Import) and all(
            alias.name in {"pyautogui", "pyperclip", "time"} and alias.asname is None
            for alias in statement.names
        ):
            continue
        if not isinstance(statement, ast.Expr) or not isinstance(statement.value, ast.Call):
            raise ValueError("Use only straight-line GUI calls with literal arguments")
        call = statement.value
        function = call.func
        if not isinstance(function, ast.Attribute) or not isinstance(function.value, ast.Name):
            raise ValueError("Use pyautogui methods, pyperclip.copy or time.sleep")
        module, method = function.value.id, function.attr
        allowed = (
            (module == "pyautogui" and method in GUI_METHODS)
            or (module == "pyperclip" and method == "copy")
            or (module == "time" and method == "sleep")
        )
        if not allowed or any(keyword.arg is None for keyword in call.keywords):
            raise ValueError("Unsupported GUI operation")
        for argument in [*call.args, *(keyword.value for keyword in call.keywords)]:
            ast.literal_eval(argument)
    compile(tree, "<general-gui>", "exec")


# The guest HTTP /execute handler itself has a 120s timeout. Kill the child
# process group first and finish the wrapper well before that outer deadline.
# Temporary files bound returned output without buffering unbounded child output.
_WRAPPER = textwrap.dedent("""\
    import json, os, signal, subprocess, sys, tempfile
    spec = json.loads(sys.argv[1])
    with tempfile.TemporaryFile() as out, tempfile.TemporaryFile() as err:
        process = subprocess.Popen(spec['argv'], cwd=spec['cwd'], stdout=out, stderr=err,
                                   start_new_session=True)
        timed_out = False
        try:
            process.wait(timeout=spec['timeout'])
        except subprocess.TimeoutExpired:
            timed_out = True
            os.killpg(process.pid, signal.SIGKILL)
            process.wait()
        def read(stream):
            size = stream.tell()
            stream.seek(0)
            return stream.read(32768).decode('utf-8', errors='replace'), size > 32768
        stdout, stdout_truncated = read(out)
        stderr, stderr_truncated = read(err)
        print(json.dumps(dict(stdout=stdout, stderr=stderr, returncode=process.returncode,
                              timed_out=timed_out, stdout_truncated=stdout_truncated,
                              stderr_truncated=stderr_truncated)))
""")


class GeneralEnvironment:
    def __init__(self, desktop: DesktopPolicyConfig, *, mode: str = "hybrid"):
        if desktop.backend != "osworld":
            raise ValueError("general currently requires desktop.backend=osworld")
        self.endpoint = f"http://{desktop.osworld.vm_ip}:{desktop.osworld.server_port}/execute"
        self.mode = mode

    async def execute(self, name: str, arguments: dict, *, max_seconds: float = 100) -> dict:
        if name == "wait_for_state":
            await asyncio.sleep(Wait.model_validate(arguments).seconds)
            return {"ok": True, "waited": True}
        if name == "execute_pyautogui_code":
            code = GuiCode.model_validate(arguments).code
            validate_gui_code(code)
            argv = ["python3", "-c", "import pyautogui, pyperclip, time\n" + code]
            cwd, timeout = "/home/user", 30
        elif name == "run_command":
            if self.mode != "hybrid":
                raise ValueError("run_command is unavailable in GUI mode")
            command = ShellCommand.model_validate(arguments)
            argv = ["bash", "-lc", command.command]
            cwd, timeout = command.cwd, command.timeout
        else:
            raise ValueError("unknown_general_tool")
        if max_seconds <= 1:
            raise ValueError("Insufficient time to execute and collect a guest result")
        timeout = min(timeout, max_seconds - 1)
        spec = {"argv": argv, "cwd": cwd, "timeout": timeout}
        # Shield submitted delivery so cancellation cannot release the parent
        # while the HTTP operation is still in flight. No second POST or replay.
        task = asyncio.create_task(self._post(spec, min(timeout + 5, max_seconds)))
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError as error:
            while not task.done():
                try:
                    await asyncio.shield(task)
                except asyncio.CancelledError:
                    continue
            # Nested asyncio.timeout contexts replace cancellation with
            # TimeoutError. Let the dispatch owner persist this result before
            # that conversion, while external cancellation still propagates.
            error.general_delivery_result = task.result()
            raise

    async def _post(self, spec: dict, timeout: float) -> dict:
        try:
            async with (
                asyncio.timeout(timeout),
                httpx.AsyncClient(trust_env=False, follow_redirects=False) as client,
            ):
                response = await client.post(
                    self.endpoint,
                    json={"command": ["python3", "-c", _WRAPPER, json.dumps(spec)], "shell": False},
                    timeout=timeout,
                )
                response.raise_for_status()
            envelope = response.json()
            if not isinstance(envelope, dict) or type(envelope.get("returncode")) is not int:
                raise ValueError("invalid guest execution envelope")
            if envelope["returncode"] != 0:
                raise ValueError("guest execution wrapper failed")
            result = json.loads(envelope["output"])
            if not isinstance(result, dict) or (
                type(result.get("returncode")) is not int
                or type(result.get("timed_out")) is not bool
                or not isinstance(result.get("stdout"), str)
                or not isinstance(result.get("stderr"), str)
            ):
                raise ValueError("invalid guest execution result")
            return {
                **result,
                "ok": result["returncode"] == 0 and not result["timed_out"],
                "delivery": "confirmed",
                "unsafe_to_replay": result["timed_out"],
            }
        except (TimeoutError, httpx.HTTPError, ValueError, KeyError, TypeError) as error:
            # A lost HTTP response cannot cancel the guest process. Wait out
            # one full guest execution window before permitting parent actions.
            # This drain can extend a caller's deadline; cancellation still
            # joins it and never authorizes a second delivery.
            await asyncio.sleep(spec["timeout"] + 1)
            return {
                "ok": False,
                "delivery": "unknown",
                "unsafe_to_replay": True,
                "error_type": type(error).__name__,
                "error": "REMOTE_EXECUTION_UNCONFIRMED",
            }
