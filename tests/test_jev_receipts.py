"""Mock HTTP receipts: lost responses never resubmit a mutation."""

import subprocess
import time
from types import SimpleNamespace

import pytest

from jev_gui.receipts import ReceiptUnavailable, run_python, run_terminal


class Reply:
    def __init__(self, data):
        self.data = data

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass

    def raise_for_status(self):
        pass

    def json(self):
        return self.data


def test_lost_submission_response_recovers_receipt_without_replay():
    requests = pytest.importorskip("requests")
    calls, events = [], []
    completed = {"status": "success", "output": "one execution", "error": "", "return_code": 0}

    def post(url, **kwargs):
        calls.append((url, kwargs))
        if url.endswith("/run_python"):
            raise requests.ReadTimeout("mock lost response AFTER delivery")
        return Reply({"state": "completed", "result": completed})

    backend = SimpleNamespace(base_url="http://mock-vm", _session=SimpleNamespace(post=post))
    result = run_python(backend, "print('one execution')", timeout=10,
                        wall_deadline=float("inf"), emit=events.append)
    assert result == completed
    assert [url.rsplit("/", 1)[-1] for url, _ in calls] == ["run_python", "file"]
    assert events[-1]["response_recovered"] and not events[-1]["action_replayed"]


def test_running_or_missing_receipt_stops_without_replay(monkeypatch):
    import jev_gui.receipts as receipts

    elapsed, calls = [0], []
    monkeypatch.setattr(receipts.time, "time", lambda: elapsed[0])
    monkeypatch.setattr(receipts.time, "sleep", lambda seconds: elapsed.__setitem__(0, elapsed[0] + seconds))

    def post(url, **kwargs):
        calls.append(url)
        return Reply({"status": "success"} if url.endswith("/run_python") else {"state": "started"})

    backend = SimpleNamespace(base_url="http://mock-vm", _session=SimpleNamespace(post=post))
    with pytest.raises(ReceiptUnavailable, match="started; command not replayed"):
        run_python(backend, "mock", timeout=1, wall_deadline=10, emit=lambda event: None)
    assert sum(url.endswith("/run_python") for url in calls) == 1
    assert not issubclass(ReceiptUnavailable, Exception)  # MCP's tool-error loop must not retry it.


def test_terminal_timeout_and_background_output_do_not_wait_for_inherited_pipes(tmp_path):
    def vm(code, timeout):
        # Explicit local mock; these commands only touch the test temp directory.
        result = subprocess.run(["python3", "-c", code], capture_output=True, text=True, timeout=4)
        assert result.returncode == 0, result.stderr
        return {"output": result.stdout}

    backend = SimpleNamespace(_run_python=vm)
    result = run_terminal(backend, {"command": "sleep 2 & printf ready", "cwd": str(tmp_path),
                                    "timeout_seconds": 1}, 10)
    assert result["stdout"] == "ready" and result["exit_code"] == 0
    result = run_terminal(backend, {"command": "sleep 20", "cwd": str(tmp_path),
                                    "timeout_seconds": 1}, 10)
    assert result["timed_out"] and result["exit_code"] < 0
    result = run_terminal(backend, {"command": "printf never", "cwd": str(tmp_path / "missing"),
                                    "timeout_seconds": 1}, 10)
    assert result["action_status"] == "not_executed" and result["exit_code"] is None


def test_launcher_detaches_job_instead_of_blocking_run_python():
    calls = []

    def post(url, **kwargs):
        calls.append((url, kwargs))
        return Reply({"status": "success"} if url.endswith("/run_python") else {
            "state": "completed", "result": {"status": "success", "output": "", "error": ""}})

    backend = SimpleNamespace(base_url="http://mock-vm", _session=SimpleNamespace(post=post))
    run_python(backend, "import time; time.sleep(60)", timeout=75,
               wall_deadline=float("inf"), emit=lambda event: None)
    launcher = calls[0][1]["json"]["code"]
    assert "start_new_session=True" in launcher and "subprocess.Popen" in launcher
    assert "subprocess.run(" not in launcher
    assert calls[1][1]["data"]["file_path"].endswith("/receipt.json")
    compile(launcher, "<mock-launcher>", "exec")
    # Parsing the embedded runner catches escaping mistakes without executing it.
    import ast

    runner = next(node.value for node in ast.walk(ast.parse(launcher)) if isinstance(node, ast.Constant)
                  and isinstance(node.value, str) and "contextlib" in node.value)
    compile(runner, "<mock-runner>", "exec")


def test_expired_mcp_deadline_never_submits_a_command():
    from northcua_runtime_shared.execution import execution_scope

    calls = []
    backend = SimpleNamespace(base_url="http://mock-vm", _session=SimpleNamespace(
        post=lambda *args, **kwargs: calls.append(args)))
    with execution_scope(time.monotonic() - 1), pytest.raises(ReceiptUnavailable, match="not executed"):
        run_python(backend, "must not run", timeout=60, wall_deadline=float("inf"), emit=lambda event: None)
    assert calls == []
