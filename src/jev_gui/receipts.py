"""Task-VM Python jobs with one submission and read-only receipt recovery.

OSWorld's /run_python kills its foreground Python after 30 seconds. A short
launcher starts an independent job; /file only reads its completion receipt. A
missing response never causes another launcher or GUI command to be sent.
"""

from __future__ import annotations

import json
import textwrap
import time
import uuid

JOB = '''
import contextlib, json, traceback
from pathlib import Path
p = Path({directory!r})
def receipt(value):
    temporary = p / "next.json"
    temporary.write_text(json.dumps(value))
    temporary.replace(p / "receipt.json")
receipt({{"state": "started"}})
status, error, return_code = "success", "", 0
with (p / "stdout").open("w") as out, (p / "stderr").open("w") as err:
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        try:
            exec(compile({code!r}, "<task-vm-command>", "exec"), {{"__name__": "__main__"}})
        except BaseException:
            status, return_code = "error", 1
            traceback.print_exc()
output = (p / "stdout").read_text(errors="replace")
error = (p / "stderr").read_text(errors="replace")
receipt({{"state": "completed", "result": {{"status": status, "output": output,
    "error": error, "return_code": return_code}}}})
'''


class ReceiptUnavailable(BaseException):
    """Unresolved input bypasses the agent's ordinary tool-error/retry handlers."""


def run_python(backend, code, *, timeout, wall_deadline, emit):
    """Submit exactly once, then read the receipt even if that response is lost."""
    import requests
    from northcua_runtime_shared.execution import current_execution

    request_id = uuid.uuid4().hex
    directory = f"/tmp/jevduo-command-{request_id}"
    deadline = min(wall_deadline, time.time() + timeout)
    context = current_execution()
    if context is not None:
        deadline = min(deadline, time.time() + context.deadline - time.monotonic())
    if deadline <= time.time():
        raise ReceiptUnavailable("Command budget exhausted before submission; not executed")
    job = textwrap.dedent(JOB).format(directory=directory, code=textwrap.dedent(code))
    launcher = (
        "import pathlib, subprocess\n"
        f"p = pathlib.Path({directory!r})\n"
        "p.mkdir(mode=0o700)\n"
        "(p / 'receipt.json').write_text('{\"state\": \"accepted\"}')\n"
        f"(p / 'job.py').write_text({job!r})\n"
        "with (p / 'runner.log').open('w') as log:\n"
        "    subprocess.Popen(['/usr/bin/python3', str(p / 'job.py')], stdin=subprocess.DEVNULL,\n"
        "        stdout=log, stderr=log, start_new_session=True, close_fds=True)\n"
        "print('accepted')\n"
    )
    emit({"route": "vm_command_submitted", "receipt_id": request_id,
          "receipt_path": directory + "/receipt.json", "time": time.time()})
    submission_error = None
    try:
        with backend._session.post(backend.base_url + "/run_python", json={"code": launcher},
                                   timeout=min(10, max(0.001, deadline - time.time()))) as response:
            response.raise_for_status()
            data = response.json()
            if data.get("status") == "error":
                submission_error = "launcher_error"
    except (requests.RequestException, ValueError) as error:
        submission_error = type(error).__name__
        emit({"route": "vm_command_response_lost", "receipt_id": request_id,
              "error_type": submission_error, "http_status": getattr(
                  getattr(error, "response", None), "status_code", None), "action_replayed": False})
    last_state = "unavailable"
    while time.time() < deadline:
        # /file is a read-only endpoint despite its HTTP POST signature. Do not
        # use the upstream mutation transport or poison its failure context.
        try:
            with backend._session.post(backend.base_url + "/file",
                    data={"file_path": directory + "/receipt.json"},
                    timeout=min(5, max(0.001, deadline - time.time()))) as response:
                response.raise_for_status()
                receipt = response.json()
            last_state = receipt["state"]
            if last_state == "completed":
                result = receipt["result"]
                emit({"route": "vm_command_completed", "receipt_id": request_id,
                      "status": result["status"], "response_recovered": bool(submission_error),
                      "action_replayed": False, "time": time.time()})
                if result["status"] == "error":
                    from northcua.runtime.mcp.desktop_native._models import DesktopNativeError

                    raise DesktopNativeError(result["error"])
                return result
        except (requests.RequestException, ValueError):
            pass
        time.sleep(min(0.5, max(0, deadline - time.time())))
    emit({"route": "vm_command_unresolved", "receipt_id": request_id, "last_state": last_state,
          "submission_error": submission_error, "action_replayed": False, "time": time.time()})
    raise ReceiptUnavailable(f"VM receipt {request_id}: {last_state}; command not replayed")


def run_terminal(backend, parameters, remaining_seconds):
    """One recorded command; kill its VM process group on timeout, never replay it."""
    seconds = max(1, min(parameters["timeout_seconds"], int(remaining_seconds)))
    payload = json.dumps({**parameters, "timeout_seconds": seconds})
    script = f"""
import json, os, signal, subprocess, tempfile, time
p = json.loads({payload!r})
started = time.monotonic()
timed_out = False
with tempfile.TemporaryFile(mode="w+", errors="replace") as stdout, \\
        tempfile.TemporaryFile(mode="w+", errors="replace") as stderr:
    try:
        process = subprocess.Popen(["bash", "-lc", p["command"]], cwd=p["cwd"],
            stdout=stdout, stderr=stderr, start_new_session=True)
    except OSError as error:
        result = {{"stdout": "", "stderr": str(error), "exit_code": None,
                   "timed_out": False, "action_status": "not_executed"}}
    else:
        try:
            process.wait(timeout=p["timeout_seconds"])
        except subprocess.TimeoutExpired:
            timed_out = True
            os.killpg(process.pid, signal.SIGKILL)
            process.wait()
        stdout.seek(0); stderr.seek(0)
        out, err = stdout.read(20001), stderr.read(20001)
        result = {{"stdout": out[:20000], "stderr": err[:20000], "exit_code": process.returncode,
            "timed_out": timed_out, "stdout_truncated": len(out) > 20000,
            "stderr_truncated": len(err) > 20000, "action_status": "acknowledged"}}
    result["seconds"] = time.monotonic() - started
    print(json.dumps(result))
"""
    response = backend._run_python(script, timeout=seconds + 15)
    result = json.loads(response["output"])
    return {
        "text": json.dumps(result, ensure_ascii=False),
        **result,
        "action_status": result.get("action_status", "acknowledged"),
        "screenshot_policy": "none",
        "execution_location": "task_vm",
        "completion_is_task_success": False,
    }
