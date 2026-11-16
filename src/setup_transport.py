"""Single-delivery transport for slow prepared software extraction in a task VM."""

from __future__ import annotations

import json
import logging
import time
from contextlib import contextmanager
from uuid import uuid4

import requests

logger = logging.getLogger(__name__)

_RUNNER = '''import json, os, signal, subprocess, sys
spec = json.loads(sys.argv[1])
root = spec['root']
with open(root+'/stdout', 'wb') as out, open(root+'/stderr', 'wb') as err:
    process = subprocess.Popen(spec['command'], shell=spec['shell'], stdout=out, stderr=err,
                               start_new_session=True)
    try:
        process.wait(timeout=spec['timeout'])
    except subprocess.TimeoutExpired:
        os.killpg(process.pid, signal.SIGKILL)
        process.wait()
with open(root+'/stdout', 'rb') as out, open(root+'/stderr', 'rb') as err:
    result = {'returncode': process.returncode, 'output': out.read(65536).decode('utf-8', 'replace'),
              'error': err.read(65536).decode('utf-8', 'replace')}
with open(root+'/result.tmp', 'w') as file:
    json.dump(result, file)
os.replace(root+'/result.tmp', root+'/result.json')
'''


def execute_background(controller, command, *, timeout, shell=False, check=False):
    """Submit once; recover a lost launch response only by reading its unique result."""
    root = "/tmp/jevduo-setup-" + uuid4().hex
    def replace(value):
        return value.replace("{CLIENT_PASSWORD}", controller.client_password)
    command = replace(command) if isinstance(command, str) else [replace(part) for part in command]
    spec = {"root": root, "command": command, "shell": shell, "timeout": timeout}
    bootstrap = (
        "import os,subprocess,sys; os.mkdir(" + repr(root) + ",0o700); "
        "subprocess.Popen([sys.executable,'-c'," + repr(_RUNNER) + "," + repr(json.dumps(spec)) + "],"
        "stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,"
        "start_new_session=True,close_fds=True); print('SUBMITTED')"
    )
    poll = (
        "import json,os; p=" + repr(root + "/result.json") + "; "
        "print(json.dumps({'done':True,'result':json.load(open(p))}) "
        "if os.path.isfile(p) else '{\"done\":false}')"
    )
    deadline = time.monotonic() + timeout + 30
    with requests.Session() as session:
        session.trust_env = False
        try:
            response = session.post(controller.http_server + "/setup/execute", json={
                "command": ["python3", "-c", bootstrap], "shell": False, "timeout": 20,
            }, timeout=30)
            response.raise_for_status()
            if response.json().get("returncode") != 0:
                raise RuntimeError("Background setup submission was rejected")
        except requests.RequestException:
            # The launch may have succeeded. Never submit it a second time.
            logger.warning("Setup submission response lost; reconciling unique result")
        last_progress = 0.0
        while time.monotonic() < deadline:
            try:
                response = session.post(controller.http_server + "/setup/execute", json={
                    "command": ["python3", "-c", poll], "shell": False, "timeout": 15,
                }, timeout=20)
                response.raise_for_status()
                envelope = response.json()
                status = json.loads(envelope["output"])
                if status.get("done"):
                    result = status["result"]
                    if check and result["returncode"] != 0:
                        raise RuntimeError(f"Background setup command exited {result['returncode']}")
                    logger.warning("Background software extraction completed: returncode=%s",
                                   result["returncode"])
                    return result
            except (requests.RequestException, ValueError, KeyError):
                pass  # The poll has no effects and may safely be retried.
            if time.monotonic() - last_progress >= 15:
                logger.warning("Waiting for background software extraction")
                last_progress = time.monotonic()
            time.sleep(2)
    raise TimeoutError("Background setup result did not arrive before its deadline")


@contextmanager
def prepared_setup_transport(task_id):
    """Keep benchmark source and setup semantics intact; adapt only its transport."""
    if task_id != "061":
        yield
        return
    from desktop_env.controllers.setup import SetupController

    original = SetupController._execute_setup

    def execute(controller, command, *args, **kwargs):
        text = command if isinstance(command, str) else " ".join(command)
        if "osworld21-darktable" in text and "tar --xattrs -xzf" in text:
            if args or any(kwargs.get(k) for k in ("until", "stdout", "stderr")):
                raise ValueError("Unsupported background setup output/continuation options")
            return execute_background(controller, command, timeout=kwargs.get("timeout", 300),
                                      shell=kwargs.get("shell", False), check=kwargs.get("check", False))
        return original(controller, command, *args, **kwargs)

    SetupController._execute_setup = execute
    try:
        yield
    finally:
        SetupController._execute_setup = original
