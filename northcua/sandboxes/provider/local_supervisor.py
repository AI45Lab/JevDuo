"""Own a local worker and its observed descendants until its lease ends.

Playwright starts browsers in separate sessions. Track process identities and an
inherited allocation marker so a worker exit cannot hide a newly detached child.
No process is signaled using a PID recovered from disk.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time
import uuid

import psutil

_OWNERSHIP_ENV = "NORTHCUA_LOCAL_ALLOCATION"


def _baseline() -> set[tuple[int, float]]:
    identities = set()
    for process in psutil.process_iter():
        try:
            identities.add((process.pid, process.create_time()))
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            pass
    return identities


def _remember_marked(marker: str, baseline: set[tuple[int, float]], owned: dict[int, psutil.Process]) -> list[int]:
    unreadable = []
    for process in psutil.process_iter():
        try:
            if process.uids().real != os.getuid():
                continue
            identity = (process.pid, process.create_time())
            if identity in baseline or not _alive(process):
                continue
            if process.environ().get(_OWNERSHIP_ENV) == marker:
                owned[process.pid] = process
        except psutil.NoSuchProcess:
            pass
        except psutil.AccessDenied:
            # A newly appeared process may be ours. Never signal it without
            # ownership evidence, or claim complete cleanup while it is opaque.
            unreadable.append(process.pid)
    return unreadable


def _remember_children(worker: psutil.Process, owned: dict[int, psutil.Process]) -> None:
    try:
        for child in worker.children(recursive=True):
            # Cache creation time so psutil's signal methods guard against PID reuse.
            child.create_time()
            owned[child.pid] = child
    except psutil.NoSuchProcess:
        pass


def _alive(process: psutil.Process) -> bool:
    try:
        return process.is_running() and process.status() != psutil.STATUS_ZOMBIE
    except psutil.NoSuchProcess:
        return False


def _cleanup(
    process: subprocess.Popen,
    worker: psutil.Process,
    owned: dict[int, psutil.Process],
    marker: str,
    baseline: set[tuple[int, float]],
) -> tuple[list[int], list[int], bool]:
    _remember_children(worker, owned)
    _remember_marked(marker, baseline, owned)
    # Give asyncio and Playwright an opportunity to close sessions normally.
    if _alive(worker):
        try:
            worker.send_signal(signal.SIGINT)
        except psutil.NoSuchProcess:
            pass
    deadline = time.monotonic() + 3
    while process.poll() is None and time.monotonic() < deadline:
        _remember_children(worker, owned)
        time.sleep(0.05)
    # Rescan after each signal/wait cycle: descendants can appear during exit,
    # even after their parent has disappeared from the original process tree.
    deadline = time.monotonic() + 6
    force_at = time.monotonic() + 3
    empty_scans = 0
    survivors = []
    unreadable = []
    while time.monotonic() < deadline:
        unreadable = _remember_marked(marker, baseline, owned)
        survivors = [p for p in [*owned.values(), worker] if _alive(p)]
        empty_scans = empty_scans + 1 if not survivors and not unreadable else 0
        if empty_scans >= 2:
            break
        for child in survivors:
            try:
                child.kill() if time.monotonic() >= force_at else child.terminate()
            except psutil.NoSuchProcess:
                pass
        psutil.wait_procs(survivors, timeout=0.1)
        time.sleep(0.05)
    process.wait(timeout=3)
    return [child.pid for child in survivors if _alive(child)], unreadable, empty_scans >= 2


def supervise(directory: Path, owner_pid: int) -> int:
    stopping = False

    def stop(*_args):
        nonlocal stopping
        stopping = True

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    process = None
    worker = None
    owned: dict[int, psutil.Process] = {}
    marker = uuid.uuid4().hex
    baseline = _baseline()
    reason = "launch_failed"
    try:
        settings = json.loads((directory / "worker.json").read_text())
        with (directory / "worker.log").open("ab", buffering=0) as log:
            process = subprocess.Popen(
                settings["argv"],
                env={**settings["environment"], _OWNERSHIP_ENV: marker},
                cwd=directory,
                stdin=subprocess.DEVNULL,
                stdout=log,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
            worker = psutil.Process(process.pid)
            worker.create_time()
            reason = "worker_exit"
            (directory / "started.json").write_text(json.dumps({"worker_pid": process.pid, "allocation": marker}))
            while process.poll() is None:
                _remember_children(worker, owned)
                if stopping or (directory / "stop").exists():
                    reason = "stopped"
                    break
                if os.getppid() != owner_pid:
                    reason = "owner_exit"
                    break
                if time.time() >= float((directory / "deadline").read_text()):
                    reason = "lease_expired"
                    break
                time.sleep(0.1)
    finally:
        survivors = []
        unreadable = []
        settled = True
        error = ""
        try:
            if process is not None and worker is not None:
                survivors, unreadable, settled = _cleanup(process, worker, owned, marker, baseline)
            elif process is not None:
                error = "Worker started without a verifiable process identity"
            if reason == "worker_exit" and not error:
                error = "Worker exited before a supervised shutdown; inspect worker.log"
        except Exception as exc:
            error = f"{type(exc).__name__}: {exc}"
        finally:
            (directory / "worker.json").unlink(missing_ok=True)
            receipt = directory / "finished.tmp"
            receipt.write_text(
                json.dumps(
                    {
                        "reason": reason,
                        "returncode": process.returncode if process else None,
                        "cleanup_confirmed": settled and not survivors and not unreadable and not error,
                        "remaining_pids": survivors,
                        "unreadable_pids": unreadable,
                        "discovery_settled": settled,
                        "cleanup_error": error,
                    }
                )
            )
            receipt.replace(directory / "finished.json")
    return 0


if __name__ == "__main__":
    raise SystemExit(supervise(Path(sys.argv[1]), int(sys.argv[2])))
